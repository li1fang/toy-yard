from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from toyyard.paths import ProjectPaths
from toyyard.util import now_iso, write_json
from toyyard.warehouse import (
    ensure_alias,
    ensure_artifact,
    ensure_package,
    ensure_report_import,
    ensure_sample,
    ensure_source,
    ensure_source_link,
    find_entity_by_alias,
    register_root,
)

PACKET_SCHEMA_VERSION = "audio_index_packet_v0"
PACKET_KIND = "audio_index"
REMOTE_ARTIFACT_STATUS = "remote_indexed"


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any]:
    return dict(row) if row is not None else {}


def _json_dict(text: str | None) -> dict[str, Any]:
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _sample_row(conn: sqlite3.Connection, sample_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM samples WHERE id = ?", (sample_id,)).fetchone()
    if row is None:
        raise ValueError(f"Unknown sample id: {sample_id}")
    return row


def _sample_alias_payloads(conn: sqlite3.Connection, sample_id: int) -> list[dict[str, str]]:
    rows = conn.execute(
        """
        SELECT external_system, alias_key, alias_value
        FROM aliases
        WHERE entity_type = 'sample' AND entity_id = ?
        ORDER BY external_system, alias_key, alias_value
        """,
        (sample_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _package_alias_payloads(conn: sqlite3.Connection, package_id: int) -> list[dict[str, str]]:
    rows = conn.execute(
        """
        SELECT external_system, alias_key, alias_value
        FROM aliases
        WHERE entity_type = 'package' AND entity_id = ?
        ORDER BY external_system, alias_key, alias_value
        """,
        (package_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _sample_source_payload(conn: sqlite3.Connection, sample_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT sources.*
        FROM sources
        JOIN source_links
          ON source_links.source_id = sources.id
        WHERE source_links.entity_type = 'sample' AND source_links.entity_id = ?
        ORDER BY sources.id
        LIMIT 1
        """,
        (sample_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Audio sample {sample_id} has no linked source")
    payload = _row_dict(row)
    payload["metadata"] = _json_dict(row["metadata_json"])
    return payload


def _artifact_payloads(conn: sqlite3.Connection, *, owner_type: str, owner_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT stage, artifact_kind, path, format, status, metadata_json
        FROM artifacts
        WHERE owner_type = ? AND owner_id = ?
        ORDER BY stage, artifact_kind, path
        """,
        (owner_type, owner_id),
    ).fetchall()
    payloads: list[dict[str, Any]] = []
    for row in rows:
        payload = dict(row)
        payload["metadata"] = _json_dict(row["metadata_json"])
        payload.pop("metadata_json", None)
        payloads.append(payload)
    return payloads


def _packet_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def export_audio_index_packet(
    conn: sqlite3.Connection,
    paths: ProjectPaths,
    *,
    session_id: str,
    node_id: str,
    output_path: Path | None = None,
) -> dict[str, Any]:
    package = find_entity_by_alias(
        conn,
        entity_type="package",
        external_system="comfyui_remote_foundry",
        alias_key="session_id",
        alias_value=session_id,
    )
    if package is None:
        raise ValueError(f"Unknown audio package session_id: {session_id}")
    sample = _sample_row(conn, int(package["sample_id"]))
    source_payload = _sample_source_payload(conn, int(sample["id"]))

    packet_body = {
        "packet_schema_version": PACKET_SCHEMA_VERSION,
        "packet_kind": PACKET_KIND,
        "lane": "audio",
        "origin_node_id": node_id,
        "created_at": now_iso(),
        "sample": {
            "canonical_sample_id": sample["canonical_sample_id"],
            "display_name": sample["display_name"],
            "source_game": sample["source_game"],
            "source_format": sample["source_format"],
            "item_family": sample["item_family"],
            "warehouse_status": sample["warehouse_status"],
            "metadata": _json_dict(sample["metadata_json"]),
            "aliases": _sample_alias_payloads(conn, int(sample["id"])),
        },
        "package": {
            "canonical_package_id": package["canonical_package_id"],
            "package_role": package["package_role"],
            "content_bucket": package["content_bucket"],
            "contract_type": package["contract_type"],
            "consumer_ready": bool(package["consumer_ready"]),
            "warehouse_status": package["warehouse_status"],
            "metadata": _json_dict(package["metadata_json"]),
            "aliases": _package_alias_payloads(conn, int(package["id"])),
        },
        "source": source_payload,
        "artifacts": {
            "source": _artifact_payloads(conn, owner_type="source", owner_id=int(source_payload["id"])),
            "package": _artifact_payloads(conn, owner_type="package", owner_id=int(package["id"])),
        },
    }
    payload_hash = _packet_hash(packet_body)
    packet_id = f"idxpkt_audio_{payload_hash[:12]}"
    payload = {**packet_body, "packet_id": packet_id, "payload_hash": payload_hash}

    packet_file = output_path.expanduser().resolve() if output_path else paths.audio_index_packet_path(node_id, session_id)
    write_json(packet_file, payload)
    return {
        "packet_id": packet_id,
        "payload_hash": payload_hash,
        "packet_path": str(packet_file),
        "session_id": session_id,
        "canonical_sample_id": sample["canonical_sample_id"],
        "canonical_package_id": package["canonical_package_id"],
        "node_id": node_id,
    }


def import_audio_index_packet(
    conn: sqlite3.Connection,
    paths: ProjectPaths,
    *,
    packet_path: Path,
) -> dict[str, Any]:
    packet_file = packet_path.expanduser().resolve()
    payload = json.loads(packet_file.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Invalid audio index packet: {packet_file}")
    if payload.get("packet_schema_version") != PACKET_SCHEMA_VERSION:
        raise ValueError(f"Unsupported packet schema: {payload.get('packet_schema_version')}")
    if payload.get("packet_kind") != PACKET_KIND or payload.get("lane") != "audio":
        raise ValueError(f"Unsupported packet kind/lane: {payload.get('packet_kind')} / {payload.get('lane')}")

    origin_node_id = str(payload.get("origin_node_id") or "").strip()
    packet_id = str(payload.get("packet_id") or "").strip()
    payload_hash = str(payload.get("payload_hash") or "").strip()
    if not origin_node_id or not packet_id or not payload_hash:
        raise ValueError(f"Audio index packet is missing origin identity: {packet_file}")

    sample_payload = dict(payload.get("sample") or {})
    package_payload = dict(payload.get("package") or {})
    source_payload = dict(payload.get("source") or {})
    artifact_payloads = dict(payload.get("artifacts") or {})

    remote_root = register_root(
        conn,
        "external_source",
        f"remote://{origin_node_id}/audio-index",
        f"remote-audio-index-{origin_node_id}",
        status="active",
        is_canonical_write_root=False,
        metadata={
            "adapter": "audio_index_packet_v0",
            "origin_node_id": origin_node_id,
            "lane": "audio",
        },
    )
    ensure_report_import(
        conn,
        root_id=remote_root["id"],
        report_kind="audio_index_packet_v0",
        report_path=f"packet://{origin_node_id}/{packet_id}",
        external_run_id=payload_hash,
        status="imported",
        metadata={"packet_path": str(packet_file)},
    )

    sample_metadata = dict(sample_payload.get("metadata") or {})
    sample_metadata.update(
        {
            "replicated_from_node": origin_node_id,
            "replica_packet_id": packet_id,
            "replica_packet_path": str(packet_file),
        }
    )
    sample = ensure_sample(
        conn,
        canonical_sample_id=str(sample_payload.get("canonical_sample_id") or ""),
        display_name=str(sample_payload.get("display_name") or ""),
        source_game=str(sample_payload.get("source_game") or "unknown"),
        source_format=str(sample_payload.get("source_format") or "unknown"),
        item_family=str(sample_payload.get("item_family") or "audio"),
        warehouse_status=str(sample_payload.get("warehouse_status") or "cataloged"),
        metadata=sample_metadata,
    )

    package_metadata = dict(package_payload.get("metadata") or {})
    package_metadata.update(
        {
            "replicated_from_node": origin_node_id,
            "replica_packet_id": packet_id,
            "replica_packet_path": str(packet_file),
            "availability": "remote_index_only",
        }
    )
    package = ensure_package(
        conn,
        sample_id=sample["id"],
        canonical_package_id=str(package_payload.get("canonical_package_id") or ""),
        package_role=str(package_payload.get("package_role") or "utterance"),
        content_bucket=str(package_payload.get("content_bucket") or "audio"),
        contract_type=str(package_payload.get("contract_type") or "unknown"),
        consumer_ready=bool(package_payload.get("consumer_ready")),
        warehouse_status=str(package_payload.get("warehouse_status") or "cataloged"),
        metadata=package_metadata,
    )

    for alias in sample_payload.get("aliases") or []:
        ensure_alias(
            conn,
            entity_type="sample",
            entity_id=sample["id"],
            external_system=str(alias.get("external_system") or ""),
            alias_key=str(alias.get("alias_key") or ""),
            alias_value=str(alias.get("alias_value") or ""),
        )
    for alias in package_payload.get("aliases") or []:
        ensure_alias(
            conn,
            entity_type="package",
            entity_id=package["id"],
            external_system=str(alias.get("external_system") or ""),
            alias_key=str(alias.get("alias_key") or ""),
            alias_value=str(alias.get("alias_value") or ""),
        )

    source_metadata = dict(source_payload.get("metadata") or {})
    source_metadata.update(
        {
            "replicated_from_node": origin_node_id,
            "replica_packet_id": packet_id,
            "remote_only": True,
        }
    )
    source = ensure_source(
        conn,
        root_id=remote_root["id"],
        source_kind="remote_audio_index",
        source_path=str(source_payload.get("source_path") or ""),
        managed_path=str(source_payload.get("managed_path") or ""),
        content_hash=str(source_payload.get("content_hash") or ""),
        ext=str(source_payload.get("ext") or ""),
        size_bytes=source_payload.get("size_bytes"),
        status="cataloged",
        metadata=source_metadata,
    )
    ensure_source_link(
        conn,
        source_id=source["id"],
        entity_type="sample",
        entity_id=sample["id"],
        relation_kind="audio_session",
    )

    def _import_artifact_rows(rows: list[dict[str, Any]], owner_type: str, owner_id: int) -> int:
        imported = 0
        for artifact in rows:
            artifact_metadata = dict(artifact.get("metadata") or {})
            artifact_metadata.update(
                {
                    "replicated_from_node": origin_node_id,
                    "replica_packet_id": packet_id,
                    "remote_only": True,
                }
            )
            ensure_artifact(
                conn,
                owner_type=owner_type,
                owner_id=owner_id,
                stage=str(artifact.get("stage") or "session"),
                artifact_kind=str(artifact.get("artifact_kind") or "unknown"),
                path=str(artifact.get("path") or ""),
                format=str(artifact.get("format") or ""),
                status=REMOTE_ARTIFACT_STATUS,
                metadata=artifact_metadata,
            )
            imported += 1
        return imported

    source_artifact_count = _import_artifact_rows(list(artifact_payloads.get("source") or []), "source", int(source["id"]))
    package_artifact_count = _import_artifact_rows(list(artifact_payloads.get("package") or []), "package", int(package["id"]))

    return {
        "packet_id": packet_id,
        "packet_path": str(packet_file),
        "origin_node_id": origin_node_id,
        "canonical_sample_id": sample["canonical_sample_id"],
        "canonical_package_id": package["canonical_package_id"],
        "source_artifacts": source_artifact_count,
        "package_artifacts": package_artifact_count,
        "availability": "remote_index_only",
    }
