from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from toyyard.manifest_artifact_check import load_json
from toyyard.paths import ProjectPaths
from toyyard.util import now_iso


COMMUNICATION_SIGNAL_VERSION = "toy-yard-communication-signal-0.1"


def _non_empty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _attachment_ready(entry: dict[str, Any]) -> bool:
    preferred_target = entry.get("preferred_attach_target") or {}
    if not isinstance(preferred_target, dict):
        preferred_target = {}
    return bool(entry.get("equip_slot")) and bool(preferred_target.get("name"))


def _portable_export_check(summary_payload: dict[str, Any], manifest_check_payload: dict[str, Any]) -> dict[str, Any]:
    failed_items = int((summary_payload.get("counts") or {}).get("failed_items", 0) or 0)
    manifest_status = str(manifest_check_payload.get("status") or "attention")
    if failed_items == 0 and manifest_status == "pass":
        status = "pass"
    else:
        status = "blocker"
    return {
        "name": "portable_export_contract",
        "status": status,
        "details": {
            "failed_items": failed_items,
            "manifest_check_status": manifest_status,
            "manifest_attention_count": int((manifest_check_payload.get("counts") or {}).get("attention_count", 0) or 0),
        },
    }


def _runtime_registry_check(registry_payload: dict[str, Any]) -> dict[str, Any]:
    characters = list(registry_payload.get("characters") or [])
    weapons = list(registry_payload.get("weapons") or [])
    ready_pairs = list(registry_payload.get("ready_pairs") or [])

    character_complete = sum(
        1
        for entry in characters
        if _non_empty(entry.get("skeletal_mesh"))
        and _non_empty(entry.get("skeleton"))
        and _non_empty(entry.get("physics_asset"))
    )
    weapon_complete = sum(1 for entry in weapons if _non_empty(entry.get("skeletal_mesh")))
    pair_complete = sum(
        1
        for entry in ready_pairs
        if _non_empty(entry.get("character_skeletal_mesh"))
        and _non_empty(entry.get("weapon_skeletal_mesh"))
        and _attachment_ready(entry)
    )

    expected_items = len(characters) + len(weapons) + len(ready_pairs)
    completed_items = character_complete + weapon_complete + pair_complete
    if expected_items == 0:
        status = "na"
    elif completed_items == 0:
        status = "pending"
    elif completed_items == expected_items:
        status = "pass"
    else:
        status = "attention"

    return {
        "name": "runtime_registry_projection",
        "status": status,
        "details": {
            "characters_total": len(characters),
            "characters_complete": character_complete,
            "weapons_total": len(weapons),
            "weapons_complete": weapon_complete,
            "ready_pairs_total": len(ready_pairs),
            "ready_pairs_complete": pair_complete,
        },
    }


def _attach_projection_check(registry_payload: dict[str, Any]) -> dict[str, Any]:
    ready_pairs = list(registry_payload.get("ready_pairs") or [])
    if not ready_pairs:
        status = "na"
        complete = 0
    else:
        complete = sum(1 for entry in ready_pairs if _attachment_ready(entry))
        if complete == len(ready_pairs):
            status = "pass"
        elif complete == 0:
            status = "attention"
        else:
            status = "attention"

    return {
        "name": "bundle_attach_projection",
        "status": status,
        "details": {
            "ready_pairs_total": len(ready_pairs),
            "ready_pairs_with_attach": complete,
        },
    }


