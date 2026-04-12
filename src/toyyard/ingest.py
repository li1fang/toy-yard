from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

from toyyard.constants import DEFAULT_VORTEX_SKYRIMSE
from toyyard.hashing import sha256_path
from toyyard.paths import ProjectPaths
from toyyard.util import normalize_ext, now_iso


def _copy_source(source_path: Path, managed_dir: Path) -> Path:
    managed_dir.mkdir(parents=True, exist_ok=True)
    if source_path.is_dir():
        destination = managed_dir / "original_dir"
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(source_path, destination)
        return destination

    ext = source_path.suffix.lower()
    destination = managed_dir / f"original{ext}"
    if not destination.exists():
        shutil.copy2(source_path, destination)
    return destination


def discover_source_paths(paths: ProjectPaths, source_kind: str, game: str | None = None) -> list[Path]:
    if source_kind == "manual_drop":
        source_dir = paths.root / "00_inbox" / "manual_drop"
    elif source_kind == "vortex" and game == "skyrimse":
        source_dir = DEFAULT_VORTEX_SKYRIMSE
    else:
        raise ValueError(f"Unsupported source scan: source={source_kind!r}, game={game!r}")

    if not source_dir.exists():
        return []
    return sorted(child for child in source_dir.iterdir() if child.is_file() or child.is_dir())


def import_path(
    conn: sqlite3.Connection,
    paths: ProjectPaths,
    source_path: Path,
    source_kind: str = "unknown",
    game_hint: str = "unknown",
) -> sqlite3.Row:
    if not source_path.exists():
        raise FileNotFoundError(source_path)

    sha256 = sha256_path(source_path)
    filename = source_path.name
    ext = ".dir" if source_path.is_dir() else normalize_ext(source_path)
    size_bytes = None if source_path.is_dir() else source_path.stat().st_size
    timestamp = now_iso()

    package = conn.execute("SELECT * FROM v1_packages WHERE sha256 = ?", (sha256,)).fetchone()
    if package is None:
        managed_dir = paths.managed_package_dir(sha256, source_path.stem)
        managed_path = _copy_source(source_path, managed_dir)
        cursor = conn.execute(
            """
            INSERT INTO v1_packages (
              sha256, source_kind, source_path, managed_path, filename, ext, size_bytes,
              game_hint, ingest_status, discovered_at, imported_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sha256,
                source_kind,
                str(source_path),
                str(managed_path),
                filename,
                ext,
                size_bytes,
                game_hint,
                "imported",
                timestamp,
                timestamp,
            ),
        )
        package_id = cursor.lastrowid
        package = conn.execute("SELECT * FROM v1_packages WHERE id = ?", (package_id,)).fetchone()

    conn.execute(
        """
        INSERT INTO v1_package_observations (
          package_id, source_kind, source_path, discovered_at, imported_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (package["id"], source_kind, str(source_path), timestamp, timestamp),
    )
    conn.commit()
    return package


def scan_source(
    conn: sqlite3.Connection,
    paths: ProjectPaths,
    source_kind: str,
    game: str | None = None,
) -> list[sqlite3.Row]:
    imported: list[sqlite3.Row] = []
    for source_path in discover_source_paths(paths, source_kind, game=game):
        imported.append(import_path(conn, paths, source_path, source_kind=source_kind, game_hint=game or "unknown"))
    return imported


def format_import_rows(rows: list[sqlite3.Row]) -> list[dict[str, object]]:
    return [
        {
            "package_id": row["id"],
            "sha256": row["sha256"][:12],
            "filename": row["filename"],
            "game_hint": row["game_hint"],
            "managed": Path(row["managed_path"]).parent.name,
        }
        for row in rows
    ]
