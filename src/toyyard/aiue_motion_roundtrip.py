from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

from toyyard.paths import ProjectPaths
from toyyard.util import now_iso, write_json
from toyyard.warehouse import canonical_root, ensure_artifact, ensure_report_import, ensure_run, json_loads, resolve_sample


REQUEST_SCHEMA_VERSION = "motion_consumer_request_v0"
RESULT_SCHEMA_VERSION = "motion_consumer_result_v0"
SUPPORTED_OPERATIONS = ("import_motion_packet", "retarget_preflight", "animation_preview")
OPERATION_STAGE = {
    "import_motion_packet": "session",
    "retarget_preflight": "verification",
    "animation_preview": "verification",
}
OPERATION_TOOL = {
    "import_motion_packet": "AiUE import_motion_packet",
    "retarget_preflight": "AiUE retarget_preflight",
    "animation_preview": "AiUE animation_preview",
}
LEGACY_SAMPLE_FILES: tuple[tuple[str, str, str], ...] = (
    ("latest_toy_yard_motion_default_source_m1_report.json", "legacy_motion_trial_report", "verification"),
    ("toy_yard_motion_default_source_m1_report.json", "legacy_motion_trial_report", "verification"),
    ("motion_packet_context.json", "consumer_context", "session"),
    ("motion_clip_selection.json", "consumer_context", "session"),
    ("motion_packet_state.json", "consumer_state", "session"),
)


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _canonical_json_hash(payload: dict[str, Any]) -> str:
    text = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _path_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _find_package(conn: sqlite3.Connection, package_id: str | None) -> sqlite3.Row | None:
    if not package_id:
        return None
    return conn.execute("SELECT * FROM packages WHERE canonical_package_id = ?", (package_id,)).fetchone()


def _sample_for_package(conn: sqlite3.Connection, package: sqlite3.Row | None) -> sqlite3.Row | None:
    if package is None:
        return None
    return conn.execute("SELECT * FROM samples WHERE id = ?", (package["sample_id"],)).fetchone()


def _summary_sample_id(export_root: Path) -> str | None:
    summary_path = export_root / "summary" / "motion_suite_summary.json"
    if not summary_path.exists():
        return None
    payload = _load_json(summary_path)
    return str(payload.get("sample_id") or "").strip() or None


def _manifest_package_map(export_root: Path) -> dict[str, Path]:
    mapping: dict[str, Path] = {}
    clips_root = export_root / "clips"
    if not clips_root.exists():
        return mapping
    for manifest_path in sorted(clips_root.rglob("manifest.json")):
        payload = _load_json(manifest_path)
        package_id = str(payload.get("package_id") or "").strip()
        if package_id:
            mapping[package_id] = manifest_path
    return mapping


def _discover_schema_payloads(root: Path, schema_version: str) -> list[tuple[Path, dict[str, Any]]]:
    if not root.exists():
        return []
    discovered: list[tuple[Path, dict[str, Any]]] = []
    for json_path in sorted(root.rglob("*.json")):
        try:
            payload = _load_json(json_path)
        except (OSError, json.JSONDecodeError):
            continue
        if str(payload.get("schema_version") or "") == schema_version:
            discovered.append((json_path, payload))
    return discovered


def _resolve_package_from_manifest_map(
    conn: sqlite3.Connection,
    manifest_map: dict[str, Path],
    *,
    package_id: str,
    manifest_path: str,
) -> sqlite3.Row | None:
    if package_id:
        package = _find_package(conn, package_id)
        if package is not None:
            return package

    manifest_value = manifest_path.strip()
    if manifest_value:
        candidate = Path(manifest_value)
        for candidate_package_id, candidate_manifest in manifest_map.items():
            if str(candidate_manifest.resolve()).lower() == str(candidate.resolve()).lower():
                return _find_package(conn, candidate_package_id)
        if candidate.name == "manifest.json" and candidate.parent.name:
            return _find_package(conn, candidate.parent.name)

    return None


