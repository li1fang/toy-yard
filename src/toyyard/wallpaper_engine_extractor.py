from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path
from typing import Any

from toyyard.paths import ProjectPaths
from toyyard.util import now_iso, write_json


DEFAULT_WORKSHOP_ROOT = Path(r"C:\Program Files (x86)\Steam\steamapps\workshop\content\431960")
KNOWN_FFMPEG_PATHS = (
    Path(r"C:\Program Files\AutoCutVideo\ffmpeg.exe"),
    Path(r"C:\ffmpeg\bin\ffmpeg.exe"),
)
KNOWN_FFPROBE_PATHS = (
    Path(r"C:\Program Files\AutoCutVideo\ffprobe.exe"),
    Path(r"C:\ffmpeg\bin\ffprobe.exe"),
)


def _resolve_tool(explicit_path: str | None, known_paths: tuple[Path, ...], fallback_name: str) -> Path:
    if explicit_path:
        candidate = Path(explicit_path).expanduser().resolve()
        if candidate.exists():
            return candidate
        raise FileNotFoundError(f"{fallback_name}_missing:{candidate}")
    resolved = shutil.which(fallback_name)
    if resolved:
        return Path(resolved).expanduser().resolve()
    for path in known_paths:
        if path.exists():
            return path.resolve()
    raise FileNotFoundError(f"{fallback_name}_not_found")


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "state_version": "0.1",
            "source_root": "",
            "extract_root": "",
            "processed_items": {},
        }
    import json

    return json.loads(path.read_text(encoding="utf-8-sig"))


def _save_state(path: Path, payload: dict[str, Any]) -> None:
    write_json(path, payload)


def _iter_item_dirs(workshop_root: Path) -> list[Path]:
    return sorted(
        [
            child
            for child in workshop_root.iterdir()
            if child.is_dir() and child.name.isdigit()
        ],
        key=lambda path: path.name,
    )


def _mp4_files(item_dir: Path) -> list[Path]:
    return sorted([path for path in item_dir.rglob("*.mp4") if path.is_file()])


def _probe_duration(ffprobe_path: Path, video_path: Path) -> float:
    result = subprocess.run(
        [
            str(ffprobe_path),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"ffprobe_failed:{video_path}")
    try:
        return float(result.stdout.strip())
    except ValueError as exc:
        raise RuntimeError(f"ffprobe_duration_parse_failed:{video_path}") from exc


def _frame_name(video_path: Path, item_dir: Path, index: int) -> str:
    rel = str(video_path.relative_to(item_dir)).replace("\\", "/")
    digest = hashlib.sha1(rel.encode("utf-8")).hexdigest()[:10]
    return f"midframe_{index:03d}_{digest}.jpg"


def _extract_middle_frame(ffmpeg_path: Path, video_path: Path, midpoint_sec: float, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            str(ffmpeg_path),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{midpoint_sec:.3f}",
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(output_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not output_path.exists():
        raise RuntimeError(result.stderr.strip() or f"ffmpeg_extract_failed:{video_path}")


def extract_wallpaper_engine_frames(
    paths: ProjectPaths,
    *,
    workshop_root: Path = DEFAULT_WORKSHOP_ROOT,
    ffmpeg_path: str | None = None,
    ffprobe_path: str | None = None,
    limit: int | None = None,
    item_id: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    workshop_root = workshop_root.expanduser().resolve()
    if not workshop_root.exists():
        raise FileNotFoundError(f"workshop_root_missing:{workshop_root}")
    ffmpeg = _resolve_tool(ffmpeg_path, KNOWN_FFMPEG_PATHS, "ffmpeg")
    ffprobe = _resolve_tool(ffprobe_path, KNOWN_FFPROBE_PATHS, "ffprobe")
    state = _load_state(paths.wallpaper_engine_state_path)
    state["state_version"] = "0.1"
    state["source_root"] = str(workshop_root)
    state["extract_root"] = str(paths.wallpaper_engine_extract_root)
    processed_items = dict(state.get("processed_items") or {})

    item_dirs = _iter_item_dirs(workshop_root)
    if item_id:
        item_dirs = [path for path in item_dirs if path.name == item_id]
    if limit is not None:
        item_dirs = item_dirs[: max(limit, 0)]

    counts = {
        "items_scanned": len(item_dirs),
        "items_extracted": 0,
        "items_skipped": 0,
        "items_without_mp4": 0,
        "videos_extracted": 0,
    }

    for current_item_dir in item_dirs:
        current_item_id = current_item_dir.name
        if current_item_id in processed_items and not force:
            counts["items_skipped"] += 1
            continue

        videos = _mp4_files(current_item_dir)
        item_extract_dir = paths.wallpaper_engine_item_extract_dir(current_item_id)
        item_manifest_path = paths.wallpaper_engine_item_manifest_path(current_item_id)
        item_manifest_path.parent.mkdir(parents=True, exist_ok=True)

        if not videos:
            payload = {
                "item_id": current_item_id,
                "source_dir": str(current_item_dir),
                "output_dir": str(item_extract_dir),
                "status": "no_mp4",
                "processed_at_utc": now_iso(),
                "videos": [],
            }
            write_json(item_manifest_path, payload)
            processed_items[current_item_id] = payload
            counts["items_without_mp4"] += 1
            continue

        extracted_videos: list[dict[str, Any]] = []
        for index, video_path in enumerate(videos, start=1):
            duration_sec = _probe_duration(ffprobe, video_path)
            midpoint_sec = max(duration_sec / 2.0, 0.0)
            frame_path = item_extract_dir / _frame_name(video_path, current_item_dir, index)
            _extract_middle_frame(ffmpeg, video_path, midpoint_sec, frame_path)
            extracted_videos.append(
                {
                    "video_path": str(video_path),
                    "video_relative_path": str(video_path.relative_to(current_item_dir)).replace("\\", "/"),
                    "duration_sec": duration_sec,
                    "midpoint_sec": midpoint_sec,
                    "frame_path": str(frame_path.resolve()),
                }
            )
            counts["videos_extracted"] += 1

        payload = {
            "item_id": current_item_id,
            "source_dir": str(current_item_dir),
            "output_dir": str(item_extract_dir.resolve()),
            "status": "pass",
            "processed_at_utc": now_iso(),
            "videos": extracted_videos,
        }
        write_json(item_manifest_path, payload)
        processed_items[current_item_id] = payload
        counts["items_extracted"] += 1

    state["processed_items"] = processed_items
    _save_state(paths.wallpaper_engine_state_path, state)
    return {
        "workshop_root": str(workshop_root),
        "extract_root": str(paths.wallpaper_engine_extract_root),
        "state_path": str(paths.wallpaper_engine_state_path),
        "ffmpeg_path": str(ffmpeg),
        "ffprobe_path": str(ffprobe),
        **counts,
    }
