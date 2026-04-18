from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable


V1_TABLE_RENAMES = {
    "packages": "v1_packages",
    "package_observations": "v1_package_observations",
    "package_files": "v1_package_files",
    "assets": "v1_assets",
    "artifacts": "v1_artifacts",
    "dependencies": "v1_dependencies",
    "compatibility": "v1_compatibility",
}

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS v1_packages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sha256 TEXT NOT NULL UNIQUE,
  source_kind TEXT NOT NULL,
  source_path TEXT NOT NULL,
  managed_path TEXT NOT NULL,
  filename TEXT NOT NULL,
  ext TEXT NOT NULL,
  size_bytes INTEGER,
  game_hint TEXT NOT NULL DEFAULT 'unknown',
  ingest_status TEXT NOT NULL DEFAULT 'imported',
  discovered_at TEXT NOT NULL,
  imported_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS v1_package_observations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  package_id INTEGER NOT NULL REFERENCES v1_packages(id) ON DELETE CASCADE,
  source_kind TEXT NOT NULL,
  source_path TEXT NOT NULL,
  discovered_at TEXT NOT NULL,
  imported_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS v1_package_files (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  package_id INTEGER NOT NULL REFERENCES v1_packages(id) ON DELETE CASCADE,
  path_in_package TEXT NOT NULL,
  ext TEXT NOT NULL,
  size_bytes INTEGER,
  role_hint TEXT NOT NULL DEFAULT 'unknown'
);

CREATE TABLE IF NOT EXISTS v1_assets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  package_id INTEGER NOT NULL REFERENCES v1_packages(id) ON DELETE CASCADE,
  asset_kind TEXT NOT NULL,
  display_name TEXT NOT NULL,
  canonical_name TEXT NOT NULL,
  completeness TEXT NOT NULL,
  readiness TEXT NOT NULL,
  primary_target TEXT NOT NULL,
  status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS v1_artifacts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  asset_id INTEGER NOT NULL REFERENCES v1_assets(id) ON DELETE CASCADE,
  artifact_kind TEXT NOT NULL,
  stage TEXT NOT NULL,
  path TEXT NOT NULL,
  format TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS v1_dependencies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  subject_type TEXT NOT NULL,
  subject_id INTEGER NOT NULL,
  dep_name TEXT NOT NULL,
  dep_kind TEXT NOT NULL,
  requirement_level TEXT NOT NULL,
  status TEXT NOT NULL,
  evidence TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS v1_compatibility (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  asset_id INTEGER NOT NULL REFERENCES v1_assets(id) ON DELETE CASCADE,
  target_kind TEXT NOT NULL,
  target_name TEXT NOT NULL,
  compat_status TEXT NOT NULL,
  confidence REAL NOT NULL,
  notes TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS roots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  root_kind TEXT NOT NULL,
  display_name TEXT NOT NULL,
  path TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'active',
  is_canonical_write_root INTEGER NOT NULL DEFAULT 0,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  root_id INTEGER NOT NULL REFERENCES roots(id) ON DELETE CASCADE,
  source_kind TEXT NOT NULL,
  source_path TEXT NOT NULL,
  managed_path TEXT,
  content_hash TEXT,
  ext TEXT NOT NULL DEFAULT '',
  size_bytes INTEGER,
  status TEXT NOT NULL DEFAULT 'observed',
  metadata_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE(root_id, source_path)
);

CREATE TABLE IF NOT EXISTS samples (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  canonical_sample_id TEXT NOT NULL UNIQUE,
  display_name TEXT NOT NULL,
  source_game TEXT NOT NULL DEFAULT 'unknown',
  source_format TEXT NOT NULL DEFAULT 'unknown',
  item_family TEXT NOT NULL DEFAULT 'unknown',
  warehouse_status TEXT NOT NULL DEFAULT 'observed',
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS packages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sample_id INTEGER NOT NULL REFERENCES samples(id) ON DELETE CASCADE,
  canonical_package_id TEXT NOT NULL UNIQUE,
  package_role TEXT NOT NULL DEFAULT 'unknown',
  content_bucket TEXT NOT NULL DEFAULT 'unknown',
  contract_type TEXT NOT NULL DEFAULT 'unknown',
  consumer_ready INTEGER NOT NULL DEFAULT 0,
  warehouse_status TEXT NOT NULL DEFAULT 'observed',
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS artifacts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  owner_type TEXT NOT NULL,
  owner_id INTEGER NOT NULL,
  stage TEXT NOT NULL,
  artifact_kind TEXT NOT NULL,
  path TEXT NOT NULL,
  format TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'observed',
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  UNIQUE(owner_type, owner_id, stage, artifact_kind, path)
);

