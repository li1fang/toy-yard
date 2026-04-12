from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from toyyard.aiue_roundtrip import import_aiue_results
from toyyard.aiue_export import export_aiue_pmx_view
from toyyard.db import init_db
from toyyard.legacy_3dgirls import import_legacy_3dgirls
from toyyard.paths import ProjectPaths
from toyyard.reports import aiue_ready_rows, lineage_gap_rows
from toyyard.repair import repair_legacy_3dgirls_lineage
from toyyard.warehouse import (
    ensure_package,
    ensure_sample,
    find_entity_by_alias,
    make_canonical_id,
    register_root,
    seed_canonical_root,
)


FIXTURES = Path(__file__).resolve().parents[1] / "_fixtures"


class ToyYardCanonicalTests(unittest.TestCase):
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

    def _copy_legacy_fixture(self) -> Path:
        destination = Path(self.temp_dir.name) / "legacy_3dgirls"
        shutil.copytree(FIXTURES / "legacy_3dgirls", destination)
        return destination

    def _seed_legacy_fixture_artifacts(self, legacy_root: Path) -> None:
        conversion_dir = legacy_root / "_fbx_out_v1" / "mingchao" / "1742887197777"
        textures_dir = legacy_root / "textures"
        conversion_dir.mkdir(parents=True, exist_ok=True)
        textures_dir.mkdir(parents=True, exist_ok=True)
        for name in ("Cantarella.fbx", "Cantarella_body.fbx", "Cantarella_weapon.fbx"):
            (conversion_dir / name).write_text(name, encoding="utf-8")
        (textures_dir / "Cantarella_albedo.png").write_text("png", encoding="utf-8")

    def test_register_root_is_idempotent(self) -> None:
        legacy_root = self._copy_legacy_fixture()
        row1 = register_root(self.conn, "legacy_workspace", legacy_root, "3dgirls")
        row2 = register_root(self.conn, "legacy_workspace", legacy_root, "3dgirls")
        count = self.conn.execute("SELECT COUNT(*) FROM roots WHERE path = ?", (str(legacy_root.resolve()),)).fetchone()[0]

        self.assertEqual(row1["id"], row2["id"])
        self.assertEqual(count, 1)

    def test_import_legacy_3dgirls_builds_canonical_aliases(self) -> None:
        legacy_root = self._copy_legacy_fixture()
        root = register_root(self.conn, "legacy_workspace", legacy_root, "3dgirls")

        result = import_legacy_3dgirls(self.conn, self.paths, root["id"])
        sample = find_entity_by_alias(
            self.conn,
            entity_type="sample",
            external_system="3dgirls",
            alias_value="mingchao_1742887197777_597530c2",
            alias_key="sample_id",
        )
        package = find_entity_by_alias(
            self.conn,
            entity_type="package",
            external_system="3dgirls",
            alias_value="mingchao_1742887197777_597530c2_character_f91ab5d6",
            alias_key="package_id",
        )

        self.assertEqual(result["packages_total"], 2)
        self.assertIsNotNone(sample)
        self.assertIsNotNone(package)
        self.assertEqual(package["content_bucket"], "character")
        self.assertEqual(package["contract_type"], "host_blueprint")
        self.assertEqual(package["consumer_ready"], 1)
        ready_rows = aiue_ready_rows(self.conn)
        self.assertEqual(len(ready_rows), 2)
        self.assertTrue(all(row["has_conversion_manifest"] for row in ready_rows))

    def test_export_aiue_pmx_view_writes_summary_registry_and_manifests(self) -> None:
        legacy_root = self._copy_legacy_fixture()
        self._seed_legacy_fixture_artifacts(legacy_root)
        root = register_root(self.conn, "legacy_workspace", legacy_root, "3dgirls")
        import_legacy_3dgirls(self.conn, self.paths, root["id"])

        result = export_aiue_pmx_view(
            self.conn,
            self.paths,
            profile="pmx-smoke",
            sample_ref="mingchao_1742887197777_597530c2",
            include_verify=True,
        )

        summary_payload = json.loads(Path(result["summary_path"]).read_text(encoding="utf-8"))
        registry_payload = json.loads(Path(result["registry_path"]).read_text(encoding="utf-8"))
        workspace_view_payload = json.loads(Path(result["workspace_view_path"]).read_text(encoding="utf-8"))
        trial_workspace_payload = json.loads(Path(result["trial_workspace_path"]).read_text(encoding="utf-8"))
        manifest_check_payload = json.loads(Path(result["manifest_artifact_check_path"]).read_text(encoding="utf-8"))
        communication_signal_payload = json.loads(Path(result["communication_signal_path"]).read_text(encoding="utf-8"))

        self.assertEqual(len(summary_payload["successes"]), 2)
        self.assertEqual(registry_payload["counts"]["ready_pairs"], 1)
        self.assertEqual(workspace_view_payload["sample_id"], result["sample_id"])
        self.assertEqual(trial_workspace_payload["paths"]["toy_yard_pmx_view_root"], result["export_root"])
        self.assertEqual(summary_payload["export_contract_version"], "toy-yard-pmx-0.3")
        self.assertEqual(registry_payload["export_contract_version"], "toy-yard-pmx-0.3")
        self.assertEqual(manifest_check_payload["status"], "pass")
        self.assertEqual(communication_signal_payload["handoff_state"], "portable_export_ready")
        self.assertTrue(communication_signal_payload["handoff_ready"])
        self.assertEqual(workspace_view_payload["communication_signal_path"], result["communication_signal_path"])
        for item in result["exported_manifests"]:
            manifest_path = Path(item["manifest_path"])
            self.assertTrue(manifest_path.exists())
            manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest_payload["source"], "toy-yard export")
            self.assertTrue(manifest_payload["output_fbx"].endswith(".fbx"))
            self.assertTrue((manifest_path.parent / manifest_payload["output_fbx"]).exists())
            self.assertTrue((manifest_path.parent / "ue_consumer_contract.json").exists())

        weapon_manifest = json.loads(Path(result["exported_manifests"][1]["manifest_path"]).read_text(encoding="utf-8"))
        self.assertTrue(weapon_manifest["output_fbx"].endswith("Cantarella_weapon.fbx"))

    def test_import_aiue_results_is_idempotent_and_roundtrips_runtime_registry(self) -> None:
        legacy_root = self._copy_legacy_fixture()
        self._seed_legacy_fixture_artifacts(legacy_root)
        root = register_root(self.conn, "legacy_workspace", legacy_root, "3dgirls")
        import_legacy_3dgirls(self.conn, self.paths, root["id"])

        first_export = export_aiue_pmx_view(
            self.conn,
            self.paths,
            profile="pmx-roundtrip-source",
            sample_ref="mingchao_1742887197777_597530c2",
            include_verify=False,
        )

        export_root = Path(first_export["export_root"])
        trial_root = Path(self.temp_dir.name) / "trial_root"
        trial_root.mkdir(parents=True, exist_ok=True)

        manifests = {
            item["package_id"]: json.loads(Path(item["manifest_path"]).read_text(encoding="utf-8"))
            for item in first_export["exported_manifests"]
        }
        manifest_paths = {
            item["package_id"]: Path(item["manifest_path"])
            for item in first_export["exported_manifests"]
        }
        sample_id = first_export["sample_id"]

        character_package_id = next(pkg_id for pkg_id, payload in manifests.items() if payload["content_bucket"] == "character")
        weapon_package_id = next(pkg_id for pkg_id, payload in manifests.items() if payload["content_bucket"] == "weapon")

        for package_id, skeletal_mesh, skeleton, physics_asset, attachment in (
            (
                character_package_id,
                "/Game/PMXPipeline/Characters/CharacterA/Meshes/Cantarella.Cantarella",
                "/Game/PMXPipeline/Characters/CharacterA/Meshes/Cantarella_Skeleton.Cantarella_Skeleton",
                "/Game/PMXPipeline/Characters/CharacterA/Meshes/Cantarella_PhysicsAsset.Cantarella_PhysicsAsset",
                None,
            ),
            (
                weapon_package_id,
                "/Game/PMXPipeline/Weapons/WeaponA/Meshes/CantarellaWeapon.CantarellaWeapon",
                "/Game/PMXPipeline/Weapons/WeaponA/Meshes/CantarellaWeapon_Skeleton.CantarellaWeapon_Skeleton",
                None,
                {
                    "equip_slot": "weapon",
                    "preferred_target": {"type": "socket", "name": "WeaponSocket"},
                },
            ),
        ):
            manifest_path = manifest_paths[package_id]
            import_report_path = manifest_path.parent / "ue_import_report.local.json"
            validation_report_path = manifest_path.parent / "ue_validation_report.local.json"
            import_report_path.write_text(
                json.dumps(
                    {
                        "generated_at_utc": "2026-04-12T00:00:00+00:00",
                        "sample_id": sample_id,
                        "package_id": package_id,
                        "manifest_path": str(manifest_path),
                        "imported_assets": {
                            "skeletal_mesh": skeletal_mesh,
                            "skeleton": skeleton,
                            "physics_asset": physics_asset,
                            "textures": [],
                            "other": [],
                        },
                        "attachment": attachment,
                    }
                ),
                encoding="utf-8",
            )
            validation_report_path.write_text(
                json.dumps(
                    {
                        "generated_at_utc": "2026-04-12T00:00:01+00:00",
                        "sample_id": sample_id,
                        "package_id": package_id,
                        "status": "pass",
                    }
                ),
                encoding="utf-8",
            )

        (trial_root / "import_package_character.action.json").write_text(
            json.dumps(
                {
                    "generated_at_utc": "2026-04-12T00:10:00+00:00",
                    "command": "import-package",
                    "status": "pass",
                    "result": {"package_id": character_package_id},
                }
            ),
            encoding="utf-8",
        )
        (trial_root / "import_package_weapon.action.json").write_text(
            json.dumps(
                {
                    "generated_at_utc": "2026-04-12T00:10:05+00:00",
                    "command": "import-package",
                    "status": "pass",
                    "result": {"package_id": weapon_package_id},
                }
            ),
            encoding="utf-8",
        )
        (trial_root / "validate_package_character.action.json").write_text(
            json.dumps(
                {
                    "generated_at_utc": "2026-04-12T00:10:10+00:00",
                    "command": "validate-package",
                    "status": "pass",
                    "result": {"package_id": character_package_id},
                }
            ),
            encoding="utf-8",
        )
        (trial_root / "refresh_assets.action.json").write_text(
            json.dumps(
                {
                    "generated_at_utc": "2026-04-12T00:11:00+00:00",
                    "command": "refresh-assets",
                    "status": "pass",
                    "result": {"registry_json_path": first_export["registry_path"]},
                }
            ),
            encoding="utf-8",
        )
        (trial_root / "ue_equipment_assets_report.trial.json").write_text(
            json.dumps({"counts": {"runtime_ready_host_blueprints": 1}, "warnings": []}),
            encoding="utf-8",
        )
        (trial_root / "ue_equipment_registry.trial.json").write_text(
            json.dumps({"counts": {"ready_pairs": 1}}),
            encoding="utf-8",
        )

        import_aiue_results(self.conn, self.paths, export_root=export_root, trial_root=trial_root)
        artifacts_after_first = self.conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
        runs_after_first = self.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        stable_character_import = self.paths.aiue_roundtrip_package_dir(sample_id, character_package_id) / "ue_import_report.local.json"
        stable_weapon_action = self.paths.aiue_roundtrip_package_dir(sample_id, weapon_package_id) / "import_package_weapon.action.json"
        stable_refresh = self.paths.aiue_roundtrip_sample_artifact_dir(sample_id) / "refresh_assets.action.json"
        self.assertTrue(stable_character_import.exists())
        self.assertTrue(stable_weapon_action.exists())
        self.assertTrue(stable_refresh.exists())

        import_aiue_results(self.conn, self.paths, export_root=export_root, trial_root=trial_root)
        artifacts_after_second = self.conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
        runs_after_second = self.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]

        self.assertEqual(artifacts_after_first, artifacts_after_second)
        self.assertEqual(runs_after_first, runs_after_second)

        shutil.rmtree(export_root)
        self.assertFalse(export_root.exists())

        second_export = export_aiue_pmx_view(
            self.conn,
            self.paths,
            profile="pmx-roundtrip-runtime",
            sample_ref="mingchao_1742887197777_597530c2",
            include_verify=False,
        )
        registry_payload = json.loads(Path(second_export["registry_path"]).read_text(encoding="utf-8"))
        character_entry = next(entry for entry in registry_payload["characters"] if entry["package_id"] == character_package_id)
        ready_pair = next(entry for entry in registry_payload["ready_pairs"] if entry["weapon_package_id"] == weapon_package_id)

        self.assertEqual(character_entry["skeletal_mesh"], "/Game/PMXPipeline/Characters/CharacterA/Meshes/Cantarella.Cantarella")
        self.assertEqual(character_entry["physics_asset"], "/Game/PMXPipeline/Characters/CharacterA/Meshes/Cantarella_PhysicsAsset.Cantarella_PhysicsAsset")
        self.assertEqual(ready_pair["weapon_skeletal_mesh"], "/Game/PMXPipeline/Weapons/WeaponA/Meshes/CantarellaWeapon.CantarellaWeapon")
        self.assertEqual(ready_pair["equip_slot"], "weapon")
        self.assertEqual(ready_pair["preferred_attach_target"]["name"], "WeaponSocket")
        communication_signal_payload = json.loads(Path(second_export["communication_signal_path"]).read_text(encoding="utf-8"))
        self.assertEqual(communication_signal_payload["handoff_state"], "runtime_registry_ready")
        self.assertTrue(communication_signal_payload["handoff_ready"])

    def test_export_uses_matching_sample_conversion_manifest_when_package_metadata_points_to_preflight(self) -> None:
        legacy_root = self._copy_legacy_fixture()
        self._seed_legacy_fixture_artifacts(legacy_root)
        root = register_root(self.conn, "legacy_workspace", legacy_root, "3dgirls")
        import_legacy_3dgirls(self.conn, self.paths, root["id"])

        sample_id = self.conn.execute("SELECT id FROM samples").fetchone()[0]
        packages = self.conn.execute("SELECT * FROM packages ORDER BY canonical_package_id").fetchall()
        preflight_path = self.conn.execute(
            """
            SELECT path
            FROM artifacts
            WHERE owner_type = 'sample' AND owner_id = ? AND stage = 'preflight' AND artifact_kind = 'preflight_report'
            ORDER BY path
            LIMIT 1
            """,
            (sample_id,),
        ).fetchone()["path"]

        for package in packages:
            metadata = json.loads(package["metadata_json"] or "{}")
            source_stem = Path(str(metadata.get("source_relative_path") or "")).stem or package["canonical_package_id"]
            metadata["manifest_path"] = rf"C:\Users\fang\Documents\3dgirls\_fbx_out_fastloop\broken\{source_stem}\manifest.json"
            metadata["report_path"] = preflight_path
            self.conn.execute(
                "UPDATE packages SET metadata_json = ? WHERE id = ?",
                (json.dumps(metadata, ensure_ascii=False), package["id"]),
            )
        self.conn.commit()

        result = export_aiue_pmx_view(
            self.conn,
            self.paths,
            profile="pmx-preflight-fallback",
            sample_ref="mingchao_1742887197777_597530c2",
            include_verify=False,
        )
        manifest_check_payload = json.loads(Path(result["manifest_artifact_check_path"]).read_text(encoding="utf-8"))

        self.assertEqual(manifest_check_payload["status"], "pass")
        for item in result["exported_manifests"]:
            manifest_payload = json.loads(Path(item["manifest_path"]).read_text(encoding="utf-8"))
            self.assertTrue(manifest_payload["output_fbx"].endswith(".fbx"))
            self.assertFalse(manifest_payload["source_lineage"]["manifest_path_resolved"].endswith("preflight_report.json"))

    def test_lineage_gap_report_flags_missing_conversion_manifest(self) -> None:
        sample = ensure_sample(
            self.conn,
            canonical_sample_id=make_canonical_id("sample", "manual", "orphan-sample"),
            display_name="Orphan Sample",
        )
        ensure_package(
            self.conn,
            sample_id=sample["id"],
            canonical_package_id=make_canonical_id("pkg", "manual", "orphan-package"),
            package_role="character",
            warehouse_status="cataloged",
        )

        gaps = lineage_gap_rows(self.conn)
        self.assertTrue(any(row["gap"] == "missing_sample_source" for row in gaps))

    def test_repair_merges_shadow_package_when_package_id_arrives_later(self) -> None:
        sample = ensure_sample(
            self.conn,
            canonical_sample_id=make_canonical_id("sample", "manual", "repair-sample"),
            display_name="Repair Sample",
        )
        weak = ensure_package(
            self.conn,
            sample_id=sample["id"],
            canonical_package_id=make_canonical_id("pkg", "manual", "repair-weak"),
            package_role="unknown",
            warehouse_status="classified",
            metadata={
                "created_from": "ue_suite_summary",
                "legacy_identity_basis": "source_relative_path",
                "source_relative_path": "repair/sample.pmx",
                "manifest_path": "C:/legacy/repair/manifest.json",
            },
        )
        strong = ensure_package(
            self.conn,
            sample_id=sample["id"],
            canonical_package_id=make_canonical_id("pkg", "manual", "repair-strong"),
            package_role="character",
            content_bucket="Characters",
            contract_type="package",
            consumer_ready=True,
            warehouse_status="ready_for_pipeline",
            metadata={
                "created_from": "ue_suite_summary",
                "legacy_identity_basis": "package_id",
                "source_relative_path": "repair/sample.pmx",
                "manifest_path": "C:/legacy/repair/manifest.json",
            },
        )
        self.conn.execute(
            """
            INSERT INTO aliases (entity_type, entity_id, external_system, alias_key, alias_value)
            VALUES ('package', ?, '3dgirls', 'package_id', 'repair_pkg_001')
            """,
            (strong["id"],),
        )
        self.conn.execute(
            """
            INSERT INTO artifacts (owner_type, owner_id, stage, artifact_kind, path, format, status, metadata_json, created_at)
            VALUES ('package', ?, 'session', 'ue_suite_summary', 'C:/legacy/repair/ue_suite_summary.json', 'json', 'imported', ?, '2026-04-12T00:00:00+00:00')
            """,
            (weak["id"], '{"source_relative_path":"repair/sample.pmx","manifest_path":"C:/legacy/repair/manifest.json"}'),
        )
        self.conn.commit()

        result = repair_legacy_3dgirls_lineage(self.conn)
        package_count = self.conn.execute("SELECT COUNT(*) FROM packages WHERE sample_id = ?", (sample["id"],)).fetchone()[0]

        self.assertEqual(result["packages_pruned"], 1)
        self.assertEqual(package_count, 1)

    def test_repair_blocks_ready_package_when_conversion_manifest_is_missing(self) -> None:
        sample = ensure_sample(
            self.conn,
            canonical_sample_id=make_canonical_id("sample", "manual", "blocked-sample"),
            display_name="Blocked Sample",
        )
        package = ensure_package(
            self.conn,
            sample_id=sample["id"],
            canonical_package_id=make_canonical_id("pkg", "manual", "blocked-package"),
            package_role="character",
            content_bucket="Characters",
            contract_type="package",
            consumer_ready=True,
            warehouse_status="ready_for_pipeline",
        )

        result = repair_legacy_3dgirls_lineage(self.conn)
        refreshed = self.conn.execute("SELECT * FROM packages WHERE id = ?", (package["id"],)).fetchone()
        gaps = lineage_gap_rows(self.conn)

        self.assertEqual(result["packages_blocked"], 1)
        self.assertEqual(refreshed["consumer_ready"], 0)
        self.assertEqual(refreshed["warehouse_status"], "blocked")
        self.assertFalse(any(row["canonical_package_id"] == refreshed["canonical_package_id"] for row in gaps))


if __name__ == "__main__":
    unittest.main()