def build_aiue_pmx_export_signal(
    *,
    profile: str,
    summary_payload: dict[str, Any],
    registry_payload: dict[str, Any],
    manifest_check_payload: dict[str, Any],
    summary_path: Path,
    registry_path: Path,
    manifest_check_path: Path,
) -> dict[str, Any]:
    portable_check = _portable_export_check(summary_payload, manifest_check_payload)
    runtime_check = _runtime_registry_check(registry_payload)
    attach_check = _attach_projection_check(registry_payload)

    if portable_check["status"] == "blocker":
        status = "blocker"
        handoff_state = "export_packet_blocked"
        handoff_ready = False
        needs_counterparty_contact = False
        problem_owner = "toy-yard"
        problem_layer = "export_contract"
        recommended_next_node = "repair_export_packet_locally"
        summary = "Portable export packet is not self-consistent yet; fix toy-yard export artifacts before involving AiUE."
    elif runtime_check["status"] == "attention":
        status = "attention"
        handoff_state = "roundtrip_projection_partial"
        handoff_ready = False
        needs_counterparty_contact = False
        problem_owner = "toy-yard"
        problem_layer = "runtime_registry"
        recommended_next_node = "repair_roundtrip_projection"
        summary = "Roundtrip evidence is only partially projected into the registry; stabilize toy-yard runtime mapping before the next cross-repo trial."
    elif attach_check["status"] == "attention":
        status = "attention"
        handoff_state = "bundle_attach_partial"
        handoff_ready = False
        needs_counterparty_contact = False
        problem_owner = "toy-yard"
        problem_layer = "attach_projection"
        recommended_next_node = "repair_bundle_projection"
        summary = "Bundle attach semantics are incomplete inside the export registry; correct toy-yard attach projection before the next handoff."
    elif runtime_check["status"] == "pass":
        status = "info"
        handoff_state = "runtime_registry_ready"
        handoff_ready = True
        needs_counterparty_contact = False
        problem_owner = "none"
        problem_layer = "none"
        recommended_next_node = "aiue_refresh_assets_or_downstream_trial"
        summary = "Portable export and runtime registry are both ready; the packet can move into AiUE refresh-assets or downstream validation."
    else:
        status = "info"
        handoff_state = "portable_export_ready"
        handoff_ready = True
        needs_counterparty_contact = False
        problem_owner = "none"
        problem_layer = "none"
        recommended_next_node = "aiue_import_validate_trial"
        summary = "Portable export packet is ready; hand off to AiUE for import and validation to generate runtime evidence."

    return {
        "signal_kind": "toy_yard_communication_signal",
        "signal_version": COMMUNICATION_SIGNAL_VERSION,
        "generated_at_utc": now_iso(),
        "producer": "toy-yard",
        "counterparty_system": "AiUE",
        "lane": "pmx",
        "profile": profile,
        "sample_id": str(summary_payload.get("sample_id") or registry_payload.get("sample_id") or ""),
        "status": status,
        "handoff_state": handoff_state,
        "handoff_ready": handoff_ready,
        "handoff_target": "AiUE" if handoff_ready else "none",
        "needs_counterparty_contact": needs_counterparty_contact,
        "problem_owner": problem_owner,
        "problem_layer": problem_layer,
        "recommended_next_node": recommended_next_node,
        "summary": summary,
        "checks": [portable_check, runtime_check, attach_check],
        "evidence_paths": {
            "summary_path": str(summary_path),
            "registry_path": str(registry_path),
            "manifest_artifact_check_path": str(manifest_check_path),
        },
    }


def build_aiue_pmx_export_signal_from_paths(paths: ProjectPaths, profile: str) -> dict[str, Any]:
    summary_path = paths.aiue_pmx_summary_path(profile)
    registry_path = paths.aiue_pmx_registry_path(profile)
    manifest_check_path = paths.aiue_pmx_manifest_artifact_check_path(profile)
    summary_payload = load_json(summary_path)
    registry_payload = load_json(registry_path)
    manifest_check_payload = load_json(manifest_check_path)
    return build_aiue_pmx_export_signal(
        profile=profile,
        summary_payload=summary_payload,
        registry_payload=registry_payload,
        manifest_check_payload=manifest_check_payload,
        summary_path=summary_path,
        registry_path=registry_path,
        manifest_check_path=manifest_check_path,
    )


def _motion_packet_contract_check(packet_check_payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": "motion_packet_contract",
        "status": str(packet_check_payload.get("status") or "attention"),
        "details": dict(packet_check_payload.get("counts") or {}),
    }


def _motion_packet_selection_check(registry_payload: dict[str, Any]) -> dict[str, Any]:
    clips = list(registry_payload.get("clips") or [])
    ready_count = sum(1 for entry in clips if bool(entry.get("selection_ready")))
    if not clips:
        status = "attention"
    elif ready_count > 0:
        status = "pass"
    else:
        status = "attention"
    return {
        "name": "motion_packet_selection",
        "status": status,
        "details": {
            "clip_count": len(clips),
            "selection_ready_count": ready_count,
        },
    }