def _stable_copy(source_path: Path, destination_path: Path) -> Path:
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, destination_path)
    return destination_path.resolve()


def _stable_json_copy(
    source_path: Path,
    destination_dir: Path,
    *,
    stem: str,
    content_hash: str,
) -> Path:
    filename = f"{stem}__{content_hash[:12]}{source_path.suffix.lower() or '.json'}"
    return _stable_copy(source_path, destination_dir / filename)


def _artifact_metadata(
    *,
    payload: dict[str, Any],
    original_path: Path,
    stable_path: Path,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    merged = {
        "payload": payload,
        "toy_yard_original_path": str(original_path.resolve()),
        "toy_yard_stable_path": str(stable_path.resolve()),
    }
    if extra:
        merged.update(extra)
    return merged


def _record_package_artifact(
    conn: sqlite3.Connection,
    package: sqlite3.Row,
    *,
    stage: str,
    artifact_kind: str,
    original_path: Path,
    stable_path: Path,
    payload: dict[str, Any],
    extra: dict[str, Any] | None = None,
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
        metadata=_artifact_metadata(payload=payload, original_path=original_path, stable_path=stable_path, extra=extra),
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
    extra: dict[str, Any] | None = None,
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
        metadata=_artifact_metadata(payload=payload, original_path=original_path, stable_path=stable_path, extra=extra),
    )


def _record_run(
    conn: sqlite3.Connection,
    *,
    entity_type: str,
    entity_id: int,
    stage: str,
    tool_name: str,
    payload: dict[str, Any],
    action_path: Path,
    summary: str = "",
) -> None:
    timestamp = str(payload.get("generated_at_utc") or payload.get("created_at_utc") or now_iso())
    ensure_run(
        conn,
        entity_type=entity_type,
        entity_id=entity_id,
        stage=stage,
        tool_name=tool_name,
        status=str(payload.get("status") or "unknown"),
        started_at=timestamp,
        ended_at=timestamp,
        log_path=str(action_path.resolve()),
        summary=summary or f"{tool_name}:{payload.get('status') or 'unknown'}",
    )


def _payload_status(payload: dict[str, Any]) -> str:
    result = payload.get("result") or {}
    return str(payload.get("status") or result.get("status") or "").strip().lower()


def _result_owner(payload: dict[str, Any]) -> str:
    communication_signal = payload.get("communication_signal") or {}
    return str(communication_signal.get("owner") or "").strip()


def _result_failure_class(payload: dict[str, Any]) -> str:
    value = payload.get("failure_class")
    return "" if value in (None, "") else str(value).strip()


def _result_operation(payload: dict[str, Any]) -> str:
    return str(payload.get("operation") or "").strip()


def _validate_consumer_request(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if str(payload.get("schema_version") or "") != REQUEST_SCHEMA_VERSION:
        errors.append("schema_version_mismatch")
    operation = str(payload.get("operation") or "").strip()
    if operation not in SUPPORTED_OPERATIONS:
        errors.append("unsupported_operation")
    packet_manifest_path = str(payload.get("packet_manifest_path") or "").strip()
    if not packet_manifest_path:
        errors.append("packet_manifest_path_missing")
    return errors


def _validate_consumer_result(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if str(payload.get("schema_version") or "") != RESULT_SCHEMA_VERSION:
        errors.append("schema_version_mismatch")

    status = str(payload.get("status") or "").strip()
    if status not in {"pass", "fail"}:
        errors.append("status_invalid")

    operation = _result_operation(payload)
    if operation not in SUPPORTED_OPERATIONS:
        errors.append("unsupported_operation")

    packet_ref = payload.get("packet_ref") or {}
    if not isinstance(packet_ref, dict):
        errors.append("packet_ref_invalid")
        packet_ref = {}
    if not str(packet_ref.get("packet_manifest_path") or "").strip():
        errors.append("packet_manifest_path_missing")

    communication_signal = payload.get("communication_signal") or {}
    if not isinstance(communication_signal, dict):
        errors.append("communication_signal_invalid")
        communication_signal = {}
    owner = str(communication_signal.get("owner") or "").strip()
    if owner not in {"toy-yard", "aiue", "none"}:
        errors.append("owner_invalid")
    if not str(communication_signal.get("reason") or "").strip():
        errors.append("communication_reason_missing")

    failure_class = _result_failure_class(payload)
    if status == "fail" and not failure_class:
        errors.append("failure_class_required_for_fail")
    if status == "fail" and owner == "none":
        errors.append("failed_result_owner_none")

    return errors


def _consumer_result_summary_entry(
    *,
    payload: dict[str, Any],
    source_path: Path,
    stable_path: Path,
    result_content_hash: str,
) -> dict[str, Any]:
    communication_signal = payload.get("communication_signal") or {}
    return {
        "operation": _result_operation(payload),
        "status": _payload_status(payload),
        "owner": str(communication_signal.get("owner") or ""),
        "failure_class": _result_failure_class(payload),
        "recommended_node": str(communication_signal.get("recommended_node") or ""),
        "sample_id": str(((payload.get("packet_ref") or {}).get("sample_id") or "")),
        "package_id": str(((payload.get("packet_ref") or {}).get("package_id") or "")),
        "source_result_path": str(source_path.resolve()),
        "stable_result_path": str(stable_path.resolve()),
        "result_content_hash": result_content_hash,
    }


def _copy_referenced_evidence(
    conn: sqlite3.Connection,
    package: sqlite3.Row,
    sample_id: str,
    payload: dict[str, Any],
    paths: ProjectPaths,
    counts: Counter[str],
) -> None:
    operation = _result_operation(payload)
    package_dir = paths.aiue_motion_roundtrip_package_dir(sample_id, package["canonical_package_id"])
    artifacts = payload.get("artifacts") or {}
    preview_evidence = payload.get("preview_evidence") or {}

    referenced: list[tuple[str, str, str]] = []
    if operation == "import_motion_packet":
        referenced.append(("motion_import_report", str(artifacts.get("import_action_path") or ""), "session"))
    elif operation == "animation_preview":
        preview_report = str(preview_evidence.get("result_json_path") or "") or str(artifacts.get("preview_action_path") or "")
        referenced.append(("motion_preview_report", preview_report, "verification"))

    for artifact_kind, candidate_path, stage in referenced:
        if not candidate_path:
            continue
        source_path = Path(candidate_path)
        if not source_path.exists() or not source_path.is_file():
            continue
        payload_hash = _path_hash(source_path)
        stable_path = _stable_json_copy(
            source_path,
            package_dir,
            stem=artifact_kind,
            content_hash=payload_hash,
        )
        extra = {"source_operation": operation, "content_hash": payload_hash}
        try:
            evidence_payload = _load_json(source_path)
        except (OSError, json.JSONDecodeError):
            evidence_payload = {"path": str(source_path.resolve())}
        _record_package_artifact(
            conn,
            package,
            stage=stage,
            artifact_kind=artifact_kind,
            original_path=source_path,
            stable_path=stable_path,
            payload=evidence_payload,
            extra=extra,
        )
        counts["package_artifacts"] += 1


def _update_package_from_result(
    conn: sqlite3.Connection,
    package: sqlite3.Row,
    *,
    payload: dict[str, Any],
    stable_result_path: Path,
    result_content_hash: str,
) -> sqlite3.Row:
    metadata = json_loads(package["metadata_json"])
    operation = _result_operation(payload)
    communication_signal = payload.get("communication_signal") or {}
    generated_assets = payload.get("generated_assets") or {}
    host_resolution = payload.get("host_resolution") or {}
    operation_status = {
        "status": _payload_status(payload),
        "owner": str(communication_signal.get("owner") or ""),
        "failure_class": _result_failure_class(payload),
        "recommended_node": str(communication_signal.get("recommended_node") or ""),
        "reason": str(communication_signal.get("reason") or ""),
        "result_content_hash": result_content_hash,
        "result_path": str(stable_result_path.resolve()),
        "generated_assets": generated_assets,
        "host_resolution": host_resolution,
    }
    existing_ops = metadata.get("motion_consumer_results") or {}
    if not isinstance(existing_ops, dict):
        existing_ops = {}
    existing_ops[operation] = operation_status
    metadata["motion_consumer_results"] = existing_ops
    metadata["latest_motion_consumer_operation"] = operation
    metadata["latest_motion_consumer_status"] = operation_status["status"]
    metadata["latest_motion_consumer_owner"] = operation_status["owner"]
    metadata["latest_motion_failure_class"] = operation_status["failure_class"]
    metadata["latest_motion_recommended_node"] = operation_status["recommended_node"]
    metadata["latest_motion_result_content_hash"] = result_content_hash
    metadata["latest_motion_result_path"] = str(stable_result_path.resolve())

    animation_asset_path = str(
        generated_assets.get("retargeted_animation_asset_path")
        or generated_assets.get("animation_asset_path")
        or metadata.get("latest_motion_animation_asset_path")
        or ""
    )
    if animation_asset_path:
        metadata["latest_motion_animation_asset_path"] = animation_asset_path
    target_skeleton = str(host_resolution.get("target_skeleton_asset_path") or metadata.get("latest_motion_target_skeleton_asset_path") or "")
    if target_skeleton:
        metadata["latest_motion_target_skeleton_asset_path"] = target_skeleton
    target_host = str(host_resolution.get("target_host_asset_path") or metadata.get("latest_motion_target_host_asset_path") or "")
    if target_host:
        metadata["latest_motion_target_host_asset_path"] = target_host

    conn.execute(
        "UPDATE packages SET metadata_json = ? WHERE id = ?",
        (json.dumps(metadata, ensure_ascii=True, sort_keys=True), package["id"]),
    )
    conn.commit()
    return conn.execute("SELECT * FROM packages WHERE id = ?", (package["id"],)).fetchone()


def _package_ready(package: sqlite3.Row) -> bool:
    metadata = json_loads(package["metadata_json"])
    operation_results = metadata.get("motion_consumer_results") or {}
    if not isinstance(operation_results, dict):
        return False
    import_result = operation_results.get("import_motion_packet") or {}
    preview_result = operation_results.get("animation_preview") or {}
    return (
        str(import_result.get("status") or "") == "pass"
        and str(preview_result.get("status") or "") == "pass"
        and str(preview_result.get("owner") or "") == "none"
    )


def _promote_package_if_ready(conn: sqlite3.Connection, package: sqlite3.Row) -> None:
    if package["consumer_ready"] or not _package_ready(package):
        return
    conn.execute(
        """
        UPDATE packages
        SET consumer_ready = 1,
            warehouse_status = 'ready_for_pipeline'
        WHERE id = ?
        """,
        (package["id"],),
    )
    conn.commit()


def _import_motion_consumer_contracts(
    conn: sqlite3.Connection,
    paths: ProjectPaths,
    *,
    export_root: Path,
    trial_root: Path,
) -> dict[str, Any]:
    export_root = export_root.expanduser().resolve()
    trial_root = trial_root.expanduser().resolve()
    manifest_map = _manifest_package_map(export_root)
    sample = resolve_sample(conn, _summary_sample_id(export_root) or "")
    if sample is None and manifest_map:
        first_package = _find_package(conn, next(iter(manifest_map)))
        sample = _sample_for_package(conn, first_package)

    request_files = _discover_schema_payloads(trial_root, REQUEST_SCHEMA_VERSION)
    result_files = _discover_schema_payloads(trial_root, RESULT_SCHEMA_VERSION)
    if not request_files and not result_files:
        raise ValueError("No motion consumer request/result schema payloads were found under the trial root.")
    if not result_files:
        raise ValueError("No motion_consumer_result_v0 payloads were found under the trial root.")

    counts: Counter[str] = Counter()
    request_entries: list[dict[str, Any]] = []
    result_entries: list[dict[str, Any]] = []

    for request_path, payload in request_files:
        errors = _validate_consumer_request(payload)
        if errors:
            raise ValueError(f"Invalid motion consumer request at {request_path}: {', '.join(errors)}")

        target_package_id = str(payload.get("target_package_id") or "").strip()
        package = _resolve_package_from_manifest_map(
            conn,
            manifest_map,
            package_id=target_package_id,
            manifest_path=str(payload.get("packet_manifest_path") or ""),
        )
        request_sample = _sample_for_package(conn, package) or sample
        if request_sample is None:
            continue

        request_hash = _canonical_json_hash(payload)
        operation = str(payload.get("operation") or "unknown")
        if package is not None:
            stable_path = _stable_json_copy(
                request_path,
                paths.aiue_motion_roundtrip_package_dir(request_sample["canonical_sample_id"], package["canonical_package_id"]),
                stem=f"consumer_request__{operation}",
                content_hash=request_hash,
            )
            _record_package_artifact(
                conn,
                package,
                stage=OPERATION_STAGE.get(operation, "session"),
                artifact_kind="consumer_request",
                original_path=request_path,
                stable_path=stable_path,
                payload=payload,
                extra={"operation": operation, "result_content_hash": request_hash},
            )
            counts["package_artifacts"] += 1
        else:
            stable_path = _stable_json_copy(
                request_path,
                paths.aiue_motion_roundtrip_sample_artifact_dir(request_sample["canonical_sample_id"]),
                stem=f"consumer_request__{operation}",
                content_hash=request_hash,
            )
            _record_sample_artifact(
                conn,
                request_sample,
                stage=OPERATION_STAGE.get(operation, "session"),
                artifact_kind="consumer_request",
                original_path=request_path,
                stable_path=stable_path,
                payload=payload,
                extra={"operation": operation, "result_content_hash": request_hash},
            )
            counts["sample_artifacts"] += 1

        request_entries.append(
            {
                "operation": operation,
                "package_id": package["canonical_package_id"] if package is not None else "",
                "sample_id": request_sample["canonical_sample_id"],
                "request_content_hash": request_hash,
                "source_request_path": str(request_path.resolve()),
                "stable_request_path": str(stable_path.resolve()),
            }
        )

    for result_path, payload in result_files:
        errors = _validate_consumer_result(payload)
        if errors:
            raise ValueError(f"Invalid motion consumer result at {result_path}: {', '.join(errors)}")

        packet_ref = payload.get("packet_ref") or {}
        package = _resolve_package_from_manifest_map(
            conn,
            manifest_map,
            package_id=str(packet_ref.get("package_id") or ""),
            manifest_path=str(packet_ref.get("packet_manifest_path") or ""),
        )
        result_sample = _sample_for_package(conn, package) or sample
        if result_sample is None and str(packet_ref.get("sample_id") or "").strip():
            result_sample = resolve_sample(conn, str(packet_ref.get("sample_id") or ""))
        if result_sample is None:
            continue

        result_hash = _canonical_json_hash(payload)
        operation = _result_operation(payload)
        destination_dir = (
            paths.aiue_motion_roundtrip_package_dir(result_sample["canonical_sample_id"], package["canonical_package_id"])
            if package is not None
            else paths.aiue_motion_roundtrip_sample_artifact_dir(result_sample["canonical_sample_id"])
        )
        stable_result_path = _stable_json_copy(
            result_path,
            destination_dir,
            stem=f"consumer_result__{operation}",
            content_hash=result_hash,
        )

        extra = {
            "operation": operation,
            "owner": _result_owner(payload),
            "failure_class": _result_failure_class(payload),
            "result_content_hash": result_hash,
        }
        if package is not None:
            _record_package_artifact(
                conn,
                package,
                stage=OPERATION_STAGE.get(operation, "session"),
                artifact_kind="consumer_result",
                original_path=result_path,
                stable_path=stable_result_path,
                payload=payload,
                extra=extra,
            )
            counts["package_artifacts"] += 1
            package = _update_package_from_result(
                conn,
                package,
                payload=payload,
                stable_result_path=stable_result_path,
                result_content_hash=result_hash,
            )
            _copy_referenced_evidence(conn, package, result_sample["canonical_sample_id"], payload, paths, counts)
            _record_run(
                conn,
                entity_type="package",
                entity_id=package["id"],
                stage=OPERATION_STAGE.get(operation, "session"),
                tool_name=OPERATION_TOOL.get(operation, f"AiUE {operation}"),
                payload=payload,
                action_path=stable_result_path,
                summary=f"{operation}:{_payload_status(payload)}:{_result_owner(payload) or 'unknown'}",
            )
            counts["runs"] += 1
            _promote_package_if_ready(conn, package)
        else:
            _record_sample_artifact(
                conn,
                result_sample,
                stage=OPERATION_STAGE.get(operation, "session"),
                artifact_kind="consumer_result",
                original_path=result_path,
                stable_path=stable_result_path,
                payload=payload,
                extra=extra,
            )
            counts["sample_artifacts"] += 1

        result_entries.append(
            _consumer_result_summary_entry(
                payload=payload,
                source_path=result_path,
                stable_path=stable_result_path,
                result_content_hash=result_hash,
            )
        )

    if sample is None and result_entries:
        sample = resolve_sample(conn, result_entries[0]["sample_id"])

    if sample is not None:
        sample_dir = paths.aiue_motion_roundtrip_sample_artifact_dir(sample["canonical_sample_id"])
        for filename, artifact_kind, stage in LEGACY_SAMPLE_FILES:
            artifact_path = trial_root / filename
            if not artifact_path.exists():
                continue
            payload = _load_json(artifact_path)
            stable_path = _copy_stable_artifact(artifact_path, sample_dir / filename)
            _record_sample_artifact(
                conn,
                sample,
                stage=stage,
                artifact_kind=artifact_kind,
                original_path=artifact_path,
                stable_path=stable_path,
                payload=payload,
            )
            counts["sample_artifacts"] += 1

    owner_counts = Counter(entry["owner"] for entry in result_entries)
    eligible_for_m1 = any(
        entry["operation"] == "animation_preview" and entry["status"] == "pass" and entry["owner"] == "none"
        for entry in result_entries
    )
    if owner_counts.get("toy-yard", 0):
        next_node = "repair_motion_packet_locally"
    elif owner_counts.get("aiue", 0):
        next_node = next((entry["recommended_node"] for entry in result_entries if entry["recommended_node"]), "aiue_motion_runtime_followup")
    elif eligible_for_m1:
        next_node = "m1_default_source_candidate"
    elif any(entry["operation"] == "import_motion_packet" and entry["status"] == "pass" for entry in result_entries):
        next_node = "animation_preview"
    else:
        next_node = "await_motion_consumer_result"

    round_summary_payload = {
        "generated_at_utc": now_iso(),
        "trial_node": "M0.5 Motion Shadow Packet Trial",
        "sample_id": sample["canonical_sample_id"] if sample is not None else "",
        "export_root": str(export_root),
        "trial_root": str(trial_root),
        "counts": {
            "request_count": len(request_entries),
            "result_count": len(result_entries),
            "package_artifacts": counts["package_artifacts"],
            "sample_artifacts": counts["sample_artifacts"],
            "runs": counts["runs"],
            "owner_toy_yard": owner_counts.get("toy-yard", 0),
            "owner_aiue": owner_counts.get("aiue", 0),
            "owner_none": owner_counts.get("none", 0),
        },
        "requests": request_entries,
        "results": result_entries,
        "eligible_for_m1": eligible_for_m1,
        "next_node": next_node,
    }
    round_summary_path = None
    if sample is not None:
        round_summary_path = paths.aiue_motion_roundtrip_sample_artifact_dir(sample["canonical_sample_id"]) / "motion_round_summary.json"
        write_json(round_summary_path, round_summary_payload)
        _record_sample_artifact(
            conn,
            sample,
            stage="verification",
            artifact_kind="motion_round_summary",
            original_path=round_summary_path,
            stable_path=round_summary_path,
            payload=round_summary_payload,
        )

    canonical = canonical_root(conn)
    if canonical is not None:
        ensure_report_import(
            conn,
            root_id=canonical["id"],
            report_kind="aiue_motion_results",
            report_path=str(trial_root),
            status="imported",
            metadata={
                "export_root": str(export_root),
                "sample_id": sample["canonical_sample_id"] if sample is not None else "",
                "result_count": len(result_entries),
                "request_count": len(request_entries),
                "next_node": next_node,
            },
        )

    return {
        "mode": "motion_consumer_seam_v0",
        "export_root": str(export_root),
        "trial_root": str(trial_root),
        "sample_id": sample["canonical_sample_id"] if sample is not None else "",
        "request_count": len(request_entries),
        "result_count": len(result_entries),
        "package_artifacts": counts["package_artifacts"],
        "sample_artifacts": counts["sample_artifacts"],
        "runs": counts["runs"],
        "owner_toy_yard": owner_counts.get("toy-yard", 0),
        "owner_aiue": owner_counts.get("aiue", 0),
        "owner_none": owner_counts.get("none", 0),
        "eligible_for_m1": int(eligible_for_m1),
        "next_node": next_node,
        "round_summary_path": str(round_summary_path.resolve()) if round_summary_path is not None else "",
    }


def _copy_stable_artifact(source_path: Path, destination_path: Path) -> Path:
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, destination_path)
    return destination_path.resolve()


def _successful_motion_roundtrip(import_payload: dict[str, Any], preview_payload: dict[str, Any]) -> bool:
    return _payload_status(import_payload) == "pass" and _payload_status(preview_payload) == "pass"


def _update_package_ready_legacy(
    conn: sqlite3.Connection,
    package: sqlite3.Row,
    *,
    import_payload: dict[str, Any],
    preview_payload: dict[str, Any],
) -> None:
    metadata = json_loads(package["metadata_json"])
    import_result = dict(import_payload.get("result") or {})
    metadata.update(
        {
            "latest_motion_animation_asset_path": str(
                import_result.get("imported_animation_asset_path")
                or ((import_result.get("imported_assets") or {}).get("animation_asset_path") or "")
            ),
            "latest_motion_target_skeleton_asset_path": str(import_result.get("target_skeleton_asset_path") or ""),
            "latest_motion_retarget_refs": import_result.get("retarget_refs") or {},
            "latest_motion_ingest_status": _payload_status(import_payload),
            "latest_motion_preview_status": _payload_status(preview_payload),
        }
    )
    conn.execute(
        """
        UPDATE packages
        SET consumer_ready = 1,
            warehouse_status = 'ready_for_pipeline',
            metadata_json = ?
        WHERE id = ?
        """,
        (json.dumps(metadata, ensure_ascii=True, sort_keys=True), package["id"]),
    )
    conn.commit()


def _import_legacy_sidecar_results(
    conn: sqlite3.Connection,
    paths: ProjectPaths,
    *,
    export_root: Path,
    trial_root: Path,
) -> dict[str, Any]:
    export_root = export_root.expanduser().resolve()
    trial_root = trial_root.expanduser().resolve()
    manifest_map = _manifest_package_map(export_root)
    sample = resolve_sample(conn, _summary_sample_id(export_root) or "")
    if sample is None and manifest_map:
        first_package = _find_package(conn, next(iter(manifest_map)))
        if first_package is not None:
            sample = _sample_for_package(conn, first_package)

    counts = {"package_artifacts": 0, "sample_artifacts": 0, "runs": 0}
    package_sidecars: dict[str, dict[str, dict[str, Any]]] = {}

    for package_id, manifest_path in manifest_map.items():
        package = _find_package(conn, package_id)
        if package is None:
            continue
        package_sample = _sample_for_package(conn, package)
        sample_id = package_sample["canonical_sample_id"] if package_sample is not None else sample["canonical_sample_id"]
        package_sidecars[package_id] = {}
        for filename, artifact_kind, stage, tool_name in (
            ("motion_import_report.local.json", "motion_import_report", "session", "AiUE import-motion-packet"),
            ("motion_preview_report.local.json", "motion_preview_report", "verification", "AiUE animation-preview"),
        ):
            artifact_path = manifest_path.parent / filename
            if not artifact_path.exists():
                continue
            payload = _load_json(artifact_path)
            stable_path = _copy_stable_artifact(
                artifact_path,
                paths.aiue_motion_roundtrip_package_dir(sample_id, package["canonical_package_id"]) / filename,
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
            _record_run(
                conn,
                entity_type="package",
                entity_id=package["id"],
                stage=stage,
                tool_name=tool_name,
                payload=payload,
                action_path=stable_path,
            )
            package_sidecars[package_id][artifact_kind] = payload
            counts["package_artifacts"] += 1
            counts["runs"] += 1

        import_payload = package_sidecars[package_id].get("motion_import_report") or {}
        preview_payload = package_sidecars[package_id].get("motion_preview_report") or {}
        if _successful_motion_roundtrip(import_payload, preview_payload):
            _update_package_ready_legacy(conn, package, import_payload=import_payload, preview_payload=preview_payload)

    if sample is not None:
        for filename, artifact_kind, stage in LEGACY_SAMPLE_FILES:
            artifact_path = trial_root / filename
            if not artifact_path.exists():
                continue
            payload = _load_json(artifact_path)
            stable_path = _copy_stable_artifact(
                artifact_path,
                paths.aiue_motion_roundtrip_sample_artifact_dir(sample["canonical_sample_id"]) / filename,
            )
            _record_sample_artifact(
                conn,
                sample,
                stage=stage,
                artifact_kind=artifact_kind,
                original_path=artifact_path,
                stable_path=stable_path,
                payload=payload,
            )
            counts["sample_artifacts"] += 1

    canonical = canonical_root(conn)
    if canonical is not None:
        ensure_report_import(
            conn,
            root_id=canonical["id"],
            report_kind="aiue_motion_results",
            report_path=str(trial_root),
            status="imported",
            metadata={
                "export_root": str(export_root),
                "sample_id": sample["canonical_sample_id"] if sample is not None else "",
                "mode": "legacy_sidecars",
            },
        )

    return {
        "mode": "legacy_sidecars",
        "export_root": str(export_root),
        "trial_root": str(trial_root),
        "sample_id": sample["canonical_sample_id"] if sample is not None else "",
        **counts,
    }


def import_aiue_motion_results(
    conn: sqlite3.Connection,
    paths: ProjectPaths,
    *,
    export_root: Path,
    trial_root: Path,
) -> dict[str, Any]:
    export_root = export_root.expanduser().resolve()
    trial_root = trial_root.expanduser().resolve()

    request_files = _discover_schema_payloads(trial_root, REQUEST_SCHEMA_VERSION)
    result_files = _discover_schema_payloads(trial_root, RESULT_SCHEMA_VERSION)
    if request_files or result_files:
        return _import_motion_consumer_contracts(conn, paths, export_root=export_root, trial_root=trial_root)
    return _import_legacy_sidecar_results(conn, paths, export_root=export_root, trial_root=trial_root)
