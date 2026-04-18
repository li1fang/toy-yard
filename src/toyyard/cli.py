from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from toyyard.audio_index_packet import export_audio_index_packet, import_audio_index_packet
from toyyard.audio_session import import_audio_session
from toyyard.aiue_roundtrip import import_aiue_results
from toyyard.aiue_export import export_aiue_pmx_view
from toyyard.aiue_motion_export import export_aiue_motion_view
from toyyard.aiue_motion_roundtrip import import_aiue_motion_results
from toyyard.bodypaint_export import export_bodypaint_view
from toyyard.bodypaint_roundtrip import import_bodypaint_results
from toyyard.communication_signal import (
    build_aiue_motion_export_signal_from_paths,
    build_aiue_pmx_export_signal_from_paths,
    build_bodypaint_export_signal_from_paths,
    build_motion_catalog_signal,
)
from toyyard.constants import DEFAULT_ROOT, ROOT_KINDS, SOURCE_KINDS
from toyyard.db import connect, init_db
from toyyard.ingest import format_import_rows, import_path, scan_source
from toyyard.image_shadow_import import import_wallpaper_engine_catalog
from toyyard.legacy_3dgirls import import_legacy_3dgirls
from toyyard.motion_handoff import import_motion_handoff
from toyyard.paths import ProjectPaths
from toyyard.remote_operator import (
    get_operator_node,
    list_operator_nodes,
    register_remote_host,
    remote_handoff_audio_index,
    remote_probe,
    remote_status,
    remote_sync_code,
    remote_toyyard,
)
from toyyard.repair import repair_legacy_3dgirls_lineage
from toyyard.reports import aiue_ready_rows, audio_catalog_rows, blocked_rows, failure_rows, image_catalog_rows, lineage_gap_rows, motion_catalog_rows, ready_rows
from toyyard.reports import image_pick_rows
from toyyard.rules import load_rules
from toyyard.triage import run_triage
from toyyard.util import print_rows
from toyyard.wallpaper_engine_extractor import DEFAULT_WORKSHOP_ROOT, extract_wallpaper_engine_frames
from toyyard.warehouse import (
    find_entity_by_alias,
    get_root,
    get_source,
    package_aliases,
    register_root,
    resolve_sample,
    sample_aliases,
    sample_artifacts,
    sample_packages,
    sample_sources,
    seed_canonical_root,
    source_artifacts,
    source_linked_samples,
)


def _paths(root: str | None) -> ProjectPaths:
    return ProjectPaths(Path(root) if root else DEFAULT_ROOT)


def _open_db(paths: ProjectPaths):
    if not paths.db_path.exists():
        raise SystemExit(f"Database not initialized at {paths.db_path}. Run `toyyard init` first.")
    return init_db(paths.db_path)


