from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from toyyard.archive import ArchiveReadError, ListedFile, list_package_files
from toyyard.manifests import write_asset_manifest, write_marker, write_package_manifest
from toyyard.paths import ProjectPaths
from toyyard.util import now_iso, slugify


@dataclass
class AnalysisResult:
    game_hint: str
    asset_kind: str
    completeness: str
    readiness: str
    status: str
    primary_target: str
    package_role_summary: dict[str, int]
    dependency_records: list[dict[str, str]]
    compatibility: dict[str, object]
    signals: dict[str, object]


def _insert_run(conn: sqlite3.Connection, entity_type: str, entity_id: int, stage: str, status: str, summary: str = "") -> int:
    cursor = conn.execute(
        """
        INSERT INTO runs (entity_type, entity_id, stage, tool_name, status, started_at, summary)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (entity_type, entity_id, stage, "toyyard", status, now_iso(), summary),
    )
    conn.commit()
    return cursor.lastrowid


def _finish_run(conn: sqlite3.Connection, run_id: int, status: str, summary: str = "", log_path: str = "") -> None:
    conn.execute(
        "UPDATE runs SET status = ?, ended_at = ?, summary = ?, log_path = ? WHERE id = ?",
        (status, now_iso(), summary, log_path, run_id),
    )
    conn.commit()


def _record_failure(
    conn: sqlite3.Connection,
    entity_type: str,
    entity_id: int,
    stage: str,
    failure_class: str,
    failure_code: str,
    message: str,
    retryable: bool,
    evidence_path: str = "",
) -> None:
    conn.execute(
        """
        INSERT INTO failures (
          entity_type, entity_id, stage, failure_class, failure_code, message,
          evidence_path, retryable, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (entity_type, entity_id, stage, failure_class, failure_code, message, evidence_path, int(retryable), now_iso()),
    )
    conn.commit()


def _path_signals(entries: list[ListedFile], rules: dict[str, dict], filename: str) -> dict[str, object]:
    generic = rules["generic"].get("signals", {})
    skyrim = rules["games"].get("skyrimse", {}).get("signals", {})

    entry_paths = [entry.path_in_package for entry in entries]
    lowered_paths = [path.lower() for path in entry_paths]
    ext_counter = Counter(entry.ext for entry in entries)

    def any_extension(values: list[str]) -> bool:
        return any(ext_counter.get(ext, 0) for ext in values)

    def matching_keywords(keywords: list[str]) -> list[str]:
        haystack = " ".join(lowered_paths + [filename.lower()])
        return sorted({keyword for keyword in keywords if keyword in haystack})

    has_mesh = any_extension(generic.get("mesh_extensions", []))
    has_texture = any_extension(generic.get("texture_extensions", []))
    has_animation = any_extension(generic.get("animation_extensions", []))
    has_plugin = any_extension(generic.get("plugin_extensions", []))
    only_textures = has_texture and not has_mesh and not has_plugin and not has_animation
    has_xml = any(path.endswith(".xml") for path in lowered_paths)
    has_bodyslide = any(marker in path for marker in skyrim.get("bodyslide_markers", []) for path in lowered_paths)
    body_keywords = matching_keywords(list({*generic.get("body_keywords", []), *skyrim.get("body_keywords", [])}))
    skeleton_keywords = matching_keywords(list({*generic.get("skeleton_keywords", []), *skyrim.get("skeleton_keywords", [])}))
    physics_keywords = matching_keywords(list({*generic.get("physics_keywords", []), *skyrim.get("physics_keywords", [])}))
    character_keywords = matching_keywords(generic.get("character_keywords", []))
    outfit_keywords = matching_keywords(generic.get("outfit_keywords", []))
    weapon_keywords = matching_keywords(generic.get("weapon_keywords", []))
    accessory_keywords = matching_keywords(generic.get("accessory_keywords", []))
    has_skyrim_extensions = any_extension(skyrim.get("game_extensions", []))
    mixed_axes = sum(bool(value) for value in (weapon_keywords, accessory_keywords, outfit_keywords, character_keywords))

    return {
        "ext_counter": dict(ext_counter),
        "has_mesh": has_mesh,
        "has_texture": has_texture,
        "has_animation": has_animation,
        "has_plugin": has_plugin,
        "only_textures": only_textures,
        "has_xml": has_xml,
        "has_bodyslide": has_bodyslide,
        "body_keywords": body_keywords,
        "skeleton_keywords": skeleton_keywords,
        "physics_keywords": physics_keywords,
        "character_keywords": character_keywords,
        "outfit_keywords": outfit_keywords,
        "weapon_keywords": weapon_keywords,
        "accessory_keywords": accessory_keywords,
        "has_skyrim_extensions": has_skyrim_extensions,
        "mixed_axes": mixed_axes,
        "entry_count": len(entries),
    }


def _infer_analysis(entries: list[ListedFile], rules: dict[str, dict], filename: str) -> AnalysisResult:
    signals = _path_signals(entries, rules, filename)
    has_body = bool(signals["body_keywords"])
    has_skeleton = bool(signals["skeleton_keywords"])
    has_physics = bool(signals["physics_keywords"]) or bool(signals["has_xml"])

    game_hint = "skyrimse" if signals["has_skyrim_extensions"] or signals["has_bodyslide"] else "unknown"

    if signals["only_textures"]:
        asset_kind = "texture_pack"
    elif signals["has_animation"] and not signals["has_mesh"]:
        asset_kind = "animation"
    elif (
        signals["has_mesh"]
        and signals["has_texture"]
        and has_body
        and has_skeleton
        and not signals["has_bodyslide"]
        and not signals["outfit_keywords"]
    ):
        asset_kind = "character"
    elif signals["has_mesh"] and (signals["outfit_keywords"] or signals["has_bodyslide"] or has_body or signals["has_plugin"]):
        asset_kind = "outfit_set"
    elif signals["weapon_keywords"] and signals["has_mesh"]:
        asset_kind = "weapon"
    elif signals["accessory_keywords"] and signals["has_mesh"] and not signals["outfit_keywords"]:
        asset_kind = "accessory"
    elif signals["mixed_axes"] >= rules["generic"].get("signals", {}).get("mixed_pack_threshold", 3):
        asset_kind = "mixed_pack"
    else:
        asset_kind = "unknown"

    if asset_kind == "character":
        completeness = "full_character"
    elif asset_kind in {"outfit_set", "accessory", "weapon"}:
        completeness = "component"
    elif asset_kind == "texture_pack":
        completeness = "textures_only"
    else:
        completeness = "partial"

    if asset_kind == "character" and signals["has_mesh"] and signals["has_texture"] and has_body and has_skeleton:
        readiness = "candidate"
    elif asset_kind in {"outfit_set", "accessory", "weapon"} and signals["has_mesh"] and signals["has_texture"]:
        readiness = "component_only"
    elif asset_kind == "texture_pack":
        readiness = "blocked"
    elif asset_kind == "unknown" and signals["has_mesh"]:
        readiness = "blocked"
    else:
        readiness = "unknown"

    if readiness == "unknown" and (signals["has_bodyslide"] or has_body or has_skeleton or has_physics):
        readiness = "blocked"

    status = "blocked" if readiness == "blocked" else "triaged"
    primary_target = "unreal"

    dependency_records: list[dict[str, str]] = []
    if signals["has_bodyslide"]:
        dependency_records.append(
            {
                "dep_name": "BodySlide",
                "dep_kind": "tool",
                "requirement_level": "required",
                "status": "detected",
                "evidence": "CalienteTools/BodySlide",
            }
        )
    for keyword in signals["body_keywords"]:
        dependency_records.append(
            {
                "dep_name": keyword.upper(),
                "dep_kind": "body",
                "requirement_level": "required",
                "status": "detected",
                "evidence": keyword,
            }
        )
    for keyword in signals["skeleton_keywords"]:
        dependency_records.append(
            {
                "dep_name": keyword.upper(),
                "dep_kind": "skeleton",
                "requirement_level": "required",
                "status": "detected",
                "evidence": keyword,
            }
        )
    for keyword in signals["physics_keywords"]:
        dependency_records.append(
            {
                "dep_name": keyword.upper(),
                "dep_kind": "physics",
                "requirement_level": "optional",
                "status": "detected",
                "evidence": keyword,
            }
        )

    if readiness == "ue_ready":
        compat_status, confidence = "native", 0.95
    elif readiness == "candidate":
        compat_status, confidence = "compatible", 0.8
    elif readiness in {"component_only", "blocked"}:
        compat_status, confidence = "requires_adapter", 0.75
    elif readiness == "rejected":
        compat_status, confidence = "incompatible", 0.95
    else:
        compat_status, confidence = "untested", 0.5

    return AnalysisResult(
        game_hint=game_hint,
        asset_kind=asset_kind,
        completeness=completeness,
        readiness=readiness,
        status=status,
        primary_target=primary_target,
        package_role_summary={
            "mesh_files": int(signals["has_mesh"]),
            "texture_files": int(signals["has_texture"]),
            "animation_files": int(signals["has_animation"]),
            "plugin_files": int(signals["has_plugin"]),
            "entry_count": int(signals["entry_count"]),
        },
        dependency_records=dependency_records,
        compatibility={
            "target_kind": "pipeline",
            "target_name": "unreal",
            "compat_status": compat_status,
            "confidence": confidence,
            "notes": f"Derived from readiness={readiness}",
        },
        signals=signals,
    )


def _role_hint(entry: ListedFile) -> str:
    mesh_exts = {".nif", ".fbx", ".obj", ".gltf", ".glb", ".mesh", ".msh"}
    texture_exts = {".dds", ".png", ".jpg", ".jpeg", ".tga", ".bmp"}
    animation_exts = {".hkx", ".anim", ".vmd", ".vpd", ".motion"}
    plugin_exts = {".esp", ".esm", ".esl", ".ini", ".json", ".xml"}
    if entry.ext in mesh_exts:
        return "mesh"
    if entry.ext in texture_exts:
        return "texture"
    if entry.ext in animation_exts:
        return "animation"
    if entry.ext in plugin_exts:
        return "plugin_or_config"
    if "bodyslide" in entry.path_in_package.lower():
        return "tooling"
    return "unknown"


def _clear_package_triage(conn: sqlite3.Connection, package_id: int) -> None:
    asset_ids = [row["id"] for row in conn.execute("SELECT id FROM v1_assets WHERE package_id = ?", (package_id,))]
    conn.execute("DELETE FROM v1_package_files WHERE package_id = ?", (package_id,))
    for asset_id in asset_ids:
        conn.execute("DELETE FROM v1_dependencies WHERE subject_type = 'asset' AND subject_id = ?", (asset_id,))
        conn.execute("DELETE FROM v1_compatibility WHERE asset_id = ?", (asset_id,))
        conn.execute("DELETE FROM v1_artifacts WHERE asset_id = ?", (asset_id,))
        conn.execute("DELETE FROM tags WHERE entity_type = 'asset' AND entity_id = ?", (asset_id,))
    conn.execute("DELETE FROM v1_assets WHERE package_id = ?", (package_id,))
    conn.execute("DELETE FROM v1_dependencies WHERE subject_type = 'package' AND subject_id = ?", (package_id,))
    conn.commit()


def _bucket_for_readiness(readiness: str) -> str:
    if readiness == "blocked":
        return "blocked_by_dependency"
    if readiness in {"candidate", "component_only", "unknown"}:
        return "review_needed"
    return "pending"


def run_triage(conn: sqlite3.Connection, paths: ProjectPaths, rules: dict[str, dict], package_id: int) -> dict[str, object]:
    package = conn.execute("SELECT * FROM v1_packages WHERE id = ?", (package_id,)).fetchone()
    if package is None:
        raise ValueError(f"Unknown package id: {package_id}")

    run_id = _insert_run(conn, "package", package_id, "triage", "running")
    _clear_package_triage(conn, package_id)
    paths.cleanup_triage_markers(package_id)
    paths.cleanup_reject_markers(package_id)

    package_path = Path(package["managed_path"])

    try:
        entries = list_package_files(package_path)
    except ArchiveReadError as exc:
        conn.execute("UPDATE v1_packages SET ingest_status = ?, game_hint = ? WHERE id = ?", ("triage_failed", "unknown", package_id))
        conn.commit()
        _record_failure(
            conn,
            "package",
            package_id,
            "triage",
            "archive_read_error",
            "unreadable_archive",
            str(exc),
            retryable=True,
            evidence_path=str(package_path),
        )
        marker_payload = {
            "package_id": package_id,
            "filename": package["filename"],
            "stage": "triage",
            "failure": "archive_read_error",
            "message": str(exc),
        }
        write_marker(paths.reject_marker_path("classify_failed", package_id), marker_payload)
        _finish_run(conn, run_id, "failed", str(exc))
        return marker_payload

    analysis = _infer_analysis(entries, rules, package["filename"])
    role_counter = Counter()
    for entry in entries:
        role_hint = _role_hint(entry)
        role_counter[role_hint] += 1
        conn.execute(
            """
            INSERT INTO v1_package_files (package_id, path_in_package, ext, size_bytes, role_hint)
            VALUES (?, ?, ?, ?, ?)
            """,
            (package_id, entry.path_in_package, entry.ext, entry.size_bytes, role_hint),
        )

    conn.execute(
        "UPDATE v1_packages SET game_hint = ?, ingest_status = ? WHERE id = ?",
        (analysis.game_hint, "triaged", package_id),
    )
    asset_name = Path(package["filename"]).stem
    asset_cursor = conn.execute(
        """
        INSERT INTO v1_assets (
          package_id, asset_kind, display_name, canonical_name, completeness,
          readiness, primary_target, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            package_id,
            analysis.asset_kind,
            asset_name,
            slugify(asset_name),
            analysis.completeness,
            analysis.readiness,
            analysis.primary_target,
            analysis.status,
        ),
    )
    asset_id = asset_cursor.lastrowid

    for record in analysis.dependency_records:
        conn.execute(
            """
            INSERT INTO v1_dependencies (
              subject_type, subject_id, dep_name, dep_kind, requirement_level, status, evidence
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("asset", asset_id, record["dep_name"], record["dep_kind"], record["requirement_level"], record["status"], record["evidence"]),
        )

    conn.execute(
        """
        INSERT INTO v1_compatibility (
          asset_id, target_kind, target_name, compat_status, confidence, notes
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            asset_id,
            analysis.compatibility["target_kind"],
            analysis.compatibility["target_name"],
            analysis.compatibility["compat_status"],
            analysis.compatibility["confidence"],
            analysis.compatibility["notes"],
        ),
    )
    conn.commit()

    package_payload = {
        "package_id": package_id,
        "filename": package["filename"],
        "provenance": {
            "source_kind": package["source_kind"],
            "source_path": package["source_path"],
            "managed_path": package["managed_path"],
            "sha256": package["sha256"],
        },
        "archive_summary": {
            "entry_count": len(entries),
            "role_counter": dict(role_counter),
            "ext_counter": analysis.signals["ext_counter"],
        },
        "detected_game_hints": [analysis.game_hint],
        "asset_ids": [asset_id],
        "dependency_signals": analysis.dependency_records,
        "readiness_summary": {
            "readiness": analysis.readiness,
            "asset_kind": analysis.asset_kind,
            "completeness": analysis.completeness,
            "primary_target": analysis.primary_target,
        },
        "last_triage_result": {
            "status": analysis.status,
            "signals": analysis.signals,
        },
    }
    asset_payload = {
        "asset_id": asset_id,
        "package_id": package_id,
        "display_name": asset_name,
        "canonical_name": slugify(asset_name),
        "asset_kind": analysis.asset_kind,
        "completeness": analysis.completeness,
        "readiness": analysis.readiness,
        "primary_target": analysis.primary_target,
        "compatibility": analysis.compatibility,
        "dependencies": analysis.dependency_records,
        "signals": analysis.signals,
    }

    write_package_manifest(paths.package_manifest_path(package_id), package_payload)
    write_asset_manifest(paths.asset_manifest_path(asset_id), asset_payload)

    triage_bucket = _bucket_for_readiness(analysis.readiness)
    write_marker(paths.triage_marker_path(triage_bucket, package_id), package_payload)

    if analysis.readiness == "blocked":
        _record_failure(
            conn,
            "asset",
            asset_id,
            "triage",
            "dependency_blocked",
            "missing_or_adapter_required",
            f"Asset classified as blocked for Unreal intake: {analysis.asset_kind}",
            retryable=True,
            evidence_path=str(paths.asset_manifest_path(asset_id)),
        )

    _finish_run(conn, run_id, "succeeded", f"{analysis.asset_kind}:{analysis.readiness}")
    return {
        "package_id": package_id,
        "asset_id": asset_id,
        "asset_kind": analysis.asset_kind,
        "readiness": analysis.readiness,
        "game_hint": analysis.game_hint,
    }
