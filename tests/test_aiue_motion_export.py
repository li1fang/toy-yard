from __future__ import annotations

import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from toyyard.aiue_motion_export import export_aiue_motion_view
from toyyard.aiue_motion_roundtrip import import_aiue_motion_results
from toyyard.communication_signal import build_aiue_motion_export_signal_from_paths
from toyyard.db import init_db
from toyyard.ingest import import_path
from toyyard.motion_handoff import import_motion_handoff
from toyyard.paths import ProjectPaths
from toyyard.warehouse import seed_canonical_root


class MotionExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "toy-yard"
        self.paths = ProjectPaths(self.root)
        self.paths.ensure_layout()
        self.conn = init_db(self.paths.db_path)
        seed_canonical_root(self.conn, self.paths.root)

    def tearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    def _write_zip_text(self, archive: zipfile.ZipFile, relative_path: str, payload: str) -> None:
        archive.writestr(f"ai-motion-motion-pack-handoff-2026-04-13/{relative_path}", payload)

    def _create_motion_handoff_zip(self) -> Path:
        zip_path = Path(self.temp_dir.name) / "ai-motion-motion-pack-handoff-2026-04-13.zip"
        with zipfile.ZipFile(zip_path, "w") as archive:
            self._write_zip_text(archive, "README.md", "# Motion Handoff\n")
            self._write_zip_text(archive, "schemas/motion-manifest.schema.json", json.dumps({"title": "manifest"}))
            self._write_zip_text(archive, "schemas/motion-skeleton.schema.json", json.dumps({"title": "skeleton"}))
            self._write_zip_text(archive, "schemas/skeleton.soma_77.json", json.dumps({"joint_count": 77}))
            manifests = [
                {
                    "schema_version": "1.0.0",
                    "pack_version": "v0.2",
                    "clip_id": "route-a-3s-turn-hand-ready--20260412T180000Z",
                    "job_name": "route-a-3s-turn-hand-ready",
                    "source_route": "route_a_kimodo",
                    "format_profile": "route_a_kimodo_somaskel77",
                    "runtime_semantics": "runtime",
                    "placeholder_motion": False,
                    "scenario_id": "route-a-3s-turn-hand-ready",
                    "scenario_tags": ["route_a", "turn", "pre_interaction"],
                    "duration_sec": 3.0,
                    "fps": 30,
                    "frame_count": 90,
                    "skeleton": {
                        "skeleton_id": "somaskel77",
                        "file": "skeleton.soma_77.json",
                    },
                    "arrays": {"root_positions": {"shape": [90, 3]}},
                    "exports": {
                        "canonical_npz": "motion.npz",
                        "bvh": "motion.bvh",
                    },
                },
                {
                    "schema_version": "1.0.0",
                    "pack_version": "v0.2",
                    "clip_id": "route-a-3s-two-hand-receive-ready--20260412T181500Z",
                    "job_name": "route-a-3s-two-hand-receive-ready",
                    "source_route": "route_a_kimodo",
                    "format_profile": "route_a_kimodo_somaskel77",
                    "runtime_semantics": "runtime",
                    "placeholder_motion": False,
                    "scenario_id": "route-a-3s-two-hand-receive-ready",
                    "scenario_tags": ["route_a", "receive", "two_hand"],
                    "duration_sec": 3.0,
                    "fps": 30,
                    "frame_count": 90,
                    "skeleton": {
                        "skeleton_id": "somaskel77",
                        "file": "skeleton.soma_77.json",
                    },
                    "arrays": {"root_positions": {"shape": [90, 3]}},
                    "exports": {
                        "canonical_npz": "motion.npz",
                        "bvh": "motion.bvh",
                    },
                },
                {
                    "schema_version": "1.0.0",
                    "pack_version": "v0.2",
                    "clip_id": "route-a-3s-half-turn-present-ready--20260412T183000Z",
                    "job_name": "route-a-3s-half-turn-present-ready",
                    "source_route": "route_a_kimodo",
                    "format_profile": "route_a_kimodo_somaskel77",
                    "runtime_semantics": "runtime",
                    "placeholder_motion": False,
                    "scenario_id": "route-a-3s-half-turn-present-ready",
                    "scenario_tags": ["route_a", "present", "half_turn"],
                    "duration_sec": 3.0,
                    "fps": 30,
                    "frame_count": 90,
                    "skeleton": {
                        "skeleton_id": "somaskel77",
                        "file": "skeleton.soma_77.json",
                    },
                    "arrays": {"root_positions": {"shape": [90, 3]}},
                    "exports": {
                        "canonical_npz": "motion.npz",
                        "bvh": "motion.bvh",
                    },
                },
            ]
            validation_report = {
                "schema_version": "1.0.0",
                "reports": [],
            }
            capability = {
                "supported_now": ["3-second single-actor short clips"],
                "supported_with_caution": ["two-hand receive-ready motions"],
                "not_yet_admitted": ["multi-actor motions"],
            }
            for manifest in manifests:
                validation_report["reports"].append(
                    {
                        "status": "pass",
                        "clip_id": manifest["clip_id"],
                        "pack_version": "v0.2",
                        "scenario_id": manifest["scenario_id"],
                        "metrics": {"heading_delta_rad": 0.7},
                    }
                )
                pack_root = f"examples/motion-packs/v0.2/{manifest['scenario_id']}"
                self._write_zip_text(archive, f"{pack_root}/manifest.json", json.dumps(manifest))
                self._write_zip_text(archive, f"{pack_root}/README.md", "# Clip Readme\n")
                self._write_zip_text(archive, f"{pack_root}/motion.npz", "npz")
                self._write_zip_text(archive, f"{pack_root}/motion.bvh", "bvh")
                self._write_zip_text(archive, f"{pack_root}/skeleton.soma_77.json", json.dumps({"joint_count": 77}))
            self._write_zip_text(archive, "examples/motion-packs/v0.2/validation.report.json", json.dumps(validation_report))
            self._write_zip_text(archive, "examples/motion-packs/v0.2/capability-assessment.json", json.dumps(capability))
        return zip_path

    def _export_one_motion_sample(self) -> tuple[dict[str, object], Path, str]:
        source_zip = self._create_motion_handoff_zip()
        v1_package = import_path(self.conn, self.paths, source_zip, source_kind="manual_drop", game_hint="ai_motion")
        import_motion_handoff(self.conn, self.paths, package_id=v1_package["id"])
        result = export_aiue_motion_view(
            self.conn,
            self.paths,
            profile="trial-motion-m1",
            sample_ref="route-a-3s-turn-hand-ready",
        )
        package_id = result["packages"][0]
        return result, Path(result["export_root"]), package_id

    def _write_json(self, path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def _package_id_for_scenario(self, scenario_id: str) -> str:
        row = self.conn.execute(
            """
            SELECT packages.canonical_package_id
            FROM packages
            JOIN samples ON samples.id = packages.sample_id
            WHERE samples.item_family = 'motion'
              AND json_extract(packages.metadata_json, '$.scenario_id') = ?
              AND json_extract(packages.metadata_json, '$.pack_version') = 'v0.2'
            ORDER BY packages.canonical_package_id
            LIMIT 1
            """,
            (scenario_id,),
        ).fetchone()
        self.assertIsNotNone(row, f"missing package for scenario {scenario_id}")
        return row["canonical_package_id"]

    def _write_consumer_request(self, trial_root: Path, manifest_path: Path, package_id: str, operation: str) -> Path:
        request_path = trial_root / f"{operation}.request.json"
        self._write_json(
            request_path,
            {
                "schema_version": "motion_consumer_request_v0",
                "operation": operation,
                "packet_manifest_path": str(manifest_path),
                "target_package_id": package_id,
            },
        )
        return request_path

    def _write_consumer_result(
        self,
        trial_root: Path,
        manifest_path: Path,
        package_id: str,
        *,
        operation: str,
        status: str,
        owner: str,
        failure_class: str | None,
        recommended_node: str | None = None,
        import_action_path: Path | None = None,
        preview_result_path: Path | None = None,
    ) -> Path:
        result_path = trial_root / f"{operation}.result.json"
        payload = {
            "schema_version": "motion_consumer_result_v0",
            "status": status,
            "operation": operation,
            "packet_ref": {
                "packet_manifest_path": str(manifest_path),
                "package_id": package_id,
                "sample_id": "sample_route-a-3s-turn-hand-ready_797943f40a",
                "clip_id": "route-a-3s-turn-hand-ready--20260412T180000Z",
                "pack_version": "v0.2",
            },
            "generated_assets": {
                "animation_asset_path": "/Game/AiUE/MotionPackets/Test/Anim_Test.Anim_Test"
            },
            "host_resolution": {
                "host_key": "demo",
                "target_host_asset_path": "/Game/AiUE/Hosts/BP_Test.BP_Test",
                "target_skeleton_asset_path": "/Game/AiUE/Registry/TestSkeleton.TestSkeleton",
            },
            "preview_evidence": {
                "result_json_path": str(preview_result_path) if preview_result_path else "",
                "subject_visible": status == "pass",
                "pose_changed": status == "pass",
            },
            "failure_class": failure_class,
            "communication_signal": {
                "should_contact_toy_yard": owner == "toy-yard",
                "owner": owner,
                "reason": "test_result",
                "recommended_node": recommended_node,
            },
            "warnings": [],
            "errors": [] if status == "pass" else ["test_error"],
            "artifacts": {
                "import_action_path": str(import_action_path) if import_action_path else "",
                "preview_action_path": "",
            },
        }
        self._write_json(result_path, payload)
        return result_path

    def test_export_motion_packet_and_import_roundtrip_results_legacy_fallback(self) -> None:
        result, export_root, package_id = self._export_one_motion_sample()

        summary = json.loads(Path(result["summary_path"]).read_text(encoding="utf-8"))
        registry = json.loads(Path(result["registry_path"]).read_text(encoding="utf-8"))
        packet_check = json.loads(Path(result["motion_packet_check_path"]).read_text(encoding="utf-8"))
        signal = build_aiue_motion_export_signal_from_paths(self.paths, "trial-motion-m1")

        self.assertEqual(summary["counts"]["clip_count"], 1)
        self.assertEqual(registry["counts"]["selection_ready"], 1)
        self.assertEqual(packet_check["status"], "pass")
        self.assertTrue(signal["handoff_ready"])

        clip_dir = export_root / "clips" / package_id
        self._write_json(
            clip_dir / "motion_import_report.local.json",
            {
                "status": "pass",
                "result": {
                    "package_id": package_id,
                    "imported_animation_asset_path": "/Game/AiUE/MotionPackets/Test/Anim_Test.Anim_Test",
                    "target_skeleton_asset_path": "/Game/AiUE/Registry/TestSkeleton.TestSkeleton",
                    "retarget_refs": {"profile": "somaskel77"},
                },
            },
        )
        self._write_json(
            clip_dir / "motion_preview_report.local.json",
            {
                "status": "pass",
                "result": {
                    "package_id": package_id,
                    "preview_asset_path": "/Game/AiUE/MotionPackets/Test/Anim_Test.Anim_Test",
                },
            },
        )
        trial_root = Path(self.temp_dir.name) / "trial-motion-legacy"
        trial_root.mkdir(parents=True, exist_ok=True)
        for name in (
            "latest_toy_yard_motion_default_source_m1_report.json",
            "motion_packet_context.json",
            "motion_clip_selection.json",
            "motion_packet_state.json",
        ):
            self._write_json(trial_root / name, {"status": "pass", "package_id": package_id})

        roundtrip = import_aiue_motion_results(self.conn, self.paths, export_root=export_root, trial_root=trial_root)
        package_row = self.conn.execute("SELECT * FROM packages WHERE canonical_package_id = ?", (package_id,)).fetchone()

        self.assertEqual(roundtrip["mode"], "legacy_sidecars")
        self.assertEqual(roundtrip["package_artifacts"], 2)
        self.assertEqual(package_row["consumer_ready"], 1)
        self.assertEqual(package_row["warehouse_status"], "ready_for_pipeline")

    def test_export_motion_packet_curated_packages_builds_mixed_profile(self) -> None:
        source_zip = self._create_motion_handoff_zip()
        v1_package = import_path(self.conn, self.paths, source_zip, source_kind="manual_drop", game_hint="ai_motion")
        import_motion_handoff(self.conn, self.paths, package_id=v1_package["id"])
        turn_hand_package = self._package_id_for_scenario("route-a-3s-turn-hand-ready")
        two_hand_receive_package = self._package_id_for_scenario("route-a-3s-two-hand-receive-ready")
        half_turn_present_package = self._package_id_for_scenario("route-a-3s-half-turn-present-ready")

        result = export_aiue_motion_view(
            self.conn,
            self.paths,
            profile="trial-motion-m2-diversity",
            package_refs=[
                turn_hand_package,
                two_hand_receive_package,
                half_turn_present_package,
                turn_hand_package,
            ],
        )

        summary = json.loads(Path(result["summary_path"]).read_text(encoding="utf-8"))
        registry = json.loads(Path(result["registry_path"]).read_text(encoding="utf-8"))
        packet_check = json.loads(Path(result["motion_packet_check_path"]).read_text(encoding="utf-8"))

        self.assertEqual(
            result["packages"],
            [
                turn_hand_package,
                two_hand_receive_package,
                half_turn_present_package,
            ],
        )
        self.assertEqual(summary["sample_id"], "")
        self.assertEqual(summary["scenario_id"], "")
        self.assertEqual(len(summary["sample_ids"]), 3)
        self.assertEqual(len(summary["scenario_ids"]), 3)
        self.assertEqual(registry["counts"]["selection_ready"], 3)
        self.assertEqual(registry["counts"]["distinct_scenarios"], 3)
        self.assertEqual(packet_check["status"], "pass")

        summary_artifact_count = self.conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM artifacts
            WHERE artifact_kind = 'motion_suite_summary' AND path = ?
            """,
            (str(Path(result["summary_path"]).resolve()),),
        ).fetchone()["count"]
        registry_artifact_count = self.conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM artifacts
            WHERE artifact_kind = 'motion_clip_registry' AND path = ?
            """,
            (str(Path(result["registry_path"]).resolve()),),
        ).fetchone()["count"]

        self.assertEqual(summary_artifact_count, 0)
        self.assertEqual(registry_artifact_count, 0)

    def test_import_motion_consumer_result_v0_is_idempotent_and_routes_aiue_owner(self) -> None:
        result, export_root, package_id = self._export_one_motion_sample()
        trial_root = Path(self.temp_dir.name) / "trial-motion-v0-fail"
        trial_root.mkdir(parents=True, exist_ok=True)
        manifest_path = export_root / "clips" / package_id / "manifest.json"

        self._write_consumer_request(trial_root, manifest_path, package_id, "import_motion_packet")
        import_action_path = trial_root / "motion" / "import_motion_packet.action.json"
        self._write_json(import_action_path, {"status": "fail", "errors": ["animation_import_failed"]})
        self._write_consumer_result(
            trial_root,
            manifest_path,
            package_id,
            operation="import_motion_packet",
            status="fail",
            owner="aiue",
            failure_class="unreal_import_failed",
            recommended_node="aiue_motion_runtime_followup",
            import_action_path=import_action_path,
        )
        self._write_json(trial_root / "motion_packet_context.json", {"status": "info"})
        self._write_json(trial_root / "motion_packet_state.json", {"status": "fail"})

        imported = import_aiue_motion_results(self.conn, self.paths, export_root=export_root, trial_root=trial_root)
        imported_again = import_aiue_motion_results(self.conn, self.paths, export_root=export_root, trial_root=trial_root)

        package_row = self.conn.execute("SELECT * FROM packages WHERE canonical_package_id = ?", (package_id,)).fetchone()
        metadata = json.loads(package_row["metadata_json"] or "{}")
        artifact_count = self.conn.execute(
            "SELECT COUNT(*) AS count FROM artifacts WHERE owner_type = 'package' AND owner_id = ?",
            (package_row["id"],),
        ).fetchone()["count"]

        self.assertEqual(imported["mode"], "motion_consumer_seam_v0")
        self.assertEqual(imported["request_count"], 1)
        self.assertEqual(imported["result_count"], 1)
        self.assertEqual(imported["owner_aiue"], 1)
        self.assertEqual(imported["next_node"], "aiue_motion_runtime_followup")
        self.assertEqual(imported_again["package_artifacts"], imported["package_artifacts"])
        self.assertEqual(metadata["latest_motion_consumer_owner"], "aiue")
        self.assertEqual(metadata["latest_motion_failure_class"], "unreal_import_failed")
        self.assertEqual(metadata["motion_consumer_results"]["import_motion_packet"]["status"], "fail")
        self.assertEqual(package_row["consumer_ready"], 0)
        self.assertGreaterEqual(artifact_count, 3)
        self.assertTrue(Path(imported["round_summary_path"]).exists())

    def test_preview_pass_promotes_package_after_result_import(self) -> None:
        result, export_root, package_id = self._export_one_motion_sample()
        trial_root = Path(self.temp_dir.name) / "trial-motion-v0-pass"
        trial_root.mkdir(parents=True, exist_ok=True)
        manifest_path = export_root / "clips" / package_id / "manifest.json"

        self._write_consumer_request(trial_root, manifest_path, package_id, "import_motion_packet")
        self._write_consumer_request(trial_root, manifest_path, package_id, "animation_preview")
        import_action_path = trial_root / "motion" / "import_motion_packet.action.json"
        preview_result_path = trial_root / "motion" / "animation_preview.result.json"
        self._write_json(import_action_path, {"status": "pass"})
        self._write_json(preview_result_path, {"status": "pass", "visible": True})
        self._write_consumer_result(
            trial_root,
            manifest_path,
            package_id,
            operation="import_motion_packet",
            status="pass",
            owner="none",
            failure_class=None,
            import_action_path=import_action_path,
        )
        self._write_consumer_result(
            trial_root,
            manifest_path,
            package_id,
            operation="animation_preview",
            status="pass",
            owner="none",
            failure_class=None,
            preview_result_path=preview_result_path,
        )

        imported = import_aiue_motion_results(self.conn, self.paths, export_root=export_root, trial_root=trial_root)
        package_row = self.conn.execute("SELECT * FROM packages WHERE canonical_package_id = ?", (package_id,)).fetchone()
        metadata = json.loads(package_row["metadata_json"] or "{}")

        self.assertEqual(imported["owner_none"], 2)
        self.assertEqual(imported["eligible_for_m1"], 1)
        self.assertEqual(imported["next_node"], "m1_default_source_candidate")
        self.assertEqual(package_row["consumer_ready"], 1)
        self.assertEqual(package_row["warehouse_status"], "ready_for_pipeline")
        self.assertEqual(metadata["motion_consumer_results"]["animation_preview"]["status"], "pass")
        self.assertEqual(metadata["latest_motion_animation_asset_path"], "/Game/AiUE/MotionPackets/Test/Anim_Test.Anim_Test")


if __name__ == "__main__":
    unittest.main()
