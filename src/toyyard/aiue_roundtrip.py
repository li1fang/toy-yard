from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from toyyard.paths import ProjectPaths
from toyyard.util import now_iso
from toyyard.warehouse import canonical_root, ensure_artifact, ensure_run, json_loads, resolve_sample


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _find_package(conn: sqlite3.Connection, package_id: str | None) -> sqlite3.Row | None:
    if not package_id:
        return None
    return conn.execute("SELECT * FROM packages WHERE canonical_package_id = ?", (package_id,)).fetchone()


def _find_sample(conn: sqlite3.Connection, sample_id: str | None) -> sqlite3.Row | None:
    if not sample_id:
        return None
    return resolve_sample(conn, sample_id)


def _sample_for_package(conn: sqlite3.Connection, package: sqlite3.Row) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM samples WHERE id = ?", (package["sample_id"],)).fetchone()


def _summary_sample_id(export_root: Path) -> str | None:
    summary_path = export_root / "summary" / "ue_suite_summary.json"
    if not summary_path.exists():
        return None
    payload = _load_json(summary_path)
    return str(payload.get("sample_id") or "").strip() or None


def _manifest_package_map(export_root: Path) -> dict[str, Path]:
    mapping: dict[str, Path] = {}
    for manifest_path in sorted((export_root / "conversion").rglob("manifest.json")):
        payload = _load_json(manifest_path)
        package_id = str(payload.get("package_id") or "").strip()
        if package_id:
            mapping[package_id] = manifest_path
    return mapping


def _package_role(package: sqlite3.Row) -> str:
    package_role = str(package["package_role"] or "").lower()
    content_bucket = str(package["content_bucket"] or "").lower()
    contract_type = str(package["contract_type"] or "").lower()
    if "weapon" in package_role or "weapon" in content_bucket or "weapon" in contract_type:
        return "weapon"
    if "character" in package_role or "host" in package_role or "character" in content_bucket or "bundle" in content_bucket:
        return "character"
    return "other"


def _copy_stable_artifact(source_path: Path, destination_path: Path) -> Path:
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, destination_path)
    return destination_path.resolve()


def _artifact_metadata(*, payload: dict[str, Any], original_path: Path, stable_path: Path) -> dict[str, Any]:
    return {
        "payload": payload,
        "toy_yard_original_path": str(original_path.resolve()),
        "toy_yard_stable_path": str(stable_path.resolve()),
    }


def _record_package_artifact(
    conn: sqlite3.Connection,
    package: sqlite3.Row,
    *,
    stage: str,
    artifact_kind: str,
    original_path: Path,
    stable_path: Path,
    payload: dict[str, Any],
) -> None:
    ensure_artifact(
        conn,
        owner_type="package",
        owner_id=package["id"],
        stage=stage,
        artifact_kind=artifact_kind,
        path=str(stable_path.resolve()),
        format=stable_path.suffix.lower(),
        status="imported",
        metadata=_artifact_metadata(payload=payload, original_path=original_path, stable_path=stable_path),
    )


def _record_sample_artifact(
    conn: sqlite3.Connection,
    sample: sqlite3.Row,
    *,
    stage: str,
    artifact_kind: str,
    original_path: Path,
    stable_path: Path,
    payload: dict[str, Any],
) -> None:
    ensure_artifact(
        conn,
        owner_type="sample",
        owner_id=sample["id"],
        stage=stage,
        artifact_kind=artifact_kind,
        path=str(stable_path.resolve()),
        format=stable_path.suffix.lower(),
        status="imported",
        metadata=_artifact_metadata(payload=payload, original_path=original_path, stable_path=stable_path),
    )


def _record_action_run(
    conn: sqlite3.Connection,
    *,
    entity_type: str,
    entity_id: int,
    stage: str,
    action_path: Path,
    payload: dict[str, Any],
) -> None:
    command = str(payload.get("command") or "unknown")
    tool_name = f"AiUE {command}"
    started_at = str(payload.get("generated_at_utc") or now_iso())
    ensure_run(
        conn,
        entity_type=entity_type,
        entity_id=entity_id,
        stage=stage,
        tool_name=tool_name,
        status=str(payload.get("status") or "unknown"),
        started_at=started_at,
        ended_at=started_at,
        log_path=str(action_path.resolve()),
        summary=f"{command}:{payload.get('status') or 'unknown'}",
    )


def _resolve_package_from_action(
    conn: sqlite3.Connection,
    *,
    manifest_map: dict[str, Path],
    action_path: Path,
    payload: dict[str, Any],
) -> sqlite3.Row | None:
    payload_package_id = str((payload.get("result") or {}).get("package_id") or "").strip()
    package = _find_package(conn, payload_package_id)
    if package is not None:
        return package

    lowered_name = action_path.name.casefold()
    if "character.action.json" in lowered_name:
        for candidate_id in manifest_map:
            candidate = _find_package(conn, candidate_id)
            if candidate is not None and _package_role(candidate) == "character":
                return candidate
    if "weapon.action.json" in lowered_name:
        for candidate_id in manifest_map:
            candidate = _find_package(conn, candidate_id)
            if candidate is not None and _package_role(candidate) == "weapon":
                return candidate

    if len(manifest_map) == 1:
        return _find_package(conn, next(iter(manifest_map)))
    return None


