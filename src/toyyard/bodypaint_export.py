from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from toyyard.bodypaint_packet_check import write_bodypaint_packet_check_report
from toyyard.communication_signal import build_bodypaint_export_signal
from toyyard.paths import ProjectPaths
from toyyard.util import now_iso, normalize_ext, write_json
from toyyard.warehouse import (
    ensure_artifact,
    json_loads,
    package_aliases,
    resolve_sample,
    sample_aliases,
    sample_packages,
    sample_sources,
)

EXPORT_CONTRACT_VERSION = "toy-yard-bodypaint-0.2"
EXPORTER_VERSION = "toyyard-bodypaint-lane-v0.2"
EXTERNAL_MODEL_PACKET_VERSION = "toy-yard-external-model-packet-0.1"
PACKET_IDENTITY_VERSION = "toy-yard-bodypaint-packet-identity-0.1"
MODEL_EXTENSIONS = {".fbx", ".pmx", ".obj", ".glb", ".gltf"}
NON_BODYPAINT_BUCKETS = {"audio", "image", "motion", "weapons", "weapon"}
SOURCE_PROVIDER_NATIVE = "native"
SOURCE_PROVIDER_AIUE_PMX = "toy_yard_aiue_pmx"
BODYPAINT_REQUIRED_OUTPUT_KEYS = ("normalized_glb", "painted_glb", "analysis_json")
BODYPAINT_OPTIONAL_OUTPUT_KEYS = ("engineering_mask", "manual_overrides")
BODYPAINT_PACKET_IDENTITY_EXCLUDED_RELPATHS = {"summary/bodypaint_packet_identity.json"}
BODYPAINT_PACKET_IDENTITY_VOLATILE_KEYS = {"generated_at_utc"}