def _print_sample_bundle(conn, sample_row) -> None:
    packages = sample_packages(conn, sample_row["id"])
    sources = sample_sources(conn, sample_row["id"])
    artifacts = sample_artifacts(conn, sample_row["id"])
    aliases = sample_aliases(conn, sample_row["id"])

    print("Sample")
    print_rows([dict(sample_row)])
    print("\nSample Aliases")
    print_rows([dict(row) for row in aliases])
    print("\nSources")
    print_rows([dict(row) for row in sources])
    print("\nPackages")
    print_rows([dict(row) for row in packages])
    print("\nArtifacts")
    print_rows([dict(row) for row in artifacts])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="toyyard")
    parser.add_argument("--root", default=str(DEFAULT_ROOT), help="Project root path")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="Create folder skeleton, SQLite DB, and default rule files")

    ingest_parser = subparsers.add_parser("ingest", help="Import or scan source packages")
    ingest_subparsers = ingest_parser.add_subparsers(dest="ingest_command", required=True)

    import_parser = ingest_subparsers.add_parser("import", help="Import one file or directory")
    import_parser.add_argument("path")
    import_parser.add_argument("--source", default="unknown", choices=SOURCE_KINDS)
    import_parser.add_argument("--game", default="unknown")

    scan_parser = ingest_subparsers.add_parser("scan", help="Scan a configured source inbox")
    scan_parser.add_argument("--source", required=True, choices=("manual_drop", "vortex"))
    scan_parser.add_argument("--game", default=None)

    root_parser = subparsers.add_parser("root", help="Manage registered physical roots")
    root_subparsers = root_parser.add_subparsers(dest="root_command", required=True)
    root_add_parser = root_subparsers.add_parser("add", help="Register a physical root")
    root_add_parser.add_argument("--kind", required=True, choices=ROOT_KINDS)
    root_add_parser.add_argument("--path", required=True)
    root_add_parser.add_argument("--name", required=True)

    import_root_parser = subparsers.add_parser("import", help="Import external or legacy roots")
    import_root_subparsers = import_root_parser.add_subparsers(dest="import_command", required=True)
    import_legacy_parser = import_root_subparsers.add_parser("legacy-3dgirls", help="Import 3dgirls as a legacy workspace root")
    import_legacy_parser.add_argument("--root-id", required=True, type=int)
    import_aiue_results_parser = import_root_subparsers.add_parser("aiue-results", help="Import AiUE trial results back into toy-yard")
    import_aiue_results_parser.add_argument("--export-root", required=True)
    import_aiue_results_parser.add_argument("--trial-root", required=True)
    import_image_shadow_parser = import_root_subparsers.add_parser("wallpaper-engine-catalog", help="Import extracted Wallpaper Engine images into the image shadow catalog")
    import_image_shadow_parser.add_argument("--state-path", default=None)
    import_aiue_motion_results_parser = import_root_subparsers.add_parser("aiue-motion-results", help="Import AiUE motion trial results back into toy-yard")
    import_aiue_motion_results_parser.add_argument("--export-root", required=True)
    import_aiue_motion_results_parser.add_argument("--trial-root", required=True)
    import_motion_parser = import_root_subparsers.add_parser("motion-handoff", help="Import one motion handoff package into the canonical motion catalog")
    import_motion_parser.add_argument("--package-id", required=True, type=int)
    import_audio_session_parser = import_root_subparsers.add_parser("audio-session", help="Import one ComfyUI audio session into the canonical audio catalog")
    import_audio_session_parser.add_argument("--manifest-path", required=True)
    import_audio_index_packet_parser = import_root_subparsers.add_parser("audio-index-packet", help="Import one SSH-synced audio index packet without copying media data")
    import_audio_index_packet_parser.add_argument("--packet-path", required=True)
    import_bodypaint_results_parser = import_root_subparsers.add_parser("bodypaint-results", help="Import BodyPaint processor outputs back into toy-yard")
    import_bodypaint_results_parser.add_argument("--profile", required=True)
    import_bodypaint_results_parser.add_argument("--package", dest="packages", action="append", default=[])

    extract_parser = subparsers.add_parser("extract", help="Run extractors over external media roots")
    extract_subparsers = extract_parser.add_subparsers(dest="extract_command", required=True)
    extract_wallpaper_parser = extract_subparsers.add_parser("wallpaper-engine", help="Extract one middle frame per Wallpaper Engine MP4 into toy-yard-managed workbench storage")
    extract_wallpaper_parser.add_argument("--workshop-root", default=str(DEFAULT_WORKSHOP_ROOT))
    extract_wallpaper_parser.add_argument("--ffmpeg", default=None)
    extract_wallpaper_parser.add_argument("--ffprobe", default=None)
    extract_wallpaper_parser.add_argument("--limit", type=int, default=None)
    extract_wallpaper_parser.add_argument("--item-id", default=None)
    extract_wallpaper_parser.add_argument("--force", action="store_true")

    inspect_parser = subparsers.add_parser("inspect", help="Inspect catalog entities")
    inspect_subparsers = inspect_parser.add_subparsers(dest="inspect_command", required=True)
    package_parser = inspect_subparsers.add_parser("package", help="Inspect one v1 intake package")
    package_parser.add_argument("package_id", type=int)
    sample_parser = inspect_subparsers.add_parser("sample", help="Inspect one canonical sample")
    sample_parser.add_argument("sample_id")
    image_parser = inspect_subparsers.add_parser("image", help="Inspect one image-shadow sample with frame artifacts")
    image_parser.add_argument("sample_ref")
    source_parser = inspect_subparsers.add_parser("source", help="Inspect one canonical source")
    source_parser.add_argument("source_id", type=int)
    package_alias_parser = inspect_subparsers.add_parser("package-alias", help="Inspect a canonical package via external alias")
    package_alias_parser.add_argument("--external-system", required=True)
    package_alias_parser.add_argument("--alias-key", default="package_id")
    package_alias_parser.add_argument("--alias-value", required=True)

    triage_parser = subparsers.add_parser("triage", help="Run triage against a v1 intake package")
    triage_subparsers = triage_parser.add_subparsers(dest="triage_command", required=True)
    triage_run_parser = triage_subparsers.add_parser("run", help="Triage one package")
    triage_run_parser.add_argument("package_id", type=int)

    lineage_parser = subparsers.add_parser("lineage", help="Show canonical lineage")
    lineage_subparsers = lineage_parser.add_subparsers(dest="lineage_command", required=True)
    lineage_show_parser = lineage_subparsers.add_parser("show", help="Show lineage for a sample")
    lineage_show_parser.add_argument("sample_id")

    export_parser = subparsers.add_parser("export", help="Export compatibility views")
    export_subparsers = export_parser.add_subparsers(dest="export_command", required=True)
    export_aiue_parser = export_subparsers.add_parser("aiue-pmx-view", help="Export a toy-yard sample as an AiUE PMX compatibility view")
    export_aiue_parser.add_argument("--profile", required=True)
    export_aiue_parser.add_argument("--sample", required=True)
    export_aiue_parser.add_argument("--include-verify", action="store_true")
    export_motion_parser = export_subparsers.add_parser("aiue-motion-view", help="Export a toy-yard motion sample as an AiUE motion packet")
    export_motion_parser.add_argument("--profile", required=True)
    export_motion_parser.add_argument("--sample", default=None)
    export_motion_parser.add_argument("--package", dest="packages", action="append", default=[])
    export_bodypaint_parser = export_subparsers.add_parser("bodypaint-view", help="Export a BodyPaint processor/viewer packet")
    export_bodypaint_parser.add_argument("--profile", required=True)
    export_bodypaint_parser.add_argument("--sample", default=None)
    export_bodypaint_parser.add_argument("--package", dest="packages", action="append", default=[])
    export_bodypaint_parser.add_argument("--aiue-pmx-profile", default=None)
    export_audio_index_parser = export_subparsers.add_parser("audio-index-packet", help="Export one canonical audio session as an index-only packet for SSH replication")
    export_audio_index_parser.add_argument("--session-id", required=True)
    export_audio_index_parser.add_argument("--node-id", required=True)
    export_audio_index_parser.add_argument("--output", default=None)

    report_parser = subparsers.add_parser("report", help="Run catalog reports")
    report_subparsers = report_parser.add_subparsers(dest="report_command", required=True)
    ready_parser = report_subparsers.add_parser("ready", help="List v1 assets ready for a target")
    ready_parser.add_argument("--target", default="unreal")
    report_subparsers.add_parser("blocked", help="List v1 blocked assets")
    failures_parser = report_subparsers.add_parser("failures", help="List failures by stage")
    failures_parser.add_argument("--stage", required=True)
    report_subparsers.add_parser("aiue-ready", help="List canonical packages ready for AiUE consumption")
    report_subparsers.add_parser("lineage-gaps", help="List canonical lineage gaps")
    report_subparsers.add_parser("motion-catalog", help="List motion catalog packages")
    report_subparsers.add_parser("image-catalog", help="List image shadow catalog packages")
    report_subparsers.add_parser("audio-catalog", help="List canonical audio session packages")
    image_picks_parser = report_subparsers.add_parser("image-picks", help="List a small human-picking view for image shadow packages")
    image_picks_parser.add_argument("--limit", type=int, default=20)
    image_picks_parser.add_argument("--workshop-item-id", default=None)
    communication_signal_parser = report_subparsers.add_parser("communication-signal", help="Emit the latest machine-readable communication signal")
    communication_signal_parser.add_argument("--lane", choices=("pmx", "motion", "bodypaint"), default="pmx")
    communication_signal_parser.add_argument("--profile", default=None)

    repair_parser = subparsers.add_parser("repair", help="Repair canonical lineage and imported records")
    repair_subparsers = repair_parser.add_subparsers(dest="repair_command", required=True)
    repair_legacy_parser = repair_subparsers.add_parser("legacy-3dgirls-lineage", help="Repair imported 3dgirls lineage in-place")
    repair_legacy_parser.add_argument("--root-id", type=int, default=None)

    remote_parser = subparsers.add_parser("remote", help="Operate registered remote toy-yard nodes over SSH")
    remote_subparsers = remote_parser.add_subparsers(dest="remote_command", required=True)
    remote_register_parser = remote_subparsers.add_parser("register-host", help="Register one remote SSH host profile")
    remote_register_parser.add_argument("--profile-path", required=True)
    remote_register_parser.add_argument("--project-root", required=True)
    remote_register_parser.add_argument("--transport-topology", default="peer_to_peer")
    remote_subparsers.add_parser("list-hosts", help="List registered remote operator hosts")
    remote_probe_parser = remote_subparsers.add_parser("probe", help="Run one SSH reachability probe")
    remote_probe_parser.add_argument("--host", required=True)
    remote_status_parser = remote_subparsers.add_parser("status", help="Inspect remote project/runtime status")
    remote_status_parser.add_argument("--host", required=True)
    remote_sync_parser = remote_subparsers.add_parser("sync-code", help="Fetch/pull one remote repo and optionally refresh install")
    remote_sync_parser.add_argument("--host", required=True)
    remote_sync_parser.add_argument("--branch", required=True)
    remote_sync_parser.add_argument("--install-editable", action="store_true")
    remote_toyyard_parser = remote_subparsers.add_parser("toyyard", help="Run one approved remote toyyard command")
    remote_toyyard_parser.add_argument("--host", required=True)
    remote_toyyard_parser.add_argument("toyyard_args", nargs=argparse.REMAINDER)

    remote_handoff_parser = remote_subparsers.add_parser("handoff", help="Run one remote packet handoff flow")
    remote_handoff_subparsers = remote_handoff_parser.add_subparsers(dest="remote_handoff_command", required=True)
    remote_handoff_audio_parser = remote_handoff_subparsers.add_parser("audio-index", help="Run one peer-to-peer audio index packet handoff")
    remote_handoff_audio_parser.add_argument("--source-host", required=True)
    remote_handoff_audio_parser.add_argument("--target-host", required=True)
    remote_handoff_audio_parser.add_argument("--session-id", required=True)
    remote_handoff_audio_parser.add_argument("--source-node-id", required=True)
    remote_handoff_audio_parser.add_argument("--target-transfer-profile", required=True)
    remote_handoff_audio_parser.add_argument("--import-target", action="store_true")

    return parser


