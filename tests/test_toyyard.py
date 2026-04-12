from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from toyyard.archive import ArchiveReadError, list_package_files
from toyyard.db import connect, init_db
from toyyard.ingest import import_path, scan_source
from toyyard.paths import ProjectPaths
from toyyard.reports import blocked_rows, failure_rows, ready_rows
from toyyard.rules import load_rules
from toyyard.triage import run_triage
from toyyard.util import slugify


FIXTURES = Path(__file__).resolve().parents[1] / "_fixtures"


class ToyYardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "toy-yard"
        self.paths = ProjectPaths(self.root)
        self.paths.ensure_layout()
        self.conn = init_db(self.paths.db_path)
        self.rules = load_rules(self.paths.rules_dir)

    def tearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    def _copy_fixture_dir(self, name: str) -> Path:
        destination = self.root / "00_inbox" / "manual_drop" / name
        shutil.copytree(FIXTURES / name, destination)
        return destination

    def _make_zip(self, fixture_name: str, archive_name: str) -> Path:
        source = FIXTURES / fixture_name
        archive_path = self.root / archive_name
        with zipfile.ZipFile(archive_path, "w") as archive:
            for child in sorted(source.rglob("*")):
                if child.is_dir():
                    continue
                archive.write(child, child.relative_to(source).as_posix())
        return archive_path

    def _make_7z(self, fixture_name: str, archive_name: str) -> Path:
        source = FIXTURES / fixture_name
        archive_path = self.root / archive_name
        result = subprocess.run(
            ["tar", "-a", "-cf", str(archive_path), "-C", str(source), "."],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            self.skipTest(f"Unable to create 7z fixture with tar: {result.stderr.strip()}")
        return archive_path

    def test_slugify(self) -> None:
        self.assertEqual(slugify("NARAKA BLADEPOINT ROSY MERIDIEM 3BA SMP"), "naraka-bladepoint-rosy-meridiem-3ba-smp")
        self.assertEqual(slugify("   "), "package")

    def test_import_dedupes_by_sha_and_tracks_observations(self) -> None:
        source = self._copy_fixture_dir("texture_only")
        row1 = import_path(self.conn, self.paths, source, source_kind="manual_drop", game_hint="unknown")
        row2 = import_path(self.conn, self.paths, source, source_kind="manual_drop", game_hint="unknown")
        package_count = self.conn.execute("SELECT COUNT(*) FROM v1_packages").fetchone()[0]
        observation_count = self.conn.execute("SELECT COUNT(*) FROM v1_package_observations").fetchone()[0]

        self.assertEqual(row1["id"], row2["id"])
        self.assertEqual(package_count, 1)
        self.assertEqual(observation_count, 2)

    def test_archive_listing_supports_directory_zip_and_7z(self) -> None:
        folder = self._copy_fixture_dir("mixed_pack")
        folder_items = list_package_files(folder)
        self.assertTrue(any(item.ext == ".nif" for item in folder_items))

        zip_path = self._make_zip("mixed_pack", "mixed_pack.zip")
        zip_items = list_package_files(zip_path)
        self.assertTrue(any(item.ext == ".dds" for item in zip_items))

        seven_zip_path = self._make_7z("mixed_pack", "mixed_pack.7z")
        seven_zip_items = list_package_files(seven_zip_path)
        self.assertTrue(any(item.ext == ".esp" for item in seven_zip_items))

    def test_rule_loading_writes_defaults(self) -> None:
        generic_rules = self.rules["generic"]["signals"]
        skyrim_rules = self.rules["games"]["skyrimse"]["signals"]
        self.assertIn(".nif", generic_rules["mesh_extensions"])
        self.assertIn("calientetools/bodyslide", skyrim_rules["bodyslide_markers"])

    def test_triage_bodyslide_fixture_becomes_outfit_component(self) -> None:
        source = self._copy_fixture_dir("skyrim_bodyslide")
        package = import_path(self.conn, self.paths, source, source_kind="manual_drop", game_hint="unknown")
        result = run_triage(self.conn, self.paths, self.rules, package["id"])

        asset = self.conn.execute("SELECT * FROM v1_assets WHERE id = ?", (result["asset_id"],)).fetchone()
        deps = self.conn.execute("SELECT dep_name FROM v1_dependencies WHERE subject_id = ?", (result["asset_id"],)).fetchall()
        manifest = json.loads(self.paths.asset_manifest_path(result["asset_id"]).read_text(encoding="utf-8"))

        self.assertEqual(asset["asset_kind"], "outfit_set")
        self.assertEqual(asset["readiness"], "component_only")
        self.assertIn("BodySlide", {row["dep_name"] for row in deps})
        self.assertEqual(manifest["readiness"], "component_only")

    def test_outfit_signals_override_weapon_keywords(self) -> None:
        source = self.root / "00_inbox" / "manual_drop" / "naraka_bladepoint_3ba_smp"
        (source / "CalienteTools" / "BodySlide" / "ShapeData").mkdir(parents=True, exist_ok=True)
        (source / "Textures").mkdir(parents=True, exist_ok=True)
        (source / "CalienteTools" / "BodySlide" / "ShapeData" / "cloth.nif").write_text("mesh", encoding="utf-8")
        (source / "Textures" / "cloth.dds").write_text("texture", encoding="utf-8")

        package = import_path(self.conn, self.paths, source, source_kind="manual_drop", game_hint="unknown")
        result = run_triage(self.conn, self.paths, self.rules, package["id"])

        asset = self.conn.execute("SELECT asset_kind, readiness FROM v1_assets WHERE id = ?", (result["asset_id"],)).fetchone()
        self.assertEqual(asset["asset_kind"], "outfit_set")
        self.assertEqual(asset["readiness"], "component_only")

    def test_texture_only_fixture_is_blocked_and_reported(self) -> None:
        source = self._copy_fixture_dir("texture_only")
        package = import_path(self.conn, self.paths, source, source_kind="manual_drop", game_hint="unknown")
        run_triage(self.conn, self.paths, self.rules, package["id"])

        blocked = blocked_rows(self.conn)
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["asset_kind"], "texture_pack")

    def test_malformed_archive_creates_failure_and_reject_marker(self) -> None:
        source = FIXTURES / "malformed_archive.7z"
        package = import_path(self.conn, self.paths, source, source_kind="manual_drop", game_hint="unknown")
        result = run_triage(self.conn, self.paths, self.rules, package["id"])
        failures = failure_rows(self.conn, "triage")

        self.assertEqual(result["failure"], "archive_read_error")
        self.assertTrue(failures)
        self.assertTrue(self.paths.reject_marker_path("classify_failed", package["id"]).exists())

    def test_scan_vortex_skyrimse_imports_once_across_rescans(self) -> None:
        vortex_root = self.root / "fake_vortex_skyrimse"
        vortex_root.mkdir(parents=True, exist_ok=True)
        archive = self._make_zip("mixed_pack", "vortex_mixed.zip")
        shutil.copy2(archive, vortex_root / archive.name)

        with mock.patch("toyyard.ingest.DEFAULT_VORTEX_SKYRIMSE", vortex_root):
            first = scan_source(self.conn, self.paths, "vortex", game="skyrimse")
            second = scan_source(self.conn, self.paths, "vortex", game="skyrimse")

        package_count = self.conn.execute("SELECT COUNT(*) FROM v1_packages").fetchone()[0]
        observation_count = self.conn.execute("SELECT COUNT(*) FROM v1_package_observations").fetchone()[0]

        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertEqual(package_count, 1)
        self.assertEqual(observation_count, 2)

    def test_ready_report_lists_candidate_asset(self) -> None:
        source = self.root / "00_inbox" / "manual_drop" / "standalone_follower_xpmsse_3ba"
        (source / "Meshes").mkdir(parents=True, exist_ok=True)
        (source / "Textures").mkdir(parents=True, exist_ok=True)
        (source / "Meshes" / "body.nif").write_text("mesh", encoding="utf-8")
        (source / "Textures" / "body.dds").write_text("texture", encoding="utf-8")

        package = import_path(self.conn, self.paths, source, source_kind="manual_drop", game_hint="unknown")
        run_triage(self.conn, self.paths, self.rules, package["id"])
        ready = ready_rows(self.conn, "unreal")

        self.assertEqual(len(ready), 1)
        self.assertEqual(ready[0]["asset_kind"], "character")


if __name__ == "__main__":
    unittest.main()