def _ordered_unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        normalized = str(value or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def _alias_priority(alias: dict[str, Any]) -> tuple[int, int, str, str, str]:
    external_system = str(alias.get("external_system") or "").strip()
    alias_key = str(alias.get("alias_key") or "").strip()
    alias_value = str(alias.get("alias_value") or "").strip()
    return (
        1 if external_system == "toy_yard" else 0,
        1 if alias_key == "package_id" else 0,
        external_system,
        alias_key,
        alias_value,
    )


def _safe_profile_reset(paths: ProjectPaths, profile: str) -> Path:
    profile_dir = paths.bodypaint_profile_dir(profile)
    if profile_dir.exists():
        publish_root = paths.bodypaint_publish_dir.resolve()
        resolved_profile = profile_dir.resolve()
        if publish_root not in resolved_profile.parents:
            raise ValueError(f"Refusing to remove non-BodyPaint publish path: {resolved_profile}")
        shutil.rmtree(profile_dir)
    return profile_dir


def _resolve_package(conn: sqlite3.Connection, package_ref: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM packages WHERE canonical_package_id = ?",
        (package_ref,),
    ).fetchone()


def _package_artifacts(conn: sqlite3.Connection, package_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM artifacts WHERE owner_type = 'package' AND owner_id = ? ORDER BY created_at, id",
        (package_id,),
    ).fetchall()


def _sample_artifacts(conn: sqlite3.Connection, sample_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM artifacts WHERE owner_type = 'sample' AND owner_id = ? ORDER BY created_at, id",
        (sample_id,),
    ).fetchall()


def _source_references(conn: sqlite3.Connection, sample_id: int) -> list[dict[str, Any]]:
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


def _is_bodypaint_candidate(package: sqlite3.Row) -> bool:
    package_role = str(package["package_role"] or "").casefold()
    content_bucket = str(package["content_bucket"] or "").casefold()
    contract_type = str(package["contract_type"] or "").casefold()
    if content_bucket in NON_BODYPAINT_BUCKETS:
        return False
    if "weapon" in package_role or "weapon" in contract_type:
        return False
    return True


def _candidate_packages_for_sample(conn: sqlite3.Connection, sample: sqlite3.Row) -> list[sqlite3.Row]:
    packages = sample_packages(conn, sample["id"])
    return [package for package in packages if _is_bodypaint_candidate(package)]


def _artifact_score(row: sqlite3.Row) -> int:
    kind = str(row["artifact_kind"] or "")
    if kind in {"normalized_glb", "portable_glb"}:
        return 100
    if kind == "portable_gltf":
        return 95
    if kind == "portable_fbx":
        return 90
    if kind == "output_fbx":
        return 80
    if kind.startswith("split_output:body") or kind.startswith("split_output:character"):
        return 70
    if normalize_ext(row["path"]) in MODEL_EXTENSIONS:
        return 50
    return 0


def _artifact_candidates(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for row in rows:
        ext = normalize_ext(row["path"])
        score = _artifact_score(row)
        if ext not in MODEL_EXTENSIONS and score == 0:
            continue
        raw_path = str(row["path"] or "")
        path = Path(raw_path)
        candidates.append(
            {
                "score": score,
                "raw_path": raw_path,
                "format": ext,
                "exists": path.exists() and path.is_file(),
                "owner_type": row["owner_type"],
                "owner_id": row["owner_id"],
                "stage": row["stage"],
                "artifact_kind": row["artifact_kind"],
                "source_kind": "artifact",
                "created_at": row["created_at"],
                "id": row["id"],
            }
        )
    return candidates


def _source_candidates(source_refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for index, source_ref in enumerate(source_refs):
        metadata = source_ref.get("metadata") or {}
        paths = [
            str(metadata.get("resolved_path") or "").strip(),
            str(source_ref.get("managed_path") or "").strip(),
            str(source_ref.get("source_path") or "").strip(),
        ]
        for raw_path in paths:
            if not raw_path:
                continue
            ext = normalize_ext(raw_path)
            if ext not in MODEL_EXTENSIONS:
                continue
            path = Path(raw_path)
            candidates.append(
                {
                    "score": 40,
                    "raw_path": raw_path,
                    "format": ext,
                    "exists": path.exists() and path.is_file(),
                    "owner_type": "source",
                    "owner_id": "",
                    "stage": "source",
                    "artifact_kind": "source_model",
                    "source_kind": str(source_ref.get("source_kind") or "source"),
                    "created_at": "",
                    "id": index,
                }
            )
    return candidates


def _select_source_model(
    conn: sqlite3.Connection,
    sample: sqlite3.Row,
    package: sqlite3.Row,
) -> dict[str, Any]:
    source_refs = _source_references(conn, sample["id"])
    candidates = [
        *_artifact_candidates(_package_artifacts(conn, package["id"])),
        *_artifact_candidates(_sample_artifacts(conn, sample["id"])),
        *_source_candidates(source_refs),
    ]
    if not candidates:
        return {
            "raw_path": "",
            "format": "",
            "exists": False,
            "artifact_kind": "",
            "source_kind": "",
            "owner_type": "",
            "owner_id": "",
            "stage": "",
        }
    candidates.sort(key=lambda item: (bool(item["exists"]), int(item["score"]), str(item["created_at"]), int(item["id"])), reverse=True)
    return candidates[0]


def _copy_if_exists(source: Path | None, destination: Path) -> Path | None:
    if source is None or not source.exists() or not source.is_file():
        return None
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return destination.resolve()


def _relative_to_base(path: Path, base_dir: Path) -> str:
    try:
        return path.relative_to(base_dir).as_posix()
    except ValueError:
        return str(path)


def _relative_to_profile(path: Path, profile_dir: Path) -> str:
    return _relative_to_base(path, profile_dir)


def _staged_source_filename(source_model: dict[str, Any]) -> str:
    raw_path = str(source_model.get("raw_path") or "")
    name = Path(raw_path).name
    if name:
        return name
    ext = str(source_model.get("format") or ".bin")
    return f"source_model{ext}"


def _load_manifest_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"Manifest must be a JSON object: {path}")
    return payload


def _normalize_identity_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _normalize_identity_json(item)
            for key, item in sorted(value.items())
            if key not in BODYPAINT_PACKET_IDENTITY_VOLATILE_KEYS
        }
    if isinstance(value, list):
        return [_normalize_identity_json(item) for item in value]
    return value


def _compute_bodypaint_packet_identity(*, profile_dir: Path, profile: str) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    total_bytes = 0
    for path in sorted(item for item in profile_dir.rglob("*") if item.is_file()):
        relpath = path.relative_to(profile_dir).as_posix()
        if relpath in BODYPAINT_PACKET_IDENTITY_EXCLUDED_RELPATHS:
            continue
        if path.suffix.lower() == ".json":
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            normalized = _normalize_identity_json(payload)
            material = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
            files.append(
                {
                    "path": relpath,
                    "kind": "json",
                    "size_bytes": path.stat().st_size,
                    "fingerprint_sha256": hashlib.sha256(material).hexdigest(),
                }
            )
        else:
            digest = hashlib.sha256()
            size_bytes = 0
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    size_bytes += len(chunk)
                    digest.update(chunk)
            files.append(
                {
                    "path": relpath,
                    "kind": "binary",
                    "size_bytes": size_bytes,
                    "fingerprint_sha256": digest.hexdigest(),
                }
            )
        total_bytes += int(files[-1]["size_bytes"])
    material = json.dumps(files, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return {
        "generated_at_utc": now_iso(),
        "identity_kind": "bodypaint_packet_identity",
        "identity_version": PACKET_IDENTITY_VERSION,
        "profile": profile,
        "export_contract_version": EXPORT_CONTRACT_VERSION,
        "exporter_version": EXPORTER_VERSION,
        "stable_fingerprint": hashlib.sha256(material).hexdigest(),
        "volatile_keys_ignored": sorted(BODYPAINT_PACKET_IDENTITY_VOLATILE_KEYS),
        "excluded_files": sorted(BODYPAINT_PACKET_IDENTITY_EXCLUDED_RELPATHS),
        "file_count": len(files),
        "total_bytes": total_bytes,
        "files": files,
    }


def _aiue_manifest_lookup_keys(payload: dict[str, Any]) -> list[str]:
    toy_yard = payload.get("toy_yard") or {}
    aliases = [alias for alias in (toy_yard.get("package_aliases") or []) if isinstance(alias, dict)]
    aliases.sort(key=_alias_priority, reverse=True)
    alias_values = [str((alias or {}).get("alias_value") or "") for alias in aliases if isinstance(alias, dict)]
    alias_triplets = [
        ":".join(
            [
                str((alias or {}).get("external_system") or "").strip(),
                str((alias or {}).get("alias_key") or "").strip(),
                str((alias or {}).get("alias_value") or "").strip(),
            ]
        )
        for alias in aliases
        if isinstance(alias, dict)
    ]
    return _ordered_unique(
        [
            *alias_triplets,
            *alias_values,
            str(toy_yard.get("package_id") or ""),
            str(payload.get("package_id") or ""),
        ]
    )


def _build_aiue_pmx_index(paths: ProjectPaths, profile: str) -> dict[str, dict[str, Any]]:
    profile_dir = paths.aiue_pmx_profile_dir(profile)
    if not profile_dir.exists():
        raise ValueError(f"AiUE PMX profile does not exist: {profile_dir}")

    conversion_dir = paths.aiue_pmx_conversion_dir(profile)
    if not conversion_dir.exists():
        raise ValueError(f"AiUE PMX conversion directory does not exist: {conversion_dir}")

    index: dict[str, dict[str, Any]] = {}
    for manifest_path in sorted(conversion_dir.rglob("manifest.json")):
        payload = _load_manifest_json(manifest_path)
        keys = _aiue_manifest_lookup_keys(payload)
        if not keys:
            continue
        entry = {
            "manifest_path": manifest_path.resolve(),
            "package_dir": manifest_path.parent.resolve(),
            "payload": payload,
        }
        for key in keys:
            index.setdefault(key, entry)
    return index


def _package_lookup_keys(conn: sqlite3.Connection, package: sqlite3.Row) -> list[str]:
    rows = [dict(row) for row in package_aliases(conn, package["id"])]
    rows.sort(key=_alias_priority, reverse=True)
    alias_values = [str(row["alias_value"] or "") for row in rows]
    alias_triplets = [
        ":".join(
            [
                str(row["external_system"] or "").strip(),
                str(row["alias_key"] or "").strip(),
                str(row["alias_value"] or "").strip(),
            ]
        )
        for row in rows
    ]
    return _ordered_unique([*alias_triplets, *alias_values, str(package["canonical_package_id"] or "")])


def _resolve_aiue_output_fbx(entry: dict[str, Any]) -> tuple[Path, str]:
    manifest_path = Path(entry["manifest_path"])
    package_dir = Path(entry["package_dir"])
    payload = entry["payload"]
    export_artifacts = payload.get("export_artifacts") or {}
    output_value = str(export_artifacts.get("output_fbx") or payload.get("output_fbx") or "").strip()
    if not output_value:
        raise ValueError(f"AiUE PMX manifest is missing output_fbx: {manifest_path}")

    output_path = Path(output_value)
    if not output_path.is_absolute():
        output_path = package_dir / output_path
    output_path = output_path.resolve()
    if output_path.suffix.lower() != ".fbx":
        raise ValueError(f"AiUE PMX bridge requires an FBX output: {manifest_path}")
    if not output_path.exists() or not output_path.is_file():
        raise FileNotFoundError(f"AiUE PMX staged FBX does not exist: {output_path}")
    return output_path, output_value


def _relative_copy_target(source: Path, root: Path) -> Path:
    try:
        return source.resolve().relative_to(root.resolve())
    except ValueError:
        return Path(source.name)


def _prepare_aiue_pmx_source_model(
    conn: sqlite3.Connection,
    paths: ProjectPaths,
    *,
    package: sqlite3.Row,
    source_dir: Path,
    aiue_pmx_profile: str,
    aiue_index: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    lookup_keys = _package_lookup_keys(conn, package)
    matched_entry = None
    matched_key = ""
    for key in lookup_keys:
        if key in aiue_index:
            matched_entry = aiue_index[key]
            matched_key = key
            break
    if matched_entry is None:
        raise ValueError(
            "AiUE PMX manifest not found for BodyPaint package "
            f"{package['canonical_package_id']} in profile {aiue_pmx_profile}"
        )

    upstream_fbx, _ = _resolve_aiue_output_fbx(matched_entry)
    upstream_package_dir = Path(matched_entry["package_dir"]).resolve()
    upstream_manifest_path = Path(matched_entry["manifest_path"]).resolve()
    staged_root = (source_dir / "upstream_aiue_pmx").resolve()
    shutil.copytree(upstream_package_dir, staged_root)
    staged_fbx = staged_root / _relative_copy_target(upstream_fbx, upstream_package_dir)
    if not staged_fbx.exists() or not staged_fbx.is_file():
        raise FileNotFoundError(f"AiUE PMX staged FBX missing after copy: {staged_fbx}")

    return {
        "provider": SOURCE_PROVIDER_AIUE_PMX,
        "upstream_profile": aiue_pmx_profile,
        "upstream_manifest_path": str(upstream_manifest_path),
        "matched_key": matched_key,
        "raw_path": str(upstream_fbx),
        "staged_path": staged_fbx,
        "format": ".fbx",
        "artifact_kind": "output_fbx",
        "source_kind": "artifact",
        "owner_type": "package",
        "owner_id": package["id"],
        "stage": "aiue_pmx",
        "available": True,
    }


def _prepare_native_source_model(
    conn: sqlite3.Connection,
    *,
    sample: sqlite3.Row,
    package: sqlite3.Row,
    source_dir: Path,
) -> dict[str, Any]:
    source_model = _select_source_model(conn, sample, package)
    raw_source_path = str(source_model.get("raw_path") or "")
    staged_source = _copy_if_exists(
        Path(raw_source_path) if raw_source_path else None,
        source_dir / _staged_source_filename(source_model),
    )
    return {
        "provider": SOURCE_PROVIDER_NATIVE,
        "upstream_profile": "",
        "upstream_manifest_path": "",
        "matched_key": "",
        "raw_path": raw_source_path,
        "staged_path": staged_source,
        "format": source_model.get("format") or normalize_ext(raw_source_path),
        "artifact_kind": source_model.get("artifact_kind") or "",
        "source_kind": source_model.get("source_kind") or "",
        "owner_type": source_model.get("owner_type") or "",
        "owner_id": source_model.get("owner_id") or "",
        "stage": source_model.get("stage") or "",
        "available": staged_source is not None,
    }


def _single_sample_owner(conn: sqlite3.Connection, sample_ids: list[str]) -> sqlite3.Row | None:
    if len(sample_ids) != 1:
        return None
    return conn.execute(
        "SELECT * FROM samples WHERE canonical_sample_id = ?",
        (sample_ids[0],),
    ).fetchone()


def export_bodypaint_view(
    conn: sqlite3.Connection,
    paths: ProjectPaths,
    *,
    profile: str,
    sample_ref: str | None = None,
    package_refs: list[str] | None = None,
    aiue_pmx_profile: str | None = None,
) -> dict[str, Any]:
    normalized_package_refs = [str(item or "").strip() for item in (package_refs or []) if str(item or "").strip()]
    if sample_ref and normalized_package_refs:
        raise ValueError("Use either --sample or --package, not both.")
    if not sample_ref and not normalized_package_refs:
        raise ValueError("BodyPaint export requires either --sample or at least one --package.")

    selections: list[tuple[sqlite3.Row, sqlite3.Row]] = []
    if normalized_package_refs:
        seen_packages: set[str] = set()
        for package_ref in normalized_package_refs:
            if package_ref in seen_packages:
                continue
            package = _resolve_package(conn, package_ref)
            if package is None:
                raise ValueError(f"Unknown package: {package_ref}")
            if not _is_bodypaint_candidate(package):
                raise ValueError(f"Package is not a BodyPaint candidate: {package_ref}")
            sample = conn.execute("SELECT * FROM samples WHERE id = ?", (package["sample_id"],)).fetchone()
            if sample is None:
                raise ValueError(f"Package has no sample owner: {package_ref}")
            selections.append((sample, package))
            seen_packages.add(package_ref)
    else:
        assert sample_ref is not None
        sample = resolve_sample(conn, sample_ref)
        if sample is None:
            raise ValueError(f"Unknown sample: {sample_ref}")
        selections.extend((sample, package) for package in _candidate_packages_for_sample(conn, sample))
    if not selections:
        raise ValueError("BodyPaint export requires at least one BodyPaint candidate package.")

    profile_dir = _safe_profile_reset(paths, profile)
    assets_dir = paths.bodypaint_assets_dir(profile)
    summary_dir = paths.bodypaint_summary_dir(profile)
    workspace_views_dir = paths.bodypaint_workspace_views_dir(profile)
    assets_dir.mkdir(parents=True, exist_ok=True)
    summary_dir.mkdir(parents=True, exist_ok=True)
    workspace_views_dir.mkdir(parents=True, exist_ok=True)
    aiue_index = _build_aiue_pmx_index(paths, aiue_pmx_profile) if aiue_pmx_profile else {}
    consumer_packet = {
        "packet_kind": "external_model_packet",
        "packet_version": EXTERNAL_MODEL_PACKET_VERSION,
        "consumer": "bodypaint",
        "required_outputs": list(BODYPAINT_REQUIRED_OUTPUT_KEYS),
        "optional_outputs": list(BODYPAINT_OPTIONAL_OUTPUT_KEYS),
        "source_providers": [SOURCE_PROVIDER_NATIVE, SOURCE_PROVIDER_AIUE_PMX],
    }

    summary_items: list[dict[str, Any]] = []
    registry_packets: list[dict[str, Any]] = []
    exported_packages: list[str] = []
    exported_sample_ids: list[str] = []

    for sample, package in selections:
        package_dir = paths.bodypaint_asset_dir(profile, package["canonical_package_id"])
        source_dir = package_dir / "source"
        outputs_dir = package_dir / "outputs"
        source_dir.mkdir(parents=True, exist_ok=True)
        outputs_dir.mkdir(parents=True, exist_ok=True)

        sample_metadata = json_loads(sample["metadata_json"])
        package_metadata = json_loads(package["metadata_json"])
        source_refs = _source_references(conn, sample["id"])
        resolved_source = (
            _prepare_aiue_pmx_source_model(
                conn,
                paths,
                package=package,
                source_dir=source_dir,
                aiue_pmx_profile=aiue_pmx_profile,
                aiue_index=aiue_index,
            )
            if aiue_pmx_profile
            else _prepare_native_source_model(
                conn,
                sample=sample,
                package=package,
                source_dir=source_dir,
            )
        )
        raw_source_path = str(resolved_source.get("raw_path") or "")
        staged_source = resolved_source.get("staged_path")
        source_model_available = bool(resolved_source.get("available"))

        expected_outputs = {
            "normalized_glb": "outputs/model.glb",
            "painted_glb": "outputs/model.bodypaint.glb",
            "analysis_json": "outputs/bodypaint.analysis.json",
            "engineering_mask": "outputs/bodypaint.engineering-mask.png",
            "manual_overrides": "outputs/bodypaint.manual-overrides.json",
        }
        manifest_payload = {
            "export_contract_version": EXPORT_CONTRACT_VERSION,
            "exporter_version": EXPORTER_VERSION,
            "source": "toy-yard export",
            "profile": profile,
            "generated_at_utc": now_iso(),
            "sample_id": sample["canonical_sample_id"],
            "package_id": package["canonical_package_id"],
            "display_name": sample["display_name"],
            "package_role": package["package_role"],
            "content_bucket": package["content_bucket"],
            "contract_type": package["contract_type"],
            "consumer_packet": consumer_packet,
            "source_model": {
                "available": source_model_available,
                "raw_path": raw_source_path,
                "staged_path": _relative_to_base(staged_source, package_dir) if staged_source else "",
                "format": resolved_source.get("format") or normalize_ext(raw_source_path),
                "artifact_kind": resolved_source.get("artifact_kind") or "",
                "source_kind": resolved_source.get("source_kind") or "",
                "owner_type": resolved_source.get("owner_type") or "",
                "owner_id": resolved_source.get("owner_id") or "",
                "stage": resolved_source.get("stage") or "",
                "provider": resolved_source.get("provider") or SOURCE_PROVIDER_NATIVE,
                "upstream_profile": resolved_source.get("upstream_profile") or "",
                "upstream_manifest_path": resolved_source.get("upstream_manifest_path") or "",
            },
            "expected_outputs": expected_outputs,
            "viewer": {
                "preferred_model": expected_outputs["painted_glb"],
                "fallback_model": _relative_to_base(staged_source, package_dir) if staged_source else raw_source_path,
                "analysis": expected_outputs["analysis_json"],
                "engineering_mask": expected_outputs["engineering_mask"],
                "override_path": expected_outputs["manual_overrides"],
            },
            "source_lineage": {
                "source_references": source_refs,
            },
            "toy_yard": {
                "sample_id": sample["canonical_sample_id"],
                "package_id": package["canonical_package_id"],
                "sample_aliases": [dict(row) for row in sample_aliases(conn, sample["id"])],
                "package_aliases": [dict(row) for row in package_aliases(conn, package["id"])],
                "sample_metadata": sample_metadata,
                "package_metadata": package_metadata,
            },
        }

        manifest_path = paths.bodypaint_asset_manifest_path(profile, package["canonical_package_id"])
        write_json(manifest_path, manifest_payload)
        ensure_artifact(
            conn,
            owner_type="package",
            owner_id=package["id"],
            stage="export",
            artifact_kind="bodypaint_input_manifest",
            path=str(manifest_path.resolve()),
            format=".json",
            status="exported",
            metadata={"profile": profile, "sample_id": sample["canonical_sample_id"]},
        )
        if staged_source is not None:
            ensure_artifact(
                conn,
                owner_type="package",
                owner_id=package["id"],
                stage="export",
                artifact_kind="bodypaint_source_model",
                path=str(staged_source),
                format=staged_source.suffix.lower(),
                status="exported",
                metadata={"profile": profile, "sample_id": sample["canonical_sample_id"]},
            )

        manifest_relpath = _relative_to_profile(manifest_path.resolve(), profile_dir)
        source_relpath = _relative_to_profile(staged_source, profile_dir) if staged_source else ""
        summary_item = {
            "sample_id": sample["canonical_sample_id"],
            "package_id": package["canonical_package_id"],
            "display_name": sample["display_name"],
            "manifest_path": str(manifest_path.resolve()),
            "source_model_available": source_model_available,
            "source_model_path": str(staged_source) if staged_source else raw_source_path,
            "source_model_format": resolved_source.get("format") or normalize_ext(raw_source_path),
            "source_provider": resolved_source.get("provider") or SOURCE_PROVIDER_NATIVE,
            "upstream_profile": resolved_source.get("upstream_profile") or "",
            "upstream_manifest_path": resolved_source.get("upstream_manifest_path") or "",
            "package_role": package["package_role"],
            "content_bucket": package["content_bucket"],
            "contract_type": package["contract_type"],
            "consumer_ready": bool(package["consumer_ready"]),
            "warehouse_status": package["warehouse_status"],
        }
        summary_items.append(summary_item)
        registry_packets.append(
            {
                **summary_item,
                "manifest_relpath": manifest_relpath,
                "source_model_relpath": source_relpath,
                "expected_outputs": expected_outputs,
            }
        )
        exported_packages.append(package["canonical_package_id"])
        exported_sample_ids.append(sample["canonical_sample_id"])

    sample_ids = _ordered_unique(exported_sample_ids)
    primary_sample_id = sample_ids[0] if len(sample_ids) == 1 else ""
    single_owner = _single_sample_owner(conn, sample_ids)
    ready_items = sum(1 for item in summary_items if item["source_model_available"])

    summary_payload = {
        "generated_at_utc": now_iso(),
        "export_contract_version": EXPORT_CONTRACT_VERSION,
        "exporter_version": EXPORTER_VERSION,
        "source": "toy-yard export",
            "profile": profile,
            "sample_id": primary_sample_id,
            "sample_ids": sample_ids,
            "consumer_packet": consumer_packet,
            "items": summary_items,
            "counts": {
                "requested_items": len(summary_items),
                "ready_items": ready_items,
                "missing_source_model_items": len(summary_items) - ready_items,
            "distinct_sample_count": len(sample_ids),
        },
    }
    summary_path = paths.bodypaint_summary_path(profile)
    write_json(summary_path, summary_payload)

    registry_payload = {
        "generated_at_utc": now_iso(),
        "export_contract_version": EXPORT_CONTRACT_VERSION,
        "exporter_version": EXPORTER_VERSION,
        "source": "toy-yard export",
            "profile": profile,
            "sample_id": primary_sample_id,
            "sample_ids": sample_ids,
            "consumer_packet": consumer_packet,
            "packets": registry_packets,
            "package_index": {
                item["package_id"]: {
                    "manifest_path": item["manifest_path"],
                    "manifest_relpath": item["manifest_relpath"],
                "source_model_available": item["source_model_available"],
                "source_model_relpath": item["source_model_relpath"],
                "expected_outputs": item["expected_outputs"],
            }
            for item in registry_packets
        },
        "counts": {
            "packets": len(registry_packets),
            "source_model_ready": ready_items,
            "distinct_samples": len(sample_ids),
        },
    }
    registry_path = paths.bodypaint_registry_path(profile)
    write_json(registry_path, registry_payload)

    packet_check_path = paths.bodypaint_packet_check_path(profile)
    packet_check_payload = write_bodypaint_packet_check_report(
        registry_payload=registry_payload,
        export_root=profile_dir.resolve(),
        output_path=packet_check_path,
    )

    communication_signal_payload = build_bodypaint_export_signal(
        profile=profile,
        summary_payload=summary_payload,
        registry_payload=registry_payload,
        packet_check_payload=packet_check_payload,
        summary_path=summary_path,
        registry_path=registry_path,
        packet_check_path=packet_check_path,
    )
    communication_signal_path = paths.bodypaint_communication_signal_path(profile)
    write_json(communication_signal_path, communication_signal_payload)
    packet_identity_path = paths.bodypaint_packet_identity_path(profile)
    packet_identity_payload = _compute_bodypaint_packet_identity(profile_dir=profile_dir.resolve(), profile=profile)
    write_json(packet_identity_path, packet_identity_payload)

    if single_owner is not None:
        ensure_artifact(
            conn,
            owner_type="sample",
            owner_id=single_owner["id"],
            stage="export",
            artifact_kind="bodypaint_suite_summary",
            path=str(summary_path.resolve()),
            format=".json",
            status="exported",
            metadata={"profile": profile},
        )
        ensure_artifact(
            conn,
            owner_type="sample",
            owner_id=single_owner["id"],
            stage="export",
            artifact_kind="bodypaint_packet_registry",
            path=str(registry_path.resolve()),
            format=".json",
            status="exported",
            metadata={"profile": profile},
        )
        ensure_artifact(
            conn,
            owner_type="sample",
            owner_id=single_owner["id"],
            stage="export",
            artifact_kind="bodypaint_packet_check",
            path=str(packet_check_path.resolve()),
            format=".json",
            status=packet_check_payload["status"],
            metadata={"profile": profile},
        )
        ensure_artifact(
            conn,
            owner_type="sample",
            owner_id=single_owner["id"],
            stage="export",
            artifact_kind="communication_signal",
            path=str(communication_signal_path.resolve()),
            format=".json",
            status=communication_signal_payload["status"],
            metadata={"profile": profile, "lane": "bodypaint"},
        )
        ensure_artifact(
            conn,
            owner_type="sample",
            owner_id=single_owner["id"],
            stage="export",
            artifact_kind="bodypaint_packet_identity",
            path=str(packet_identity_path.resolve()),
            format=".json",
            status="exported",
            metadata={
                "profile": profile,
                "lane": "bodypaint",
                "stable_fingerprint": packet_identity_payload["stable_fingerprint"],
            },
        )

    workspace_view_payload = {
        "version": "0.1",
        "export_contract_version": EXPORT_CONTRACT_VERSION,
        "exporter_version": EXPORTER_VERSION,
        "profile": profile,
        "toy_yard_root": str(paths.root),
        "toy_yard_bodypaint_view_root": str(profile_dir.resolve()),
        "summary_path": str(summary_path.resolve()),
        "registry_path": str(registry_path.resolve()),
        "packet_check_path": str(packet_check_path.resolve()),
        "packet_identity_path": str(packet_identity_path.resolve()),
        "communication_signal_path": str(communication_signal_path.resolve()),
        "sample_id": primary_sample_id,
        "sample_ids": sample_ids,
        "package_ids": exported_packages,
        "consumer_packet": consumer_packet,
    }
    workspace_view_path = paths.bodypaint_workspace_view_path(profile)
    write_json(workspace_view_path, workspace_view_payload)

    trial_workspace_payload = {
        "version": "0.1.0",
        "schema_version": 1,
        "paths": {
            "toy_yard_bodypaint_view_root": str(profile_dir.resolve()),
            "bodypaint_repo_root": r"C:\Projects\BodyPaint",
            "bodypaint_processor_output_root": str(assets_dir.resolve()),
        },
        "consumer_packet": consumer_packet,
        "processor": {
            "expected_contract": EXPORT_CONTRACT_VERSION,
            "normalized_format": "glb",
            "analysis_filename": "bodypaint.analysis.json",
            "painted_model_filename": "model.bodypaint.glb",
            "engineering_mask_filename": "bodypaint.engineering-mask.png",
        },
    }
    trial_workspace_path = paths.bodypaint_trial_workspace_path(profile)
    write_json(trial_workspace_path, trial_workspace_payload)

    return {
        "profile": profile,
        "sample_id": primary_sample_id,
        "sample_ids": sample_ids,
        "packages": exported_packages,
        "ready_items": ready_items,
        "export_root": str(profile_dir.resolve()),
        "summary_path": str(summary_path.resolve()),
        "registry_path": str(registry_path.resolve()),
        "packet_check_path": str(packet_check_path.resolve()),
        "packet_identity_path": str(packet_identity_path.resolve()),
        "communication_signal_path": str(communication_signal_path.resolve()),
        "workspace_view_path": str(workspace_view_path.resolve()),
        "trial_workspace_path": str(trial_workspace_path.resolve()),
    }