def cmd_init(paths: ProjectPaths) -> int:
    paths.ensure_layout()
    conn = init_db(paths.db_path)
    seed_canonical_root(conn, paths.root)
    conn.close()
    print(f"Initialized toy-yard at {paths.root}")
    return 0


def cmd_ingest_import(paths: ProjectPaths, args: argparse.Namespace) -> int:
    conn = _open_db(paths)
    row = import_path(conn, paths, Path(args.path), source_kind=args.source, game_hint=args.game)
    print_rows(format_import_rows([row]))
    conn.close()
    return 0


def cmd_ingest_scan(paths: ProjectPaths, args: argparse.Namespace) -> int:
    conn = _open_db(paths)
    rows = scan_source(conn, paths, args.source, game=args.game)
    print_rows(format_import_rows(rows))
    conn.close()
    return 0


def cmd_root_add(paths: ProjectPaths, args: argparse.Namespace) -> int:
    conn = _open_db(paths)
    row = register_root(conn, args.kind, args.path, args.name)
    print_rows([dict(row)])
    conn.close()
    return 0


def cmd_import_legacy_3dgirls(paths: ProjectPaths, root_id: int) -> int:
    conn = _open_db(paths)
    result = import_legacy_3dgirls(conn, paths, root_id)
    print_rows([result])
    conn.close()
    return 0


