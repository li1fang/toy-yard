from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

from toyyard.paths import ProjectPaths
from toyyard.util import normalize_ext, slugify
from toyyard.warehouse import (
    ensure_alias,
    ensure_artifact,
    ensure_package,
    ensure_report_import,
    ensure_sample,
    ensure_source,
    ensure_source_link,
    find_entity_by_alias,
    get_root,
    json_loads,
    make_canonical_id,
)


SUMMARY_GLOBS = (
    "_preflight_out/**/preflight_batch_summary.json",
    "_fbx_out_*/**/ue_suite_summary.json",
    "_ue_suite_reports/**/ue_suite_summary.json",
)
PRELIGHT_GLOB = "_preflight_out/**/preflight_report.json"
CONVERSION_MANIFEST_GLOB = "_fbx_out_*/**/manifest.json"
MATERIALS_REPORT_GLOB = "_fbx_out_*/**/materials_report.json"
WEAPON_REPORT_GLOB = "_fbx_reports_weapon/**/*.json"
EQUIPMENT_REPORT_GLOBS = (
    "_fbx_out_*/**/ue_equipment_registry.json",
    "_fbx_out_*/**/ue_equipment_assets_report*.json",
)


def _load_json(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8-sig")
    return json.loads(text)


def _normalize_key(value: str | None) -> str:
    return str(value or "").strip().replace("/", "\\").lower()


def _safe_relative(base: Path, target: Path) -> str:
    try:
        return target.relative_to(base).as_posix()
    except ValueError:
        return str(target)


def _candidate_source_path(root_path: Path, source_file: str | None, source_relative_path: str | None = None) -> Path | None:
    candidates = []
    if source_file:
        candidates.append(Path(source_file))
    if source_relative_path:
        candidates.append(root_path / source_relative_path)
    for candidate in candidates:
        expanded = candidate.expanduser()
        if expanded.exists():
            return expanded.resolve()
    return None


def _guess_source_game(root_path: Path, report_path: Path, sample_alias: str | None, source_path: str | None) -> str:
    if sample_alias and "_" in sample_alias:
        return sample_alias.split("_", 1)[0]
    relative_parts = [part for part in _safe_relative(root_path, report_path).split("/") if part]
    if len(relative_parts) >= 2 and relative_parts[0].startswith("_"):
        return relative_parts[1]
    if source_path:
        source_parts = [part for part in str(source_path).replace("\\", "/").split("/") if part]
        if len(source_parts) >= 2:
            return source_parts[-2]
    return "unknown"


def _best_display_name(payload: dict[str, Any], fallback: str) -> str:
    catalog_context = payload.get("catalog_context") or {}
    model = payload.get("model") or {}
    return (
        str(catalog_context.get("display_name") or "").strip()
        or str(model.get("name") or "").strip()
        or fallback
    )


def _source_record(
    conn: sqlite3.Connection,
    *,
    root_id: int,
    root_path: Path,
    source_file: str | None,
    source_relative_path: str | None,
    source_kind: str,
    metadata: dict[str, Any] | None = None,
):
    if not source_file and not source_relative_path:
        source_file = "unknown"
    source_candidate = _candidate_source_path(root_path, source_file, source_relative_path)
    source_path = source_file or source_relative_path or "unknown"
    ext = normalize_ext(source_candidate) if source_candidate else normalize_ext(source_path)
    size_bytes = source_candidate.stat().st_size if source_candidate and source_candidate.is_file() else None
    return ensure_source(
        conn,
        root_id=root_id,
        source_kind=source_kind,
        source_path=source_path,
        managed_path=None,
        content_hash=None,
        ext=ext,
        size_bytes=size_bytes,
        status="observed",
        metadata={
            "source_relative_path": source_relative_path or "",
            "resolved_path": str(source_candidate) if source_candidate else "",
            **(metadata or {}),
        },
    )


def _ensure_sample_only_from_preflight(
    conn: sqlite3.Connection,
    *,
    root_id: int,
    root_path: Path,
    sample_alias: str | None,
    display_name: str,
    source_file: str | None,
    source_relative_path: str | None,
    source_format: str,
    warehouse_status: str,
    sample_metadata: dict[str, Any] | None = None,
) -> tuple[sqlite3.Row, sqlite3.Row]:
    source = _source_record(
        conn,
        root_id=root_id,
        root_path=root_path,
        source_file=source_file,
        source_relative_path=source_relative_path,
        source_kind="legacy_workspace",
        metadata={"from": "preflight_report"},
    )
    sample_key = sample_alias or source_file or source_relative_path or display_name
    canonical_sample_id = make_canonical_id("sample", "3dgirls", sample_key, display_name=sample_key)
    sample = ensure_sample(
        conn,
        canonical_sample_id=canonical_sample_id,
        display_name=display_name,
        source_game=_guess_source_game(root_path, root_path / "placeholder", sample_alias, source_file),
        source_format=source_format or "unknown",
        item_family="unknown",
        warehouse_status=warehouse_status,
        metadata=sample_metadata,
    )
    if sample_alias:
        ensure_alias(
            conn,
            entity_type="sample",
            entity_id=sample["id"],
            external_system="3dgirls",
            alias_key="sample_id",
            alias_value=sample_alias,
        )
    ensure_source_link(conn, source_id=source["id"], entity_type="sample", entity_id=sample["id"])
    return source, sample


def _ensure_sample_and_package_from_entry(
    conn: sqlite3.Connection,
    *,
    root_id: int,
    root_path: Path,
    external_system: str,
    sample_alias: str | None,
    package_alias: str | None,
    display_name: str,
    source_file: str | None,
    source_relative_path: str | None,
    source_format: str = "unknown",
    package_role: str = "unknown",
    content_bucket: str = "unknown",
    contract_type: str = "unknown",
    consumer_ready: bool = False,
    warehouse_status: str = "cataloged",
    sample_metadata: dict[str, Any] | None = None,
    package_metadata: dict[str, Any] | None = None,
) -> tuple[sqlite3.Row, sqlite3.Row, sqlite3.Row]:
    source = _source_record(
        conn,
        root_id=root_id,
        root_path=root_path,
        source_file=source_file,
        source_relative_path=source_relative_path,
        source_kind="legacy_workspace",
        metadata={"external_system": external_system},
    )
    sample_key = sample_alias or source_file or source_relative_path or display_name
    canonical_sample_id = make_canonical_id("sample", external_system, sample_key, display_name=sample_key)
    sample = ensure_sample(
        conn,
        canonical_sample_id=canonical_sample_id,
        display_name=display_name,
        source_game=_guess_source_game(root_path, root_path / "placeholder", sample_alias, source_file),
        source_format=source_format or "unknown",
        item_family=package_role or "unknown",
        warehouse_status=warehouse_status,
        metadata=sample_metadata,
    )
    if sample_alias:
        ensure_alias(
            conn,
            entity_type="sample",
            entity_id=sample["id"],
            external_system=external_system,
            alias_key="sample_id",
            alias_value=sample_alias,
        )

    package_key = package_alias or f"{sample_key}:{package_role or 'unknown'}"
    canonical_package_id = make_canonical_id("pkg", external_system, package_key, display_name=package_key)
    package = ensure_package(
        conn,
        sample_id=sample["id"],
        canonical_package_id=canonical_package_id,
        package_role=package_role or "unknown",
        content_bucket=content_bucket or "unknown",
        contract_type=contract_type or "unknown",
        consumer_ready=consumer_ready,
        warehouse_status=warehouse_status,
        metadata=package_metadata,
    )
    if package_alias:
        ensure_alias(
            conn,
            entity_type="package",
            entity_id=package["id"],
            external_system=external_system,
            alias_key="package_id",
            alias_value=package_alias,
        )

    ensure_source_link(conn, source_id=source["id"], entity_type="sample", entity_id=sample["id"])
    ensure_source_link(conn, source_id=source["id"], entity_type="package", entity_id=package["id"])
    return source, sample, package


def _linked_packages_for_source(conn: sqlite3.Connection, source_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT packages.*
        FROM packages
        JOIN source_links
          ON source_links.entity_type = 'package' AND source_links.entity_id = packages.id
        WHERE source_links.source_id = ?
        ORDER BY packages.canonical_package_id
        """,
        (source_id,),
    ).fetchall()


def _package_alias_exists(
    conn: sqlite3.Connection,
    *,
    package_id: int,
    alias_key: str,
    alias_value: str | None,
) -> bool:
    if not alias_value:
        return False
    row = conn.execute(
        """
        SELECT 1
        FROM aliases
        WHERE entity_type = 'package' AND entity_id = ? AND alias_key = ? AND alias_value = ?
        """,
        (package_id, alias_key, alias_value),
    ).fetchone()
    return row is not None


def _sample_packages(conn: sqlite3.Connection, sample_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM packages WHERE sample_id = ? ORDER BY id",
        (sample_id,),
    ).fetchall()


def _package_alias_count(conn: sqlite3.Connection, package_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM aliases WHERE entity_type = 'package' AND entity_id = ?",
        (package_id,),
    ).fetchone()[0]


def _package_identity_basis(entry: dict[str, Any]) -> str:
    if entry.get("package_id"):
        return "package_id"
    if entry.get("source_relative_path"):
        return "source_relative_path"
    if entry.get("manifest_path"):
        return "manifest_path"
    return "sample_scoped_primary"


def _ensure_optional_package_alias(
    conn: sqlite3.Connection,
    *,
    package_id: int,
    alias_key: str,
    alias_value: str | None,
) -> None:
    if not alias_value:
        return
    try:
        ensure_alias(
            conn,
            entity_type="package",
            entity_id=package_id,
            external_system="3dgirls",
            alias_key=alias_key,
            alias_value=alias_value,
        )
    except ValueError:
        return


def _candidate_package_for_summary_entry(
    conn: sqlite3.Connection,
    *,
    sample_id: int,
    package_alias: str | None,
    source_relative_path: str | None,
    manifest_path: str | None,
) -> sqlite3.Row | None:
    if package_alias:
        package = find_entity_by_alias(
            conn,
            entity_type="package",
            external_system="3dgirls",
            alias_value=package_alias,
            alias_key="package_id",
        )
        if package is not None:
            return package

    packages = _sample_packages(conn, sample_id)
    for package in packages:
        if _package_alias_exists(conn, package_id=package["id"], alias_key="source_relative_path", alias_value=source_relative_path):
            return package
        if _package_alias_exists(conn, package_id=package["id"], alias_key="manifest_path", alias_value=manifest_path):
            return package

    if len(packages) == 1:
        return packages[0]

    fallback_packages = []
    for package in packages:
        metadata = json_loads(package["metadata_json"])
        if (
            _package_alias_count(conn, package["id"]) == 0
            and str(package["package_role"] or "unknown") == "unknown"
            and not package["consumer_ready"]
            and metadata.get("created_from") in {"preflight_report_fallback", "ue_suite_summary"}
        ):
            fallback_packages.append(package)
    if len(fallback_packages) == 1:
        return fallback_packages[0]
    return None


def _rebind_package_identity(
    conn: sqlite3.Connection,
    *,
    package: sqlite3.Row,
    canonical_package_id: str,
    package_role: str,
    content_bucket: str,
    contract_type: str,
    consumer_ready: bool,
    warehouse_status: str,
    metadata: dict[str, Any] | None = None,
) -> sqlite3.Row:
    existing_meta = json_loads(package["metadata_json"])
    merged_meta = {**existing_meta, **(metadata or {})}
    conn.execute(
        """
        UPDATE packages
        SET canonical_package_id = ?, package_role = ?, content_bucket = ?, contract_type = ?,
            consumer_ready = ?, warehouse_status = ?, metadata_json = ?
        WHERE id = ?
        """,
        (
            canonical_package_id,
            package_role or package["package_role"],
            content_bucket or package["content_bucket"],
            contract_type or package["contract_type"],
            int(bool(consumer_ready or package["consumer_ready"])),
            warehouse_status if warehouse_status else package["warehouse_status"],
            json.dumps(merged_meta, ensure_ascii=True, sort_keys=True),
            package["id"],
        ),
    )
    conn.commit()
    return conn.execute("SELECT * FROM packages WHERE id = ?", (package["id"],)).fetchone()


def _sample_for_source(conn: sqlite3.Connection, source_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT samples.*
        FROM samples
        JOIN source_links
          ON source_links.entity_type = 'sample' AND source_links.entity_id = samples.id
        WHERE source_links.source_id = ?
        ORDER BY samples.id
        LIMIT 1
        """,
        (source_id,),
    ).fetchone()


def _import_preflight_batch_summary(
    conn: sqlite3.Connection,
    *,
    root_id: int,
    root_path: Path,
    report_path: Path,
    counts: Counter,
) -> None:
    payload = _load_json(report_path)
    ensure_report_import(
        conn,
        root_id=root_id,
        report_kind="preflight_batch_summary",
        report_path=str(report_path),
        metadata={"root_relative_path": _safe_relative(root_path, report_path)},
    )
    for entry in payload.get("reports") or []:
        sample_alias = str(entry.get("sample_id") or "").strip() or None
        package_alias = str(entry.get("package_id") or "").strip() or None
        source_file = entry.get("source_file")
        source_relative_path = entry.get("source_relative_path")
        fallback_name = Path(str(source_file or source_relative_path or package_alias or sample_alias or "sample")).stem
        _, sample, package = _ensure_sample_and_package_from_entry(
            conn,
            root_id=root_id,
            root_path=root_path,
            external_system="3dgirls",
            sample_alias=sample_alias,
            package_alias=package_alias,
            display_name=fallback_name,
            source_file=source_file,
            source_relative_path=source_relative_path,
            source_format=normalize_ext(source_file or source_relative_path or ""),
            package_role=str(entry.get("package_role") or "unknown"),
            warehouse_status="cataloged",
            sample_metadata={"source_relative_path": source_relative_path or ""},
            package_metadata={
                "risk_level": entry.get("risk_level"),
                "warning_count": entry.get("warning_count"),
                "report_path": entry.get("report_path"),
                "source_relative_path": source_relative_path or "",
            },
        )
        ensure_artifact(
            conn,
            owner_type="package",
            owner_id=package["id"],
            stage="preflight",
            artifact_kind="preflight_batch_summary",
            path=str(report_path),
            format="json",
            status="imported",
            metadata={
                "sample_alias": sample_alias or "",
                "package_alias": package_alias or "",
                "report_path": entry.get("report_path") or "",
            },
        )
        counts["samples"] += int(sample_alias is not None)
        counts["packages"] += 1


def _import_preflight_report(
    conn: sqlite3.Connection,
    *,
    root_id: int,
    root_path: Path,
    report_path: Path,
    counts: Counter,
) -> None:
    payload = _load_json(report_path)
    sample_alias = str(payload.get("sample_id") or "").strip() or None
    source_file = payload.get("source_file")
    source_relative_path = payload.get("source_relative_path")
    display_name = _best_display_name(payload, Path(str(source_file or source_relative_path or report_path.parent.name)).stem)
    source = _source_record(
        conn,
        root_id=root_id,
        root_path=root_path,
        source_file=source_file,
        source_relative_path=source_relative_path,
        source_kind="legacy_workspace",
        metadata={"preflight_report_path": str(report_path)},
    )
    sample = find_entity_by_alias(
        conn,
        entity_type="sample",
        external_system="3dgirls",
        alias_value=sample_alias or "",
        alias_key="sample_id",
    )
    if sample is None:
        _, sample = _ensure_sample_only_from_preflight(
            conn,
            root_id=root_id,
            root_path=root_path,
            sample_alias=sample_alias,
            display_name=display_name,
            source_file=source_file,
            source_relative_path=source_relative_path,
            source_format=str(payload.get("source_format") or normalize_ext(source_file or source_relative_path or "")),
            warehouse_status="classified" if (payload.get("risk") or {}).get("can_convert", True) else "blocked",
            sample_metadata={
                "catalog_context": payload.get("catalog_context") or {},
                "model": payload.get("model") or {},
                "risk": payload.get("risk") or {},
                "suggested_profile": payload.get("suggested_profile") or "",
            },
        )
    else:
        ensure_source_link(conn, source_id=source["id"], entity_type="sample", entity_id=sample["id"])

    ensure_report_import(
        conn,
        root_id=root_id,
        report_kind="preflight_report",
        report_path=str(report_path),
        external_run_id=sample_alias or "",
        metadata={"root_relative_path": _safe_relative(root_path, report_path)},
    )
    ensure_artifact(
        conn,
        owner_type="sample",
        owner_id=sample["id"],
        stage="preflight",
        artifact_kind="preflight_report",
        path=str(report_path),
        format="json",
        status="imported",
        metadata={
            "source_file": source_file or "",
            "source_relative_path": source_relative_path or "",
            "risk": payload.get("risk") or {},
            "counts": payload.get("counts") or {},
            "display_name": display_name,
        },
    )
    counts["preflight_reports"] += 1


def _import_conversion_like_report(
    conn: sqlite3.Connection,
    *,
    root_id: int,
    root_path: Path,
    report_path: Path,
    report_kind: str,
    counts: Counter,
) -> None:
    payload = _load_json(report_path)
    relative_path = _safe_relative(root_path, report_path)
    lower_relative = relative_path.lower()
    stage = "verification" if "_fbx_out_v1_verify" in lower_relative else "conversion"
    source_file = payload.get("source_file")
    source = _source_record(
        conn,
        root_id=root_id,
        root_path=root_path,
        source_file=source_file,
        source_relative_path=payload.get("source_relative_path"),
        source_kind="legacy_workspace",
        metadata={"report_path": str(report_path)},
    )
    sample = _sample_for_source(conn, source["id"])
    if sample is None:
        fallback_name = Path(str(source_file or report_path.parent.name)).stem
        _, sample, _ = _ensure_sample_and_package_from_entry(
            conn,
            root_id=root_id,
            root_path=root_path,
            external_system="3dgirls",
            sample_alias=None,
            package_alias=None,
            display_name=fallback_name,
            source_file=source_file,
            source_relative_path=None,
            source_format=normalize_ext(source_file or ""),
            package_role="unknown",
            warehouse_status="classified",
            sample_metadata={"created_from": report_kind},
            package_metadata={"created_from": report_kind},
        )

    ensure_report_import(
        conn,
        root_id=root_id,
        report_kind=report_kind,
        report_path=str(report_path),
        metadata={"root_relative_path": relative_path},
    )
    ensure_artifact(
        conn,
        owner_type="sample",
        owner_id=sample["id"],
        stage=stage,
        artifact_kind=report_kind,
        path=str(report_path),
        format="json",
        status="imported",
        metadata={
            "source_file": source_file or "",
            "output_fbx": payload.get("output_fbx") or "",
            "split_outputs": payload.get("split_outputs") or {},
        },
    )

    if report_kind == "conversion_manifest":
        output_fbx = payload.get("output_fbx")
        if isinstance(output_fbx, str) and output_fbx.strip():
            ensure_artifact(
                conn,
                owner_type="sample",
                owner_id=sample["id"],
                stage="conversion",
                artifact_kind="output_fbx",
                path=str(output_fbx),
                format=normalize_ext(output_fbx),
                status="observed",
                metadata={"manifest_path": str(report_path)},
            )
        for split_name, split_path in (payload.get("split_outputs") or {}).items():
            if not isinstance(split_path, str) or not split_path.strip():
                continue
            ensure_artifact(
                conn,
                owner_type="sample",
                owner_id=sample["id"],
                stage="conversion",
                artifact_kind=f"split_output:{slugify(split_name)}",
                path=str(split_path),
                format=normalize_ext(split_path),
                status="observed",
                metadata={"manifest_path": str(report_path)},
            )
    counts[report_kind] += 1


def _import_summary_report(
    conn: sqlite3.Connection,
    *,
    root_id: int,
    root_path: Path,
    report_path: Path,
    counts: Counter,
) -> None:
    payload = _load_json(report_path)
    ensure_report_import(
        conn,
        root_id=root_id,
        report_kind="ue_suite_summary",
        report_path=str(report_path),
        metadata={"root_relative_path": _safe_relative(root_path, report_path)},
    )
    for entry in payload.get("successes") or []:
        legacy_sample_id = str(entry.get("sample_id") or "").strip() or None
        legacy_package_id = str(entry.get("package_id") or "").strip() or None
        sample = None
        if legacy_sample_id:
            sample = find_entity_by_alias(
                conn,
                entity_type="sample",
                external_system="3dgirls",
                alias_value=legacy_sample_id,
                alias_key="sample_id",
            )
        source_relative_path = str(entry.get("source_relative_path") or "").strip() or None
        manifest_path = str(entry.get("manifest_path") or "").strip() or None
        if sample is None:
            fallback_name = Path(str(source_relative_path or legacy_package_id or "sample")).stem
            _, sample = _ensure_sample_only_from_preflight(
                conn,
                root_id=root_id,
                root_path=root_path,
                sample_alias=legacy_sample_id,
                display_name=fallback_name,
                source_file=None,
                source_relative_path=source_relative_path,
                source_format="unknown",
                warehouse_status="cataloged",
                sample_metadata={"created_from": "ue_suite_summary"},
            )

        package = _candidate_package_for_summary_entry(
            conn,
            sample_id=sample["id"],
            package_alias=legacy_package_id,
            source_relative_path=source_relative_path,
            manifest_path=manifest_path,
        )
        identity_basis = _package_identity_basis(entry)
        package_role = str(entry.get("package_role") or "unknown")
        content_bucket = str(entry.get("content_bucket") or "unknown")
        contract_type = str(entry.get("contract_type") or "unknown")
        package_metadata = {
            "created_from": "ue_suite_summary",
            "manifest_path": manifest_path or "",
            "source_relative_path": source_relative_path or "",
            "score": entry.get("score"),
            "status": entry.get("status"),
            "legacy_identity_basis": identity_basis,
        }
        if package is None:
            package_key = legacy_package_id or source_relative_path or manifest_path or sample["canonical_sample_id"]
            canonical_package_id = make_canonical_id("pkg", "3dgirls", package_key, display_name=package_key)
            package = ensure_package(
                conn,
                sample_id=sample["id"],
                canonical_package_id=canonical_package_id,
                package_role=package_role,
                content_bucket=content_bucket,
                contract_type=contract_type,
                consumer_ready=bool(entry.get("consumer_ready")),
                warehouse_status="ready_for_pipeline" if entry.get("consumer_ready") else "classified",
                metadata=package_metadata,
            )
        else:
            canonical_package_id = package["canonical_package_id"]
            if legacy_package_id and _package_alias_count(conn, package["id"]) == 0:
                canonical_package_id = make_canonical_id("pkg", "3dgirls", legacy_package_id, display_name=legacy_package_id)
            package = _rebind_package_identity(
                conn,
                package=package,
                canonical_package_id=canonical_package_id,
                package_role=package_role or package["package_role"],
                content_bucket=content_bucket or package["content_bucket"],
                contract_type=contract_type or package["contract_type"],
                consumer_ready=bool(entry.get("consumer_ready")),
                warehouse_status="ready_for_pipeline" if entry.get("consumer_ready") else "classified",
                metadata=package_metadata,
            )
        if legacy_package_id:
            ensure_alias(
                conn,
                entity_type="package",
                entity_id=package["id"],
                external_system="3dgirls",
                alias_key="package_id",
                alias_value=legacy_package_id,
            )
        elif source_relative_path:
            _ensure_optional_package_alias(
                conn,
                package_id=package["id"],
                alias_key="source_relative_path",
                alias_value=source_relative_path,
            )
        if manifest_path:
            _ensure_optional_package_alias(
                conn,
                package_id=package["id"],
                alias_key="manifest_path",
                alias_value=manifest_path,
            )
        ensure_artifact(
            conn,
            owner_type="package",
            owner_id=package["id"],
            stage="session",
            artifact_kind="ue_suite_summary",
            path=str(report_path),
            format="json",
            status="imported",
            metadata=entry,
        )
        counts["summary_entries"] += 1


def _import_root_level_report(
    conn: sqlite3.Connection,
    *,
    root_id: int,
    report_path: Path,
    report_kind: str,
    counts: Counter,
) -> None:
    payload = _load_json(report_path)
    ensure_report_import(
        conn,
        root_id=root_id,
        report_kind=report_kind,
        report_path=str(report_path),
        metadata={"top_level_keys": sorted(payload.keys())},
    )
    counts[report_kind] += 1


def import_legacy_3dgirls(conn: sqlite3.Connection, paths: ProjectPaths, root_id: int) -> dict[str, object]:
    root = get_root(conn, root_id)
    root_path = Path(root["path"])
    if not root_path.exists():
        raise FileNotFoundError(root_path)

    counts: Counter[str] = Counter()

    for pattern in SUMMARY_GLOBS:
        for report_path in sorted(root_path.glob(pattern)):
            if report_path.name == "preflight_batch_summary.json":
                _import_preflight_batch_summary(conn, root_id=root_id, root_path=root_path, report_path=report_path, counts=counts)
            elif report_path.name == "ue_suite_summary.json":
                _import_summary_report(conn, root_id=root_id, root_path=root_path, report_path=report_path, counts=counts)

    for report_path in sorted(root_path.glob(PRELIGHT_GLOB)):
        _import_preflight_report(conn, root_id=root_id, root_path=root_path, report_path=report_path, counts=counts)

    for report_path in sorted(root_path.glob(CONVERSION_MANIFEST_GLOB)):
        relative = _safe_relative(root_path, report_path).lower()
        if "_ue_suite_reports/" in relative.replace("\\", "/"):
            continue
        _import_conversion_like_report(
            conn,
            root_id=root_id,
            root_path=root_path,
            report_path=report_path,
            report_kind="conversion_manifest",
            counts=counts,
        )

    for report_path in sorted(root_path.glob(MATERIALS_REPORT_GLOB)):
        _import_conversion_like_report(
            conn,
            root_id=root_id,
            root_path=root_path,
            report_path=report_path,
            report_kind="materials_report",
            counts=counts,
        )

    for report_path in sorted(root_path.glob(WEAPON_REPORT_GLOB)):
        _import_root_level_report(conn, root_id=root_id, report_path=report_path, report_kind="weapon_report", counts=counts)

    for pattern in EQUIPMENT_REPORT_GLOBS:
        for report_path in sorted(root_path.glob(pattern)):
            _import_root_level_report(conn, root_id=root_id, report_path=report_path, report_kind=report_path.stem, counts=counts)

    sample_count = conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
    package_count = conn.execute("SELECT COUNT(*) FROM packages").fetchone()[0]
    source_count = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
    report_count = conn.execute("SELECT COUNT(*) FROM report_imports WHERE root_id = ?", (root_id,)).fetchone()[0]

    return {
        "root_id": root_id,
        "root_name": root["display_name"],
        "samples_total": sample_count,
        "packages_total": package_count,
        "sources_total": source_count,
        "reports_total": report_count,
        "import_counts": dict(counts),
    }
