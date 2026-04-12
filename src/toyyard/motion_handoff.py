from __future__ import annotations

import json
import sqlite3
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Any

from toyyard.paths import ProjectPaths
from toyyard.warehouse import (
    canonical_root,
    ensure_alias,
    ensure_artifact,
    ensure_package,
    ensure_sample,
    ensure_source,
    ensure_source_link,
    find_entity_by_alias,
    make_canonical_id,
)


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _load_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def _extract_package_if_needed(managed_zip: Path, inspect_dir: Path) -> None:
    if inspect_dir.exists() and any(inspect_dir.iterdir()):
        return
    inspect_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(managed_zip) as archive:
        archive.extractall(inspect_dir)


def _handoff_root(inspect_dir: Path) -> Path:
    if (inspect_dir / "examples" / "motion-packs").exists():
        return inspect_dir
    children = [child for child in inspect_dir.iterdir() if child.is_dir()]
    for child in children:
        if (child / "examples" / "motion-packs").exists():
            return child
    return inspect_dir


def _release_dir(root: Path, pack_version: str) -> Path:
    return root / "examples" / "motion-packs" / pack_version


def _validation_entry(
    validation_payload: dict[str, Any],
    *,
    clip_id: str,
    scenario_id: str,
    pack_version: str,
) -> dict[str, Any]:
    reports = validation_payload.get("reports") or []
    for entry in reports:
        if str(entry.get("clip_id") or "").strip() == clip_id:
            return dict(entry)
    for entry in reports:
        if (
            str(entry.get("scenario_id") or "").strip() == scenario_id
            and str(entry.get("pack_version") or "").strip() == pack_version
        ):
            return dict(entry)
    return {}


def _relative_manifest_paths(root: Path) -> list[Path]:
    return sorted((root / "examples" / "motion-packs").glob("*/*/manifest.json"))


def _align_canonical_id(
    conn: sqlite3.Connection,
    *,
    table_name: str,
    row_id: int,
    column_name: str,
    wanted_value: str,
) -> None:
    row = conn.execute(f'SELECT {column_name} FROM {table_name} WHERE id = ?', (row_id,)).fetchone()
    if row is None or row[column_name] == wanted_value:
        return
    conflict = conn.execute(
        f"SELECT id FROM {table_name} WHERE {column_name} = ? AND id != ?",
        (wanted_value, row_id),
    ).fetchone()
    if conflict is not None:
        return
    conn.execute(
        f"UPDATE {table_name} SET {column_name} = ? WHERE id = ?",
        (wanted_value, row_id),
    )
    conn.commit()


