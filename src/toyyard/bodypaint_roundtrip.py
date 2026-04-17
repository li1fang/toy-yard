from __future__ import annotations

import json
import sqlite3
import shutil
from pathlib import Path
from typing import Any

from toyyard.paths import ProjectPaths
from toyyard.util import now_iso, normalize_ext, write_json
from toyyard.warehouse import ensure_artifact


REQUIRED_OUTPUTS = {
    "normalized_glb": "normalized_glb",
    "painted_glb": "painted_glb",
    "analysis_json": "analysis_json",
}
OPTIONAL_OUTPUTS = {
    "manual_overrides": "manual_overrides",
    "engineering_mask": "engineering_mask",
}


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _packet_manifest_path(profile_dir: Path, packet: dict[str, Any]) -> Path:
    raw = str(packet.get("manifest_path") or "").strip()
    if raw:
        candidate = Path(raw)
        if candidate.exists():
            return candidate.resolve()
    rel = str(packet.get("manifest_relpath") or "").strip()
    if rel:
        return (profile_dir / rel).resolve()
    raise ValueError(f"BodyPaint packet is missing a manifest path: {packet.get('package_id') or '<unknown>'}")


def _output_path(manifest_path: Path, manifest_payload: dict[str, Any], key: str) -> Path:
    expected_outputs = manifest_payload.get("expected_outputs") or {}
    rel = str(expected_outputs.get(key) or "").strip()
    if not rel:
        raise ValueError(f"BodyPaint manifest is missing expected_outputs.{key}: {manifest_path}")
    return (manifest_path.parent / rel).resolve()


