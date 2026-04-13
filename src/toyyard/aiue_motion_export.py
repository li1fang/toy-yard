from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from toyyard.communication_signal import build_aiue_motion_export_signal
from toyyard.motion_packet_check import write_motion_packet_check_report
from toyyard.paths import ProjectPaths
from toyyard.util import now_iso, write_json
from toyyard.warehouse import (
    ensure_artifact,
    find_entity_by_alias,
    json_loads,
    resolve_sample,
    sample_aliases,
    sample_packages,
    sample_sources,
)


EXPORT_CONTRACT_VERSION = "toy-yard-motion-1.0"
EXPORTER_VERSION = "toyyard-m1"


def _load_json_file(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return {}


def _resolve_motion_sample(conn: sqlite3.Connection, sample_ref: str) -> sqlite3.Row | None:
    sample = resolve_sample(conn, sample_ref)
    if sample is not None:
        return sample
    return find_entity_by_alias(
        conn,
        entity_type="sample",
        external_system="ai_motion",
        alias_key="scenario_id",
        alias_value=sample_ref,
    )


def _resolve_motion_package(conn: sqlite3.Connection, package_ref: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
        FROM packages
        WHERE canonical_package_id = ?
        """,
        (package_ref,),
    ).fetchone()


def _ordered_unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        normalized = str(value or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def _package_artifacts(conn: sqlite3.Connection, package_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM artifacts WHERE owner_type = 'package' AND owner_id = ? ORDER BY created_at, id",
        (package_id,),
    ).fetchall()


def _sample_source_artifacts(conn: sqlite3.Connection, sample_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT artifacts.*
        FROM artifacts
        JOIN source_links
          ON source_links.entity_type = 'sample'
         AND source_links.entity_id = ?
         AND source_links.source_id = artifacts.owner_id
        WHERE artifacts.owner_type = 'source'
        ORDER BY artifacts.created_at, artifacts.id
        """,
        (sample_id,),
    ).fetchall()


def _artifact_metadata(row: sqlite3.Row | None) -> dict[str, Any]:
    return json_loads(row["metadata_json"]) if row is not None else {}


def _find_artifact(rows: list[sqlite3.Row], artifact_kind: str) -> sqlite3.Row | None:
    matches = [row for row in rows if row["artifact_kind"] == artifact_kind]
    if not matches:
        return None
    return sorted(matches, key=lambda row: (row["created_at"], row["id"]), reverse=True)[0]


def _copy_if_exists(source: Path | None, destination: Path) -> Path | None:
    if source is None or not source.exists() or not source.is_file():
        return None
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return destination.resolve()


def _package_metadata(package: sqlite3.Row) -> dict[str, Any]:
    return json_loads(package["metadata_json"])


def _source_references(conn: sqlite3.Connection, sample_id: int) -> list[dict[str, Any]]:
    return [
        {
            "source_path": row["source_path"],
            "managed_path": row["managed_path"],
            "source_kind": row["source_kind"],
            "content_hash": row["content_hash"],
            "metadata": json_loads(row["metadata_json"]),
        }
        for row in sample_sources(conn, sample_id)
    ]


def _capability_for_pack_version(source_artifacts: list[sqlite3.Row], pack_version: str) -> dict[str, Any]:
    for row in source_artifacts:
        if row["artifact_kind"] != "motion_capability_assessment":
            continue
        metadata = _artifact_metadata(row)
        if str(metadata.get("pack_version") or "") == pack_version:
            payload = metadata.get("payload")
            return dict(payload) if isinstance(payload, dict) else _load_json_file(Path(row["path"]))
    return {}


def _validation_payload(row: sqlite3.Row | None) -> dict[str, Any]:
    metadata = _artifact_metadata(row)
    payload = metadata.get("payload")
    return dict(payload) if isinstance(payload, dict) else {}


def _selection_ready(manifest_payload: dict[str, Any]) -> bool:
    validation = manifest_payload.get("validation") or {}
    return (
        str(validation.get("status") or "").strip().lower() == "pass"
        and str(manifest_payload.get("runtime_semantics") or "") == "runtime"
        and manifest_payload.get("placeholder_motion") is False
        and str(manifest_payload.get("format_profile") or "") == "route_a_kimodo_somaskel77"
        and str((manifest_payload.get("skeleton") or {}).get("skeleton_id") or "") == "somaskel77"
    )


def _safe_profile_reset(paths: ProjectPaths, profile: str) -> Path:
    profile_dir = paths.aiue_motion_profile_dir(profile)
    if profile_dir.exists():
        publish_root = paths.aiue_motion_publish_dir.resolve()
        resolved_profile = profile_dir.resolve()
        if publish_root not in resolved_profile.parents:
            raise ValueError(f"Refusing to remove non-motion publish path: {resolved_profile}")
        shutil.rmtree(profile_dir)
    return profile_dir


def _record_export_artifacts(
    conn: sqlite3.Connection,
    *,
    sample: sqlite3.Row,
    package: sqlite3.Row,
    manifest_path: Path,
    staged_files: list[tuple[str, Path]],
) -> None:
    ensure_artifact(
        conn,
        owner_type="package",
        owner_id=package["id"],
        stage="export",
        artifact_kind="motion_packet_manifest",
        path=str(manifest_path.resolve()),
        format=".json",
        status="exported",
        metadata={"sample_id": sample["canonical_sample_id"], "package_id": package["canonical_package_id"]},
    )
    for artifact_kind, artifact_path in staged_files:
        ensure_artifact(
            conn,
            owner_type="package",
            owner_id=package["id"],
            stage="export",
            artifact_kind=artifact_kind,
            path=str(artifact_path.resolve()),
            format=artifact_path.suffix.lower(),
            status="exported",
            metadata={"sample_id": sample["canonical_sample_id"], "package_id": package["canonical_package_id"]},
        )


def export_aiue_motion_view(
    conn: sqlite3.Connection,
    paths: ProjectPaths,
    *,
    profile: str,
    sample_ref: str | None = None,
    package_refs: list[str] | None = None,
) -> dict[str, Any]:
    normalized_package_refs = [str(item or "").strip() for item in (package_refs or []) if str(item or "").strip()]
    if sample_ref and normalized_package_refs:
        raise ValueError("Use either --sample or --package, not both.")
    if not sample_ref and not normalized_package_refs:
        raise ValueError("Motion export requires either --sample or at least one --package.")

    selections: list[tuple[sqlite3.Row, sqlite3.Row]] = []
    if normalized_package_refs:
        seen_packages: set[str] = set()
        for package_ref in normalized_package_refs:
            if package_ref in seen_packages:
                continue
            package = _resolve_motion_package(conn, package_ref)
            if package is None:
                raise ValueError(f"Unknown motion package: {package_ref}")
            if str(package["content_bucket"] or "") != "motion":
                raise ValueError(f"Package is not a motion package: {package_ref}")
            sample = conn.execute("SELECT * FROM samples WHERE id = ?", (package["sample_id"],)).fetchone()
            if sample is None:
                raise ValueError(f"Motion package has no sample owner: {package_ref}")
            selections.append((sample, package))
            seen_packages.add(package_ref)
    else:
        assert sample_ref is not None
        sample = _resolve_motion_sample(conn, sample_ref)
        if sample is None:
            raise ValueError(f"Unknown motion sample: {sample_ref}")
        packages = [row for row in sample_packages(conn, sample["id"]) if str(row["content_bucket"] or "") == "motion"]
        if not packages:
            raise ValueError(f"Sample has no motion packages: {sample_ref}")
        selections.extend((sample, package) for package in packages)

    profile_dir = _safe_profile_reset(paths, profile)
    clips_dir = paths.aiue_motion_clips_dir(profile)
    summary_dir = paths.aiue_motion_summary_dir(profile)
    workspace_views_dir = paths.aiue_motion_workspace_views_dir(profile)
    clips_dir.mkdir(parents=True, exist_ok=True)
    summary_dir.mkdir(parents=True, exist_ok=True)
    workspace_views_dir.mkdir(parents=True, exist_ok=True)

    summary_items: list[dict[str, Any]] = []
    registry_items: list[dict[str, Any]] = []
    manifest_paths: list[Path] = []
    exported_packages: list[str] = []
    exported_sample_ids: list[str] = []
    exported_scenario_ids: list[str] = []

    for sample, package in selections:
        sample_metadata = json_loads(sample["metadata_json"])
        sample_alias_rows = sample_aliases(conn, sample["id"])
        source_references = _source_references(conn, sample["id"])
        source_artifacts = _sample_source_artifacts(conn, sample["id"])
        metadata = _package_metadata(package)
        package_dir = paths.aiue_motion_clip_dir(profile, package["canonical_package_id"])
        package_dir.mkdir(parents=True, exist_ok=True)

        artifact_rows = _package_artifacts(conn, package["id"])
        manifest_artifact = _find_artifact(artifact_rows, "motion_manifest")
        npz_artifact = _find_artifact(artifact_rows, "motion_npz")
        bvh_artifact = _find_artifact(artifact_rows, "motion_bvh")
        skeleton_artifact = _find_artifact(artifact_rows, "motion_skeleton")
        readme_artifact = _find_artifact(artifact_rows, "motion_readme")
        validation_artifact = _find_artifact(artifact_rows, "motion_validation_result")

        source_manifest = _load_json_file(Path(manifest_artifact["path"])) if manifest_artifact else {}
        validation = _validation_payload(validation_artifact)
        capability = _capability_for_pack_version(source_artifacts, str(metadata.get("pack_version") or ""))

        staged_npz = _copy_if_exists(Path(npz_artifact["path"]) if npz_artifact else None, package_dir / "motion.npz")
        staged_bvh = _copy_if_exists(Path(bvh_artifact["path"]) if bvh_artifact else None, package_dir / "motion.bvh")
        skeleton_filename = Path(str((source_manifest.get("skeleton") or {}).get("file") or "skeleton.soma_77.json")).name
        staged_skeleton = _copy_if_exists(
            Path(skeleton_artifact["path"]) if skeleton_artifact else None,
            package_dir / skeleton_filename,
        )
        staged_readme = _copy_if_exists(Path(readme_artifact["path"]) if readme_artifact else None, package_dir / "README.md")

        manifest_payload = {
            "export_contract_version": EXPORT_CONTRACT_VERSION,
            "exporter_version": EXPORTER_VERSION,
            "source": "toy-yard export",
            "sample_id": sample["canonical_sample_id"],
            "package_id": package["canonical_package_id"],
            "scenario_id": metadata.get("scenario_id") or sample_metadata.get("scenario_id") or "",
            "clip_id": metadata.get("clip_id") or "",
            "pack_version": metadata.get("pack_version") or "",
            "runtime_semantics": metadata.get("runtime_semantics") or source_manifest.get("runtime_semantics") or "",
            "placeholder_motion": bool(metadata.get("placeholder_motion")),
            "format_profile": metadata.get("format_profile") or source_manifest.get("format_profile") or "",
            "fps": metadata.get("fps") or source_manifest.get("fps"),
            "duration_sec": metadata.get("duration_sec") or source_manifest.get("duration_sec"),
            "frame_count": metadata.get("frame_count") or source_manifest.get("frame_count"),
            "skeleton": {
                **dict(source_manifest.get("skeleton") or {}),
                "skeleton_id": metadata.get("skeleton_id") or (source_manifest.get("skeleton") or {}).get("skeleton_id") or "",
                "file": skeleton_filename,
            },
            "arrays": dict(source_manifest.get("arrays") or {}),
            "exports": dict(source_manifest.get("exports") or {}),
            "validation": {
                "status": validation.get("status") or metadata.get("validation_status") or "",
                "metrics": validation.get("metrics") or metadata.get("validation_metrics") or {},
                "source_path": validation_artifact["path"] if validation_artifact else "",
            },
            "capability": capability,
            "source_lineage": {
                "source_references": source_references,
                "manifest_path": manifest_artifact["path"] if manifest_artifact else "",
                "motion_npz_path": npz_artifact["path"] if npz_artifact else "",
                "motion_bvh_path": bvh_artifact["path"] if bvh_artifact else "",
                "skeleton_path": skeleton_artifact["path"] if skeleton_artifact else "",
                "readme_path": readme_artifact["path"] if readme_artifact else "",
            },
            "export_artifacts": {
                "manifest_path": "manifest.json",
                "motion_npz": "motion.npz" if staged_npz else "",
                "motion_bvh": "motion.bvh" if staged_bvh else "",
                "skeleton_path": skeleton_filename if staged_skeleton else "",
                "readme_path": "README.md" if staged_readme else "",
            },
            "toy_yard": {
                "sample_id": sample["canonical_sample_id"],
                "package_id": package["canonical_package_id"],
                "sample_aliases": [dict(row) for row in sample_alias_rows],
                "sample_metadata": sample_metadata,
                "package_metadata": metadata,
            },
        }

        manifest_path = paths.aiue_motion_clip_manifest_path(profile, package["canonical_package_id"])
        write_json(manifest_path, manifest_payload)
        manifest_paths.append(manifest_path)
        exported_packages.append(package["canonical_package_id"])
        exported_sample_ids.append(sample["canonical_sample_id"])
        exported_scenario_ids.append(str(manifest_payload["scenario_id"] or ""))

        selection_ready = _selection_ready(manifest_payload) and staged_bvh is not None and staged_npz is not None and staged_skeleton is not None
        summary_item = {
            "sample_id": sample["canonical_sample_id"],
            "package_id": package["canonical_package_id"],
            "scenario_id": manifest_payload["scenario_id"],
            "clip_id": manifest_payload["clip_id"],
            "pack_version": manifest_payload["pack_version"],
            "manifest_path": str(manifest_path.resolve()),
            "motion_bvh": manifest_payload["export_artifacts"]["motion_bvh"],
            "runtime_semantics": manifest_payload["runtime_semantics"],
            "placeholder_motion": manifest_payload["placeholder_motion"],
            "format_profile": manifest_payload["format_profile"],
            "skeleton_id": manifest_payload["skeleton"]["skeleton_id"],
            "validation_status": manifest_payload["validation"]["status"],
            "selection_ready": selection_ready,
            "consumer_ready": bool(package["consumer_ready"]),
            "warehouse_status": package["warehouse_status"],
        }
        summary_items.append(summary_item)
        registry_items.append(
            {
                **summary_item,
                "fps": manifest_payload["fps"],
                "duration_sec": manifest_payload["duration_sec"],
                "frame_count": manifest_payload["frame_count"],
                "manifest_relpath": str(manifest_path.relative_to(profile_dir)).replace("\\", "/"),
                "artifacts": {
                    "motion_npz": manifest_payload["export_artifacts"]["motion_npz"],
                    "motion_bvh": manifest_payload["export_artifacts"]["motion_bvh"],
                    "skeleton_path": manifest_payload["export_artifacts"]["skeleton_path"],
                    "readme_path": manifest_payload["export_artifacts"]["readme_path"],
                },
            }
        )

        staged_files: list[tuple[str, Path]] = []
        if staged_npz is not None:
            staged_files.append(("portable_motion_npz", staged_npz))
        if staged_bvh is not None:
            staged_files.append(("portable_motion_bvh", staged_bvh))
        if staged_skeleton is not None:
            staged_files.append(("portable_motion_skeleton", staged_skeleton))
        if staged_readme is not None:
            staged_files.append(("portable_motion_readme", staged_readme))
        _record_export_artifacts(conn, sample=sample, package=package, manifest_path=manifest_path, staged_files=staged_files)

    sample_ids = _ordered_unique(exported_sample_ids)
    scenario_ids = _ordered_unique(exported_scenario_ids)
    primary_sample_id = sample_ids[0] if len(sample_ids) == 1 else ""
    primary_scenario_id = scenario_ids[0] if len(scenario_ids) == 1 else ""
    single_sample_owner = None
    if primary_sample_id:
        single_sample_owner = conn.execute(
            "SELECT * FROM samples WHERE canonical_sample_id = ?",
            (primary_sample_id,),
        ).fetchone()

    summary_payload = {
        "generated_at_utc": now_iso(),
        "export_contract_version": EXPORT_CONTRACT_VERSION,
        "exporter_version": EXPORTER_VERSION,
        "source": "toy-yard export",
        "profile": profile,
        "sample_id": primary_sample_id,
        "sample_ids": sample_ids,
        "scenario_id": primary_scenario_id,
        "scenario_ids": scenario_ids,
        "clips": summary_items,
        "counts": {
            "clip_count": len(summary_items),
            "selection_ready_clips": sum(1 for item in summary_items if item["selection_ready"]),
            "consumer_ready_clips": sum(1 for item in summary_items if item["consumer_ready"]),
            "distinct_sample_count": len(sample_ids),
            "distinct_scenario_count": len(scenario_ids),
        },
    }
    summary_path = paths.aiue_motion_summary_path(profile)
    write_json(summary_path, summary_payload)
    if single_sample_owner is not None:
        ensure_artifact(
            conn,
            owner_type="sample",
            owner_id=single_sample_owner["id"],
            stage="export",
            artifact_kind="motion_suite_summary",
            path=str(summary_path.resolve()),
            format=".json",
            status="exported",
            metadata={"profile": profile},
        )

    registry_payload = {
        "generated_at_utc": now_iso(),
        "export_contract_version": EXPORT_CONTRACT_VERSION,
        "exporter_version": EXPORTER_VERSION,
        "source": "toy-yard export",
        "profile": profile,
        "sample_id": primary_sample_id,
        "sample_ids": sample_ids,
        "scenario_id": primary_scenario_id,
        "scenario_ids": scenario_ids,
        "clips": registry_items,
        "package_index": {
            item["package_id"]: {
                "manifest_path": item["manifest_path"],
                "manifest_relpath": item["manifest_relpath"],
                "selection_ready": item["selection_ready"],
                "consumer_ready": item["consumer_ready"],
            }
            for item in registry_items
        },
        "counts": {
            "clips": len(registry_items),
            "selection_ready": sum(1 for item in registry_items if item["selection_ready"]),
            "distinct_samples": len(sample_ids),
            "distinct_scenarios": len(scenario_ids),
        },
    }
    registry_path = paths.aiue_motion_registry_path(profile)
    write_json(registry_path, registry_payload)
    if single_sample_owner is not None:
        ensure_artifact(
            conn,
            owner_type="sample",
            owner_id=single_sample_owner["id"],
            stage="export",
            artifact_kind="motion_clip_registry",
            path=str(registry_path.resolve()),
            format=".json",
            status="exported",
            metadata={"profile": profile},
        )

    packet_check_path = paths.aiue_motion_packet_check_path(profile)
    packet_check_payload = write_motion_packet_check_report(
        manifest_paths=manifest_paths,
        export_root=profile_dir,
        output_path=packet_check_path,
    )
    if single_sample_owner is not None:
        ensure_artifact(
            conn,
            owner_type="sample",
            owner_id=single_sample_owner["id"],
            stage="export",
            artifact_kind="motion_packet_check",
            path=str(packet_check_path.resolve()),
            format=".json",
            status=packet_check_payload["status"],
            metadata={"profile": profile, "counts": packet_check_payload["counts"]},
        )

    communication_signal_payload = build_aiue_motion_export_signal(
        profile=profile,
        summary_payload=summary_payload,
        registry_payload=registry_payload,
        packet_check_payload=packet_check_payload,
        summary_path=summary_path,
        registry_path=registry_path,
        packet_check_path=packet_check_path,
    )
    communication_signal_path = paths.aiue_motion_communication_signal_path(profile)
    write_json(communication_signal_path, communication_signal_payload)
    if single_sample_owner is not None:
        ensure_artifact(
            conn,
            owner_type="sample",
            owner_id=single_sample_owner["id"],
            stage="export",
            artifact_kind="communication_signal",
            path=str(communication_signal_path.resolve()),
            format=".json",
            status=communication_signal_payload["status"],
            metadata={"profile": profile, "lane": "motion"},
        )

    workspace_view_payload = {
        "version": "1.0",
        "export_contract_version": EXPORT_CONTRACT_VERSION,
        "exporter_version": EXPORTER_VERSION,
        "profile": profile,
        "toy_yard_root": str(paths.root),
        "toy_yard_motion_view_root": str(profile_dir.resolve()),
        "summary_path": str(summary_path.resolve()),
        "registry_path": str(registry_path.resolve()),
        "motion_packet_check_path": str(packet_check_path.resolve()),
        "communication_signal_path": str(communication_signal_path.resolve()),
        "sample_id": primary_sample_id,
        "sample_ids": sample_ids,
        "scenario_id": primary_scenario_id,
        "scenario_ids": scenario_ids,
        "package_ids": exported_packages,
    }
    workspace_view_path = paths.aiue_motion_workspace_view_path(profile)
    write_json(workspace_view_path, workspace_view_payload)

    trial_workspace_payload = {
        "version": "0.1.0",
        "schema_version": 1,
        "paths": {
            "toy_yard_motion_view_root": str(profile_dir.resolve()),
            "aiue_repo_root": "${config_dir}\\..\\..\\..\\..\\..\\AiUE",
            "auto_ue_cli_output_root": "C:\\path\\to\\UnrealProject\\Saved\\motion_pipeline\\auto_ue_cli",
            "unreal_project_root": "C:\\path\\to\\UnrealProject",
            "asset_root": "/Game/AiUE/ImportedAssets",
            "motion_asset_root": "/Game/AiUE/MotionPackets",
            "motion_target_skeleton_asset_path": "/Game/AiUE/Registry/TargetSkeleton.TargetSkeleton",
        },
        "motion": {
            "preview_level_path": "/Game/ThirdPerson/Lvl_ThirdPerson",
            "runtime_ready_only": True,
            "packet_profile": "route_a_kimodo_somaskel77",
        },
    }
    trial_workspace_path = paths.aiue_motion_trial_workspace_path(profile)
    write_json(trial_workspace_path, trial_workspace_payload)

    return {
        "profile": profile,
        "sample_id": primary_sample_id,
        "sample_ids": sample_ids,
        "scenario_ids": scenario_ids,
        "packages": exported_packages,
        "export_root": str(profile_dir.resolve()),
        "summary_path": str(summary_path.resolve()),
        "registry_path": str(registry_path.resolve()),
        "motion_packet_check_path": str(packet_check_path.resolve()),
        "communication_signal_path": str(communication_signal_path.resolve()),
        "workspace_view_path": str(workspace_view_path.resolve()),
        "trial_workspace_path": str(trial_workspace_path.resolve()),
    }
