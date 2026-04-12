from __future__ import annotations

import json
import sqlite3


def ready_rows(conn: sqlite3.Connection, target: str) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT v1_assets.id AS asset_id, v1_assets.display_name, v1_assets.asset_kind, v1_assets.readiness,
               v1_packages.filename, v1_packages.game_hint
        FROM v1_assets
        JOIN v1_packages ON v1_packages.id = v1_assets.package_id
        WHERE v1_assets.primary_target = ? AND v1_assets.readiness IN ('candidate', 'ue_ready')
        ORDER BY v1_assets.id
        """,
        (target,),
    ).fetchall()
    return [dict(row) for row in rows]


def blocked_rows(conn: sqlite3.Connection) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT
          v1_assets.id AS asset_id,
          v1_assets.display_name,
          v1_assets.asset_kind,
          v1_assets.readiness,
          IFNULL(GROUP_CONCAT(DISTINCT v1_dependencies.dep_name), '') AS dependencies,
          IFNULL(GROUP_CONCAT(DISTINCT failures.failure_class), '') AS failure_classes
        FROM v1_assets
        LEFT JOIN v1_dependencies
          ON v1_dependencies.subject_type = 'asset' AND v1_dependencies.subject_id = v1_assets.id
        LEFT JOIN failures
          ON failures.entity_type = 'asset' AND failures.entity_id = v1_assets.id
        WHERE v1_assets.readiness = 'blocked'
        GROUP BY v1_assets.id, v1_assets.display_name, v1_assets.asset_kind, v1_assets.readiness
        ORDER BY v1_assets.id
        """
    ).fetchall()
    return [dict(row) for row in rows]


def failure_rows(conn: sqlite3.Connection, stage: str) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT id, entity_type, entity_id, stage, failure_class, failure_code, retryable, message
        FROM failures
        WHERE stage = ?
        ORDER BY id
        """,
        (stage,),
    ).fetchall()
    return [dict(row) for row in rows]


def aiue_ready_rows(conn: sqlite3.Connection) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT
          packages.canonical_package_id,
          samples.canonical_sample_id,
          samples.display_name,
          packages.package_role,
          packages.content_bucket,
          packages.contract_type,
          packages.consumer_ready,
          packages.warehouse_status,
          EXISTS (
            SELECT 1
            FROM artifacts
            WHERE artifacts.owner_type = 'sample'
              AND artifacts.owner_id = samples.id
              AND artifacts.stage = 'conversion'
              AND artifacts.artifact_kind = 'conversion_manifest'
          ) AS has_conversion_manifest
        FROM packages
        JOIN samples ON samples.id = packages.sample_id
        WHERE packages.consumer_ready = 1
          AND (
            packages.warehouse_status IN ('ready_for_pipeline', 'published', 'classified', 'cataloged')
          )
          AND EXISTS (
            SELECT 1
            FROM artifacts
            WHERE artifacts.owner_type = 'sample'
              AND artifacts.owner_id = samples.id
              AND artifacts.stage = 'conversion'
              AND artifacts.artifact_kind = 'conversion_manifest'
          )
        ORDER BY samples.canonical_sample_id, packages.canonical_package_id
        """
    ).fetchall()
    return [dict(row) for row in rows]


def lineage_gap_rows(conn: sqlite3.Connection) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT *
        FROM (
          SELECT
            packages.canonical_package_id,
            samples.canonical_sample_id,
            packages.package_role,
            CASE
              WHEN NOT EXISTS (
                SELECT 1
                FROM source_links
                WHERE source_links.entity_type = 'sample' AND source_links.entity_id = samples.id
              ) THEN 'missing_sample_source'
              WHEN NOT EXISTS (
                SELECT 1
                FROM aliases
                WHERE aliases.entity_type = 'package' AND aliases.entity_id = packages.id
              ) THEN 'missing_package_alias'
              WHEN NOT EXISTS (
                SELECT 1
                FROM artifacts
                WHERE artifacts.owner_type = 'sample'
                  AND artifacts.owner_id = samples.id
                  AND artifacts.stage = 'conversion'
                  AND artifacts.artifact_kind = 'conversion_manifest'
              ) THEN 'missing_conversion_manifest'
              ELSE 'ok'
            END AS gap
          FROM packages
          JOIN samples ON samples.id = packages.sample_id
          WHERE packages.warehouse_status NOT IN ('blocked', 'rejected')
        )
        WHERE gap != 'ok'
        ORDER BY canonical_sample_id, canonical_package_id
        """
    ).fetchall()
    return [dict(row) for row in rows]


def motion_catalog_rows(conn: sqlite3.Connection) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT
          samples.canonical_sample_id,
          packages.canonical_package_id,
          packages.consumer_ready,
          packages.warehouse_status,
          packages.metadata_json
        FROM packages
        JOIN samples ON samples.id = packages.sample_id
        WHERE samples.item_family = 'motion'
           OR packages.content_bucket = 'motion'
        ORDER BY samples.canonical_sample_id, packages.canonical_package_id
        """
    ).fetchall()

    catalog: list[dict[str, object]] = []
    for row in rows:
        metadata = json.loads(row["metadata_json"] or "{}")
        catalog.append(
            {
                "canonical_sample_id": row["canonical_sample_id"],
                "canonical_package_id": row["canonical_package_id"],
                "scenario_id": metadata.get("scenario_id", ""),
                "clip_id": metadata.get("clip_id", ""),
                "pack_version": metadata.get("pack_version", ""),
                "runtime_semantics": metadata.get("runtime_semantics", ""),
                "validation_status": metadata.get("validation_status", ""),
                "consumer_ready": row["consumer_ready"],
                "warehouse_status": row["warehouse_status"],
            }
        )
    return catalog
