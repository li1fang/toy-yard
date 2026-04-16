from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from toyyard.paths import ProjectPaths
from toyyard.util import now_iso, write_json
from toyyard.warehouse import (
    canonical_root,
    ensure_alias,
    ensure_artifact,
    ensure_package,
    ensure_report_import,
    ensure_sample,
    ensure_source,
    ensure_source_link,
    json_loads,
    make_canonical_id,
    register_root,
)


def _load_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"audio session manifest must be a JSON object: {path}")
    return payload


_AUDIO_CONTRACT_RANK = {
    "single_utterance_audio_v0": 1,
    "single_utterance_wav_v0": 1,
    "transcribed_audio_v0": 2,
}


def _copy_managed_artifact(source: Path, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    source_resolved = source.expanduser().resolve()
    target_resolved = target.expanduser().resolve()
    if source_resolved != target_resolved:
        shutil.copy2(source_resolved, target_resolved)
    return target


def _path_suffix(path: Path, fallback: str) -> str:
    suffix = path.suffix.lower()
    return suffix if suffix else fallback


def _artifact_copy(
    source_path: str | None,
    target_dir: Path,
    default_name: str,
) -> Path | None:
    if not source_path:
        return None
    source = Path(source_path).expanduser().resolve()
    if not source.exists():
        return None
    target_name = source.name or default_name
    return _copy_managed_artifact(source, target_dir / target_name)


def _audio_contract_type(normalized_path: Path, downloaded: dict[str, Any]) -> str:
    if downloaded.get("result_json") or downloaded.get("transcript_text"):
        return "transcribed_audio_v0"
    if normalized_path.suffix.lower() == ".wav":
        return "single_utterance_wav_v0"
    return "single_utterance_audio_v0"


def _preferred_audio_contract(current: str, incoming: str) -> str:
    current_rank = _AUDIO_CONTRACT_RANK.get(current, 0)
    incoming_rank = _AUDIO_CONTRACT_RANK.get(incoming, 0)
    if incoming_rank >= current_rank:
        return incoming
    return current


def _refresh_audio_package(
    conn: sqlite3.Connection,
    *,
    package_id: int,
    contract_type: str,
    metadata: dict[str, Any],
) -> sqlite3.Row:
    existing = conn.execute("SELECT * FROM packages WHERE id = ?", (package_id,)).fetchone()
    if existing is None:
        raise ValueError(f"Unknown package id: {package_id}")
    effective_contract = _preferred_audio_contract(str(existing["contract_type"] or ""), contract_type)
    merged_metadata = json_loads(existing["metadata_json"])
    merged_metadata.update({key: value for key, value in metadata.items() if value not in (None, "")})
    conn.execute(
        """
        UPDATE packages
        SET contract_type = ?, metadata_json = ?
        WHERE id = ?
        """,
        (effective_contract, json.dumps(merged_metadata, ensure_ascii=True, sort_keys=True), package_id),
    )
    conn.commit()
    refreshed = conn.execute("SELECT * FROM packages WHERE id = ?", (package_id,)).fetchone()
    if refreshed is None:
        raise ValueError(f"Package vanished during refresh: {package_id}")
    return refreshed


def import_audio_session(
    conn: sqlite3.Connection,
    paths: ProjectPaths,
    *,
    manifest_path: Path,
) -> dict[str, Any]:
    manifest_file = manifest_path.expanduser().resolve()
    if not manifest_file.exists():
        raise FileNotFoundError(manifest_file)

    payload = _load_manifest(manifest_file)
    session_id = str(payload.get("session_id") or "")
    if not session_id:
        raise ValueError(f"audio session manifest is missing session_id: {manifest_file}")

    session_root = Path(str(payload.get("session_root") or manifest_file.parent)).expanduser().resolve()
    source_payload = payload.get("source") or {}
    normalized_payload = payload.get("normalized_audio") or {}
    result_summary = payload.get("result_summary") or {}
    downloaded = payload.get("downloaded_artifacts") or {}

    source_candidate = str(source_payload.get("original_path") or source_payload.get("copied_path") or "")
    source_path = Path(source_candidate).expanduser().resolve() if source_candidate else None
    normalized_path = Path(str(normalized_payload.get("path") or "")).expanduser().resolve()
    if not normalized_path.exists():
        raise FileNotFoundError(f"normalized audio is missing: {normalized_path}")

    display_name = (source_path.stem if source_path else normalized_path.stem) or session_id
    contract_type = _audio_contract_type(normalized_path, downloaded)
    sample_id = make_canonical_id("sample", "audio", "comfyui_remote_foundry", session_id, display_name=display_name)
    package_id = make_canonical_id("pkg", "audio", "comfyui_remote_foundry", session_id, display_name=display_name)

    sample_dir = paths.audio_sample_dir(sample_id)
    package_dir = paths.audio_package_dir(sample_id, package_id)
    source_dir = package_dir / "source"
    normalized_dir = package_dir / "normalized"
    transcript_dir = package_dir / "transcripts"

    copied_source_path = _artifact_copy(str(source_path) if source_path else None, source_dir, "source_audio.wav")
    copied_normalized_path = _copy_managed_artifact(
        normalized_path,
        normalized_dir / f"normalized_audio{_path_suffix(normalized_path, '.wav')}",
    )
    copied_transcript_path = _artifact_copy(downloaded.get("transcript_text"), transcript_dir, "transcript.txt")
    copied_segments_path = _artifact_copy(downloaded.get("segments_json"), transcript_dir, "segments.json")

    language_report_path = transcript_dir / "language_report.json"
    if result_summary or payload.get("service_submission"):
        write_json(
            language_report_path,
            {
                "session_id": session_id,
                "language": result_summary.get("language", ""),
                "duration_sec": float(result_summary.get("duration_sec") or normalized_payload.get("duration_sec") or 0.0),
                "generated_at": now_iso(),
            },
        )

    promotion_manifest_path = paths.audio_package_manifest_path(sample_id, package_id)
    write_json(
        promotion_manifest_path,
        {
            "session_id": session_id,
            "canonical_sample_id": sample_id,
            "canonical_package_id": package_id,
            "contract_type": contract_type,
            "source_audio_path": str(copied_source_path) if copied_source_path else "",
            "normalized_audio_path": str(copied_normalized_path),
            "transcript_text_path": str(copied_transcript_path) if copied_transcript_path else "",
            "segments_json_path": str(copied_segments_path) if copied_segments_path else "",
            "language_report_path": str(language_report_path) if language_report_path.exists() else "",
            "imported_at": now_iso(),
        },
    )

    session_artifact_root = session_root.parent if session_root.parent.exists() else session_root
    external_root = register_root(
        conn,
        "external_source",
        str(session_artifact_root),
        "comfyui-remote-foundry-audio-sessions",
        status="active",
        is_canonical_write_root=False,
        metadata={"adapter": "comfyui_remote_foundry_audio_session"},
    )

    source = ensure_source(
        conn,
        root_id=external_root["id"],
        source_kind="comfyui_audio_session",
        source_path=str(source_path or normalized_path),
        managed_path=str(copied_normalized_path),
        content_hash=str(normalized_payload.get("sha256") or ""),
        ext=normalized_path.suffix.lower(),
        size_bytes=normalized_path.stat().st_size,
        status="cataloged",
        metadata={
            "session_id": session_id,
            "session_root": str(session_root),
            "manifest_path": str(manifest_file),
            "contract_type": contract_type,
        },
    )

    sample = ensure_sample(
        conn,
        canonical_sample_id=sample_id,
        display_name=display_name,
        source_game="comfyui_remote_foundry",
        source_format="audio_session",
        item_family="audio",
        warehouse_status="cataloged",
        metadata={
            "session_id": session_id,
            "source_kind": source_payload.get("kind", ""),
        },
    )
    package = ensure_package(
        conn,
        sample_id=sample["id"],
        canonical_package_id=package_id,
        package_role="utterance",
        content_bucket="audio",
        contract_type=contract_type,
        consumer_ready=False,
        warehouse_status="cataloged",
        metadata={
            "session_id": session_id,
            "duration_sec": float(result_summary.get("duration_sec") or normalized_payload.get("duration_sec") or 0.0),
            "language": result_summary.get("language", ""),
        },
    )
    package = _refresh_audio_package(
        conn,
        package_id=package["id"],
        contract_type=contract_type,
        metadata={
            "session_id": session_id,
            "duration_sec": float(result_summary.get("duration_sec") or normalized_payload.get("duration_sec") or 0.0),
            "language": result_summary.get("language", ""),
        },
    )
    ensure_alias(
        conn,
        entity_type="sample",
        entity_id=sample["id"],
        external_system="comfyui_remote_foundry",
        alias_key="session_id",
        alias_value=session_id,
    )
    ensure_alias(
        conn,
        entity_type="package",
        entity_id=package["id"],
        external_system="comfyui_remote_foundry",
        alias_key="session_id",
        alias_value=session_id,
    )
    ensure_source_link(
        conn,
        source_id=source["id"],
        entity_type="sample",
        entity_id=sample["id"],
        relation_kind="audio_session",
    )

    if copied_source_path is not None:
        ensure_artifact(
            conn,
            owner_type="source",
            owner_id=source["id"],
            stage="source",
            artifact_kind="source_audio",
            path=str(copied_source_path),
            format=copied_source_path.suffix.lower(),
            status="cataloged",
            metadata={"session_id": session_id},
        )
    ensure_artifact(
        conn,
        owner_type="package",
        owner_id=package["id"],
        stage="session",
        artifact_kind="normalized_audio",
        path=str(copied_normalized_path),
        format=copied_normalized_path.suffix.lower(),
        status="cataloged",
        metadata={"session_id": session_id},
    )
    if copied_transcript_path is not None:
        ensure_artifact(
            conn,
            owner_type="package",
            owner_id=package["id"],
            stage="session",
            artifact_kind="transcript_text",
            path=str(copied_transcript_path),
            format=copied_transcript_path.suffix.lower(),
            status="cataloged",
            metadata={"session_id": session_id},
        )
    if copied_segments_path is not None:
        ensure_artifact(
            conn,
            owner_type="package",
            owner_id=package["id"],
            stage="session",
            artifact_kind="segments_json",
            path=str(copied_segments_path),
            format=copied_segments_path.suffix.lower(),
            status="cataloged",
            metadata={"session_id": session_id},
        )
    if language_report_path.exists():
        ensure_artifact(
            conn,
            owner_type="package",
            owner_id=package["id"],
            stage="session",
            artifact_kind="language_report",
            path=str(language_report_path),
            format=language_report_path.suffix.lower(),
            status="cataloged",
            metadata={"session_id": session_id},
        )
    ensure_artifact(
        conn,
        owner_type="package",
        owner_id=package["id"],
        stage="session",
        artifact_kind="promotion_manifest",
        path=str(promotion_manifest_path),
        format=promotion_manifest_path.suffix.lower(),
        status="cataloged",
        metadata={"session_id": session_id},
    )

    canonical = canonical_root(conn)
    if canonical is not None:
        ensure_report_import(
            conn,
            root_id=canonical["id"],
            report_kind="audio_session_import",
            report_path=str(manifest_file),
            status="imported",
            metadata={"session_id": session_id, "contract_type": contract_type},
        )

    return {
        "session_id": session_id,
        "canonical_sample_id": sample_id,
        "canonical_package_id": package_id,
        "promotion_manifest_path": str(promotion_manifest_path),
        "contract_type": contract_type,
    }
