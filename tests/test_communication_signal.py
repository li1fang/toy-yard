from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from toyyard.communication_signal import build_aiue_motion_export_signal, build_aiue_pmx_export_signal


class CommunicationSignalTests(unittest.TestCase):
    def test_blocker_when_portable_export_is_inconsistent(self) -> None:
        signal = build_aiue_pmx_export_signal(
            profile="trial-broken",
            summary_payload={"sample_id": "sample_1", "counts": {"failed_items": 1}},
            registry_payload={"sample_id": "sample_1", "characters": [], "weapons": [], "ready_pairs": []},
            manifest_check_payload={"status": "attention", "counts": {"attention_count": 1}},
            summary_path=Path("C:/tmp/summary.json"),
            registry_path=Path("C:/tmp/registry.json"),
            manifest_check_path=Path("C:/tmp/check.json"),
        )

        self.assertEqual(signal["status"], "blocker")
        self.assertEqual(signal["handoff_state"], "export_packet_blocked")
        self.assertFalse(signal["handoff_ready"])
        self.assertEqual(signal["problem_layer"], "export_contract")

    def test_ready_for_aiue_when_runtime_evidence_has_not_arrived_yet(self) -> None:
        signal = build_aiue_pmx_export_signal(
            profile="trial-portable",
            summary_payload={"sample_id": "sample_2", "counts": {"failed_items": 0}},
            registry_payload={
                "sample_id": "sample_2",
                "characters": [{"package_id": "pkg_character", "skeletal_mesh": "", "skeleton": "", "physics_asset": ""}],
                "weapons": [{"package_id": "pkg_weapon", "skeletal_mesh": ""}],
                "ready_pairs": [
                    {
                        "character_package_id": "pkg_character",
                        "weapon_package_id": "pkg_weapon",
                        "character_skeletal_mesh": "",
                        "weapon_skeletal_mesh": "",
                        "equip_slot": "weapon",
                        "preferred_attach_target": {"type": "socket", "name": "WeaponSocket"},
                    }
                ],
            },
            manifest_check_payload={"status": "pass", "counts": {"attention_count": 0}},
            summary_path=Path("C:/tmp/summary.json"),
            registry_path=Path("C:/tmp/registry.json"),
            manifest_check_path=Path("C:/tmp/check.json"),
        )

        self.assertEqual(signal["status"], "info")
        self.assertEqual(signal["handoff_state"], "portable_export_ready")
        self.assertTrue(signal["handoff_ready"])
        self.assertEqual(signal["recommended_next_node"], "aiue_import_validate_trial")

    def test_runtime_registry_ready_after_roundtrip(self) -> None:
        signal = build_aiue_pmx_export_signal(
            profile="trial-runtime",
            summary_payload={"sample_id": "sample_3", "counts": {"failed_items": 0}},
            registry_payload={
                "sample_id": "sample_3",
                "characters": [
                    {
                        "package_id": "pkg_character",
                        "skeletal_mesh": "/Game/Characters/Cantarella.Cantarella",
                        "skeleton": "/Game/Characters/Cantarella_Skeleton.Cantarella_Skeleton",
                        "physics_asset": "/Game/Characters/Cantarella_Physics.Cantarella_Physics",
                    }
                ],
                "weapons": [
                    {
                        "package_id": "pkg_weapon",
                        "skeletal_mesh": "/Game/Weapons/CantarellaWeapon.CantarellaWeapon",
                    }
                ],
                "ready_pairs": [
                    {
                        "character_package_id": "pkg_character",
                        "weapon_package_id": "pkg_weapon",
                        "character_skeletal_mesh": "/Game/Characters/Cantarella.Cantarella",
                        "weapon_skeletal_mesh": "/Game/Weapons/CantarellaWeapon.CantarellaWeapon",
                        "equip_slot": "weapon",
                        "preferred_attach_target": {"type": "socket", "name": "WeaponSocket"},
                    }
                ],
            },
            manifest_check_payload={"status": "pass", "counts": {"attention_count": 0}},
            summary_path=Path("C:/tmp/summary.json"),
            registry_path=Path("C:/tmp/registry.json"),
            manifest_check_path=Path("C:/tmp/check.json"),
        )

        self.assertEqual(signal["status"], "info")
        self.assertEqual(signal["handoff_state"], "runtime_registry_ready")
        self.assertTrue(signal["handoff_ready"])
        self.assertEqual(signal["recommended_next_node"], "aiue_refresh_assets_or_downstream_trial")

    def test_motion_packet_ready_for_m0_5_shadow_trial(self) -> None:
        signal = build_aiue_motion_export_signal(
            profile="trial-motion",
            summary_payload={"sample_id": "sample_motion"},
            registry_payload={
                "sample_id": "sample_motion",
                "clips": [
                    {
                        "package_id": "pkg_motion",
                        "selection_ready": True,
                    }
                ],
            },
            packet_check_payload={"status": "pass", "counts": {"manifest_count": 1, "selection_ready_count": 1}},
            summary_path=Path("C:/tmp/motion_summary.json"),
            registry_path=Path("C:/tmp/motion_registry.json"),
            packet_check_path=Path("C:/tmp/motion_check.json"),
        )

        self.assertEqual(signal["status"], "info")
        self.assertTrue(signal["handoff_ready"])
        self.assertEqual(signal["recommended_next_node"], "aiue_import_motion_packet")
        self.assertIn("M0.5 shadow-consumer ingest", signal["summary"])


if __name__ == "__main__":
    unittest.main()
