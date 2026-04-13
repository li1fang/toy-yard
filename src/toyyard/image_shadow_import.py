from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from toyyard.paths import ProjectPaths
from toyyard.warehouse import (
    canonical_root,
    ensure_alias,
    ensure_artifact,
    ensure_package,
    ensure_report_import,
    ensure_sample,
    ensure_source,
    ensure_source_link,
    make_canonical_id,
    register_root,
)


def _load_state(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def import_wallpaper_engine_catalog(
    conn: sqlite3.Connection,
    paths: ProjectPaths,
    *,
    state_path: Path | None = None,
) -> dict[str, Any]:
    state_file = (state_path or paths.wallpaper_engine_state_path).expanduser().resolve()
    if not state_file.exists():
        raise FileNotFoundError(f"wallpaper_engine_state_missing:{state_file}")

    state = _load_state(state_file)
    workshop_root = Path(str(state.get("source_root") or "")).expanduser().resolve()
    external_root = register_root(
        conn,
        "external_source",
        str(workshop_root),
        "wallpaper-engine-workshop",
        status="active",
        is_canonical_write_root=False,
        metadata={"adapter": "wallpaper_engine_extractor"},
    )
    canonical = canonical_root(conn)
    if canonical is None:
        raise ValueError("Canonical root is not initialized")

    counts = {
        "samples_total": 0,
        "packages_total": 0,
        "artifacts_total": 0,
    }

    for item_id, entry in sorted((state.get("processed_items") or {}).items()):
        if str((entry or {}).get("status") or "") != "pass":
            continue
        source_dir = Path(str(entry.get("source_dir") or "")).expanduser().resolve()
        videos = list(entry.get("videos") or [])
        if not videos:
            continue

        sample_id = make_canonical_id("sample", "image", "wallpaper_engine", item_id, display_name=f"wallpaper-engine-{item_id}")
        package_id = make_canonical_id("pkg", "image", "wallpaper_engine", item_id, "midframe-v0", display_name=f"wallpaper-engine-{item_id}-midframe-v0")

        source = ensure_source(
            conn,
            root_id=external_root["id"],
            source_kind="wallpaper_engine_item",
            source_path=str(source_dir),
            managed_path=str(Path(str(entry.get("output_dir") or "")).expanduser().resolve()),
            content_hash="",
            ext="",
            size_bytes=None,
            status="cataloged",
            metadata={
                "workshop_item_id": item_id,
                "extract_manifest_path": str(paths.wallpaper_engine_item_manifest_path(item_id)),
                "video_count": len(videos),
                "adapter": "wallpaper_engine_extractor",
            },
        )
        sample = ensure_sample(
            conn,
            canonical_sample_id=sample_id,
            display_name=f"wallpaper-engine-{item_id}",
            source_game="wallpaper_engine",
            source_format="workshop_video_wallpaper",
            item_family="image",
            warehouse_status="cataloged",
            metadata={
                "workshop_item_id": item_id,
                "image_lane": "shadow_v0",
                "intended_for_3d": True,
                "intended_for_video": True,
                "source_adapter": "wallpaper_engine_extractor",
            },
        )
        package = ensure_package(
            conn,
            sample_id=sample["id"],
            canonical_package_id=package_id,
            package_role="image_set",
            content_bucket="image",
            contract_type="wallpaper_engine_midframe_v0",
            consumer_ready=False,
            warehouse_status="cataloged",
            metadata={
                "workshop_item_id": item_id,
                "video_count": len(videos),
                "frame_count": len(videos),
                "image_roles": ["reference_image", "midframe_extract"],
                "downstream_targets": ["3d_seed", "video_seed"],
                "extract_manifest_path": str(paths.wallpaper_engine_item_manifest_path(item_id)),
            },
        )
        ensure_alias(
            conn,
            entity_type="sample",
            entity_id=sample["id"],
            external_system="wallpaper_engine",
            alias_key="workshop_item_id",
            alias_value=item_id,
        )
        ensure_alias(
            conn,
            entity_type="package",
            entity_id=package["id"],
            external_system="wallpaper_engine",
            alias_key="workshop_item_id",
            alias_value=item_id,
        )
        ensure_source_link(
            conn,
            source_id=source["id"],
            entity_type="sample",
            entity_id=sample["id"],
            relation_kind="wallpaper_engine_midframe",
        )
        counts["samples_total"] += 1
        counts["packages_total"] += 1

        manifest_path = paths.wallpaper_engine_item_manifest_path(item_id)
        if manifest_path.exists():
            ensure_artifact(
                conn,
                owner_type="source",
                owner_id=source["id"],
                stage="source",
                artifact_kind="extract_manifest",
                path=str(manifest_path.resolve()),
                format=".json",
                status="cataloged",
                metadata={"workshop_item_id": item_id},
            )
            counts["artifacts_total"] += 1

        for index, video in enumerate(videos, start=1):
            frame_path = Path(str(video.get("frame_path") or "")).expanduser().resolve()
            if not frame_path.exists():
                continue
            ensure_artifact(
                conn,
                owner_type="package",
                owner_id=package["id"],
                stage="source",
                artifact_kind="reference_image",
                path=str(frame_path),
                format=frame_path.suffix.lower(),
                status="cataloged",
                metadata={
                    "workshop_item_id": item_id,
                    "video_path": str(video.get("video_path") or ""),
                    "video_relative_path": str(video.get("video_relative_path") or ""),
                    "duration_sec": video.get("duration_sec"),
                    "midpoint_sec": video.get("midpoint_sec"),
                    "image_role": "midframe_extract",
                    "image_index": index,
                },
            )
            counts["artifacts_total"] += 1

    ensure_report_import(
        conn,
        root_id=canonical["id"],
        report_kind="image_shadow_import",
        report_path=str(state_file),
        status="imported",
        metadata={
            "adapter": "wallpaper_engine_extractor",
            "source_root": str(workshop_root),
        },
    )
    return {
        "state_path": str(state_file),
        "workshop_root": str(workshop_root),
        **counts,
    }