CREATE TABLE IF NOT EXISTS aliases (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_type TEXT NOT NULL,
  entity_id INTEGER NOT NULL,
  external_system TEXT NOT NULL,
  alias_key TEXT NOT NULL,
  alias_value TEXT NOT NULL,
  UNIQUE(entity_type, external_system, alias_key, alias_value)
);

CREATE TABLE IF NOT EXISTS report_imports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  root_id INTEGER NOT NULL REFERENCES roots(id) ON DELETE CASCADE,
  report_kind TEXT NOT NULL,
  report_path TEXT NOT NULL,
  external_run_id TEXT NOT NULL DEFAULT '',
  imported_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'imported',
  metadata_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE(root_id, report_path)
);

CREATE TABLE IF NOT EXISTS operator_nodes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  node_id TEXT NOT NULL UNIQUE,
  profile_name TEXT NOT NULL UNIQUE,
  ssh_info_path TEXT NOT NULL,
  project_root TEXT NOT NULL,
  os_family TEXT NOT NULL,
  shell_family TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'registered',
  transport_topology TEXT NOT NULL DEFAULT 'peer_to_peer',
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS operator_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  operation_id TEXT NOT NULL UNIQUE,
  node_id TEXT NOT NULL,
  operation_kind TEXT NOT NULL,
  status TEXT NOT NULL,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  result_path TEXT NOT NULL DEFAULT '',
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS source_links (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  entity_type TEXT NOT NULL,
  entity_id INTEGER NOT NULL,
  relation_kind TEXT NOT NULL DEFAULT 'primary',
  UNIQUE(source_id, entity_type, entity_id, relation_kind)
);

CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_type TEXT NOT NULL,
  entity_id INTEGER NOT NULL,
  stage TEXT NOT NULL,
  tool_name TEXT NOT NULL,
  status TEXT NOT NULL,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  log_path TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS failures (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_type TEXT NOT NULL,
  entity_id INTEGER NOT NULL,
  stage TEXT NOT NULL,
  failure_class TEXT NOT NULL,
  failure_code TEXT NOT NULL,
  message TEXT NOT NULL,
  evidence_path TEXT NOT NULL DEFAULT '',
  retryable INTEGER NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tags (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_type TEXT NOT NULL,
  entity_id INTEGER NOT NULL,
  tag TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_aliases_lookup
  ON aliases(external_system, alias_value, entity_type);

CREATE INDEX IF NOT EXISTS idx_packages_sample
  ON packages(sample_id);

CREATE INDEX IF NOT EXISTS idx_artifacts_owner
  ON artifacts(owner_type, owner_id, stage);

CREATE INDEX IF NOT EXISTS idx_source_links_entity
  ON source_links(entity_type, entity_id);

CREATE INDEX IF NOT EXISTS idx_operator_runs_node
  ON operator_runs(node_id, operation_kind, started_at);
"""


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row["name"] for row in rows}


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    rows = conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()
    return {row["name"] for row in rows}


def _should_rename_v1_table(conn: sqlite3.Connection, old_name: str) -> bool:
    columns = _table_columns(conn, old_name)
    if old_name == "packages":
        return "sha256" in columns and "canonical_package_id" not in columns
    if old_name == "artifacts":
        return "asset_id" in columns and "owner_type" not in columns
    return True


def _migrate_v1_tables(conn: sqlite3.Connection) -> None:
    existing = _table_names(conn)
    renamed = False
    for old_name, new_name in V1_TABLE_RENAMES.items():
        if old_name not in existing or new_name in existing:
            continue
        if not _should_rename_v1_table(conn, old_name):
            continue
        conn.execute(f'ALTER TABLE "{old_name}" RENAME TO "{new_name}"')
        renamed = True
    if renamed:
        conn.commit()


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    _migrate_v1_tables(conn)
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn
