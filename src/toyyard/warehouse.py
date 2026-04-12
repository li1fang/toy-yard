from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from toyyard.util import now_iso, slugify


UNKNOWN_VALUES = {"", "unknown", "observed", None}


def json_loads(text: str | None) -> dict[str, Any]:
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def json_merge(existing_text: str | None, incoming: dict[str, Any] | None) -> str:
    payload = json_loads(existing_text)
    if incoming:
        payload.update({key: value for key, value in incoming.items() if value not in (None, "")})
    return json.dumps(payload, ensure_ascii=True, sort_keys=True)


def make_canonical_id(prefix: str, *parts: object, display_name: str | None = None) -> str:
    normalized_parts = [str(part).strip() for part in parts if str(part).strip()]
    digest = hashlib.sha1("|".join(normalized_parts).encode("utf-8")).hexdigest()[:10]
    label = slugify(display_name or normalized_parts[0] if normalized_parts else prefix)
    return f"{prefix}_{label[:48]}_{digest}"


def _row_by_id(conn: sqlite3.Connection, table_name: str, row_id: int) -> sqlite3.Row:
    row = conn.execute(f'SELECT * FROM "{table_name}" WHERE id = ?', (row_id,)).fetchone()
    if row is None:
        raise ValueError(f"Unknown {table_name} id: {row_id}")
    return row


def _prefer_value(current: Any, incoming: Any) -> Any:
    if incoming in UNKNOWN_VALUES:
        return current
    if current in UNKNOWN_VALUES:
        return incoming
    return current


def register_root(
    conn: sqlite3.Connection,
    root_kind: str,
    path: str | Path,
    display_name: str,
    *,
    status: str = "active",
    is_canonical_write_root: bool = False,
    metadata: dict[str, Any] | None = None,
) -> sqlite3.Row:
    normalized_path = str(Path(path).expanduser().resolve())
    existing = conn.execute("SELECT * FROM roots WHERE path = ?", (normalized_path,)).fetchone()
    if existing is None:
        cursor = conn.execute(
            """
            INSERT INTO roots (
              root_kind, display_name, path, status, is_canonical_write_root, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                root_kind,
                display_name,
                normalized_path,
                status,
                int(is_canonical_write_root),
                json.dumps(metadata or {}, ensure_ascii=True, sort_keys=True),
            ),
        )
        conn.commit()
        return _row_by_id(conn, "roots", cursor.lastrowid)

    merged_metadata = json_merge(existing["metadata_json"], metadata)
    conn.execute(
        """
        UPDATE roots
        SET root_kind = ?, display_name = ?, status = ?, is_canonical_write_root = ?, metadata_json = ?
        WHERE id = ?
        """,
        (
            root_kind,
            _prefer_value(existing["display_name"], display_name),
            _prefer_value(existing["status"], status),
            int(is_canonical_write_root or bool(existing["is_canonical_write_root"])),
            merged_metadata,
            existing["id"],
        ),
    )
    conn.commit()
    return _row_by_id(conn, "roots", existing["id"])


def seed_canonical_root(conn: sqlite3.Connection, root_path: Path) -> sqlite3.Row:
    return register_root(
        conn,
        "canonical_warehouse",
        root_path,
        "toy-yard",
        is_canonical_write_root=True,
        metadata={"managed_root": str(root_path)},
    )


def get_root(conn: sqlite3.Connection, root_id: int) -> sqlite3.Row:
    return _row_by_id(conn, "roots", root_id)


def get_source(conn: sqlite3.Connection, source_id: int) -> sqlite3.Row:
    return _row_by_id(conn, "sources", source_id)


def list_roots(conn: sqlite3.Connection, *, root_kind: str | None = None) -> list[sqlite3.Row]:
    if root_kind:
        return conn.execute(
            "SELECT * FROM roots WHERE root_kind = ? ORDER BY is_canonical_write_root DESC, id",
            (root_kind,),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM roots ORDER BY is_canonical_write_root DESC, id"
    ).fetchall()


def canonical_root(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
        FROM roots
        WHERE is_canonical_write_root = 1
        ORDER BY id
        LIMIT 1
        """
    ).fetchone()


