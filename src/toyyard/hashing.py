from __future__ import annotations

import hashlib
from pathlib import Path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_directory(path: Path) -> str:
    digest = hashlib.sha256()
    for child in sorted(path.rglob("*")):
        relative = child.relative_to(path).as_posix().encode("utf-8")
        digest.update(relative)
        digest.update(b"\0")
        if child.is_file():
            with child.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def sha256_path(path: Path) -> str:
    return sha256_directory(path) if path.is_dir() else sha256_file(path)
