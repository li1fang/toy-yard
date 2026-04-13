from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from toyyard.util import now_iso, write_json


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def resolve_packet_artifact(manifest_path: Path, artifact_path: str | None) -> Path | None:
    if not artifact_path:
        return None
    candidate = Path(str(artifact_path)).expanduser()
    if candidate.is_absolute():
        return candidate.resolve(strict=False)
    return (manifest_path.parent / candidate).resolve(strict=False)


def is_under_root(path: Path | None, export_root: Path) -> bool:
    if path is None:
        return False
    try:
        path.resolve(strict=False).relative_to(export_root.resolve())
        return True
    except ValueError:
        return False


def inspect_motion_packet_manifest(manifest_path: Path, export_root: Path) -> dict[str, Any]:
    payload = load_json(manifest_path)
    export_artifacts = payload.get("export_artifacts") or {}
    validation = payload.get("validation") or {}
    skeleton = payload.get("skeleton") or {}
    motion_npz = resolve_packet_artifact(manifest_path, export_artifacts.get("motion_npz"))
    motion_bvh = resolve_packet_artifact(manifest_path, export_artifacts.get("motion_bvh"))
    skeleton_path = resolve_packet_artifact(manifest_path, export_artifacts.get("skeleton_path"))

    checks = [
        {
            "name": "manifest_exists",
            "status": "pass",
            "details": {"manifest_path": str(manifest_path.resolve())},
        },
        {
            "name": "motion_npz_present",
            "status": "pass" if motion_npz and motion_npz.exists() and is_under_root(motion_npz, export_root) else "fail",
            "details": {"path": str(motion_npz) if motion_npz else ""},
        },
        {
            "name": "motion_bvh_present",
            "status": "pass" if motion_bvh and motion_bvh.exists() and is_under_root(motion_bvh, export_root) else "fail",
            "details": {"path": str(motion_bvh) if motion_bvh else ""},
        },
        {
            "name": "skeleton_present",
            "status": "pass" if skeleton_path and skeleton_path.exists() and is_under_root(skeleton_path, export_root) else "fail",
            "details": {"path": str(skeleton_path) if skeleton_path else ""},
        },
        {
            "name": "validation_status",
            "status": "pass" if str(validation.get("status") or payload.get("validation_status") or "").strip().lower() == "pass" else "fail",
            "details": {"status": validation.get("status") or payload.get("validation_status") or ""},
        },
        {
            "name": "runtime_semantics",
            "status": "pass" if str(payload.get("runtime_semantics") or "") == "runtime" else "fail",
            "details": {"runtime_semantics": payload.get("runtime_semantics")},
        },
        {
            "name": "placeholder_motion",
            "status": "pass" if payload.get("placeholder_motion") is False else "fail",
            "details": {"placeholder_motion": payload.get("placeholder_motion")},
        },
        {
            "name": "format_profile",
            "status": "pass" if str(payload.get("format_profile") or "") == "route_a_kimodo_somaskel77" else "fail",
            "details": {"format_profile": payload.get("format_profile")},
        },
        {
            "name": "skeleton_id",
            "status": "pass" if str(skeleton.get("skeleton_id") or "") == "somaskel77" else "fail",
            "details": {"skeleton_id": skeleton.get("skeleton_id")},
        },
    ]
    failures = [item["name"] for item in checks if item["status"] != "pass"]
    status = "pass" if not failures else "fail"
    return {
        "package_id": str(payload.get("package_id") or ""),
        "sample_id": str(payload.get("sample_id") or ""),
        "clip_id": str(payload.get("clip_id") or ""),
        "pack_version": str(payload.get("pack_version") or ""),
        "status": status,
        "checks": checks,
        "failures": failures,
        "selection_ready": not failures,
    }


def build_motion_packet_check_report(*, manifest_paths: list[Path], export_root: Path) -> dict[str, Any]:
    items = [inspect_motion_packet_manifest(path, export_root) for path in manifest_paths]
    passed = sum(1 for item in items if item["status"] == "pass")
    if not items:
        status = "attention"
    elif passed == len(items):
        status = "pass"
    elif passed == 0:
        status = "fail"
    else:
        status = "attention"
    return {
        "generated_at_utc": now_iso(),
        "status": status,
        "counts": {
            "manifest_count": len(items),
            "pass_count": passed,
            "fail_count": sum(1 for item in items if item["status"] == "fail"),
            "selection_ready_count": sum(1 for item in items if item["selection_ready"]),
        },
        "items": items,
    }


def write_motion_packet_check_report(*, manifest_paths: list[Path], export_root: Path, output_path: Path) -> dict[str, Any]:
    payload = build_motion_packet_check_report(manifest_paths=manifest_paths, export_root=export_root)
    write_json(output_path, payload)
    return payload