def import_aiue_results(
    conn: sqlite3.Connection,
    paths: ProjectPaths,
    *,
    export_root: Path,
    trial_root: Path,
) -> dict[str, object]:
    export_root = export_root.expanduser().resolve()
    trial_root = trial_root.expanduser().resolve()
    manifest_map = _manifest_package_map(export_root)
    canonical = canonical_root(conn)
    sample = _find_sample(conn, _summary_sample_id(export_root))
    if sample is None and manifest_map:
        first_package = _find_package(conn, next(iter(manifest_map)))
        if first_package is not None:
            sample = _sample_for_package(conn, first_package)

    counts = {
        "package_artifacts": 0,
        "sample_artifacts": 0,
        "runs": 0,
    }

    for package_id, manifest_path in manifest_map.items():
        package = _find_package(conn, package_id)
        if package is None:
            continue
        package_sample = _sample_for_package(conn, package)
        sample_id = (
            package_sample["canonical_sample_id"]
            if package_sample is not None
            else sample["canonical_sample_id"] if sample is not None else ""
        )
        for filename, artifact_kind, stage in (
            ("ue_import_report.local.json", "ue_import_report", "session"),
            ("ue_validation_report.local.json", "ue_validation_report", "verification"),
        ):
            artifact_path = manifest_path.parent / filename
            if not artifact_path.exists():
                continue
            payload = _load_json(artifact_path)
            stable_path = _copy_stable_artifact(
                artifact_path,
                paths.aiue_roundtrip_package_dir(sample_id, package["canonical_package_id"]) / filename,
            )
            _record_package_artifact(
                conn,
                package,
                stage=stage,
                artifact_kind=artifact_kind,
                original_path=artifact_path,
                stable_path=stable_path,
                payload=payload,
            )
            counts["package_artifacts"] += 1

    for pattern, stage, expected_owner in (
        ("import_package*.action.json", "session", "package"),
        ("validate_package*.action.json", "verification", "package"),
        ("refresh_assets*.action.json", "verification", "sample"),
    ):
        for action_path in sorted(trial_root.glob(pattern)):
            payload = _load_json(action_path)
            if expected_owner == "package":
                package = _resolve_package_from_action(
                    conn,
                    manifest_map=manifest_map,
                    action_path=action_path,
                    payload=payload,
                )
                if package is None:
                    continue
                package_sample = _sample_for_package(conn, package)
                sample_id = (
                    package_sample["canonical_sample_id"]
                    if package_sample is not None
                    else sample["canonical_sample_id"] if sample is not None else ""
                )
                stable_path = _copy_stable_artifact(
                    action_path,
                    paths.aiue_roundtrip_package_dir(sample_id, package["canonical_package_id"]) / action_path.name,
                )
                _record_package_artifact(
                    conn,
                    package,
                    stage=stage,
                    artifact_kind="aiue_action_result",
                    original_path=action_path,
                    stable_path=stable_path,
                    payload=payload,
                )
                _record_action_run(
                    conn,
                    entity_type="package",
                    entity_id=package["id"],
                    stage=stage,
                    action_path=stable_path,
                    payload=payload,
                )
                counts["package_artifacts"] += 1
                counts["runs"] += 1
                continue

            if sample is None:
                continue
            stable_path = _copy_stable_artifact(
                action_path,
                paths.aiue_roundtrip_sample_artifact_dir(sample["canonical_sample_id"]) / action_path.name,
            )
            _record_sample_artifact(
                conn,
                sample,
                stage=stage,
                artifact_kind="aiue_action_result",
                original_path=action_path,
                stable_path=stable_path,
                payload=payload,
            )
            _record_action_run(
                conn,
                entity_type="sample",
                entity_id=sample["id"],
                stage=stage,
                action_path=stable_path,
                payload=payload,
            )
            counts["sample_artifacts"] += 1
            counts["runs"] += 1

    if sample is not None:
        for pattern, artifact_kind in (
            ("ue_equipment_assets_report*.json", "ue_equipment_assets_report"),
            ("ue_equipment_registry*.json", "ue_equipment_registry"),
        ):
            for artifact_path in sorted(trial_root.glob(pattern)):
                payload = _load_json(artifact_path)
                stable_path = _copy_stable_artifact(
                    artifact_path,
                    paths.aiue_roundtrip_sample_artifact_dir(sample["canonical_sample_id"]) / artifact_path.name,
                )
                _record_sample_artifact(
                    conn,
                    sample,
                    stage="verification",
                    artifact_kind=artifact_kind,
                    original_path=artifact_path,
                    stable_path=stable_path,
                    payload=payload,
                )
                counts["sample_artifacts"] += 1

    if canonical is not None:
        conn.execute(
            """
            INSERT OR IGNORE INTO report_imports (
              root_id, report_kind, report_path, external_run_id, imported_at, status, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                canonical["id"],
                "aiue_results",
                str(trial_root),
                "",
                now_iso(),
                "imported",
                json.dumps(
                    {
                        "export_root": str(export_root),
                        "sample_id": sample["canonical_sample_id"] if sample is not None else "",
                    },
                    ensure_ascii=True,
                    sort_keys=True,
                ),
            ),
        )
        conn.commit()

    return {
        "export_root": str(export_root),
        "trial_root": str(trial_root),
        "sample_id": sample["canonical_sample_id"] if sample is not None else "",
        **counts,
    }
