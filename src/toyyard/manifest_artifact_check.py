from __future__ import annotations

import json
from pathlib import Path

from toyyard.util import now_iso, write_json


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def resolve_manifest_artifact(manifest_path: Path, artifact_path: str | None) -> Path | None:
    if not artifact_path:
        return None
    candidate = Path(artifact_path).expanduser()
    if candidate.is_absolute():
        if candidate.exists():
            return candidate.resolve()
        local_candidate = candidate
    else:
        local_candidate = (manifest_path.parent / candidate)
        if local_candidate.exists():
            return local_candidate.resolve()

    local_sibling = manifest_path.parent / candidate.name
    if local_sibling.exists():
        return local_sibling.resolve()
    return local_candidate.resolve(strict=False)


def is_under_root(path: Path | None, root: Path | None) -> bool:
    if path is None or root is None:
        return False
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except Exception:
        return False


def inspect_manifest_artifacts(manifest_path: Path, export_root: Path | None = None) -> dict:
    manifest = load_json(manifest_path)
    issues: list[str] = []

    output_fbx_raw = manifest.get("output_fbx")
    output_fbx_resolved = resolve_manifest_artifact(manifest_path, output_fbx_raw)
    output_fbx_exists = bool(output_fbx_resolved and output_fbx_resolved.exists())
    output_fbx_in_export_root = is_under_root(output_fbx_resolved, export_root)
    if not output_fbx_exists:
        issues.append("output_fbx_missing")
    if output_fbx_raw and not output_fbx_in_export_root:
        issues.append("output_fbx_outside_export_root")

    source_file_raw = manifest.get("source_file")
    source_file_resolved = resolve_manifest_artifact(manifest_path, source_file_raw)
    source_file_exists = bool(source_file_resolved and source_file_resolved.exists())
    source_file_in_export_root = is_under_root(source_file_resolved, export_root)
    if source_file_raw and not source_file_in_export_root:
        issues.append("source_file_external_reference")

    textures = []
    missing_texture_count = 0
    external_texture_count = 0
    for index, texture in enumerate(manifest.get("textures") or [], start=1):
        if isinstance(texture, str):
            texture = {
                "original_path": texture,
                "relocated_path": texture,
                "material_name": "",
            }
        relocated_raw = texture.get("relocated_path")
        original_raw = texture.get("original_path")
        relocated_resolved = resolve_manifest_artifact(manifest_path, relocated_raw)
        original_resolved = resolve_manifest_artifact(manifest_path, original_raw)
        relocated_exists = bool(relocated_resolved and relocated_resolved.exists())
        original_exists = bool(original_resolved and original_resolved.exists())
        chosen_resolved = relocated_resolved if relocated_exists else original_resolved
        chosen_exists = relocated_exists or original_exists
        chosen_in_export_root = is_under_root(chosen_resolved, export_root)
        texture_issues: list[str] = []
        if not chosen_exists:
            texture_issues.append("texture_artifact_missing")
            missing_texture_count += 1
        if (relocated_raw or original_raw) and not chosen_in_export_root:
            texture_issues.append("texture_reference_outside_export_root")
            external_texture_count += 1
        textures.append(
            {
                "index": index,
                "material_name": texture.get("material_name") or texture.get("normalized_name") or "",
                "relocated_path_raw": relocated_raw,
                "relocated_path_resolved": str(relocated_resolved) if relocated_resolved else "",
                "relocated_exists": relocated_exists,
                "original_path_raw": original_raw,
                "original_path_resolved": str(original_resolved) if original_resolved else "",
                "original_exists": original_exists,
                "chosen_path": str(chosen_resolved) if chosen_resolved else "",
                "chosen_exists": chosen_exists,
                "chosen_in_export_root": chosen_in_export_root,
                "issues": texture_issues,
            }
        )

    if missing_texture_count:
        issues.append(f"missing_texture_artifacts:{missing_texture_count}")
    if external_texture_count:
        issues.append(f"external_texture_references:{external_texture_count}")

    blocking_issues = [
        issue
        for issue in issues
        if issue != "source_file_external_reference"
    ]

    consumer_contract_path = manifest_path.parent / "ue_consumer_contract.json"
    import_report_path = manifest_path.parent / "ue_import_report.local.json"
    validation_report_path = manifest_path.parent / "ue_validation_report.local.json"

    return {
        "status": "pass" if not blocking_issues else "attention",
        "manifest_path": str(manifest_path),
        "package_id": manifest.get("package_id"),
        "sample_id": manifest.get("sample_id"),
        "export_root": str(export_root) if export_root else "",
        "source_file": {
            "raw": source_file_raw,
            "resolved": str(source_file_resolved) if source_file_resolved else "",
            "exists": source_file_exists,
            "in_export_root": source_file_in_export_root,
        },
        "output_fbx": {
            "raw": output_fbx_raw,
            "resolved": str(output_fbx_resolved) if output_fbx_resolved else "",
            "exists": output_fbx_exists,
            "in_export_root": output_fbx_in_export_root,
        },
        "textures": textures,
        "texture_count_declared": len(manifest.get("textures") or []),
        "sidecar_reports": {
            "consumer_contract_path": str(consumer_contract_path),
            "consumer_contract_exists": consumer_contract_path.exists(),
            "ue_import_report_path": str(import_report_path),
            "ue_import_report_exists": import_report_path.exists(),
            "ue_validation_report_path": str(validation_report_path),
            "ue_validation_report_exists": validation_report_path.exists(),
        },
        "issues": issues,
    }


def build_manifest_artifact_check_report(*, manifest_paths: list[Path], export_root: Path | None) -> dict:
    per_manifest = [inspect_manifest_artifacts(path, export_root=export_root) for path in manifest_paths]
    counts = {
        "manifest_count": len(per_manifest),
        "pass_count": sum(1 for entry in per_manifest if entry["status"] == "pass"),
        "attention_count": sum(1 for entry in per_manifest if entry["status"] != "pass"),
        "output_fbx_missing_count": sum(1 for entry in per_manifest if "output_fbx_missing" in entry["issues"]),
        "source_file_external_reference_count": sum(1 for entry in per_manifest if "source_file_external_reference" in entry["issues"]),
        "missing_texture_manifest_count": sum(
            1 for entry in per_manifest if any(issue.startswith("missing_texture_artifacts:") for issue in entry["issues"])
        ),
        "external_texture_manifest_count": sum(
            1 for entry in per_manifest if any(issue.startswith("external_texture_references:") for issue in entry["issues"])
        ),
    }
    return {
        "tool_name": "toy-yard",
        "report_kind": "toy_yard_manifest_artifact_check",
        "generated_at_utc": now_iso(),
        "status": "pass" if counts["attention_count"] == 0 else "attention",
        "export_root": str(export_root) if export_root else "",
        "counts": counts,
        "per_manifest_results": per_manifest,
    }


def write_manifest_artifact_check_report(*, manifest_paths: list[Path], export_root: Path | None, output_path: Path) -> dict:
    payload = build_manifest_artifact_check_report(manifest_paths=manifest_paths, export_root=export_root)
    write_json(output_path, payload)
    return payload
