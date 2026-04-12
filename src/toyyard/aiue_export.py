from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from toyyard.manifest_artifact_check import write_manifest_artifact_check_report
from toyyard.paths import ProjectPaths
from toyyard.util import now_iso, normalize_ext, write_json
from toyyard.warehouse import (
    ensure_artifact,
    json_loads,
    list_roots,
    package_aliases,
    resolve_sample,
    sample_aliases,
    sample_artifacts,
    sample_packages,
    sample_sources,
)


EXPORT_CONTRACT_VERSION = "toy-yard-pmx-0.3"
EXPORTER_VERSION = "toyyard-t1.5"


def _load_json_file(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return {}


def _artifact_metadata(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {}
    return json_loads(row["metadata_json"])


def _artifact_payload(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {}
    path_payload = _load_json_file(Path(row["path"]))
    if path_payload:
        return path_payload

    metadata = _artifact_metadata(row)
    wrapped_payload = metadata.get("payload")
    if isinstance(wrapped_payload, dict) and wrapped_payload:
        return wrapped_payload
    if metadata:
        original_path = str(metadata.get("toy_yard_original_path") or "").strip()
        original_payload = _load_json_file(Path(original_path)) if original_path else {}
        if original_payload:
            return original_payload
    return metadata if metadata else {}


def _latest_artifact(rows: list[sqlite3.Row], *, stage: str, artifact_kind: str) -> sqlite3.Row | None:
    matches = [row for row in rows if row["stage"] == stage and row["artifact_kind"] == artifact_kind]
    if not matches:
        return None
    return sorted(matches, key=lambda row: (row["created_at"], row["id"]), reverse=True)[0]


def _package_artifacts(conn: sqlite3.Connection, package_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM artifacts WHERE owner_type = 'package' AND owner_id = ? ORDER BY created_at, id",
        (package_id,),
    ).fetchall()


def _source_refs(conn: sqlite3.Connection, sample_id: int) -> list[dict[str, Any]]:
    return [
        {
            "source_path": row["source_path"],
            "managed_path": row["managed_path"],
            "source_kind": row["source_kind"],
            "ext": row["ext"],
            "size_bytes": row["size_bytes"],
            "metadata": json_loads(row["metadata_json"]),
        }
        for row in sample_sources(conn, sample_id)
    ]


def _classify_registry_role(package: sqlite3.Row) -> str:
    package_role = str(package["package_role"] or "").lower()
    content_bucket = str(package["content_bucket"] or "").lower()
    contract_type = str(package["contract_type"] or "").lower()
    if "weapon" in package_role or "weapon" in content_bucket or "weapon" in contract_type:
        return "weapon"
    if "character" in package_role or "host" in package_role or content_bucket in {"character", "characters", "bundle"}:
        return "character"
    return "other"


def _pair_candidates(packages: list[sqlite3.Row]) -> list[tuple[sqlite3.Row, sqlite3.Row]]:
    characters = [package for package in packages if _classify_registry_role(package) == "character" and package["consumer_ready"]]
    weapons = [package for package in packages if _classify_registry_role(package) == "weapon" and package["consumer_ready"]]
    return [(character, weapon) for character in characters for weapon in weapons]


def _root_paths(conn: sqlite3.Connection) -> list[Path]:
    return [Path(row["path"]) for row in list_roots(conn)]


def _resolve_against_roots(raw_path: str | None, roots: list[Path]) -> Path | None:
    if not raw_path:
        return None
    candidate = Path(raw_path).expanduser()
    if candidate.exists():
        return candidate.resolve()
    parts = list(candidate.parts)
    if not parts:
        return None
    lowered_parts = [part.lower() for part in parts]
    for root in roots:
        root_name = root.name.lower()
        if root_name in lowered_parts:
            index = lowered_parts.index(root_name)
            suffix = Path(*parts[index + 1 :]) if index + 1 < len(parts) else Path()
            remapped = root / suffix
            if remapped.exists():
                return remapped.resolve()
        for index in range(len(parts)):
            suffix = Path(*parts[index:])
            remapped = root / suffix
            if remapped.exists():
                return remapped.resolve()
    return None


def _resolve_with_source_refs(raw_path: str | None, roots: list[Path], source_references: list[dict[str, Any]]) -> Path | None:
    resolved = _resolve_against_roots(raw_path, roots)
    if resolved is not None:
        return resolved
    for source_ref in source_references:
        metadata = source_ref.get("metadata") or {}
        resolved_path = str(metadata.get("resolved_path") or "").strip()
        if resolved_path:
            candidate = Path(resolved_path)
            if candidate.exists():
                return candidate.resolve()
        source_relative_path = str(metadata.get("source_relative_path") or "").strip()
        if not source_relative_path:
            continue
        for root in roots:
            candidate = root / source_relative_path
            if candidate.exists():
                return candidate.resolve()
    return None


def _normalize_texture_entries(textures: list[Any]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for index, entry in enumerate(textures, start=1):
        if isinstance(entry, str):
            normalized.append(
                {
                    "material_name": "",
                    "normalized_name": Path(entry).name or f"texture_{index:03d}",
                    "original_path": entry,
                    "relocated_path": entry,
                }
            )
            continue
        normalized.append(dict(entry))
    return normalized


def _split_output_for_package(manifest_payload: dict[str, Any], package: sqlite3.Row) -> str | None:
    split_outputs = manifest_payload.get("split_outputs") or {}
    if not isinstance(split_outputs, dict):
        return None
    role = _classify_registry_role(package)
    if role == "weapon":
        for key in ("weapon", "weapons"):
            value = split_outputs.get(key)
            if isinstance(value, str) and value.strip():
                return value
    if role == "character":
        for key in ("body", "character", "characters", "host"):
            value = split_outputs.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return None


def _package_manifest_candidates(package: sqlite3.Row) -> list[str]:
    metadata = json_loads(package["metadata_json"])
    candidates = []
    for key in ("manifest_path", "report_path"):
        value = str(metadata.get(key) or "").strip()
        if value:
            candidates.append(value)
    return candidates


def _package_source_relative_path(package: sqlite3.Row) -> str:
    metadata = json_loads(package["metadata_json"])
    return str(metadata.get("source_relative_path") or "").strip()


def _package_alias_values(package: sqlite3.Row, package_alias_rows: list[sqlite3.Row]) -> set[str]:
    values = {str(package["canonical_package_id"] or "").strip()}
    values.update(str(row["alias_value"] or "").strip() for row in package_alias_rows)
    return {value for value in values if value}


def _is_conversion_manifest_payload(payload: dict[str, Any]) -> bool:
    if not payload:
        return False
    output_fbx = str(payload.get("output_fbx") or "").strip()
    if output_fbx:
        return True
    split_outputs = payload.get("split_outputs") or {}
    if isinstance(split_outputs, dict):
        for value in split_outputs.values():
            if isinstance(value, str) and value.strip():
                return True
    textures = payload.get("textures")
    materials = payload.get("materials")
    if isinstance(textures, list) and textures and isinstance(materials, list):
        return True
    return False


def _match_package_artifact_score(
    *,
    package: sqlite3.Row,
    package_alias_rows: list[sqlite3.Row],
    payload: dict[str, Any] | None,
    artifact_path: Path,
) -> int:
    score = 0
    alias_values = _package_alias_values(package, package_alias_rows)
    payload = payload or {}
    payload_package_id = str(payload.get("package_id") or "").strip()
    if payload_package_id and payload_package_id in alias_values:
        score += 100

    package_source_relative = _package_source_relative_path(package).replace("\\", "/").casefold()
    payload_source_relative = str(payload.get("source_relative_path") or "").replace("\\", "/").casefold()
    if package_source_relative and payload_source_relative and package_source_relative == payload_source_relative:
        score += 80

    package_stem = Path(_package_source_relative_path(package)).stem.casefold()
    parent_name = artifact_path.parent.name.casefold()
    if package_stem and parent_name and package_stem == parent_name:
        score += 40

    output_stem = Path(str(payload.get("output_fbx") or "")).stem.casefold()
    if package_stem and output_stem and package_stem == output_stem:
        score += 20

    package_role = _classify_registry_role(package)
    payload_role = str(
        (payload.get("package_hints") or {}).get("source_role")
        or payload.get("package_role")
        or ""
    ).casefold()
    if package_role != "other" and payload_role and package_role in payload_role:
        score += 10

    return score


def _best_sample_stage_artifact(
    sample_level_artifacts: list[sqlite3.Row],
    *,
    package: sqlite3.Row,
    package_alias_rows: list[sqlite3.Row],
    stage: str,
    artifact_kind: str,
) -> sqlite3.Row | None:
    candidates: list[tuple[int, str, sqlite3.Row]] = []
    for artifact in sample_level_artifacts:
        if artifact["stage"] != stage or artifact["artifact_kind"] != artifact_kind:
            continue
        payload = _load_json_file(Path(artifact["path"])) if artifact_kind == "conversion_manifest" else None
        if artifact_kind == "conversion_manifest" and not _is_conversion_manifest_payload(payload):
            continue
        score = _match_package_artifact_score(
            package=package,
            package_alias_rows=package_alias_rows,
            payload=payload,
            artifact_path=Path(artifact["path"]),
        )
        if score > 0:
            candidates.append((score, str(artifact["path"]), artifact))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return candidates[0][2]


def _resolve_package_manifest_payload(
    package: sqlite3.Row,
    *,
    package_alias_rows: list[sqlite3.Row],
    roots: list[Path],
    sample_level_artifacts: list[sqlite3.Row],
    fallback_payload: dict[str, Any],
) -> tuple[dict[str, Any], str, str]:
    for raw_path in _package_manifest_candidates(package):
        resolved_path = _resolve_against_roots(raw_path, roots)
        payload = _load_json_file(resolved_path)
        if _is_conversion_manifest_payload(payload):
            return payload, raw_path, str(resolved_path)
    sample_manifest = _best_sample_stage_artifact(
        sample_level_artifacts,
        package=package,
        package_alias_rows=package_alias_rows,
        stage="conversion",
        artifact_kind="conversion_manifest",
    )
    if sample_manifest is not None:
        resolved_path = Path(sample_manifest["path"])
        payload = _load_json_file(resolved_path)
        if _is_conversion_manifest_payload(payload):
            return payload, str(resolved_path), str(resolved_path)
    return dict(fallback_payload), "", ""


def _safe_copy(source: Path | None, destination: Path) -> Path | None:
    if source is None or not source.exists() or not source.is_file():
        return None
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return destination.resolve()


def _relative_to_package_dir(path: Path, package_dir: Path) -> str:
    try:
        return path.relative_to(package_dir).as_posix()
    except ValueError:
        return str(path)


def _pick_texture_filename(entry: dict[str, Any], resolved_source: Path | None, index: int) -> str:
    normalized_name = str(entry.get("normalized_name") or "").strip()
    if normalized_name:
        return normalized_name
    raw_path = str(entry.get("relocated_path") or entry.get("original_path") or "").strip()
    if raw_path:
        name = Path(raw_path).name
        if name:
            return name
    if resolved_source is not None:
        return resolved_source.name
    ext = normalize_ext(raw_path) or ".bin"
    return f"texture_{index:03d}{ext}"


def _stage_textures(
    manifest_payload: dict[str, Any],
    *,
    package_dir: Path,
    roots: list[Path],
    source_references: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    staged_entries: list[dict[str, Any]] = []
    staged_artifacts: list[dict[str, Any]] = []
    textures_dir = package_dir / "textures"
    for index, entry in enumerate(_normalize_texture_entries(manifest_payload.get("textures") or []), start=1):
        relocated_raw = str(entry.get("relocated_path") or "").strip()
        original_raw = str(entry.get("original_path") or "").strip()
        preferred_source = relocated_raw or original_raw
        resolved_source = _resolve_with_source_refs(preferred_source, roots, source_references)
        if resolved_source is None and original_raw and preferred_source != original_raw:
            resolved_source = _resolve_with_source_refs(original_raw, roots, source_references)
        filename = _pick_texture_filename(entry, resolved_source, index)
        staged_path = _safe_copy(resolved_source, textures_dir / filename) if resolved_source is not None else None
        portable_entry = dict(entry)
        portable_entry["original_path"] = original_raw or preferred_source
        portable_entry["relocated_path"] = (
            _relative_to_package_dir(staged_path, package_dir)
            if staged_path is not None
            else portable_entry.get("relocated_path") or portable_entry.get("original_path") or ""
        )
        staged_entries.append(portable_entry)
        if staged_path is not None:
            staged_artifacts.append(
                {
                    "path": staged_path,
                    "source_path": str(resolved_source),
                    "material_name": portable_entry.get("material_name") or portable_entry.get("normalized_name") or "",
                }
            )
    return staged_entries, staged_artifacts


def _runtime_payload(package_artifacts: list[sqlite3.Row]) -> dict[str, Any]:
    import_artifact = _latest_artifact(package_artifacts, stage="session", artifact_kind="ue_import_report")
    import_payload = _artifact_payload(import_artifact)
    if not import_payload:
        action_artifact = _latest_artifact(package_artifacts, stage="session", artifact_kind="aiue_action_result")
        action_payload = _artifact_payload(action_artifact)
        if str(action_payload.get("command") or "").strip() == "import-package":
            import_payload = dict(action_payload.get("result") or {})

    imported_assets = import_payload.get("imported_assets") or {}
    attachment = import_payload.get("attachment")
    return {
        "skeletal_mesh": str(imported_assets.get("skeletal_mesh") or ""),
        "skeleton": str(imported_assets.get("skeleton") or ""),
        "physics_asset": str(imported_assets.get("physics_asset") or ""),
        "textures": list(imported_assets.get("textures") or []),
        "attachment": attachment if isinstance(attachment, dict) else {},
    }


def _member_source_role(package: sqlite3.Row) -> str:
    role = _classify_registry_role(package)
    if role in {"character", "weapon"}:
        return role
    return "other"


def _consumer_contract(
    sample: sqlite3.Row,
    packages: list[sqlite3.Row],
    package: sqlite3.Row,
    *,
    manifest_payload: dict[str, Any],
) -> dict[str, Any]:
    role = _classify_registry_role(package)
    owners = [item["canonical_package_id"] for item in packages if _classify_registry_role(item) == "character"]
    weapons = [item["canonical_package_id"] for item in packages if _classify_registry_role(item) == "weapon"]
    preferred_owner_package_id = owners[0] if role == "weapon" and owners else None
    attachment = manifest_payload.get("attachment")
    if not isinstance(attachment, dict) and role == "weapon":
        attachment = {
            "equip_slot": "weapon",
            "preferred_target": {
                "type": "socket",
                "name": "WeaponSocket",
            },
        }
    if not isinstance(attachment, dict):
        attachment = {}

    bundle_context = {
        "is_multi_package_bundle": len(packages) > 1,
        "member_count": len(packages),
        "members": [
            {
                "package_id": member["canonical_package_id"],
                "source_role": _member_source_role(member),
                "package_role": member["package_role"],
                "is_current_package": member["id"] == package["id"],
            }
            for member in packages
        ],
        "owner_package_ids": owners,
        "weapon_package_ids": weapons,
        "linked_package_ids": [
            member["canonical_package_id"]
            for member in packages
            if member["id"] != package["id"]
        ],
        "preferred_owner_package_id": preferred_owner_package_id,
    }
    preferred_attach_target = attachment.get("preferred_target") or attachment.get("preferred_attach_target") or {}
    return {
        "export_contract_version": EXPORT_CONTRACT_VERSION,
        "exporter_version": EXPORTER_VERSION,
        "source": "toy-yard export",
        "sample_id": sample["canonical_sample_id"],
        "package_id": package["canonical_package_id"],
        "package_role": package["package_role"],
        "contract_type": package["contract_type"],
        "consumer_ready": bool(package["consumer_ready"]),
        "preferred_owner_package_id": preferred_owner_package_id,
        "bundle_context": bundle_context,
        "attachment": attachment or None,
        "preferred_attach_target": preferred_attach_target or None,
    }


def _source_lineage(
    *,
    source_references: list[dict[str, Any]],
    manifest_source_path_raw: str,
    manifest_source_path_resolved: str,
    manifest_path_raw: str,
    manifest_path_resolved: str,
    output_fbx_raw: str,
    output_fbx_resolved: str,
) -> dict[str, Any]:
    return {
        "manifest_path_raw": manifest_path_raw,
        "manifest_path_resolved": manifest_path_resolved,
        "source_file_raw": manifest_source_path_raw,
        "source_file_resolved": manifest_source_path_resolved,
        "output_fbx_raw": output_fbx_raw,
        "output_fbx_resolved": output_fbx_resolved,
        "source_references": source_references,
    }


def _record_export_artifacts(
    conn: sqlite3.Connection,
    *,
    package: sqlite3.Row,
    package_manifest_path: Path,
    consumer_contract_path: Path,
    staged_fbx: Path | None,
    staged_textures: list[dict[str, Any]],
) -> None:
    ensure_artifact(
        conn,
        owner_type="package",
        owner_id=package["id"],
        stage="export",
        artifact_kind="conversion_manifest",
        path=str(package_manifest_path),
        format=".json",
        status="exported",
        metadata={"package_id": package["canonical_package_id"]},
    )
    ensure_artifact(
        conn,
        owner_type="package",
        owner_id=package["id"],
        stage="export",
        artifact_kind="ue_consumer_contract",
        path=str(consumer_contract_path),
        format=".json",
        status="exported",
        metadata={"package_id": package["canonical_package_id"]},
    )
    if staged_fbx is not None:
        ensure_artifact(
            conn,
            owner_type="package",
            owner_id=package["id"],
            stage="export",
            artifact_kind="portable_fbx",
            path=str(staged_fbx),
            format=staged_fbx.suffix.lower(),
            status="exported",
            metadata={"package_id": package["canonical_package_id"]},
        )
    for texture in staged_textures:
        ensure_artifact(
            conn,
            owner_type="package",
            owner_id=package["id"],
            stage="export",
            artifact_kind="portable_texture",
            path=str(texture["path"]),
            format=Path(texture["path"]).suffix.lower(),
            status="exported",
            metadata={
                "package_id": package["canonical_package_id"],
                "source_path": texture.get("source_path") or "",
                "material_name": texture.get("material_name") or "",
            },
        )


def export_aiue_pmx_view(
    conn: sqlite3.Connection,
    paths: ProjectPaths,
    *,
    profile: str,
    sample_ref: str,
    include_verify: bool = False,
) -> dict[str, Any]:
    sample = resolve_sample(conn, sample_ref)
    if sample is None:
        raise ValueError(f"Unknown sample reference: {sample_ref}")

    packages = sample_packages(conn, sample["id"])
    if not packages:
        raise ValueError(f"Sample has no packages: {sample['canonical_sample_id']}")

    roots = _root_paths(conn)
    sample_level_artifacts = sample_artifacts(conn, sample["id"])
    fallback_manifest_artifact = _latest_artifact(sample_level_artifacts, stage="conversion", artifact_kind="conversion_manifest")
    fallback_manifest_payload = _load_json_file(Path(fallback_manifest_artifact["path"])) if fallback_manifest_artifact else {}
    materials_report_artifact = _latest_artifact(sample_level_artifacts, stage="conversion", artifact_kind="materials_report")
    verify_manifest_artifact = _latest_artifact(sample_level_artifacts, stage="verification", artifact_kind="conversion_manifest")

    profile_dir = paths.aiue_pmx_profile_dir(profile)
    if profile_dir.exists():
        shutil.rmtree(profile_dir)
    conversion_dir = paths.aiue_pmx_conversion_dir(profile)
    summary_dir = paths.aiue_pmx_summary_dir(profile)
    workspace_views_dir = paths.aiue_pmx_workspace_views_dir(profile)
    conversion_dir.mkdir(parents=True, exist_ok=True)
    summary_dir.mkdir(parents=True, exist_ok=True)
    workspace_views_dir.mkdir(parents=True, exist_ok=True)

    source_references = _source_refs(conn, sample["id"])
    sample_alias_rows = sample_aliases(conn, sample["id"])
    exported_manifests: list[dict[str, Any]] = []
    summary_successes: list[dict[str, Any]] = []
    summary_failures: list[dict[str, Any]] = []

    runtime_by_package: dict[str, dict[str, Any]] = {}
    consumer_contracts: dict[str, dict[str, Any]] = {}
    package_manifest_paths: dict[str, Path] = {}
    package_export_roots: dict[str, Path] = {}
    package_manifest_exports: dict[str, dict[str, Any]] = {}

    for package in packages:
        package_alias_rows = package_aliases(conn, package["id"])
        package_artifacts = _package_artifacts(conn, package["id"])
        runtime_payload = _runtime_payload(package_artifacts)
        runtime_by_package[package["canonical_package_id"]] = runtime_payload

        package_manifest_payload, manifest_path_raw, manifest_path_resolved = _resolve_package_manifest_payload(
            package,
            package_alias_rows=package_alias_rows,
            roots=roots,
            sample_level_artifacts=sample_level_artifacts,
            fallback_payload=fallback_manifest_payload,
        )
        manifest_payload = dict(package_manifest_payload)
        selected_output_fbx = _split_output_for_package(manifest_payload, package) or manifest_payload.get("output_fbx")
        resolved_output_fbx = _resolve_with_source_refs(str(selected_output_fbx or ""), roots, source_references)
        package_materials_report_artifact = _best_sample_stage_artifact(
            sample_level_artifacts,
            package=package,
            package_alias_rows=package_alias_rows,
            stage="conversion",
            artifact_kind="materials_report",
        )

        package_dir = paths.aiue_pmx_package_dir(profile, package["canonical_package_id"])
        package_dir.mkdir(parents=True, exist_ok=True)
        package_export_roots[package["canonical_package_id"]] = package_dir
        staged_fbx = _safe_copy(
            resolved_output_fbx,
            package_dir / (resolved_output_fbx.name if resolved_output_fbx is not None else Path(str(selected_output_fbx or "model.fbx")).name),
        )

        staged_textures, staged_texture_artifacts = _stage_textures(
            manifest_payload,
            package_dir=package_dir,
            roots=roots,
            source_references=source_references,
        )

        manifest_source_raw = str(manifest_payload.get("source_file") or "")
        manifest_source_resolved = _resolve_with_source_refs(manifest_source_raw, roots, source_references)

        consumer_contract = _consumer_contract(
            sample,
            packages,
            package,
            manifest_payload=manifest_payload,
        )
        consumer_contracts[package["canonical_package_id"]] = consumer_contract
        consumer_contract_path = paths.aiue_pmx_package_consumer_contract_path(profile, package["canonical_package_id"])
        write_json(consumer_contract_path, consumer_contract)

        manifest_payload["export_contract_version"] = EXPORT_CONTRACT_VERSION
        manifest_payload["exporter_version"] = EXPORTER_VERSION
        manifest_payload["source"] = "toy-yard export"
        manifest_payload["package_id"] = package["canonical_package_id"]
        manifest_payload["sample_id"] = sample["canonical_sample_id"]
        manifest_payload["package_role"] = package["package_role"]
        manifest_payload["content_bucket"] = package["content_bucket"]
        manifest_payload["contract_type"] = package["contract_type"]
        manifest_payload["consumer_ready"] = bool(package["consumer_ready"])
        manifest_payload["warehouse_status"] = package["warehouse_status"]
        manifest_payload["source_references"] = source_references
        manifest_payload["materials_report_path"] = (
            package_materials_report_artifact["path"]
            if package_materials_report_artifact
            else materials_report_artifact["path"] if materials_report_artifact else ""
        )
        manifest_payload["verify_manifest_path"] = verify_manifest_artifact["path"] if include_verify and verify_manifest_artifact else ""
        manifest_payload["source_lineage"] = _source_lineage(
            source_references=source_references,
            manifest_source_path_raw=manifest_source_raw,
            manifest_source_path_resolved=str(manifest_source_resolved) if manifest_source_resolved else "",
            manifest_path_raw=manifest_path_raw,
            manifest_path_resolved=manifest_path_resolved,
            output_fbx_raw=str(selected_output_fbx or ""),
            output_fbx_resolved=str(resolved_output_fbx) if resolved_output_fbx else "",
        )
        manifest_payload["export_artifacts"] = {
            "manifest_path": "manifest.json",
            "output_fbx": _relative_to_package_dir(staged_fbx, package_dir) if staged_fbx is not None else "",
            "textures_root": "textures" if staged_textures else "",
            "textures": [
                {
                    "path": entry["relocated_path"],
                    "material_name": entry.get("material_name") or entry.get("normalized_name") or "",
                }
                for entry in staged_textures
            ],
        }
        if staged_fbx is not None:
            manifest_payload["output_fbx"] = _relative_to_package_dir(staged_fbx, package_dir)
        manifest_payload["textures"] = staged_textures
        manifest_payload["toy_yard"] = {
            "sample_id": sample["canonical_sample_id"],
            "package_id": package["canonical_package_id"],
            "sample_aliases": [dict(row) for row in sample_alias_rows],
            "package_aliases": [dict(row) for row in package_alias_rows],
            "package_metadata": json_loads(package["metadata_json"]),
            "sample_metadata": json_loads(sample["metadata_json"]),
        }

        package_manifest_path = paths.aiue_pmx_package_manifest_path(profile, package["canonical_package_id"])
        write_json(package_manifest_path, manifest_payload)
        package_manifest_paths[package["canonical_package_id"]] = package_manifest_path
        package_manifest_exports[package["canonical_package_id"]] = manifest_payload
        _record_export_artifacts(
            conn,
            package=package,
            package_manifest_path=package_manifest_path,
            consumer_contract_path=consumer_contract_path,
            staged_fbx=staged_fbx,
            staged_textures=staged_texture_artifacts,
        )

        exported_manifests.append(
            {
                "package_id": package["canonical_package_id"],
                "manifest_path": str(package_manifest_path),
                "consumer_ready": bool(package["consumer_ready"]),
                "status": "ready" if staged_fbx is not None else "export_failed",
            }
        )
        summary_row = {
            "package_id": package["canonical_package_id"],
            "sample_id": sample["canonical_sample_id"],
            "manifest_path": str(package_manifest_path),
            "manifest_path_local": str(package_manifest_path),
            "package_export_root": str(package_dir),
            "output_fbx": manifest_payload["export_artifacts"]["output_fbx"],
            "textures_root": manifest_payload["export_artifacts"]["textures_root"],
            "source_relative_path": _package_source_relative_path(package),
            "package_role": package["package_role"],
            "content_bucket": package["content_bucket"],
            "contract_type": package["contract_type"],
            "consumer_ready": bool(package["consumer_ready"]),
            "status": "ready" if staged_fbx is not None else "export_failed",
            "score": json_loads(package["metadata_json"]).get("score"),
            "verify_artifacts": [dict(row) for row in package_artifacts if row["stage"] == "verification"] if include_verify else [],
        }
        if staged_fbx is not None:
            summary_successes.append(summary_row)
        else:
            summary_failures.append(
                {
                    **summary_row,
                    "reason": "portable_output_fbx_missing",
                }
            )

    summary_payload = {
        "generated_at_utc": now_iso(),
        "export_contract_version": EXPORT_CONTRACT_VERSION,
        "exporter_version": EXPORTER_VERSION,
        "source": "toy-yard export",
        "suite_name": f"toy-yard:{profile}",
        "conversion_root": str(conversion_dir),
        "summary_root": str(summary_dir),
        "profile": profile,
        "sample_id": sample["canonical_sample_id"],
        "successes": summary_successes,
        "failures": summary_failures,
        "counts": {
            "requested_items": len(packages),
            "completed_items": len(summary_successes),
            "successful_items": len(summary_successes),
            "failed_items": len(summary_failures),
        },
        "content_bucket_counts": {
            key: sum(1 for entry in summary_successes if entry["content_bucket"] == key)
            for key in sorted({entry["content_bucket"] for entry in summary_successes})
        },
        "package_role_counts": {
            key: sum(1 for entry in summary_successes if entry["package_role"] == key)
            for key in sorted({entry["package_role"] for entry in summary_successes})
        },
        "contract_type_counts": {
            key: sum(1 for entry in summary_successes if entry["contract_type"] == key)
            for key in sorted({entry["contract_type"] for entry in summary_successes})
        },
        "consumer_ready_counts": {
            "ready": sum(1 for entry in summary_successes if entry["consumer_ready"]),
            "not_ready": sum(1 for entry in summary_successes if not entry["consumer_ready"]),
        },
    }
    summary_path = paths.aiue_pmx_summary_path(profile)
    write_json(summary_path, summary_payload)
    ensure_artifact(
        conn,
        owner_type="sample",
        owner_id=sample["id"],
        stage="export",
        artifact_kind="ue_suite_summary",
        path=str(summary_path),
        format=".json",
        status="exported",
        metadata={"profile": profile},
    )

    package_index = {}
    character_entries = []
    weapon_entries = []
    for package in packages:
        package_id = package["canonical_package_id"]
        runtime_payload = runtime_by_package[package_id]
        manifest_export = package_manifest_exports[package_id]
        package_index[package_id] = {
            "package_id": package_id,
            "sample_id": sample["canonical_sample_id"],
            "package_role": package["package_role"],
            "content_bucket": package["content_bucket"],
            "contract_type": package["contract_type"],
            "consumer_ready": bool(package["consumer_ready"]),
            "manifest_path": str(package_manifest_paths[package_id]),
            "package_export_root": str(package_export_roots[package_id]),
            "output_fbx": manifest_export.get("export_artifacts", {}).get("output_fbx", ""),
            "textures_root": manifest_export.get("export_artifacts", {}).get("textures_root", ""),
        }
        entry = {
            "package_id": package_id,
            "sample_id": sample["canonical_sample_id"],
            "consumer_ready": bool(package["consumer_ready"]),
            "package_role": package["package_role"],
            "content_bucket": package["content_bucket"],
            "contract_type": package["contract_type"],
        }
        role = _classify_registry_role(package)
        if role == "character":
            entry.update(
                {
                    "skeletal_mesh": runtime_payload["skeletal_mesh"],
                    "skeleton": runtime_payload["skeleton"],
                    "physics_asset": runtime_payload["physics_asset"],
                }
            )
            character_entries.append(entry)
        elif role == "weapon":
            entry.update(
                {
                    "skeletal_mesh": runtime_payload["skeletal_mesh"],
                }
            )
            weapon_entries.append(entry)

    ready_pairs = []
    for character, weapon in _pair_candidates(packages):
        character_runtime = runtime_by_package[character["canonical_package_id"]]
        weapon_runtime = runtime_by_package[weapon["canonical_package_id"]]
        attachment = weapon_runtime.get("attachment") or consumer_contracts[weapon["canonical_package_id"]].get("attachment") or {}
        preferred_attach_target = attachment.get("preferred_target") or attachment.get("preferred_attach_target") or consumer_contracts[weapon["canonical_package_id"]].get("preferred_attach_target") or {}
        ready_pairs.append(
            {
                "character_package_id": character["canonical_package_id"],
                "weapon_package_id": weapon["canonical_package_id"],
                "sample_id": sample["canonical_sample_id"],
                "character_skeletal_mesh": character_runtime["skeletal_mesh"],
                "weapon_skeletal_mesh": weapon_runtime["skeletal_mesh"],
                "equip_slot": attachment.get("equip_slot"),
                "preferred_attach_target": preferred_attach_target or None,
            }
        )

    registry_payload = {
        "generated_at_utc": now_iso(),
        "export_contract_version": EXPORT_CONTRACT_VERSION,
        "exporter_version": EXPORTER_VERSION,
        "source": "toy-yard export",
        "sample_id": sample["canonical_sample_id"],
        "characters": character_entries,
        "weapons": weapon_entries,
        "ready_pairs": ready_pairs,
        "bundles": [
            {
                "sample_id": sample["canonical_sample_id"],
                "package_ids": [package["canonical_package_id"] for package in packages],
                "equippable": bool(ready_pairs),
            }
        ],
        "package_index": package_index,
        "counts": {
            "packages": len(packages),
            "characters": len(character_entries),
            "weapons": len(weapon_entries),
            "ready_pairs": len(ready_pairs),
        },
    }
    registry_path = paths.aiue_pmx_registry_path(profile)
    write_json(registry_path, registry_payload)
    ensure_artifact(
        conn,
        owner_type="sample",
        owner_id=sample["id"],
        stage="export",
        artifact_kind="ue_equipment_registry",
        path=str(registry_path),
        format=".json",
        status="exported",
        metadata={"profile": profile},
    )

    manifest_check_path = paths.aiue_pmx_manifest_artifact_check_path(profile)
    manifest_check_payload = write_manifest_artifact_check_report(
        manifest_paths=list(package_manifest_paths.values()),
        export_root=profile_dir,
        output_path=manifest_check_path,
    )
    ensure_artifact(
        conn,
        owner_type="sample",
        owner_id=sample["id"],
        stage="export",
        artifact_kind="manifest_artifact_check",
        path=str(manifest_check_path),
        format=".json",
        status=manifest_check_payload["status"],
        metadata={"profile": profile, "counts": manifest_check_payload["counts"]},
    )

    if include_verify and verify_manifest_artifact:
        verify_index = {
            "generated_at_utc": now_iso(),
            "sample_id": sample["canonical_sample_id"],
            "verify_manifest_path": verify_manifest_artifact["path"],
        }
        write_json(summary_dir / "verification_index.json", verify_index)

    workspace_view_payload = {
        "version": "0.3",
        "export_contract_version": EXPORT_CONTRACT_VERSION,
        "exporter_version": EXPORTER_VERSION,
        "profile": profile,
        "toy_yard_root": str(paths.root),
        "toy_yard_pmx_view_root": str(profile_dir),
        "conversion_root": str(conversion_dir),
        "summary_root": str(summary_dir),
        "summary_path": str(summary_path),
        "registry_path": str(registry_path),
        "manifest_artifact_check_path": str(manifest_check_path),
        "sample_id": sample["canonical_sample_id"],
        "package_ids": [package["canonical_package_id"] for package in packages],
        "source_aliases": [dict(row) for row in sample_alias_rows],
    }
    workspace_view_path = paths.aiue_pmx_workspace_view_path(profile)
    write_json(workspace_view_path, workspace_view_payload)

    trial_workspace_payload = {
        "version": "0.1.0",
        "schema_version": 1,
        "paths": {
            "toy_yard_pmx_view_root": str(profile_dir),
            "conversion_root": str(conversion_dir),
            "dataset_root": "<optional-or-legacy-dataset-root>",
            "dataset_catalog_root": "<optional-or-legacy-dataset-catalog-root>",
            "preflight_root": "<optional-or-legacy-preflight-root>",
            "unreal_project_root": "C:\\path\\to\\UnrealProject",
            "aiue_repo_root": "${config_dir}\\..\\..\\..\\..\\..\\AiUE",
            "blender_addon_root": "C:\\path\\to\\cats-blender-plugin-master",
            "blender_python_exe": "C:\\Program Files\\Blender Foundation\\Blender 3.6\\3.6\\python\\bin\\python.exe",
            "unreal_editor_cmd": "C:\\Program Files\\Epic Games\\UE_5.7\\Engine\\Binaries\\Win64\\UnrealEditor-Cmd.exe",
            "unreal_editor_gui": "C:\\Program Files\\Epic Games\\UE_5.7\\Engine\\Binaries\\Win64\\UnrealEditor.exe",
            "asset_root": "/Game/AiUE/ImportedAssets",
            "visual_review_output_root": "C:\\path\\to\\UnrealProject\\Saved\\pmx_pipeline\\visual_review",
            "capability_probe_root": "C:\\path\\to\\UnrealProject\\Saved\\pmx_pipeline\\capabilities",
            "auto_ue_cli_output_root": "C:\\path\\to\\UnrealProject\\Saved\\pmx_pipeline\\auto_ue_cli",
        },
        "defaults": {
            "daily_suite": "smoke",
            "animation_validation_scope": "all",
        },
        "animation": {
            "validation_mode": "editor_world_scene_sweep",
            "scene_level_path": "/Game/ThirdPerson/Lvl_ThirdPerson",
            "capture_enabled": True,
            "capture_width": 1280,
            "capture_height": 720,
            "camera_distance": 320.0,
            "camera_lateral_offset": -160.0,
            "camera_height": 120.0,
            "capture_delay_seconds": 0.2,
        },
        "probe": {
            "default_mode": "cmd_nullrhi",
            "preferred_capture_mode": "editor_rendered",
            "capture_repeat_count": 3,
            "capture_finalize_wait_seconds": 8,
        },
    }
    trial_workspace_path = paths.aiue_pmx_trial_workspace_path(profile)
    write_json(trial_workspace_path, trial_workspace_payload)

    return {
        "profile": profile,
        "sample_id": sample["canonical_sample_id"],
        "packages": [package["canonical_package_id"] for package in packages],
        "has_conversion_manifest": fallback_manifest_artifact is not None,
        "summary_path": str(summary_path),
        "registry_path": str(registry_path),
        "manifest_artifact_check_path": str(manifest_check_path),
        "workspace_view_path": str(workspace_view_path),
        "trial_workspace_path": str(trial_workspace_path),
        "export_root": str(profile_dir),
        "exported_manifests": exported_manifests,
    }
