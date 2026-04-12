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

from toyyard.db import init_db
from toyyard.ingest import import_path
from toyyard.motion_handoff import import_motion_handoff
from toyyard.paths import ProjectPaths
from toyyard.reports import motion_catalog_rows
from toyyard.warehouse import seed_canonical_root, source_artifacts, source_linked_samples


class MotionHandoffTests(unittest.TestCase):
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

            for pack_version in ("v0.1", "v0.2"):
                manifest = {
                    "schema_version": "1.0.0",
                    "pack_version": pack_version,
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
                    "exports": {
                        "canonical_npz": "motion.npz",
                        "bvh": "motion.bvh",
                    },
                }
                validation_report = {
                    "schema_version": "1.0.0",
                    "reports": [
                        {
                            "status": "pass",
                            "clip_id": manifest["clip_id"],
                            "pack_version": pack_version,
                            "scenario_id": manifest["scenario_id"],
                            "metrics": {"heading_delta_rad": 0.7},
                        }
                    ],
                }
                capability = {
                    "supported_now": ["3-second single-actor short clips"],
                    "supported_with_caution": ["two-hand receive-ready motions"],
                    "not_yet_admitted": ["multi-actor motions"],
                }
                pack_root = f"examples/motion-packs/{pack_version}/route-a-3s-turn-hand-ready"
                self._write_zip_text(archive, f"{pack_root}/manifest.json", json.dumps(manifest))
                self._write_zip_text(archive, f"{pack_root}/README.md", "# Clip Readme\n")
                self._write_zip_text(archive, f"{pack_root}/motion.npz", "npz")
                self._write_zip_text(archive, f"{pack_root}/motion.bvh", "bvh")
                self._write_zip_text(archive, f"{pack_root}/skeleton.soma_77.json", json.dumps({"joint_count": 77}))
                self._write_zip_text(archive, f"examples/motion-packs/{pack_version}/validation.report.json", json.dumps(validation_report))
                self._write_zip_text(archive, f"examples/motion-packs/{pack_version}/capability-assessment.json", json.dumps(capability))
        return zip_path

    def test_import_motion_handoff_catalogs_source_scenario_and_versioned_clips(self) -> None:
        source_zip = self._create_motion_handoff_zip()
        v1_package = import_path(self.conn, self.paths, source_zip, source_kind="manual_drop", game_hint="ai_motion")

        result = import_motion_handoff(self.conn, self.paths, package_id=v1_package["id"])
        catalog_rows = motion_catalog_rows(self.conn)
        source_row = self.conn.execute("SELECT * FROM sources ORDER BY id").fetchone()
        source_artifact_rows = source_artifacts(self.conn, source_row["id"])
        linked_samples = source_linked_samples(self.conn, source_row["id"])
        package_capability_count = self.conn.execute(
            "SELECT COUNT(*) FROM artifacts WHERE owner_type = 'package' AND artifact_kind = 'motion_capability_assessment'"
        ).fetchone()[0]
        clip_version_alias_count = self.conn.execute(
            "SELECT COUNT(*) FROM aliases WHERE external_system = 'ai_motion' AND alias_key = 'clip_version'"
        ).fetchone()[0]

        self.assertEqual(result["samples_total"], 1)
        self.assertEqual(result["packages_total"], 2)
        self.assertEqual(len(catalog_rows), 2)
        self.assertEqual({row["pack_version"] for row in catalog_rows}, {"v0.1", "v0.2"})
        self.assertEqual(len({row["canonical_sample_id"] for row in catalog_rows}), 1)
        self.assertEqual({row["clip_id"] for row in catalog_rows}, {"route-a-3s-turn-hand-ready--20260412T180000Z"})
        self.assertTrue(all(row["runtime_semantics"] == "runtime" for row in catalog_rows))
        self.assertTrue(all(row["validation_status"] == "pass" for row in catalog_rows))
        self.assertTrue(all(row["warehouse_status"] == "cataloged" for row in catalog_rows))
        self.assertTrue(all(row["consumer_ready"] == 0 for row in catalog_rows))
        self.assertEqual(len(linked_samples), 1)
        self.assertGreaterEqual(len(source_artifact_rows), 7)
        self.assertEqual(package_capability_count, 0)
        self.assertEqual(clip_version_alias_count, 2)

        sample_metadata = json.loads(self.conn.execute("SELECT metadata_json FROM samples").fetchone()[0])
        self.assertEqual(sample_metadata["scenario_id"], "route-a-3s-turn-hand-ready")
        self.assertIn("turn", sample_metadata["scenario_tags"])
        self.assertEqual(len(sample_metadata["supported_now"]), 2)


if __name__ == "__main__":
    unittest.main()
