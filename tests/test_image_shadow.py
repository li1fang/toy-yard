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

from toyyard.db import init_db
from toyyard.image_shadow_import import import_wallpaper_engine_catalog
from toyyard.paths import ProjectPaths
from toyyard.reports import image_pick_rows
from toyyard.wallpaper_engine_extractor import _iter_item_dirs, _load_state
from toyyard.warehouse import seed_canonical_root


class ImageShadowTests(unittest.TestCase):
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

    def test_item_dirs_only_include_numeric_workshop_folders(self) -> None:
        workshop_root = Path(self.temp_dir.name) / "431960"
        (workshop_root / "123456").mkdir(parents=True, exist_ok=True)
        (workshop_root / "not_an_item").mkdir(parents=True, exist_ok=True)
        (workshop_root / "234567").mkdir(parents=True, exist_ok=True)

        item_dirs = _iter_item_dirs(workshop_root)

        self.assertEqual([path.name for path in item_dirs], ["123456", "234567"])

    def test_import_wallpaper_engine_catalog_creates_image_shadow_sample_package_and_artifacts(self) -> None:
        workshop_root = Path(self.temp_dir.name) / "431960"
        source_dir = workshop_root / "2865004998"
        source_dir.mkdir(parents=True, exist_ok=True)
        frame_dir = self.paths.wallpaper_engine_item_extract_dir("2865004998")
        frame_dir.mkdir(parents=True, exist_ok=True)
        frame_path = frame_dir / "midframe_001_demo.jpg"
        frame_path.write_bytes(b"jpg")
        manifest_path = self.paths.wallpaper_engine_item_manifest_path("2865004998")
        manifest_payload = {
            "item_id": "2865004998",
            "source_dir": str(source_dir),
            "output_dir": str(frame_dir),
            "status": "pass",
            "processed_at_utc": "2026-04-13T07:00:00+07:00",
            "videos": [
                {
                    "video_path": str(source_dir / "demo.mp4"),
                    "video_relative_path": "demo.mp4",
                    "duration_sec": 10.0,
                    "midpoint_sec": 5.0,
                    "frame_path": str(frame_path),
                }
            ],
        }
        manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")
        self.paths.wallpaper_engine_state_path.parent.mkdir(parents=True, exist_ok=True)
        self.paths.wallpaper_engine_state_path.write_text(
            json.dumps(
                {
                    "state_version": "0.1",
                    "source_root": str(workshop_root),
                    "extract_root": str(self.paths.wallpaper_engine_extract_root),
                    "processed_items": {
                        "2865004998": manifest_payload,
                    },
                }
            ),
            encoding="utf-8",
        )

        state = _load_state(self.paths.wallpaper_engine_state_path)
        result = import_wallpaper_engine_catalog(self.conn, self.paths)
        sample_row = self.conn.execute("SELECT * FROM samples WHERE item_family = 'image'").fetchone()
        package_row = self.conn.execute("SELECT * FROM packages WHERE content_bucket = 'image'").fetchone()
        artifact_rows = self.conn.execute(
            "SELECT * FROM artifacts WHERE owner_type = 'package' AND owner_id = ? ORDER BY id",
            (package_row["id"],),
        ).fetchall()

        self.assertEqual(state["processed_items"]["2865004998"]["status"], "pass")
        self.assertEqual(result["samples_total"], 1)
        self.assertEqual(result["packages_total"], 1)
        self.assertEqual(sample_row["source_game"], "wallpaper_engine")
        self.assertEqual(package_row["contract_type"], "wallpaper_engine_midframe_v0")
        self.assertTrue(any(row["artifact_kind"] == "reference_image" for row in artifact_rows))

        pick_rows = image_pick_rows(self.conn, limit=10)
        self.assertEqual(len(pick_rows), 1)
        self.assertEqual(pick_rows[0]["workshop_item_id"], "2865004998")
        self.assertEqual(pick_rows[0]["video_relative_path"], "demo.mp4")
        self.assertTrue(str(pick_rows[0]["frame_path"]).endswith("midframe_001_demo.jpg"))


if __name__ == "__main__":
    unittest.main()
