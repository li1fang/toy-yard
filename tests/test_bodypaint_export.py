from __future__ import annotations

import json
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
from toyyard.warehouse import ensure_artifact, ensure_package, ensure_sample, make_canonical_id, seed_canonical_root


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
        signal = build_bodypaint_export_signal_from_paths(self.paths, "bodypaint-smoke")
        manifest_path = Path(registry["package_index"][package_id]["manifest_path"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        staged_source = Path(result["export_root"]) / registry["package_index"][package_id]["source_model_relpath"]

        self.assertEqual(result["packages"], [package_id])
        self.assertEqual(result["ready_items"], 1)
        self.assertEqual(summary["counts"]["ready_items"], 1)
        self.assertEqual(registry["counts"]["source_model_ready"], 1)
        self.assertTrue(staged_source.exists())
        self.assertEqual(manifest["expected_outputs"]["normalized_glb"], "outputs/model.glb")
        self.assertEqual(manifest["expected_outputs"]["painted_glb"], "outputs/model.bodypaint.glb")
        self.assertEqual(manifest["viewer"]["preferred_model"], "outputs/model.bodypaint.glb")
        self.assertEqual(manifest["source_model"]["artifact_kind"], "output_fbx")
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

    def test_export_bodypaint_view_surfaces_missing_source_model_as_blocker(self) -> None:
        sample_id, package_id, _ = self._seed_character_package(model_exists=False)

        result = export_bodypaint_view(
            self.conn,
            self.paths,
            profile="bodypaint-missing-source",
            package_refs=[package_id],
        )
        registry = json.loads(Path(result["registry_path"]).read_text(encoding="utf-8"))
        signal = build_bodypaint_export_signal_from_paths(self.paths, "bodypaint-missing-source")

        self.assertEqual(result["ready_items"], 0)
        self.assertFalse(registry["package_index"][package_id]["source_model_available"])
        self.assertEqual(signal["status"], "blocker")
        self.assertEqual(signal["handoff_state"], "bodypaint_input_missing")
        self.assertFalse(signal["handoff_ready"])

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
        (output_dir / "bodypaint.analysis.json").write_text(json.dumps({"version": "0.1"}), encoding="utf-8")
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
        artifact_kinds = {
            row["artifact_kind"]
            for row in self.conn.execute("SELECT artifact_kind FROM artifacts WHERE stage = 'bodypaint'").fetchall()
        }
        self.assertIn("normalized_glb", artifact_kinds)
        self.assertIn("painted_glb", artifact_kinds)
        self.assertIn("analysis_json", artifact_kinds)
        self.assertIn("manual_overrides", artifact_kinds)
        self.assertIn("bodypaint_result_import", artifact_kinds)

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
        (output_dir / "bodypaint.analysis.json").write_text(json.dumps({"version": "0.1"}), encoding="utf-8")

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
