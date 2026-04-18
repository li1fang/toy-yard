from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from toyyard.bodypaint_export import export_bodypaint_view
from toyyard.bodypaint_roundtrip import import_bodypaint_results
from toyyard.communication_signal import build_bodypaint_export_signal_from_paths
from toyyard.db import init_db
from toyyard.paths import ProjectPaths
from toyyard.warehouse import ensure_alias, ensure_artifact, ensure_package, ensure_sample, make_canonical_id, seed_canonical_root


class BodyPaintExportTests(unittest.TestCase):
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

    def _seed_character_package(self, *, model_exists: bool = True) -> tuple[str, str, Path]:
        model_path = self.root / "source_models" / "hero.fbx"
        model_path.parent.mkdir(parents=True, exist_ok=True)
        if model_exists:
            model_path.write_text("fbx", encoding="utf-8")

        sample = ensure_sample(
            self.conn,
            canonical_sample_id=make_canonical_id("sample", "bodypaint", "hero"),
            display_name="Hero",
            source_game="unknown",
            source_format=".fbx",
            item_family="character",
            warehouse_status="ready_for_pipeline",
        )
        package = ensure_package(
            self.conn,
            sample_id=sample["id"],
            canonical_package_id=make_canonical_id("pkg", "bodypaint", "hero-character"),
            package_role="character",
            content_bucket="Characters",
            contract_type="package",
            consumer_ready=True,
            warehouse_status="ready_for_pipeline",
        )
        ensure_artifact(
            self.conn,
            owner_type="sample",
            owner_id=sample["id"],
            stage="conversion",
            artifact_kind="output_fbx",
            path=str(model_path),
            format=".fbx",
            status="observed",
            metadata={"source": "test"},
        )
        return sample["canonical_sample_id"], package["canonical_package_id"], model_path

    def _seed_bundle_sample(self) -> tuple[str, str, str]:
        sample = ensure_sample(
            self.conn,
            canonical_sample_id=make_canonical_id("sample", "bodypaint", "bundle"),
            display_name="Bundle",
            source_game="unknown",
            source_format=".fbx",
            item_family="character",
            warehouse_status="ready_for_pipeline",
        )
        character = ensure_package(
            self.conn,
            sample_id=sample["id"],
            canonical_package_id=make_canonical_id("pkg", "bodypaint", "bundle-character"),
            package_role="character",
            content_bucket="Characters",
            contract_type="package",
            consumer_ready=True,
            warehouse_status="ready_for_pipeline",
        )
        weapon = ensure_package(
            self.conn,
            sample_id=sample["id"],
            canonical_package_id=make_canonical_id("pkg", "bodypaint", "bundle-weapon"),
            package_role="weapon",
            content_bucket="Weapons",
            contract_type="weapon_package",
            consumer_ready=True,
            warehouse_status="ready_for_pipeline",
        )
        return sample["canonical_sample_id"], character["canonical_package_id"], weapon["canonical_package_id"]

    def _seed_weapon_only_sample(self) -> tuple[str, str]:
        sample = ensure_sample(
            self.conn,
            canonical_sample_id=make_canonical_id("sample", "bodypaint", "weapon-only"),
            display_name="Weapon Only",
            source_game="unknown",
            source_format=".fbx",
            item_family="weapon",
            warehouse_status="ready_for_pipeline",
        )
        weapon = ensure_package(
            self.conn,
            sample_id=sample["id"],
            canonical_package_id=make_canonical_id("pkg", "bodypaint", "weapon-only"),
            package_role="weapon",
            content_bucket="Weapons",
            contract_type="weapon_package",
            consumer_ready=True,
            warehouse_status="ready_for_pipeline",
        )
        return sample["canonical_sample_id"], weapon["canonical_package_id"]

    def _seed_aiue_pmx_conversion(
        self,
        *,
        profile: str,
        package_key: str,
        package_dir_name: str,
        output_name: str = "hero.fbx",
        include_output_fbx: bool = True,
        output_exists: bool = True,
    ) -> Path:
        package_dir = self.paths.aiue_pmx_conversion_dir(profile) / package_dir_name
        package_dir.mkdir(parents=True, exist_ok=True)
        textures_dir = package_dir / "textures"
        textures_dir.mkdir(parents=True, exist_ok=True)
        (textures_dir / "diffuse.png").write_bytes(b"png")
        (package_dir / "ue_consumer_contract.json").write_text(json.dumps({"version": "1"}), encoding="utf-8")

        manifest_payload = {
            "package_id": package_key,
            "toy_yard": {
                "package_id": package_key,
                "package_aliases": [{"alias_value": package_key}],
            },
        }
        if include_output_fbx:
            manifest_payload["output_fbx"] = output_name
            manifest_payload["export_artifacts"] = {"output_fbx": output_name}
        manifest_path = package_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")
        if include_output_fbx and output_exists:
            (package_dir / output_name).write_text("fbx", encoding="utf-8")
        return manifest_path

    def test_export_bodypaint_view_writes_packet_registry_and_signal(self) -> None:
        sample_id, package_id, _ = self._seed_character_package()

        result = export_bodypaint_view(
            self.conn,
            self.paths,
            profile="bodypaint-smoke",
            sample_ref=sample_id,
        )

        summary = json.loads(Path(result["summary_path"]).read_text(encoding="utf-8"))
        registry = json.loads(Path(result["registry_path"]).read_text(encoding="utf-8"))
        packet_check = json.loads(Path(result["packet_check_path"]).read_text(encoding="utf-8"))
        packet_identity = json.loads(Path(result["packet_identity_path"]).read_text(encoding="utf-8"))
        signal = build_bodypaint_export_signal_from_paths(self.paths, "bodypaint-smoke")
        manifest_path = Path(registry["package_index"][package_id]["manifest_path"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        staged_source = Path(result["export_root"]) / registry["package_index"][package_id]["source_model_relpath"]
        workspace_view = json.loads(Path(result["workspace_view_path"]).read_text(encoding="utf-8"))

        self.assertEqual(result["packages"], [package_id])
        self.assertEqual(result["ready_items"], 1)
        self.assertEqual(summary["counts"]["ready_items"], 1)
        self.assertEqual(registry["counts"]["source_model_ready"], 1)
        self.assertEqual(packet_check["status"], "pass")
        self.assertEqual(packet_check["counts"]["ready_packet_count"], 1)
        self.assertTrue(staged_source.exists())
        self.assertEqual(summary["consumer_packet"]["packet_kind"], "external_model_packet")
        self.assertEqual(summary["consumer_packet"]["consumer"], "bodypaint")
        self.assertEqual(registry["consumer_packet"]["packet_version"], "toy-yard-external-model-packet-0.1")
        self.assertEqual(manifest["expected_outputs"]["normalized_glb"], "outputs/model.glb")
        self.assertEqual(manifest["expected_outputs"]["painted_glb"], "outputs/model.bodypaint.glb")
        self.assertEqual(manifest["consumer_packet"]["packet_kind"], "external_model_packet")
        self.assertEqual(manifest["viewer"]["preferred_model"], "outputs/model.bodypaint.glb")
        self.assertEqual(manifest["source_model"]["artifact_kind"], "output_fbx")
        self.assertEqual(packet_identity["identity_kind"], "bodypaint_packet_identity")
        self.assertEqual(packet_identity["identity_version"], "toy-yard-bodypaint-packet-identity-0.1")
        self.assertTrue(packet_identity["stable_fingerprint"])
        self.assertEqual(packet_identity["volatile_keys_ignored"], ["generated_at_utc"])
        self.assertEqual(packet_identity["excluded_files"], ["summary/bodypaint_packet_identity.json"])
        self.assertFalse(any(item["path"] == "summary/bodypaint_packet_identity.json" for item in packet_identity["files"]))
        self.assertTrue(any(item["path"] == "summary/bodypaint_packet_registry.json" for item in packet_identity["files"]))
        self.assertEqual(workspace_view["packet_identity_path"], result["packet_identity_path"])
        self.assertEqual(signal["handoff_state"], "bodypaint_packet_ready")
        self.assertTrue(signal["handoff_ready"])
        self.assertEqual(signal["handoff_target"], "BodyPaint")

        artifact_kinds = {
            row["artifact_kind"]
            for row in self.conn.execute("SELECT artifact_kind FROM artifacts WHERE stage = 'export'").fetchall()
        }
        self.assertIn("bodypaint_input_manifest", artifact_kinds)
        self.assertIn("bodypaint_source_model", artifact_kinds)
        self.assertIn("bodypaint_packet_registry", artifact_kinds)
        self.assertIn("bodypaint_packet_identity", artifact_kinds)

    def test_export_bodypaint_view_bridges_aiue_pmx_fbx_and_copies_upstream_package(self) -> None:
        sample_id, package_id, _ = self._seed_character_package()
        package_row = self.conn.execute(
            "SELECT * FROM packages WHERE canonical_package_id = ?",
            (package_id,),
        ).fetchone()
        assert package_row is not None
        ensure_alias(
            self.conn,
            entity_type="package",
            entity_id=package_row["id"],
            external_system="toy_yard",
            alias_key="package_id",
            alias_value="cassia-bodypaint-key",
        )
        self._seed_aiue_pmx_conversion(
            profile="trial-cassia-solo",
            package_key="cassia-bodypaint-key",
            package_dir_name="pkg_cassia_character",
            output_name="cassia.fbx",
        )

        result = export_bodypaint_view(
            self.conn,
            self.paths,
            profile="bodypaint-aiue-bridge",
            package_refs=[package_id],
            aiue_pmx_profile="trial-cassia-solo",
        )

        registry = json.loads(Path(result["registry_path"]).read_text(encoding="utf-8"))
        packet = registry["packets"][0]
        manifest_path = Path(packet["manifest_path"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        staged_root = manifest_path.parent / "source" / "upstream_aiue_pmx"

        self.assertEqual(packet["source_provider"], "toy_yard_aiue_pmx")
        self.assertEqual(packet["upstream_profile"], "trial-cassia-solo")
        self.assertTrue((staged_root / "manifest.json").exists())
        self.assertTrue((staged_root / "cassia.fbx").exists())
        self.assertTrue((staged_root / "textures" / "diffuse.png").exists())
        self.assertTrue((staged_root / "ue_consumer_contract.json").exists())
        self.assertEqual(manifest["source_model"]["provider"], "toy_yard_aiue_pmx")
        self.assertEqual(manifest["source_model"]["upstream_profile"], "trial-cassia-solo")
        self.assertTrue(manifest["source_model"]["staged_path"].endswith("source/upstream_aiue_pmx/cassia.fbx"))
        self.assertEqual(manifest["viewer"]["override_path"], "outputs/bodypaint.manual-overrides.json")

    def test_export_bodypaint_view_surfaces_missing_source_model_as_blocker(self) -> None:
        sample_id, package_id, _ = self._seed_character_package(model_exists=False)

        result = export_bodypaint_view(
            self.conn,
            self.paths,
            profile="bodypaint-missing-source",
            package_refs=[package_id],
        )
        registry = json.loads(Path(result["registry_path"]).read_text(encoding="utf-8"))
        packet_check = json.loads(Path(result["packet_check_path"]).read_text(encoding="utf-8"))
        signal = build_bodypaint_export_signal_from_paths(self.paths, "bodypaint-missing-source")

        self.assertEqual(result["ready_items"], 0)
        self.assertFalse(registry["package_index"][package_id]["source_model_available"])
        self.assertEqual(packet_check["status"], "attention")
        self.assertEqual(signal["status"], "blocker")
        self.assertEqual(signal["handoff_state"], "bodypaint_input_missing")
        self.assertFalse(signal["handoff_ready"])
        self.assertEqual(signal["evidence_paths"]["packet_check_path"], result["packet_check_path"])

    def test_export_bodypaint_view_with_aiue_pmx_profile_filters_weapon_package_from_bundle(self) -> None:
        sample_id, character_package_id, weapon_package_id = self._seed_bundle_sample()
        character_row = self.conn.execute(
            "SELECT * FROM packages WHERE canonical_package_id = ?",
            (character_package_id,),
        ).fetchone()
        assert character_row is not None
        ensure_alias(
            self.conn,
            entity_type="package",
            entity_id=character_row["id"],
            external_system="toy_yard",
            alias_key="package_id",
            alias_value="yidhari-character",
        )
        self._seed_aiue_pmx_conversion(
            profile="trial-yidhari-bundle",
            package_key="yidhari-character",
            package_dir_name="pkg_yidhari_character",
            output_name="yidhari.fbx",
        )

        result = export_bodypaint_view(
            self.conn,
            self.paths,
            profile="bodypaint-yidhari-bundle",
            sample_ref=sample_id,
            aiue_pmx_profile="trial-yidhari-bundle",
        )

        self.assertEqual(result["packages"], [character_package_id])
        self.assertNotIn(weapon_package_id, result["packages"])

    def test_export_bodypaint_view_rejects_explicit_weapon_package(self) -> None:
        _, _, weapon_package_id = self._seed_bundle_sample()

        with self.assertRaisesRegex(ValueError, "not a BodyPaint candidate"):
            export_bodypaint_view(
                self.conn,
                self.paths,
                profile="bodypaint-explicit-weapon",
                package_refs=[weapon_package_id],
            )

    def test_export_bodypaint_view_fails_when_sample_has_no_bodypaint_candidates(self) -> None:
        sample_id, _ = self._seed_weapon_only_sample()

        with self.assertRaisesRegex(ValueError, "at least one BodyPaint candidate"):
            export_bodypaint_view(
                self.conn,
                self.paths,
                profile="bodypaint-no-candidate",
                sample_ref=sample_id,
            )

    def test_export_bodypaint_view_prefers_namespaced_alias_match_over_plain_alias_value(self) -> None:
        _, package_id, _ = self._seed_character_package()
        package_row = self.conn.execute(
            "SELECT * FROM packages WHERE canonical_package_id = ?",
            (package_id,),
        ).fetchone()
        assert package_row is not None

        ensure_alias(
            self.conn,
            entity_type="package",
            entity_id=package_row["id"],
            external_system="toy_yard",
            alias_key="package_id",
            alias_value="shared-key",
        )
        ensure_alias(
            self.conn,
            entity_type="package",
            entity_id=package_row["id"],
            external_system="legacy_demo",
            alias_key="package_id",
            alias_value="shared-key",
        )

        wrong_manifest_dir = self.paths.aiue_pmx_conversion_dir("trial-alias-priority") / "pkg_wrong"
        wrong_manifest_dir.mkdir(parents=True, exist_ok=True)
        (wrong_manifest_dir / "wrong.fbx").write_text("wrong", encoding="utf-8")
        (wrong_manifest_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "package_id": "shared-key",
                    "toy_yard": {
                        "package_id": "shared-key",
                        "package_aliases": [
                            {
                                "external_system": "legacy_demo",
                                "alias_key": "package_id",
                                "alias_value": "shared-key",
                            }
                        ],
                    },
                    "output_fbx": "wrong.fbx",
                    "export_artifacts": {"output_fbx": "wrong.fbx"},
                }
            ),
            encoding="utf-8",
        )

        self._seed_aiue_pmx_conversion(
            profile="trial-alias-priority",
            package_key="shared-key",
            package_dir_name="pkg_correct",
            output_name="correct.fbx",
        )
        correct_manifest = self.paths.aiue_pmx_conversion_dir("trial-alias-priority") / "pkg_correct" / "manifest.json"
        correct_payload = json.loads(correct_manifest.read_text(encoding="utf-8"))
        correct_payload["toy_yard"]["package_aliases"] = [
            {
                "external_system": "toy_yard",
                "alias_key": "package_id",
                "alias_value": "shared-key",
            }
        ]
        correct_manifest.write_text(json.dumps(correct_payload), encoding="utf-8")

        result = export_bodypaint_view(
            self.conn,
            self.paths,
            profile="bodypaint-alias-priority",
            package_refs=[package_id],
            aiue_pmx_profile="trial-alias-priority",
        )

        registry = json.loads(Path(result["registry_path"]).read_text(encoding="utf-8"))
        packet = registry["packets"][0]
        self.assertEqual(packet["source_provider"], "toy_yard_aiue_pmx")
        self.assertTrue(packet["source_model_path"].endswith("correct.fbx"))

    def test_export_bodypaint_view_fails_when_aiue_pmx_profile_missing(self) -> None:
        _, package_id, _ = self._seed_character_package()

        with self.assertRaisesRegex(ValueError, "AiUE PMX profile does not exist"):
            export_bodypaint_view(
                self.conn,
                self.paths,
                profile="bodypaint-missing-upstream-profile",
                package_refs=[package_id],
                aiue_pmx_profile="missing-profile",
            )

    def test_export_bodypaint_view_fails_when_aiue_manifest_does_not_match_package(self) -> None:
        _, package_id, _ = self._seed_character_package()
        self._seed_aiue_pmx_conversion(
            profile="trial-mismatch",
            package_key="different-package-key",
            package_dir_name="pkg_mismatch",
        )

        with self.assertRaisesRegex(ValueError, "AiUE PMX manifest not found"):
            export_bodypaint_view(
                self.conn,
                self.paths,
                profile="bodypaint-upstream-mismatch",
                package_refs=[package_id],
                aiue_pmx_profile="trial-mismatch",
            )

    def test_export_bodypaint_view_fails_when_aiue_manifest_lacks_output_fbx(self) -> None:
        _, package_id, _ = self._seed_character_package()
        package_row = self.conn.execute(
            "SELECT * FROM packages WHERE canonical_package_id = ?",
            (package_id,),
        ).fetchone()
        assert package_row is not None
        ensure_alias(
            self.conn,
            entity_type="package",
            entity_id=package_row["id"],
            external_system="toy_yard",
            alias_key="package_id",
            alias_value="missing-output-fbx",
        )
        self._seed_aiue_pmx_conversion(
            profile="trial-missing-output-fbx",
            package_key="missing-output-fbx",
            package_dir_name="pkg_missing_output",
            include_output_fbx=False,
        )

        with self.assertRaisesRegex(ValueError, "missing output_fbx"):
            export_bodypaint_view(
                self.conn,
                self.paths,
                profile="bodypaint-missing-output-fbx",
                package_refs=[package_id],
                aiue_pmx_profile="trial-missing-output-fbx",
            )

    def test_export_bodypaint_view_fails_when_aiue_staged_fbx_is_missing(self) -> None:
        _, package_id, _ = self._seed_character_package()
        package_row = self.conn.execute(
            "SELECT * FROM packages WHERE canonical_package_id = ?",
            (package_id,),
        ).fetchone()
        assert package_row is not None
        ensure_alias(
            self.conn,
            entity_type="package",
            entity_id=package_row["id"],
            external_system="toy_yard",
            alias_key="package_id",
            alias_value="missing-staged-fbx",
        )
        self._seed_aiue_pmx_conversion(
            profile="trial-missing-staged-fbx",
            package_key="missing-staged-fbx",
            package_dir_name="pkg_missing_fbx",
            output_exists=False,
        )

        with self.assertRaisesRegex(FileNotFoundError, "staged FBX does not exist"):
            export_bodypaint_view(
                self.conn,
                self.paths,
                profile="bodypaint-missing-staged-fbx",
                package_refs=[package_id],
                aiue_pmx_profile="trial-missing-staged-fbx",
            )

    def test_import_bodypaint_results_registers_outputs_idempotently(self) -> None:
        sample_id, package_id, _ = self._seed_character_package()
        result = export_bodypaint_view(
            self.conn,
            self.paths,
            profile="bodypaint-results",
            sample_ref=sample_id,
        )
        registry = json.loads(Path(result["registry_path"]).read_text(encoding="utf-8"))
        manifest_path = Path(registry["package_index"][package_id]["manifest_path"])
        output_dir = manifest_path.parent / "outputs"
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "model.glb").write_bytes(b"glb")
        (output_dir / "model.bodypaint.glb").write_bytes(b"painted")
        (output_dir / "bodypaint.analysis.json").write_text(json.dumps({"version": "0.2"}), encoding="utf-8")
        (output_dir / "bodypaint.manual-overrides.json").write_text(json.dumps({"axis_flip": None}), encoding="utf-8")

        first = import_bodypaint_results(self.conn, self.paths, profile="bodypaint-results")
        artifact_count_after_first = self.conn.execute(
            "SELECT COUNT(*) FROM artifacts WHERE stage = 'bodypaint'"
        ).fetchone()[0]
        second = import_bodypaint_results(self.conn, self.paths, profile="bodypaint-results")
        artifact_count_after_second = self.conn.execute(
            "SELECT COUNT(*) FROM artifacts WHERE stage = 'bodypaint'"
        ).fetchone()[0]

        self.assertEqual(first["packages"], [package_id])
        self.assertEqual(first["artifacts"], 4)
        self.assertEqual(second["artifacts"], 4)
        self.assertEqual(artifact_count_after_first, artifact_count_after_second)
        artifact_rows = self.conn.execute(
            "SELECT artifact_kind, path FROM artifacts WHERE stage = 'bodypaint'"
        ).fetchall()
        artifact_kinds = {row["artifact_kind"] for row in artifact_rows}
        self.assertIn("normalized_glb", artifact_kinds)
        self.assertIn("painted_glb", artifact_kinds)
        self.assertIn("analysis_json", artifact_kinds)
        self.assertIn("manual_overrides", artifact_kinds)
        self.assertIn("bodypaint_result_import", artifact_kinds)
        stable_paths = [
            Path(row["path"])
            for row in artifact_rows
            if row["artifact_kind"] in {"normalized_glb", "painted_glb", "analysis_json", "manual_overrides"}
        ]
        self.assertTrue(all("04_registry" in str(path) and "bodypaint_roundtrip" in str(path) for path in stable_paths))
        shutil.rmtree(Path(result["export_root"]))
        self.assertTrue(all(path.exists() for path in stable_paths))

    def test_import_bodypaint_results_fails_when_analysis_is_missing(self) -> None:
        sample_id, package_id, _ = self._seed_character_package()
        result = export_bodypaint_view(
            self.conn,
            self.paths,
            profile="bodypaint-missing-analysis",
            sample_ref=sample_id,
        )
        registry = json.loads(Path(result["registry_path"]).read_text(encoding="utf-8"))
        output_dir = Path(registry["package_index"][package_id]["manifest_path"]).parent / "outputs"
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "model.glb").write_bytes(b"glb")
        (output_dir / "model.bodypaint.glb").write_bytes(b"painted")

        with self.assertRaisesRegex(FileNotFoundError, "analysis_json"):
            import_bodypaint_results(self.conn, self.paths, profile="bodypaint-missing-analysis")

    def test_import_bodypaint_results_fails_when_painted_glb_is_missing(self) -> None:
        sample_id, package_id, _ = self._seed_character_package()
        result = export_bodypaint_view(
            self.conn,
            self.paths,
            profile="bodypaint-missing-painted",
            sample_ref=sample_id,
        )
        registry = json.loads(Path(result["registry_path"]).read_text(encoding="utf-8"))
        output_dir = Path(registry["package_index"][package_id]["manifest_path"]).parent / "outputs"
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "model.glb").write_bytes(b"glb")
        (output_dir / "bodypaint.analysis.json").write_text(json.dumps({"version": "0.2"}), encoding="utf-8")

        with self.assertRaisesRegex(FileNotFoundError, "painted_glb"):
            import_bodypaint_results(self.conn, self.paths, profile="bodypaint-missing-painted")

    def test_import_bodypaint_results_fails_when_package_unknown(self) -> None:
        sample_id, package_id, _ = self._seed_character_package()
        result = export_bodypaint_view(
            self.conn,
            self.paths,
            profile="bodypaint-unknown-package",
            sample_ref=sample_id,
        )
        registry_path = Path(result["registry_path"])
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        packet = registry["packets"][0]
        registry["package_index"]["pkg_missing"] = registry["package_index"].pop(package_id)
        registry["packets"][0] = {**packet, "package_id": "pkg_missing"}
        registry_path.write_text(json.dumps(registry), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "Unknown BodyPaint package"):
            import_bodypaint_results(self.conn, self.paths, profile="bodypaint-unknown-package")


if __name__ == "__main__":
    unittest.main()
