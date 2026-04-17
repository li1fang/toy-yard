from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from toyyard.util import now_iso, write_json


REQUIRED_OUTPUT_KEYS = ("normalized_glb", "painted_glb", "analysis_json")
OPTIONAL_OUTPUT_KEYS = ("engineering_mask", "manual_overrides")
SUPPORTED_SOURCE_PROVIDERS = {"native", "toy_yard_aiue_pmx"}


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


def _manifest_path(packet: dict[str, Any], export_root: Path) -> Path:
    manifest_path = str(packet.get("manifest_path") or "").strip()
    if manifest_path:
        return Path(manifest_path).resolve(strict=False)
    manifest_relpath = str(packet.get("manifest_relpath") or "").strip()
    if manifest_relpath:
        return (export_root / manifest_relpath).resolve(strict=False)
    return (export_root / "_missing" / "bodypaint_input_manifest.json").resolve(strict=False)


def _output_path_status(manifest_path: Path, export_root: Path, relpath: str) -> str:
    if not relpath:
        return "fail"
    if Path(relpath).is_absolute():
        return "fail"
    resolved = resolve_packet_artifact(manifest_path, relpath)
    return "pass" if is_under_root(resolved, export_root) else "fail"


def inspect_bodypaint_packet(packet: dict[str, Any], export_root: Path) -> dict[str, Any]:
    manifest_path = _manifest_path(packet, export_root)
    manifest_exists = manifest_path.exists() and manifest_path.is_file()
    payload = load_json(manifest_path) if manifest_exists else {}
    source_model = payload.get("source_model") or {}
    expected_outputs = payload.get("expected_outputs") or {}
    source_available = bool(source_model.get("available"))
    source_path = resolve_packet_artifact(manifest_path, source_model.get("staged_path")) if manifest_exists else None
    source_provider = str(source_model.get("provider") or "").strip()

    checks = [
        {
            "name": "manifest_exists",
            "status": "pass" if manifest_exists else "fail",
            "details": {"manifest_path": str(manifest_path)},
        },
        {
            "name": "source_provider_supported",
            "status": "pass" if (not manifest_exists or not source_provider or source_provider in SUPPORTED_SOURCE_PROVIDERS) else "fail",
            "details": {"provider": source_provider},
        },
        {
            "name": "source_model_ready",
            "status": (
                "fail"
                if source_available and (source_path is None or not source_path.exists() or not is_under_root(source_path, export_root))
                else "attention"
                if not source_available
                else "pass"
            ),
            "details": {
                "available": source_available,
                "staged_path": str(source_path) if source_path else "",
            },
        },
    ]

    for key in REQUIRED_OUTPUT_KEYS:
        relpath = str(expected_outputs.get(key) or "").strip()
        checks.append(
            {
                "name": f"expected_output:{key}",
                "status": _output_path_status(manifest_path, export_root, relpath) if manifest_exists else "fail",
                "details": {"path": relpath},
            }
        )
    for key in OPTIONAL_OUTPUT_KEYS:
        relpath = str(expected_outputs.get(key) or "").strip()
        checks.append(
            {
                "name": f"optional_output:{key}",
                "status": "pass" if not manifest_exists or _output_path_status(manifest_path, export_root, relpath) == "pass" else "attention",
                "details": {"path": relpath},
            }
        )

    failures = [item["name"] for item in checks if item["status"] == "fail"]
    attentions = [item["name"] for item in checks if item["status"] == "attention"]
    if failures:
        status = "fail"
    elif attentions:
        status = "attention"
    else:
        status = "pass"

    return {
        "sample_id": str(packet.get("sample_id") or ""),
        "package_id": str(packet.get("package_id") or ""),
        "status": status,
        "ready_for_consumer": not failures and source_available,
        "checks": checks,
        "failures": failures,
        "attentions": attentions,
    }


def build_bodypaint_packet_check_report(*, registry_payload: dict[str, Any], export_root: Path) -> dict[str, Any]:
    packets = [dict(packet) for packet in registry_payload.get("packets") or []]
    items = [inspect_bodypaint_packet(packet, export_root) for packet in packets]
    fail_count = sum(1 for item in items if item["status"] == "fail")
    attention_count = sum(1 for item in items if item["status"] == "attention")
    ready_count = sum(1 for item in items if item["ready_for_consumer"])
    if not items:
        status = "attention"
    elif fail_count:
        status = "fail"
    elif attention_count:
        status = "attention"
    else:
        status = "pass"
    return {
        "generated_at_utc": now_iso(),
        "status": status,
        "counts": {
            "packet_count": len(items),
            "ready_packet_count": ready_count,
            "fail_count": fail_count,
            "attention_count": attention_count,
        },
        "items": items,
    }


def write_bodypaint_packet_check_report(*, registry_payload: dict[str, Any], export_root: Path, output_path: Path) -> dict[str, Any]:
    payload = build_bodypaint_packet_check_report(registry_payload=registry_payload, export_root=export_root)
    write_json(output_path, payload)
    return payload