def ensure_source(
    conn: sqlite3.Connection,
    *,
    root_id: int,
    source_kind: str,
    source_path: str,
    managed_path: str | None = None,
    content_hash: str | None = None,
    ext: str = "",
    size_bytes: int | None = None,
    status: str = "observed",
    metadata: dict[str, Any] | None = None,
) -> sqlite3.Row:
    existing = conn.execute(
        "SELECT * FROM sources WHERE root_id = ? AND source_path = ?",
        (root_id, source_path),
    ).fetchone()
    if existing is None:
        cursor = conn.execute(
            """
            INSERT INTO sources (
              root_id, source_kind, source_path, managed_path, content_hash, ext,
              size_bytes, status, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                root_id,
                source_kind,
                source_path,
                managed_path,
                content_hash,
                ext,
                size_bytes,
                status,
                json.dumps(metadata or {}, ensure_ascii=True, sort_keys=True),
            ),
        )
        conn.commit()
        return _row_by_id(conn, "sources", cursor.lastrowid)

    conn.execute(
        """
        UPDATE sources
        SET source_kind = ?, managed_path = ?, content_hash = ?, ext = ?, size_bytes = ?, status = ?, metadata_json = ?
        WHERE id = ?
        """,
        (
            _prefer_value(existing["source_kind"], source_kind),
            _prefer_value(existing["managed_path"], managed_path),
            _prefer_value(existing["content_hash"], content_hash),
            _prefer_value(existing["ext"], ext),
            existing["size_bytes"] if size_bytes is None else size_bytes,
            _prefer_value(existing["status"], status),
            json_merge(existing["metadata_json"], metadata),
            existing["id"],
        ),
    )
    conn.commit()
    return _row_by_id(conn, "sources", existing["id"])


def ensure_alias(
    conn: sqlite3.Connection,
    *,
    entity_type: str,
    entity_id: int,
    external_system: str,
    alias_key: str,
    alias_value: str,
) -> sqlite3.Row:
    existing = conn.execute(
        """
        SELECT * FROM aliases
        WHERE entity_type = ? AND external_system = ? AND alias_key = ? AND alias_value = ?
        """,
        (entity_type, external_system, alias_key, alias_value),
    ).fetchone()
    if existing is not None:
        if existing["entity_id"] != entity_id:
            raise ValueError(
                f"Alias collision for {external_system}:{alias_key}={alias_value}: "
                f"{existing['entity_type']}#{existing['entity_id']} already owns it"
            )
        return existing

    cursor = conn.execute(
        """
        INSERT INTO aliases (entity_type, entity_id, external_system, alias_key, alias_value)
        VALUES (?, ?, ?, ?, ?)
        """,
        (entity_type, entity_id, external_system, alias_key, alias_value),
    )
    conn.commit()
    return _row_by_id(conn, "aliases", cursor.lastrowid)


def find_entity_by_alias(
    conn: sqlite3.Connection,
    *,
    entity_type: str,
    external_system: str,
    alias_value: str,
    alias_key: str | None = None,
) -> sqlite3.Row | None:
    sql = """
        SELECT aliases.*
        FROM aliases
        WHERE entity_type = ? AND external_system = ? AND alias_value = ?
    """
    params: list[Any] = [entity_type, external_system, alias_value]
    if alias_key:
        sql += " AND alias_key = ?"
        params.append(alias_key)
    alias = conn.execute(sql, params).fetchone()
    if alias is None:
        return None
    table_name = "samples" if entity_type == "sample" else "packages"
    return _row_by_id(conn, table_name, alias["entity_id"])


def ensure_sample(
    conn: sqlite3.Connection,
    *,
    canonical_sample_id: str,
    display_name: str,
    source_game: str = "unknown",
    source_format: str = "unknown",
    item_family: str = "unknown",
    warehouse_status: str = "observed",
    metadata: dict[str, Any] | None = None,
) -> sqlite3.Row:
    existing = conn.execute(
        "SELECT * FROM samples WHERE canonical_sample_id = ?",
        (canonical_sample_id,),
    ).fetchone()
    if existing is None:
        cursor = conn.execute(
            """
            INSERT INTO samples (
              canonical_sample_id, display_name, source_game, source_format, item_family,
              warehouse_status, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                canonical_sample_id,
                display_name,
                source_game,
                source_format,
                item_family,
                warehouse_status,
                json.dumps(metadata or {}, ensure_ascii=True, sort_keys=True),
            ),
        )
        conn.commit()
        return _row_by_id(conn, "samples", cursor.lastrowid)

    conn.execute(
        """
        UPDATE samples
        SET display_name = ?, source_game = ?, source_format = ?, item_family = ?, warehouse_status = ?, metadata_json = ?
        WHERE id = ?
        """,
        (
            _prefer_value(existing["display_name"], display_name),
            _prefer_value(existing["source_game"], source_game),
            _prefer_value(existing["source_format"], source_format),
            _prefer_value(existing["item_family"], item_family),
            warehouse_status if existing["warehouse_status"] in UNKNOWN_VALUES else existing["warehouse_status"],
            json_merge(existing["metadata_json"], metadata),
            existing["id"],
        ),
    )
    conn.commit()
    return _row_by_id(conn, "samples", existing["id"])