def cmd_import_aiue_results(paths: ProjectPaths, export_root: str, trial_root: str) -> int:
    conn = _open_db(paths)
    result = import_aiue_results(
        conn,
        paths,
        export_root=Path(export_root),
        trial_root=Path(trial_root),
    )
    print_rows([result])
    conn.close()
    return 0


def cmd_import_motion_handoff(paths: ProjectPaths, package_id: int) -> int:
    conn = _open_db(paths)
    result = import_motion_handoff(conn, paths, package_id=package_id)
    print_rows([result])
    conn.close()
    return 0


def cmd_import_audio_session(paths: ProjectPaths, manifest_path: str) -> int:
    conn = _open_db(paths)
    result = import_audio_session(conn, paths, manifest_path=Path(manifest_path))
    conn.close()
    print(json.dumps(result, ensure_ascii=True))
    return 0


def cmd_import_audio_index_packet(paths: ProjectPaths, packet_path: str) -> int:
    conn = _open_db(paths)
    result = import_audio_index_packet(conn, paths, packet_path=Path(packet_path))
    conn.close()
    print(json.dumps(result, ensure_ascii=True))
    return 0


def cmd_import_bodypaint_results(paths: ProjectPaths, args: argparse.Namespace) -> int:
    conn = _open_db(paths)
    result = import_bodypaint_results(
        conn,
        paths,
        profile=args.profile,
        package_refs=args.packages,
    )
    conn.close()
    print_rows(
        [
            {
                "profile": result["profile"],
                "sample_ids": ",".join(result["sample_ids"]),
                "packages": len(result["packages"]),
                "artifacts": result["artifacts"],
                "report_path": result["report_path"],
            }
        ]
    )
    return 0


def cmd_import_aiue_motion_results(paths: ProjectPaths, export_root: str, trial_root: str) -> int:
    conn = _open_db(paths)
    result = import_aiue_motion_results(
        conn,
        paths,
        export_root=Path(export_root),
        trial_root=Path(trial_root),
    )
    print_rows([result])
    conn.close()
    return 0


