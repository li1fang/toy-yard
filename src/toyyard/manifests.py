from __future__ import annotations

from pathlib import Path

from toyyard.util import write_json


def write_package_manifest(path: Path, payload: dict) -> None:
    write_json(path, payload)


def write_asset_manifest(path: Path, payload: dict) -> None:
    write_json(path, payload)


def write_marker(path: Path, payload: dict) -> None:
    write_json(path, payload)