def ensure_package(
    conn: sqlite3.Connection,
    *,
    sample_id: int,
    canonical_package_id: str,
    package_role: str = "unknown",
    content_bucket: str = "unknown",
    contract_type: str = "unknown",
    consumer_ready: bool = False,
    warehouse_status: str = "observed",
    metadata: dict[str, Any] | None = None,
) -> sqlite3.Row:
    existing = conn.execute(
        "SELECT * FROM packages WHERE canonical_package_id = ?",
        (canonical_package_id,),
    ).fetchone()
    if existing is None:
        cursor = conn.execute(
            """
            INSERT INTO packages (
              sample_id, canonical_package_id, package_role, content_bucket, contract_type,
              consumer_ready, warehouse_status, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sample_id,
                canonical_package_id,
                package_role,
                content_bucket,
                contract_type,
                int(consumer_ready),
                warehouse_status,
                json.dumps(metadata or {}, ensure_ascii=True, sort_keys=True),
            ),
        )
        conn.commit()
        return _row_by_id(conn, "packages", cursor.lastrowid)

    conn.execute(
        """
        UPDATE packages
        SET sample_id = ?, package_role = ?, content_bucket = ?, contract_type = ?,
            consumer_ready = ?, warehouse_status = ?, metadata_json = ?
        WHERE id = ?
        """,
        (
            existing["sample_id"] or sample_id,
            _prefer_value(existing["package_role"], package_role),
            _prefer_value(existing["content_bucket"], content_bucket),
            _prefer_value(existing["contract_type"], contract_type),
            int(bool(consumer_ready or existing["consumer_ready"])),
            warehouse_status if existing["warehouse_status"] in UNKNOWN_VALUES else existing["warehouse_status"],
            json_merge(existing["metadata_json"], metadata),
            existing["id"],
        ),
    )
    conn.commit()
    return _row_by_id(conn, "packages", existing["id"])


def ensure_source_link(
    conn: sqlite3.Connection,
    *,
    source_id: int,
    entity_type: str,
    entity_id: int,
    relation_kind: str = "primary",
) -> sqlite3.Row:
    existing = conn.execute(
        """
        SELECT * FROM source_links
        WHERE source_id = ? AND entity_type = ? AND entity_id = ? AND relation_kind = ?
        """,
        (source_id, entity_type, entity_id, relation_kind),
    ).fetchone()
    if existing is not None:
        return existing

    cursor = conn.execute(
        """
        INSERT INTO source_links (source_id, entity_type, entity_id, relation_kind)
        VALUES (?, ?, ?, ?)
        """,
        (source_id, entity_type, entity_id, relation_kind),
    )
    conn.commit()
    return _row_by_id(conn, "source_links", cursor.lastrowid)


def ensure_artifact(
    conn: sqlite3.Connection,
    *,
    owner_type: str,
    owner_id: int,
    stage: str,
    artifact_kind: str,
    path: str,
    format: str = "",
    status: str = "observed",
    metadata: dict[str, Any] | None = None,
) -> sqlite3.Row:
    existing = conn.execute(
        """
        SELECT * FROM artifacts
        WHERE owner_type = ? AND owner_id = ? AND stage = ? AND artifact_kind = ? AND path = ?
        """,
        (owner_type, owner_id, stage, artifact_kind, path),
    ).fetchone()
    if existing is None:
        cursor = conn.execute(
            """
            INSERT INTO artifacts (
              owner_type, owner_id, stage, artifact_kind, path, format, status, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                owner_type,
                owner_id,
                stage,
                artifact_kind,
                path,
                format,
                status,
                json.dumps(metadata or {}, ensure_ascii=True, sort_keys=True),
                now_iso(),
            ),
        )
        conn.commit()
        return _row_by_id(conn, "artifacts", cursor.lastrowid)

    conn.execute(
        """
        UPDATE artifacts
        SET format = ?, status = ?, metadata_json = ?
        WHERE id = ?
        """,
        (
            _prefer_value(existing["format"], format),
            _prefer_value(existing["status"], status),
            json_merge(existing["metadata_json"], metadata),
            existing["id"],
        ),
    )
    conn.commit()
    return _row_by_id(conn, "artifacts", existing["id"])