def cmd_import_wallpaper_engine_catalog(paths: ProjectPaths, state_path: str | None) -> int:
    conn = _open_db(paths)
    result = import_wallpaper_engine_catalog(
        conn,
        paths,
        state_path=Path(state_path) if state_path else None,
    )
    print_rows([result])
    conn.close()
    return 0


def cmd_extract_wallpaper_engine(paths: ProjectPaths, args: argparse.Namespace) -> int:
    result = extract_wallpaper_engine_frames(
        paths,
        workshop_root=Path(args.workshop_root),
        ffmpeg_path=args.ffmpeg,
        ffprobe_path=args.ffprobe,
        limit=args.limit,
        item_id=args.item_id,
        force=bool(args.force),
    )
    print_rows([result])
    return 0


def cmd_inspect_package(paths: ProjectPaths, package_id: int) -> int:
    conn = _open_db(paths)
    package = conn.execute("SELECT * FROM v1_packages WHERE id = ?", (package_id,)).fetchone()
    if package is None:
        raise SystemExit(f"Unknown package id: {package_id}")
    assets = conn.execute(
        """
        SELECT id, asset_kind, display_name, completeness, readiness, primary_target, status
        FROM v1_assets
        WHERE package_id = ?
        ORDER BY id
        """,
        (package_id,),
    ).fetchall()
    deps = conn.execute(
        """
        SELECT subject_type, subject_id, dep_name, dep_kind, requirement_level, status, evidence
        FROM v1_dependencies
        WHERE subject_type = 'asset' AND subject_id IN (SELECT id FROM v1_assets WHERE package_id = ?)
        ORDER BY id
        """,
        (package_id,),
    ).fetchall()
    print("Package")
    print_rows([dict(package)])
    print("\nAssets")
    print_rows([dict(row) for row in assets])
    print("\nDependencies")
    print_rows([dict(row) for row in deps])
    conn.close()
    return 0


def cmd_inspect_sample(paths: ProjectPaths, sample_ref: str) -> int:
    conn = _open_db(paths)
    sample = resolve_sample(conn, sample_ref)
    if sample is None:
        raise SystemExit(f"Unknown sample: {sample_ref}")
    _print_sample_bundle(conn, sample)
    conn.close()
    return 0


def cmd_inspect_source(paths: ProjectPaths, source_id: int) -> int:
    conn = _open_db(paths)
    source = get_source(conn, source_id)
    artifacts = source_artifacts(conn, source_id)
    linked_samples = source_linked_samples(conn, source_id)
    print("Source")
    print_rows([dict(source)])
    print("\nSource Artifacts")
    print_rows([dict(row) for row in artifacts])
    print("\nLinked Samples")
    print_rows([dict(row) for row in linked_samples])
    conn.close()
    return 0


def cmd_inspect_package_alias(paths: ProjectPaths, external_system: str, alias_key: str, alias_value: str) -> int:
    conn = _open_db(paths)
    package = find_entity_by_alias(
        conn,
        entity_type="package",
        external_system=external_system,
        alias_value=alias_value,
        alias_key=alias_key,
    )
    if package is None:
        raise SystemExit(f"Unknown package alias: {external_system}:{alias_key}={alias_value}")
    sample = conn.execute("SELECT * FROM samples WHERE id = ?", (package["sample_id"],)).fetchone()
    aliases = package_aliases(conn, package["id"])
    print("Package")
    print_rows([dict(package)])
    print("\nSample")
    print_rows([dict(sample)] if sample else [])
    print("\nAliases")
    print_rows([dict(row) for row in aliases])
    conn.close()
    return 0


def cmd_lineage_show(paths: ProjectPaths, sample_ref: str) -> int:
    return cmd_inspect_sample(paths, sample_ref)


def cmd_triage_run(paths: ProjectPaths, package_id: int) -> int:
    conn = _open_db(paths)
    rules = load_rules(paths.rules_dir)
    result = run_triage(conn, paths, rules, package_id)
    print_rows([result])
    conn.close()
    return 0


def cmd_export_aiue_pmx_view(paths: ProjectPaths, args: argparse.Namespace) -> int:
    conn = _open_db(paths)
    result = export_aiue_pmx_view(
        conn,
        paths,
        profile=args.profile,
        sample_ref=args.sample,
        include_verify=bool(args.include_verify),
    )
    print_rows(
        [
            {
                "profile": result["profile"],
                "sample_id": result["sample_id"],
                "packages": len(result["packages"]),
                "has_conversion_manifest": result["has_conversion_manifest"],
                "export_root": result["export_root"],
                "summary_path": result["summary_path"],
                "manifest_artifact_check_path": result["manifest_artifact_check_path"],
                "communication_signal_path": result["communication_signal_path"],
                "trial_workspace_path": result["trial_workspace_path"],
            }
        ]
    )
    conn.close()
    return 0


