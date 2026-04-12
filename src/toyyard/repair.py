from __future__ import annotations

import json
import sqlite3
from collections import Counter
from typing import Any

from toyyard.warehouse import ensure_alias, json_loads
from toyyard.util import now_iso


def _package_alias_count(conn: sqlite3.Connection, package_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM aliases WHERE entity_type = 'package' AND entity_id = ?",
        (package_id,),
    ).fetchone()[0]


def _package_artifact_metadata(conn: sqlite3.Connection, package_id: int) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT metadata_json
        FROM artifacts
        WHERE owner_type = 'package' AND owner_id = ?
        ORDER BY id DESC
        """,
        (package_id,),
    ).fetchall()
    merged: dict[str, Any] = {}
    for row in rows:
        merged.update(json_loads(row["metadata_json"]))
    return merged


def _ensure_optional_alias(conn: sqlite3.Connection, package_id: int, alias_key: str, alias_value: str | None) -> bool:
    if not alias_value:
        return False
    try:
        ensure_alias(
            conn,
            entity_type="package",
            entity_id=package_id,
            external_system="3dgirls",
            alias_key=alias_key,
            alias_value=str(alias_value),
        )
    except ValueError:
        return False
    return True


def _package_quality(package: sqlite3.Row, alias_count: int) -> tuple[int, int, int]:
    return (
        int(alias_count > 0),
        int(bool(package["consumer_ready"])),
        int(str(package["package_role"] or "unknown") != "unknown"),
    )


def _has_package_id_alias(conn: sqlite3.Connection, package_id: int) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM aliases
        WHERE entity_type = 'package' AND entity_id = ? AND alias_key = 'package_id'
        """,
        (package_id,),
    ).fetchone()
    return row is not None


def _update_package_metadata(conn: sqlite3.Connection, package_id: int, metadata: dict[str, Any]) -> None:
    package = conn.execute("SELECT metadata_json FROM packages WHERE id = ?", (package_id,)).fetchone()
    existing = json_loads(package["metadata_json"])
    existing.update({key: value for key, value in metadata.items() if value not in (None, "")})
    conn.execute(
        "UPDATE packages SET metadata_json = ? WHERE id = ?",
        (json.dumps(existing, ensure_ascii=True, sort_keys=True), package_id),
    )


