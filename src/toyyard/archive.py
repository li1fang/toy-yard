from __future__ import annotations

import shutil
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path


class ArchiveReadError(RuntimeError):
    pass


@dataclass(frozen=True)
class ListedFile:
    path_in_package: str
    ext: str
    size_bytes: int | None


def _list_directory(path: Path) -> list[ListedFile]:
    items: list[ListedFile] = []
    for child in sorted(path.rglob("*")):
        if child.is_dir():
            continue
        relative = child.relative_to(path).as_posix()
        items.append(ListedFile(relative, child.suffix.lower(), child.stat().st_size))
    return items


def _list_zip(path: Path) -> list[ListedFile]:
    items: list[ListedFile] = []
    try:
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                items.append(ListedFile(info.filename, Path(info.filename).suffix.lower(), info.file_size))
    except zipfile.BadZipFile as exc:
        raise ArchiveReadError(f"Unreadable zip archive: {path}") from exc
    return items


def _list_with_tar(path: Path) -> list[ListedFile]:
    tar_exe = shutil.which("tar")
    if not tar_exe:
        raise ArchiveReadError("tar executable not available to inspect archive")
    result = subprocess.run(
        [tar_exe, "-tf", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ArchiveReadError(result.stderr.strip() or f"Unable to inspect archive: {path}")
    items: list[ListedFile] = []
    for line in result.stdout.splitlines():
        candidate = line.strip()
        if not candidate or candidate.endswith("/"):
            continue
        items.append(ListedFile(candidate, Path(candidate).suffix.lower(), None))
    return items


def list_package_files(path: Path) -> list[ListedFile]:
    if path.is_dir():
        return _list_directory(path)
    if path.suffix.lower() == ".zip":
        return _list_zip(path)
    return _list_with_tar(path)