def cmd_inspect_image(paths: ProjectPaths, sample_ref: str) -> int:
    conn = _open_db(paths)
    sample = resolve_sample(conn, sample_ref)
    if sample is None:
        raise SystemExit(f"Unknown image sample: {sample_ref}")
    packages = sample_packages(conn, sample["id"])
    print("Sample")
    print_rows([dict(sample)])
    print("\nImage Packages")
    print_rows([dict(row) for row in packages])
    for package in packages:
        artifacts = conn.execute(
            """
            SELECT artifact_kind, path, metadata_json
            FROM artifacts
            WHERE owner_type = 'package' AND owner_id = ?
            ORDER BY id
            """,
            (package["id"],),
        ).fetchall()
        image_rows = []
        for artifact in artifacts:
            metadata = json.loads(artifact["metadata_json"] or "{}")
            image_rows.append(
                {
                    "artifact_kind": artifact["artifact_kind"],
                    "path": artifact["path"],
                    "video_relative_path": metadata.get("video_relative_path", ""),
                    "duration_sec": metadata.get("duration_sec", ""),
                    "midpoint_sec": metadata.get("midpoint_sec", ""),
                    "image_role": metadata.get("image_role", ""),
                }
            )
        print(f"\nArtifacts For {package['canonical_package_id']}")
        print_rows(image_rows)
    conn.close()
    return 0


def cmd_export_aiue_motion_view(paths: ProjectPaths, args: argparse.Namespace) -> int:
    sample_mode = bool(args.sample)
    package_mode = bool(args.packages)
    if sample_mode == package_mode:
        raise SystemExit("`toyyard export aiue-motion-view` requires exactly one of --sample or --package.")
    conn = _open_db(paths)
    result = export_aiue_motion_view(
        conn,
        paths,
        profile=args.profile,
        sample_ref=args.sample,
        package_refs=args.packages,
    )
    print_rows(
        [
            {
                "profile": result["profile"],
                "sample_id": result["sample_id"],
                "sample_ids": ",".join(result["sample_ids"]),
                "scenario_ids": ",".join(result["scenario_ids"]),
                "packages": len(result["packages"]),
                "export_root": result["export_root"],
                "summary_path": result["summary_path"],
                "registry_path": result["registry_path"],
                "motion_packet_check_path": result["motion_packet_check_path"],
                "communication_signal_path": result["communication_signal_path"],
                "trial_workspace_path": result["trial_workspace_path"],
            }
        ]
    )
    conn.close()
    return 0


def cmd_export_bodypaint_view(paths: ProjectPaths, args: argparse.Namespace) -> int:
    sample_mode = bool(args.sample)
    package_mode = bool(args.packages)
    if sample_mode == package_mode:
        raise SystemExit("`toyyard export bodypaint-view` requires exactly one of --sample or --package.")
    conn = _open_db(paths)
    result = export_bodypaint_view(
        conn,
        paths,
        profile=args.profile,
        sample_ref=args.sample,
        package_refs=args.packages,
        aiue_pmx_profile=args.aiue_pmx_profile,
    )
    print_rows(
        [
            {
                "profile": result["profile"],
                "sample_id": result["sample_id"],
                "sample_ids": ",".join(result["sample_ids"]),
                "packages": len(result["packages"]),
                "ready_items": result["ready_items"],
                "export_root": result["export_root"],
                "summary_path": result["summary_path"],
                "registry_path": result["registry_path"],
                "packet_check_path": result["packet_check_path"],
                "communication_signal_path": result["communication_signal_path"],
                "workspace_view_path": result["workspace_view_path"],
            }
        ]
    )
    conn.close()
    return 0


def cmd_export_audio_index_packet(paths: ProjectPaths, args: argparse.Namespace) -> int:
    conn = _open_db(paths)
    result = export_audio_index_packet(
        conn,
        paths,
        session_id=args.session_id,
        node_id=args.node_id,
        output_path=Path(args.output) if args.output else None,
    )
    conn.close()
    print(json.dumps(result, ensure_ascii=True))
    return 0


def cmd_report_ready(paths: ProjectPaths, target: str) -> int:
    conn = _open_db(paths)
    print_rows(ready_rows(conn, target))
    conn.close()
    return 0


def cmd_report_blocked(paths: ProjectPaths) -> int:
    conn = _open_db(paths)
    print_rows(blocked_rows(conn))
    conn.close()
    return 0


def cmd_report_failures(paths: ProjectPaths, stage: str) -> int:
    conn = _open_db(paths)
    print_rows(failure_rows(conn, stage))
    conn.close()
    return 0


def cmd_report_aiue_ready(paths: ProjectPaths) -> int:
    conn = _open_db(paths)
    print_rows(aiue_ready_rows(conn))
    conn.close()
    return 0