def ensure_report_import(
    conn: sqlite3.Connection,
    *,
    root_id: int,
    report_kind: str,
    report_path: str,
    external_run_id: str = "",
    status: str = "imported",
    metadata: dict[str, Any] | None = None,
) -> sqlite3.Row:
    existing = conn.execute(
        "SELECT * FROM report_imports WHERE root_id = ? AND report_path = ?",
        (root_id, report_path),
    ).fetchone()
    if existing is None:
        cursor = conn.execute(
            """
            INSERT INTO report_imports (
              root_id, report_kind, report_path, external_run_id, imported_at, status, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                root_id,
                report_kind,
                report_path,
                external_run_id,
                now_iso(),
                status,
                json.dumps(metadata or {}, ensure_ascii=True, sort_keys=True),
            ),
        )
        conn.commit()
        return _row_by_id(conn, "report_imports", cursor.lastrowid)

    conn.execute(
        """
        UPDATE report_imports
        SET report_kind = ?, external_run_id = ?, status = ?, metadata_json = ?
        WHERE id = ?
        """,
        (
            _prefer_value(existing["report_kind"], report_kind),
            _prefer_value(existing["external_run_id"], external_run_id),
            _prefer_value(existing["status"], status),
            json_merge(existing["metadata_json"], metadata),
            existing["id"],
        ),
    )
    conn.commit()
    return _row_by_id(conn, "report_imports", existing["id"])


def ensure_run(
    conn: sqlite3.Connection,
    *,
    entity_type: str,
    entity_id: int,
    stage: str,
    tool_name: str,
    status: str,
    started_at: str | None = None,
    ended_at: str | None = None,
    log_path: str = "",
    summary: str = "",
) -> sqlite3.Row:
    started_value = started_at or now_iso()
    existing = conn.execute(
        """
        SELECT *
        FROM runs
        WHERE entity_type = ? AND entity_id = ? AND stage = ? AND tool_name = ? AND log_path = ?
        """,
        (entity_type, entity_id, stage, tool_name, log_path),
    ).fetchone()
    if existing is None:
        cursor = conn.execute(
            """
            INSERT INTO runs (
              entity_type, entity_id, stage, tool_name, status, started_at, ended_at, log_path, summary
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (entity_type, entity_id, stage, tool_name, status, started_value, ended_at, log_path, summary),
        )
        conn.commit()
        return _row_by_id(conn, "runs", cursor.lastrowid)

    conn.execute(
        """
        UPDATE runs
        SET status = ?, started_at = ?, ended_at = ?, summary = ?, log_path = ?
        WHERE id = ?
        """,
        (
            status,
            existing["started_at"] or started_value,
            ended_at or existing["ended_at"],
            summary or existing["summary"],
            log_path or existing["log_path"],
            existing["id"],
        ),
    )
    conn.commit()
    return _row_by_id(conn, "runs", existing["id"])


def resolve_sample(conn: sqlite3.Connection, sample_ref: str) -> sqlite3.Row | None:
    row = conn.execute("SELECT * FROM samples WHERE canonical_sample_id = ?", (sample_ref,)).fetchone()
    if row is not None:
        return row
    return find_entity_by_alias(conn, entity_type="sample", external_system="3dgirls", alias_value=sample_ref)


def sample_packages(conn: sqlite3.Connection, sample_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM packages WHERE sample_id = ? ORDER BY canonical_package_id",
        (sample_id,),
    ).fetchall()


def sample_aliases(conn: sqlite3.Connection, sample_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM aliases WHERE entity_type = 'sample' AND entity_id = ? ORDER BY external_system, alias_key",
        (sample_id,),
    ).fetchall()


def package_aliases(conn: sqlite3.Connection, package_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM aliases WHERE entity_type = 'package' AND entity_id = ? ORDER BY external_system, alias_key",
        (package_id,),
    ).fetchall()


def sample_sources(conn: sqlite3.Connection, sample_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT sources.*
        FROM sources
        JOIN source_links ON source_links.source_id = sources.id
        WHERE source_links.entity_type = 'sample' AND source_links.entity_id = ?
        ORDER BY sources.id
        """,
        (sample_id,),
    ).fetchall()


def sample_artifacts(conn: sqlite3.Connection, sample_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT *
        FROM artifacts
        WHERE owner_type = 'sample' AND owner_id = ?
        ORDER BY created_at, id
        """,
        (sample_id,),
    ).fetchall()


def source_artifacts(conn: sqlite3.Connection, source_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT *
        FROM artifacts
        WHERE owner_type = 'source' AND owner_id = ?
        ORDER BY created_at, id
        """,
        (source_id,),
    ).fetchall()


def source_linked_samples(conn: sqlite3.Connection, source_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT samples.*
        FROM samples
        JOIN source_links
          ON source_links.entity_type = 'sample' AND source_links.entity_id = samples.id
        WHERE source_links.source_id = ?
        ORDER BY samples.canonical_sample_id
        """,
        (source_id,),
    ).fetchall()