def _package(conn: sqlite3.Connection, package_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM packages WHERE canonical_package_id = ?",
        (package_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Unknown BodyPaint package: {package_id}")
    return row


def _sample(conn: sqlite3.Connection, sample_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM samples WHERE id = ?", (sample_id,)).fetchone()


def _selected_packets(registry_payload: dict[str, Any], package_refs: list[str] | None) -> list[dict[str, Any]]:
    packets = [dict(packet) for packet in registry_payload.get("packets") or []]
    selected = [str(item or "").strip() for item in (package_refs or []) if str(item or "").strip()]
    if not selected:
        return packets
    selected_set = set(selected)
    matched = [packet for packet in packets if str(packet.get("package_id") or "") in selected_set]
    matched_ids = {str(packet.get("package_id") or "") for packet in matched}
    missing = sorted(selected_set - matched_ids)
    if missing:
        raise ValueError(f"BodyPaint package is not present in registry: {', '.join(missing)}")
    return matched


def _record_output_artifact(
    conn: sqlite3.Connection,
    *,
    package: sqlite3.Row,
    key: str,
    path: Path,
    source_path: Path,
    profile: str,
    sample_id: str,
) -> None:
    ensure_artifact(
        conn,
        owner_type="package",
        owner_id=package["id"],
        stage="bodypaint",
        artifact_kind=key,
        path=str(path),
        format=normalize_ext(path),
        status="imported",
        metadata={
            "profile": profile,
            "sample_id": sample_id,
            "package_id": package["canonical_package_id"],
            "source_path": str(source_path.resolve()),
            "stable_path": str(path.resolve()),
        },
    )


def _stable_output_path(paths: ProjectPaths, *, sample_id: str, package_id: str, artifact_kind: str, source_path: Path) -> Path:
    ext = source_path.suffix.lower()
    return paths.bodypaint_roundtrip_package_dir(sample_id, package_id) / f"{artifact_kind}{ext}"


def _copy_to_stable_evidence(source_path: Path, destination_path: Path) -> Path:
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, destination_path)
    return destination_path.resolve()


def import_bodypaint_results(
    conn: sqlite3.Connection,
    paths: ProjectPaths,
    *,
    profile: str,
    package_refs: list[str] | None = None,
) -> dict[str, Any]:
    profile_dir = paths.bodypaint_profile_dir(profile).resolve()
    registry_path = paths.bodypaint_registry_path(profile).resolve()
    if not registry_path.exists():
        raise FileNotFoundError(f"BodyPaint registry not found: {registry_path}")

    registry_payload = _load_json(registry_path)
    packets = _selected_packets(registry_payload, package_refs)
    if not packets:
        raise ValueError(f"BodyPaint registry has no packets to import: {registry_path}")

    imported_artifacts: list[dict[str, str]] = []
    sample_ids: list[str] = []
    package_ids: list[str] = []

    for packet in packets:
        package_id = str(packet.get("package_id") or "").strip()
        if not package_id:
            raise ValueError("BodyPaint packet is missing package_id")
        package = _package(conn, package_id)
        sample = _sample(conn, package["sample_id"])
        sample_id = str(packet.get("sample_id") or (sample["canonical_sample_id"] if sample else ""))
        stable_sample_id = sample_id or (sample["canonical_sample_id"] if sample else f"sample-{package['sample_id']}")
        manifest_path = _packet_manifest_path(profile_dir, packet)
        if not manifest_path.exists():
            raise FileNotFoundError(f"BodyPaint manifest not found: {manifest_path}")
        manifest_payload = _load_json(manifest_path)

        for output_key, artifact_kind in REQUIRED_OUTPUTS.items():
            output_path = _output_path(manifest_path, manifest_payload, output_key)
            if not output_path.exists():
                raise FileNotFoundError(
                    f"bodypaint_required_output_missing:{package_id}:{output_key}:{output_path}"
                )
            stable_path = _copy_to_stable_evidence(
                output_path,
                _stable_output_path(
                    paths,
                    sample_id=stable_sample_id,
                    package_id=package_id,
                    artifact_kind=artifact_kind,
                    source_path=output_path,
                ),
            )
            _record_output_artifact(
                conn,
                package=package,
                key=artifact_kind,
                path=stable_path,
                source_path=output_path,
                profile=profile,
                sample_id=stable_sample_id,
            )
            imported_artifacts.append(
                {
                    "package_id": package_id,
                    "artifact_kind": artifact_kind,
                    "source_path": str(output_path.resolve()),
                    "stable_path": str(stable_path),
                }
            )

        for output_key, artifact_kind in OPTIONAL_OUTPUTS.items():
            output_path = _output_path(manifest_path, manifest_payload, output_key)
            if not output_path.exists():
                continue
            stable_path = _copy_to_stable_evidence(
                output_path,
                _stable_output_path(
                    paths,
                    sample_id=stable_sample_id,
                    package_id=package_id,
                    artifact_kind=artifact_kind,
                    source_path=output_path,
                ),
            )
            _record_output_artifact(
                conn,
                package=package,
                key=artifact_kind,
                path=stable_path,
                source_path=output_path,
                profile=profile,
                sample_id=stable_sample_id,
            )
            imported_artifacts.append(
                {
                    "package_id": package_id,
                    "artifact_kind": artifact_kind,
                    "source_path": str(output_path.resolve()),
                    "stable_path": str(stable_path),
                }
            )

        if stable_sample_id:
            sample_ids.append(stable_sample_id)
        package_ids.append(package_id)

    unique_sample_ids = sorted(set(sample_ids))
    report_payload = {
        "generated_at_utc": now_iso(),
        "source": "toy-yard import bodypaint-results",
        "profile": profile,
        "sample_ids": unique_sample_ids,
        "package_ids": package_ids,
        "artifacts": imported_artifacts,
        "counts": {
            "packages": len(package_ids),
            "artifacts": len(imported_artifacts),
        },
    }
    report_path = paths.bodypaint_roundtrip_report_path(profile)
    write_json(report_path, report_payload)

    if len(unique_sample_ids) == 1:
        sample_row = conn.execute(
            "SELECT * FROM samples WHERE canonical_sample_id = ?",
            (unique_sample_ids[0],),
        ).fetchone()
        if sample_row is not None:
            ensure_artifact(
                conn,
                owner_type="sample",
                owner_id=sample_row["id"],
                stage="bodypaint",
                artifact_kind="bodypaint_result_import",
                path=str(report_path.resolve()),
                format=".json",
                status="imported",
                metadata={"profile": profile, "package_count": len(package_ids)},
            )

    return {
        "profile": profile,
        "sample_ids": unique_sample_ids,
        "packages": package_ids,
        "artifacts": len(imported_artifacts),
        "report_path": str(report_path.resolve()),
    }