def cmd_report_lineage_gaps(paths: ProjectPaths) -> int:
    conn = _open_db(paths)
    print_rows(lineage_gap_rows(conn))
    conn.close()
    return 0


def cmd_report_motion_catalog(paths: ProjectPaths) -> int:
    conn = _open_db(paths)
    print_rows(motion_catalog_rows(conn))
    conn.close()
    return 0


def cmd_report_image_catalog(paths: ProjectPaths) -> int:
    conn = _open_db(paths)
    print_rows(image_catalog_rows(conn))
    conn.close()
    return 0


def cmd_report_audio_catalog(paths: ProjectPaths) -> int:
    conn = _open_db(paths)
    print_rows(audio_catalog_rows(conn))
    conn.close()
    return 0


def cmd_report_image_picks(paths: ProjectPaths, limit: int, workshop_item_id: str | None) -> int:
    conn = _open_db(paths)
    print_rows(image_pick_rows(conn, limit=limit, workshop_item_id=workshop_item_id))
    conn.close()
    return 0


def cmd_report_communication_signal(paths: ProjectPaths, lane: str, profile: str | None) -> int:
    if lane == "pmx":
        if not profile:
            raise SystemExit("`toyyard report communication-signal --lane pmx` requires --profile.")
        payload = build_aiue_pmx_export_signal_from_paths(paths, profile)
        print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
        return 0

    if lane == "bodypaint":
        if not profile:
            raise SystemExit("`toyyard report communication-signal --lane bodypaint` requires --profile.")
        payload = build_bodypaint_export_signal_from_paths(paths, profile)
        print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
        return 0

    if profile:
        payload = build_aiue_motion_export_signal_from_paths(paths, profile)
        print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
        return 0

    conn = _open_db(paths)
    payload = build_motion_catalog_signal(conn)
    conn.close()
    print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


def cmd_repair_legacy_3dgirls_lineage(paths: ProjectPaths) -> int:
    conn = _open_db(paths)
    result = repair_legacy_3dgirls_lineage(conn)
    print_rows([result or {"status": "no_changes"}])
    conn.close()
    return 0


def cmd_remote_register_host(paths: ProjectPaths, args: argparse.Namespace) -> int:
    conn = _open_db(paths)
    result = register_remote_host(
        conn,
        paths,
        profile_path=Path(args.profile_path),
        project_root=args.project_root,
        transport_topology=args.transport_topology,
    )
    conn.close()
    print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