def _record_failure_if_missing(conn: sqlite3.Connection, package_id: int, message: str) -> bool:
    existing = conn.execute(
        """
        SELECT 1
        FROM failures
        WHERE entity_type = 'package' AND entity_id = ? AND stage = 'conversion' AND failure_code = 'missing_conversion_manifest'
        """,
        (package_id,),
    ).fetchone()
    if existing:
        return False
    conn.execute(
        """
        INSERT INTO failures (
          entity_type, entity_id, stage, failure_class, failure_code, message,
          evidence_path, retryable, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "package",
            package_id,
            "conversion",
            "missing_artifact",
            "missing_conversion_manifest",
            message,
            "",
            1,
            now_iso(),
        ),
    )
    return True


def _delete_package(conn: sqlite3.Connection, package_id: int) -> None:
    conn.execute("DELETE FROM aliases WHERE entity_type = 'package' AND entity_id = ?", (package_id,))
    conn.execute("DELETE FROM source_links WHERE entity_type = 'package' AND entity_id = ?", (package_id,))
    conn.execute("DELETE FROM artifacts WHERE owner_type = 'package' AND owner_id = ?", (package_id,))
    conn.execute("DELETE FROM runs WHERE entity_type = 'package' AND entity_id = ?", (package_id,))
    conn.execute("DELETE FROM failures WHERE entity_type = 'package' AND entity_id = ?", (package_id,))
    conn.execute("DELETE FROM tags WHERE entity_type = 'package' AND entity_id = ?", (package_id,))
    conn.execute("DELETE FROM packages WHERE id = ?", (package_id,))


def _move_package_links(conn: sqlite3.Connection, from_package_id: int, to_package_id: int) -> None:
    rows = conn.execute(
        """
        SELECT *
        FROM source_links
        WHERE entity_type = 'package' AND entity_id = ?
        """,
        (from_package_id,),
    ).fetchall()
    for row in rows:
        exists = conn.execute(
            """
            SELECT 1
            FROM source_links
            WHERE source_id = ? AND entity_type = 'package' AND entity_id = ? AND relation_kind = ?
            """,
            (row["source_id"], to_package_id, row["relation_kind"]),
        ).fetchone()
        if exists:
            conn.execute("DELETE FROM source_links WHERE id = ?", (row["id"],))
        else:
            conn.execute("UPDATE source_links SET entity_id = ? WHERE id = ?", (to_package_id, row["id"]))


def repair_legacy_3dgirls_lineage(conn: sqlite3.Connection) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    packages = conn.execute("SELECT * FROM packages ORDER BY sample_id, id").fetchall()

    for package in packages:
        metadata = json_loads(package["metadata_json"])
        artifact_metadata = _package_artifact_metadata(conn, package["id"])
        derived = {
            "manifest_path": metadata.get("manifest_path") or artifact_metadata.get("manifest_path"),
            "source_relative_path": metadata.get("source_relative_path") or artifact_metadata.get("source_relative_path"),
            "legacy_identity_basis": metadata.get("legacy_identity_basis"),
            "created_from": metadata.get("created_from") or artifact_metadata.get("created_from"),
        }
        if not derived["legacy_identity_basis"]:
            if artifact_metadata.get("package_id"):
                derived["legacy_identity_basis"] = "package_id"
            elif derived["source_relative_path"]:
                derived["legacy_identity_basis"] = "source_relative_path"
            elif derived["manifest_path"]:
                derived["legacy_identity_basis"] = "manifest_path"
            else:
                derived["legacy_identity_basis"] = "sample_scoped_primary"
        _update_package_metadata(conn, package["id"], derived)

        alias_count = _package_alias_count(conn, package["id"])
        if alias_count == 0:
            if _ensure_optional_alias(conn, package["id"], "source_relative_path", derived["source_relative_path"]):
                counts["aliases_added"] += 1
            if _ensure_optional_alias(conn, package["id"], "manifest_path", derived["manifest_path"]):
                counts["aliases_added"] += 1

    sample_rows = conn.execute("SELECT * FROM samples ORDER BY id").fetchall()
    for sample in sample_rows:
        sample_packages = conn.execute(
            "SELECT * FROM packages WHERE sample_id = ? ORDER BY id",
            (sample["id"],),
        ).fetchall()
        if len(sample_packages) < 2:
            continue
        identity_groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
        for package in sample_packages:
            metadata = json_loads(package["metadata_json"])
            identity = (
                str(metadata.get("source_relative_path") or ""),
                str(metadata.get("manifest_path") or ""),
            )
            if identity == ("", ""):
                continue
            identity_groups.setdefault(identity, []).append(package)
        for grouped_packages in identity_groups.values():
            if len(grouped_packages) < 2:
                continue
            package_id_primary = [package for package in grouped_packages if _has_package_id_alias(conn, package["id"])]
            if len(package_id_primary) != 1:
                continue
            primary = package_id_primary[0]
            for candidate in grouped_packages:
                if candidate["id"] == primary["id"]:
                    continue
                candidate_meta = json_loads(candidate["metadata_json"])
                if (
                    not candidate["consumer_ready"]
                    and str(candidate["package_role"] or "unknown") == "unknown"
                    and candidate_meta.get("legacy_identity_basis") in {"source_relative_path", "manifest_path", "sample_scoped_primary"}
                ):
                    conn.execute(
                        "UPDATE artifacts SET owner_id = ? WHERE owner_type = 'package' AND owner_id = ?",
                        (primary["id"], candidate["id"]),
                    )
                    _move_package_links(conn, candidate["id"], primary["id"])
                    _delete_package(conn, candidate["id"])
                    counts["packages_pruned"] += 1

        sample_packages = conn.execute(
            "SELECT * FROM packages WHERE sample_id = ? ORDER BY id",
            (sample["id"],),
        ).fetchall()
        alias_counts = {package["id"]: _package_alias_count(conn, package["id"]) for package in sample_packages}
        strong_packages = [
            package for package in sample_packages if _package_quality(package, alias_counts[package["id"]]) > (0, 0, 0)
        ]
        weak_packages = []
        for package in sample_packages:
            metadata = json_loads(package["metadata_json"])
            if (
                alias_counts[package["id"]] == 0
                and not package["consumer_ready"]
                and str(package["package_role"] or "unknown") == "unknown"
                and metadata.get("created_from") in {"preflight_report_fallback", "ue_suite_summary"}
            ):
                weak_packages.append(package)
        if len(strong_packages) != 1 or not weak_packages:
            continue
        primary = strong_packages[0]
        for weak in weak_packages:
            conn.execute(
                "UPDATE artifacts SET owner_id = ? WHERE owner_type = 'package' AND owner_id = ?",
                (primary["id"], weak["id"]),
            )
            _move_package_links(conn, weak["id"], primary["id"])
            _delete_package(conn, weak["id"])
            counts["packages_pruned"] += 1

    packages = conn.execute(
        """
        SELECT p.*, s.display_name
        FROM packages p
        JOIN samples s ON s.id = p.sample_id
        ORDER BY p.id
        """
    ).fetchall()
    for package in packages:
        has_conversion_manifest = conn.execute(
            """
            SELECT 1
            FROM artifacts
            WHERE owner_type = 'sample' AND owner_id = ?
              AND stage = 'conversion' AND artifact_kind = 'conversion_manifest'
            LIMIT 1
            """,
            (package["sample_id"],),
        ).fetchone()
        if has_conversion_manifest:
            continue
        metadata = json_loads(package["metadata_json"])
        if package["consumer_ready"] or package["warehouse_status"] in {"ready_for_pipeline", "cataloged", "classified"}:
            conn.execute(
                """
                UPDATE packages
                SET consumer_ready = 0, warehouse_status = 'blocked'
                WHERE id = ?
                """,
                (package["id"],),
            )
            _update_package_metadata(
                conn,
                package["id"],
                {
                    "blocked_reason": "missing_conversion_manifest",
                    "blocked_at_repair": True,
                },
            )
            if _record_failure_if_missing(
                conn,
                package["id"],
                f"Sample is missing conversion_manifest artifact: {package['display_name']}",
            ):
                counts["packages_blocked"] += 1

    conn.commit()
    return dict(counts)