def build_aiue_motion_export_signal(
    *,
    profile: str,
    summary_payload: dict[str, Any],
    registry_payload: dict[str, Any],
    packet_check_payload: dict[str, Any],
    summary_path: Path,
    registry_path: Path,
    packet_check_path: Path,
) -> dict[str, Any]:
    contract_check = _motion_packet_contract_check(packet_check_payload)
    selection_check = _motion_packet_selection_check(registry_payload)

    if contract_check["status"] != "pass":
        status = "blocker"
        handoff_state = "motion_packet_blocked"
        handoff_ready = False
        problem_owner = "toy-yard"
        problem_layer = "packet_check"
        recommended_next_node = "repair_motion_packet_locally"
        summary = "Motion packet is not self-consistent yet; repair export artifacts and validation before involving AiUE."
    elif selection_check["status"] != "pass":
        status = "attention"
        handoff_state = "motion_packet_unselected"
        handoff_ready = False
        problem_owner = "toy-yard"
        problem_layer = "packet_selection"
        recommended_next_node = "repair_motion_packet_selection"
        summary = "Motion packet exists but no clip is currently selection-ready for AiUE ingest."
    else:
        status = "info"
        handoff_state = "motion_packet_ready"
        handoff_ready = True
        problem_owner = "none"
        problem_layer = "none"
        recommended_next_node = "aiue_import_motion_packet"
        summary = "Motion packet is portable, validated, and ready for AiUE M0.5 shadow-consumer ingest."

    return {
        "signal_kind": "toy_yard_communication_signal",
        "signal_version": COMMUNICATION_SIGNAL_VERSION,
        "generated_at_utc": now_iso(),
        "producer": "toy-yard",
        "counterparty_system": "AiUE",
        "lane": "motion",
        "profile": profile,
        "sample_id": str(summary_payload.get("sample_id") or registry_payload.get("sample_id") or ""),
        "sample_ids": list(summary_payload.get("sample_ids") or registry_payload.get("sample_ids") or []),
        "scenario_ids": list(summary_payload.get("scenario_ids") or registry_payload.get("scenario_ids") or []),
        "status": status,
        "handoff_state": handoff_state,
        "handoff_ready": handoff_ready,
        "handoff_target": "AiUE" if handoff_ready else "none",
        "needs_counterparty_contact": False,
        "problem_owner": problem_owner,
        "problem_layer": problem_layer,
        "recommended_next_node": recommended_next_node,
        "summary": summary,
        "checks": [contract_check, selection_check],
        "evidence_paths": {
            "summary_path": str(summary_path),
            "registry_path": str(registry_path),
            "motion_packet_check_path": str(packet_check_path),
        },
    }


def build_aiue_motion_export_signal_from_paths(paths: ProjectPaths, profile: str) -> dict[str, Any]:
    summary_path = paths.aiue_motion_summary_path(profile)
    registry_path = paths.aiue_motion_registry_path(profile)
    packet_check_path = paths.aiue_motion_packet_check_path(profile)
    summary_payload = load_json(summary_path)
    registry_payload = load_json(registry_path)
    packet_check_payload = load_json(packet_check_path)
    return build_aiue_motion_export_signal(
        profile=profile,
        summary_payload=summary_payload,
        registry_payload=registry_payload,
        packet_check_payload=packet_check_payload,
        summary_path=summary_path,
        registry_path=registry_path,
        packet_check_path=packet_check_path,
    )


def build_motion_catalog_signal(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT
          samples.canonical_sample_id,
          packages.canonical_package_id,
          packages.consumer_ready,
          packages.warehouse_status,
          packages.metadata_json
        FROM packages
        JOIN samples ON samples.id = packages.sample_id
        WHERE samples.item_family = 'motion'
           OR packages.content_bucket = 'motion'
        ORDER BY samples.canonical_sample_id, packages.canonical_package_id
        """
    ).fetchall()

    scenarios: set[str] = set()
    supported_now = 0
    for row in rows:
        metadata = json.loads(row["metadata_json"] or "{}")
        scenarios.add(str(metadata.get("scenario_id") or row["canonical_sample_id"]))
        if str(metadata.get("validation_status") or "").strip().lower() in {"pass", "supported_now"}:
            supported_now += 1

    package_total = len(rows)
    status = "info" if package_total else "attention"
    handoff_ready = package_total > 0
    return {
        "signal_kind": "toy_yard_communication_signal",
        "signal_version": COMMUNICATION_SIGNAL_VERSION,
        "generated_at_utc": now_iso(),
        "producer": "toy-yard",
        "counterparty_system": "AiUE",
        "lane": "motion",
        "profile": "",
        "sample_id": "",
        "status": status,
        "handoff_state": "motion_catalog_ready" if handoff_ready else "motion_catalog_empty",
        "handoff_ready": handoff_ready,
        "handoff_target": "none",
        "needs_counterparty_contact": False,
        "problem_owner": "none" if handoff_ready else "toy-yard",
        "problem_layer": "none" if handoff_ready else "motion_catalog",
        "recommended_next_node": "motion_packet_contract" if handoff_ready else "import_motion_handoff",
        "summary": (
            "Motion shadow lane is cataloged and ready for packet-contract work."
            if handoff_ready
            else "Motion shadow lane has no cataloged packages yet."
        ),
        "checks": [
            {
                "name": "motion_catalog_presence",
                "status": "pass" if handoff_ready else "attention",
                "details": {
                    "scenario_count": len(scenarios),
                    "package_count": package_total,
                    "supported_now_count": supported_now,
                },
            }
        ],
        "evidence_paths": {},
    }