def import_motion_handoff(
    conn: sqlite3.Connection,
    paths: ProjectPaths,
    *,
    package_id: int,
) -> dict[str, Any]:
    v1_package = conn.execute("SELECT * FROM v1_packages WHERE id = ?", (package_id,)).fetchone()
    if v1_package is None:
        raise ValueError(f"Unknown v1 package id: {package_id}")

    managed_zip = Path(v1_package["managed_path"]).expanduser().resolve()
    if not managed_zip.exists():
        raise ValueError(f"Managed package is missing: {managed_zip}")

    inspect_dir = paths.motion_inspect_dir(v1_package["id"], Path(v1_package["filename"]).stem)
    _extract_package_if_needed(managed_zip, inspect_dir)
    handoff_root = _handoff_root(inspect_dir)

    canonical = canonical_root(conn)
    if canonical is None:
        raise ValueError("Canonical root is not initialized")

    source = ensure_source(
        conn,
        root_id=canonical["id"],
        source_kind="motion_handoff",
        source_path=v1_package["source_path"],
        managed_path=str(managed_zip),
        content_hash=v1_package["sha256"],
        ext=v1_package["ext"],
        size_bytes=v1_package["size_bytes"],
        status="cataloged",
        metadata={
            "v1_package_id": v1_package["id"],
            "inspect_root": str(inspect_dir),
            "handoff_root": str(handoff_root),
            "filename": v1_package["filename"],
        },
    )

    source_artifact_count = 0
    package_artifact_count = 0

    top_readme = handoff_root / "README.md"
    if top_readme.exists():
        ensure_artifact(
            conn,
            owner_type="source",
            owner_id=source["id"],
            stage="source",
            artifact_kind="handoff_readme",
            path=str(top_readme),
            format=".md",
            status="cataloged",
            metadata={"content_preview": _load_text(top_readme)[:500]},
        )
        source_artifact_count += 1

    schema_dir = handoff_root / "schemas"
    for schema_name, artifact_kind in (
        ("motion-manifest.schema.json", "motion_manifest_schema"),
        ("motion-skeleton.schema.json", "motion_skeleton_schema"),
    ):
        schema_path = schema_dir / schema_name
        if not schema_path.exists():
            continue
        ensure_artifact(
            conn,
            owner_type="source",
            owner_id=source["id"],
            stage="source",
            artifact_kind=artifact_kind,
            path=str(schema_path),
            format=".json",
            status="cataloged",
            metadata={"payload": _load_json(schema_path)},
        )
        source_artifact_count += 1

    capability_payloads: dict[str, dict[str, Any]] = {}
    validation_payloads: dict[str, dict[str, Any]] = {}
    for release_dir in sorted((handoff_root / "examples" / "motion-packs").glob("*")):
        if not release_dir.is_dir():
            continue
        pack_version = release_dir.name
        capability_path = release_dir / "capability-assessment.json"
        if capability_path.exists():
            capability_payload = _load_json(capability_path)
            capability_payloads[pack_version] = capability_payload
            ensure_artifact(
                conn,
                owner_type="source",
                owner_id=source["id"],
                stage="source",
                artifact_kind="motion_capability_assessment",
                path=str(capability_path),
                format=".json",
                status="cataloged",
                metadata={"pack_version": pack_version, "payload": capability_payload},
            )
            source_artifact_count += 1

        validation_path = release_dir / "validation.report.json"
        if validation_path.exists():
            validation_payload = _load_json(validation_path)
            validation_payloads[pack_version] = validation_payload
            ensure_artifact(
                conn,
                owner_type="source",
                owner_id=source["id"],
                stage="source",
                artifact_kind="motion_validation_report",
                path=str(validation_path),
                format=".json",
                status="cataloged",
                metadata={"pack_version": pack_version, "payload": validation_payload},
            )
            source_artifact_count += 1

    manifests_by_scenario: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for manifest_path in _relative_manifest_paths(handoff_root):
        payload = _load_json(manifest_path)
        payload["_manifest_path"] = str(manifest_path)
        manifests_by_scenario[str(payload.get("scenario_id") or "").strip()].append(payload)

    sample_rows: dict[str, sqlite3.Row] = {}
    package_rows: list[sqlite3.Row] = []

    for scenario_id, manifests in sorted(manifests_by_scenario.items()):
        if not scenario_id:
            continue
        scenario_tags = sorted(
            {
                tag
                for manifest in manifests
                for tag in (manifest.get("scenario_tags") or [])
                if str(tag).strip()
            }
        )
        support_refs: dict[str, list[dict[str, Any]]] = {
            "supported_now": [],
            "supported_with_caution": [],
            "not_yet_admitted": [],
        }
        for manifest in manifests:
            pack_version = str(manifest.get("pack_version") or Path(manifest["_manifest_path"]).parents[1].name).strip()
            capability_payload = capability_payloads.get(pack_version, {})
            capability_path = _release_dir(handoff_root, pack_version) / "capability-assessment.json"
            for key in support_refs:
                values = capability_payload.get(key) or []
                if values:
                    support_refs[key].append(
                        {
                            "pack_version": pack_version,
                            "source_path": str(capability_path),
                            "values": values,
                        }
                    )

        wanted_sample_id = make_canonical_id("sample", "motion", scenario_id, display_name=scenario_id)
        existing_sample = find_entity_by_alias(
            conn,
            entity_type="sample",
            external_system="ai_motion",
            alias_key="scenario_id",
            alias_value=scenario_id,
        )
        if existing_sample is not None:
            _align_canonical_id(
                conn,
                table_name="samples",
                row_id=existing_sample["id"],
                column_name="canonical_sample_id",
                wanted_value=wanted_sample_id,
            )
        sample = ensure_sample(
            conn,
            canonical_sample_id=wanted_sample_id,
            display_name=scenario_id,
            source_game="ai_motion",
            source_format="motion_pack_v1",
            item_family="motion",
            warehouse_status="cataloged",
            metadata={
                "scenario_id": scenario_id,
                "scenario_tags": scenario_tags,
                **support_refs,
            },
        )
        sample_rows[scenario_id] = sample
        ensure_alias(
            conn,
            entity_type="sample",
            entity_id=sample["id"],
            external_system="ai_motion",
            alias_key="scenario_id",
            alias_value=scenario_id,
        )
        ensure_source_link(
            conn,
            source_id=source["id"],
            entity_type="sample",
            entity_id=sample["id"],
            relation_kind="motion_handoff",
        )

        for manifest in sorted(manifests, key=lambda item: (str(item.get("pack_version") or ""), str(item.get("clip_id") or ""))):
            pack_version = str(manifest.get("pack_version") or Path(manifest["_manifest_path"]).parents[1].name).strip()
            clip_id = str(manifest.get("clip_id") or "").strip()
            manifest_path = Path(manifest["_manifest_path"])
            package_dir = manifest_path.parent
            validation_payload = validation_payloads.get(pack_version, {})
            validation_entry = _validation_entry(
                validation_payload,
                clip_id=clip_id,
                scenario_id=scenario_id,
                pack_version=pack_version,
            )
            wanted_package_id = make_canonical_id(
                "pkg",
                "motion",
                scenario_id,
                pack_version,
                clip_id,
                display_name=f"{scenario_id}-{pack_version}",
            )
            existing_package = find_entity_by_alias(
                conn,
                entity_type="package",
                external_system="ai_motion",
                alias_key="clip_version",
                alias_value=f"{clip_id}@{pack_version}",
            )
            if existing_package is not None:
                _align_canonical_id(
                    conn,
                    table_name="packages",
                    row_id=existing_package["id"],
                    column_name="canonical_package_id",
                    wanted_value=wanted_package_id,
                )

            package = ensure_package(
                conn,
                sample_id=sample["id"],
                canonical_package_id=wanted_package_id,
                package_role="motion_clip",
                content_bucket="motion",
                contract_type="formal_motion_pack",
                consumer_ready=False,
                warehouse_status="cataloged",
                metadata={
                    "scenario_id": scenario_id,
                    "clip_id": clip_id,
                    "pack_version": pack_version,
                    "job_name": manifest.get("job_name"),
                    "source_route": manifest.get("source_route"),
                    "format_profile": manifest.get("format_profile"),
                    "runtime_semantics": manifest.get("runtime_semantics"),
                    "placeholder_motion": manifest.get("placeholder_motion"),
                    "skeleton_id": (manifest.get("skeleton") or {}).get("skeleton_id"),
                    "fps": manifest.get("fps"),
                    "duration_sec": manifest.get("duration_sec"),
                    "frame_count": manifest.get("frame_count"),
                    "validation_status": validation_entry.get("status") or "",
                    "validation_metrics": validation_entry.get("metrics") or {},
                    "manifest_path": str(manifest_path),
                },
            )
            package_rows.append(package)
            ensure_alias(
                conn,
                entity_type="package",
                entity_id=package["id"],
                external_system="ai_motion",
                alias_key="clip_version",
                alias_value=f"{clip_id}@{pack_version}",
            )
            current_clip_alias = find_entity_by_alias(
                conn,
                entity_type="package",
                external_system="ai_motion",
                alias_key="clip_id",
                alias_value=clip_id,
            )
            if current_clip_alias is None or current_clip_alias["id"] == package["id"]:
                ensure_alias(
                    conn,
                    entity_type="package",
                    entity_id=package["id"],
                    external_system="ai_motion",
                    alias_key="clip_id",
                    alias_value=clip_id,
                )

            npz_rel = str((manifest.get("exports") or {}).get("canonical_npz") or "motion.npz")
            bvh_rel = str((manifest.get("exports") or {}).get("bvh") or "motion.bvh")
            skeleton_rel = str((manifest.get("skeleton") or {}).get("file") or "skeleton.soma_77.json")
            readme_path = package_dir / "README.md"
            npz_path = package_dir / npz_rel
            bvh_path = package_dir / bvh_rel
            skeleton_path = package_dir / skeleton_rel
            validation_path = _release_dir(handoff_root, pack_version) / "validation.report.json"

            package_artifacts = (
                ("motion_manifest", manifest_path, ".json", {"payload": manifest}),
                ("motion_npz", npz_path, npz_path.suffix.lower(), {}),
                ("motion_bvh", bvh_path, bvh_path.suffix.lower(), {}),
                ("motion_skeleton", skeleton_path, skeleton_path.suffix.lower(), {}),
                ("motion_readme", readme_path, ".md", {"content_preview": _load_text(readme_path)[:500]} if readme_path.exists() else {}),
                (
                    "motion_validation_result",
                    validation_path,
                    ".json",
                    {"pack_version": pack_version, "payload": validation_entry},
                ),
            )
            for artifact_kind, artifact_path, artifact_format, artifact_metadata in package_artifacts:
                if not artifact_path.exists() and artifact_kind not in {"motion_validation_result"}:
                    continue
                ensure_artifact(
                    conn,
                    owner_type="package",
                    owner_id=package["id"],
                    stage="source",
                    artifact_kind=artifact_kind,
                    path=str(artifact_path),
                    format=artifact_format,
                    status="cataloged",
                    metadata=artifact_metadata,
                )
                package_artifact_count += 1

    conn.commit()
    return {
        "source_id": source["id"],
        "inspect_root": str(inspect_dir),
        "handoff_root": str(handoff_root),
        "samples_total": len(sample_rows),
        "packages_total": len(package_rows),
        "source_artifacts": source_artifact_count,
        "package_artifacts": package_artifact_count,
    }