def cmd_remote_list_hosts(paths: ProjectPaths) -> int:
    conn = _open_db(paths)
    rows = []
    for row in list_operator_nodes(conn):
        payload = dict(row)
        payload["metadata_json"] = json.loads(payload.get("metadata_json") or "{}")
        rows.append(payload)
    conn.close()
    print(json.dumps(rows, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


def cmd_remote_probe(paths: ProjectPaths, host: str) -> int:
    conn = _open_db(paths)
    result = remote_probe(conn, paths, host_ref=host)
    conn.close()
    print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


def cmd_remote_status(paths: ProjectPaths, host: str) -> int:
    conn = _open_db(paths)
    result = remote_status(conn, paths, host_ref=host)
    conn.close()
    print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


def cmd_remote_sync_code(paths: ProjectPaths, args: argparse.Namespace) -> int:
    conn = _open_db(paths)
    result = remote_sync_code(
        conn,
        paths,
        host_ref=args.host,
        branch=args.branch,
        install_editable=bool(args.install_editable),
    )
    conn.close()
    print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


def cmd_remote_toyyard(paths: ProjectPaths, args: argparse.Namespace) -> int:
    conn = _open_db(paths)
    result = remote_toyyard(conn, paths, host_ref=args.host, toyyard_args=args.toyyard_args)
    conn.close()
    print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


def cmd_remote_handoff_audio_index(paths: ProjectPaths, args: argparse.Namespace) -> int:
    conn = _open_db(paths)
    result = remote_handoff_audio_index(
        conn,
        paths,
        source_host_ref=args.source_host,
        target_host_ref=args.target_host,
        session_id=args.session_id,
        source_node_id=args.source_node_id,
        target_transfer_profile=args.target_transfer_profile,
        import_target=bool(args.import_target),
    )
    conn.close()
    print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    paths = _paths(args.root)

    if args.command == "init":
        return cmd_init(paths)
    if args.command == "ingest" and args.ingest_command == "import":
        return cmd_ingest_import(paths, args)
    if args.command == "ingest" and args.ingest_command == "scan":
        return cmd_ingest_scan(paths, args)
    if args.command == "root" and args.root_command == "add":
        return cmd_root_add(paths, args)
    if args.command == "import" and args.import_command == "legacy-3dgirls":
        return cmd_import_legacy_3dgirls(paths, args.root_id)
    if args.command == "import" and args.import_command == "aiue-results":
        return cmd_import_aiue_results(paths, args.export_root, args.trial_root)
    if args.command == "import" and args.import_command == "wallpaper-engine-catalog":
        return cmd_import_wallpaper_engine_catalog(paths, args.state_path)
    if args.command == "import" and args.import_command == "aiue-motion-results":
        return cmd_import_aiue_motion_results(paths, args.export_root, args.trial_root)
    if args.command == "import" and args.import_command == "motion-handoff":
        return cmd_import_motion_handoff(paths, args.package_id)
    if args.command == "import" and args.import_command == "audio-session":
        return cmd_import_audio_session(paths, args.manifest_path)
    if args.command == "import" and args.import_command == "audio-index-packet":
        return cmd_import_audio_index_packet(paths, args.packet_path)
    if args.command == "import" and args.import_command == "bodypaint-results":
        return cmd_import_bodypaint_results(paths, args)
    if args.command == "extract" and args.extract_command == "wallpaper-engine":
        return cmd_extract_wallpaper_engine(paths, args)
    if args.command == "inspect" and args.inspect_command == "package":
        return cmd_inspect_package(paths, args.package_id)
    if args.command == "inspect" and args.inspect_command == "sample":
        return cmd_inspect_sample(paths, args.sample_id)
    if args.command == "inspect" and args.inspect_command == "image":
        return cmd_inspect_image(paths, args.sample_ref)
    if args.command == "inspect" and args.inspect_command == "source":
        return cmd_inspect_source(paths, args.source_id)
    if args.command == "inspect" and args.inspect_command == "package-alias":
        return cmd_inspect_package_alias(paths, args.external_system, args.alias_key, args.alias_value)
    if args.command == "triage" and args.triage_command == "run":
        return cmd_triage_run(paths, args.package_id)
    if args.command == "lineage" and args.lineage_command == "show":
        return cmd_lineage_show(paths, args.sample_id)
    if args.command == "export" and args.export_command == "aiue-pmx-view":
        return cmd_export_aiue_pmx_view(paths, args)
    if args.command == "export" and args.export_command == "aiue-motion-view":
        return cmd_export_aiue_motion_view(paths, args)
    if args.command == "export" and args.export_command == "bodypaint-view":
        return cmd_export_bodypaint_view(paths, args)
    if args.command == "export" and args.export_command == "audio-index-packet":
        return cmd_export_audio_index_packet(paths, args)
    if args.command == "report" and args.report_command == "ready":
        return cmd_report_ready(paths, args.target)
    if args.command == "report" and args.report_command == "blocked":
        return cmd_report_blocked(paths)
    if args.command == "report" and args.report_command == "failures":
        return cmd_report_failures(paths, args.stage)
    if args.command == "report" and args.report_command == "aiue-ready":
        return cmd_report_aiue_ready(paths)
    if args.command == "report" and args.report_command == "lineage-gaps":
        return cmd_report_lineage_gaps(paths)
    if args.command == "report" and args.report_command == "motion-catalog":
        return cmd_report_motion_catalog(paths)
    if args.command == "report" and args.report_command == "image-catalog":
        return cmd_report_image_catalog(paths)
    if args.command == "report" and args.report_command == "audio-catalog":
        return cmd_report_audio_catalog(paths)
    if args.command == "report" and args.report_command == "image-picks":
        return cmd_report_image_picks(paths, args.limit, args.workshop_item_id)
    if args.command == "report" and args.report_command == "communication-signal":
        return cmd_report_communication_signal(paths, args.lane, args.profile)
    if args.command == "repair" and args.repair_command == "legacy-3dgirls-lineage":
        return cmd_repair_legacy_3dgirls_lineage(paths)
    if args.command == "remote" and args.remote_command == "register-host":
        return cmd_remote_register_host(paths, args)
    if args.command == "remote" and args.remote_command == "list-hosts":
        return cmd_remote_list_hosts(paths)
    if args.command == "remote" and args.remote_command == "probe":
        return cmd_remote_probe(paths, args.host)
    if args.command == "remote" and args.remote_command == "status":
        return cmd_remote_status(paths, args.host)
    if args.command == "remote" and args.remote_command == "sync-code":
        return cmd_remote_sync_code(paths, args)
    if args.command == "remote" and args.remote_command == "toyyard":
        return cmd_remote_toyyard(paths, args)
    if args.command == "remote" and args.remote_command == "handoff" and args.remote_handoff_command == "audio-index":
        return cmd_remote_handoff_audio_index(paths, args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
