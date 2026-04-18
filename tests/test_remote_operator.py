from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from toyyard.db import init_db
from toyyard.paths import ProjectPaths
from toyyard.remote_operator import (
    load_ssh_host_profile,
    register_remote_host,
    remote_handoff_bodypaint_view,
    remote_probe,
    remote_status,
    remote_toyyard,
)
from toyyard.warehouse import seed_canonical_root


class _Completed:
    def __init__(self, *, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class RemoteOperatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "toy-yard"
        self.paths = ProjectPaths(self.root)
        self.paths.ensure_layout()
        self.conn = init_db(self.paths.db_path)
        seed_canonical_root(self.conn, self.paths.root)
        self.profile_path = self.paths.operator_host_profile_path("linux-toyyard-01")
        self.profile_path.parent.mkdir(parents=True, exist_ok=True)
        self.profile_payload = {
            "schema_version": "ssh_host_profile_v1",
            "document_role": "machine_readable_ssh_host_profile",
            "profile_name": "linux-toyyard-01",
            "node": {
                "node_id": "linux-toyyard-01",
                "display_name": "Linux Toy-Yard Node 01",
                "os_family": "linux",
                "shell_family": "bash",
                "path_style": "posix",
            },
            "network": {
                "primary_hostname": "linux-toyyard-01.local",
                "primary_ipv4": "192.168.1.50",
            },
            "ssh": {
                "transport": "ssh",
                "port": 22,
                "username": "whipz",
                "host_key_verification": {
                    "required": True,
                    "known_hosts_path": "C:/Users/garro/.ssh/vcompose-fabric-known_hosts",
                },
                "auth": {
                    "supported_methods": ["publickey"],
                    "preferred_method": "publickey",
                    "credential_ref": {
                        "kind": "ssh_key_file",
                        "local_path": "C:/Users/garro/.ssh/vcompose-fabric-mesh",
                    },
                },
            },
            "filesystem": {
                "scp_path_style": "posix",
            },
            "transfer_profiles": [
                {
                    "profile_id": "toy_yard_index_packets",
                    "remote_directory": "/srv/toy-yard/_exchange/index_packets",
                    "path_style": "posix",
                    "create_if_missing": False,
                }
            ],
        }
        self.profile_path.write_text(json.dumps(self.profile_payload), encoding="utf-8")
        self.windows_profile_path = self.paths.operator_host_profile_path("windows-3070ti-operator-target")
        self.windows_profile_payload = {
            "schema_version": "ssh_host_profile_v1",
            "document_role": "machine_readable_ssh_host_profile",
            "profile_name": "windows-3070ti-operator-target",
            "node": {
                "node_id": "windows-gpu-3070ti-01",
                "display_name": "Windows Toy-Yard Target",
                "os_family": "windows",
                "shell_family": "powershell",
                "path_style": "windows",
            },
            "network": {
                "primary_hostname": "light",
                "primary_ipv4": "192.168.1.44",
            },
            "ssh": {
                "transport": "ssh",
                "port": 22,
                "username": "garro",
                "host_key_verification": {
                    "required": True,
                    "known_hosts_path": "C:/Users/garro/.ssh/vcompose-fabric-known_hosts",
                    "source_known_hosts_path": "/home/whipz/.ssh/windows-known_hosts",
                },
                "auth": {
                    "supported_methods": ["publickey"],
                    "preferred_method": "publickey",
                    "credential_ref": {
                        "kind": "ssh_key_file",
                        "local_path": "C:/Users/garro/.ssh/vcompose-fabric-mesh",
                        "linux_path": "/home/whipz/.ssh/windows-3070ti-lan",
                        "windows_path": "C:/Users/garro/.ssh/windows-3070ti-lan",
                    },
                },
            },
            "filesystem": {
                "scp_path_style": "windows_drive",
            },
            "transfer_profiles": [
                {
                    "profile_id": "toy_yard_bodypaint_packets",
                    "remote_directory": "C:/Projects/toy-yard/_exchange/consumer_packets/bodypaint",
                    "path_style": "windows_drive",
                    "create_if_missing": True,
                }
            ],
        }
        self.windows_profile_path.write_text(json.dumps(self.windows_profile_payload), encoding="utf-8")

    def tearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    def test_load_ssh_host_profile_requires_schema(self) -> None:
        payload = load_ssh_host_profile(self.profile_path)
        self.assertEqual(payload["profile_name"], "linux-toyyard-01")
        self.assertEqual(payload["node"]["shell_family"], "bash")

    def test_register_remote_host_is_idempotent(self) -> None:
        first = register_remote_host(
            self.conn,
            self.paths,
            profile_path=self.profile_path,
            project_root="/srv/toy-yard",
        )
        second = register_remote_host(
            self.conn,
            self.paths,
            profile_path=self.profile_path,
            project_root="/srv/toy-yard",
        )
        count = self.conn.execute("SELECT COUNT(*) FROM operator_nodes").fetchone()[0]
        self.assertEqual(first["profile_name"], second["profile_name"])
        self.assertEqual(count, 1)

    @patch("toyyard.remote_operator.subprocess.run")
    def test_remote_probe_records_result_file(self, mock_run) -> None:
        register_remote_host(self.conn, self.paths, profile_path=self.profile_path, project_root="/srv/toy-yard")
        mock_run.return_value = _Completed(returncode=0, stdout="whipz\n", stderr="")

        result = remote_probe(self.conn, self.paths, host_ref="linux-toyyard-01")

        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["observations"]["hostname_stdout"], "whipz")
        result_path = self.paths.operator_run_result_path(result["operation_id"])
        self.assertTrue(result_path.exists())

    @patch("toyyard.remote_operator.subprocess.run")
    def test_remote_status_returns_status_payload(self, mock_run) -> None:
        register_remote_host(self.conn, self.paths, profile_path=self.profile_path, project_root="/srv/toy-yard")
        payload = {
            "project_root": "/srv/toy-yard",
            "repo_root": "/srv/toy-yard/repo",
            "repo_exists": True,
            "git_branch": "main",
            "git_commit": "abc1234",
            "git_origin": "https://example.com/repo.git",
            "python_executable": "/usr/bin/python3",
            "python_version": "3.13.7",
            "venv_candidates": [],
            "db_path": "/srv/toy-yard/_db/toyyard.sqlite",
            "db_exists": True,
            "exchange_root": "/srv/toy-yard/_exchange",
            "index_packet_root": "/srv/toy-yard/_exchange/index_packets",
            "index_packet_root_exists": True,
            "toyyard_entry": "/srv/toy-yard/repo/toyyard.py",
            "toyyard_entry_exists": True,
        }
        mock_run.return_value = _Completed(returncode=0, stdout=json.dumps(payload) + "\n", stderr="")

        result = remote_status(self.conn, self.paths, host_ref="linux-toyyard-01")

        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["status_payload"]["repo_root"], "/srv/toy-yard/repo")
        self.assertEqual(result["status_payload"]["git_commit"], "abc1234")

    def test_remote_toyyard_rejects_unapproved_command(self) -> None:
        register_remote_host(self.conn, self.paths, profile_path=self.profile_path, project_root="/srv/toy-yard")
        with self.assertRaises(ValueError):
            remote_toyyard(self.conn, self.paths, host_ref="linux-toyyard-01", toyyard_args=["repair", "legacy-3dgirls-lineage"])

    @patch("toyyard.remote_operator.subprocess.run")
    def test_remote_handoff_bodypaint_view_transfers_export_directory(self, mock_run) -> None:
        register_remote_host(self.conn, self.paths, profile_path=self.profile_path, project_root="/srv/toy-yard")
        register_remote_host(self.conn, self.paths, profile_path=self.windows_profile_path, project_root="C:/Projects/toy-yard")
        export_payload = {
            "profile": "trial-bodypaint-cassia",
            "sample_ids": ["sample_cassia"],
            "package_ids": ["pkg_cassia_body"],
            "export_root": "/srv/toy-yard/05_publish/bodypaint/trial-bodypaint-cassia",
            "summary_path": "/srv/toy-yard/05_publish/bodypaint/trial-bodypaint-cassia/summary/bodypaint_suite_summary.json",
            "registry_path": "/srv/toy-yard/05_publish/bodypaint/trial-bodypaint-cassia/summary/bodypaint_packet_registry.json",
            "packet_check_path": "/srv/toy-yard/05_publish/bodypaint/trial-bodypaint-cassia/summary/bodypaint_packet_check.json",
            "communication_signal_path": "/srv/toy-yard/05_publish/bodypaint/trial-bodypaint-cassia/summary/communication_signal.json",
        }
        verify_payload = {
            "required_files": {
                "summary_path": True,
                "registry_path": True,
                "packet_check_path": True,
                "communication_signal_path": True,
            },
            "registry_packet_count": 1,
            "packet_check_status": "pass",
            "communication_signal_status": "pass",
            "acceptance": {
                "summary_exists": True,
                "registry_exists": True,
                "packet_check_exists": True,
                "communication_signal_exists": True,
                "has_packets": True,
            },
        }
        tree_payload = {
            "root": "/srv/toy-yard/05_publish/bodypaint/trial-bodypaint-cassia",
            "exists": True,
            "file_count": 4,
            "total_bytes": 400,
            "tree_hash": "tree123",
            "files": [
                {"path": "summary/bodypaint_suite_summary.json", "size_bytes": 100, "sha256": "a"},
                {"path": "summary/bodypaint_packet_registry.json", "size_bytes": 100, "sha256": "b"},
                {"path": "summary/bodypaint_packet_check.json", "size_bytes": 100, "sha256": "c"},
                {"path": "summary/communication_signal.json", "size_bytes": 100, "sha256": "d"},
            ],
        }
        mock_run.side_effect = [
            _Completed(returncode=0, stdout=json.dumps({"project_root": "/srv/toy-yard", "repo_root": "/srv/toy-yard/repo", "repo_exists": True, "git_branch": "main", "git_commit": "abc123", "git_origin": "https://example.com/repo.git", "python_executable": "/usr/bin/python3", "python_version": "3.13.7", "venv_candidates": [], "db_path": "/srv/toy-yard/_db/toyyard.sqlite", "db_exists": True, "exchange_root": "/srv/toy-yard/_exchange", "index_packet_root": "/srv/toy-yard/_exchange/index_packets", "index_packet_root_exists": True, "toyyard_entry": "/srv/toy-yard/repo/toyyard.py", "toyyard_entry_exists": True}) + "\n"),
            _Completed(returncode=0, stdout=json.dumps(export_payload) + "\n"),
            _Completed(returncode=0, stdout=json.dumps(tree_payload) + "\n"),
            _Completed(returncode=0, stdout="", stderr=""),
            _Completed(returncode=0, stdout="", stderr=""),
            _Completed(returncode=0, stdout="", stderr=""),
            _Completed(returncode=0, stdout=json.dumps(verify_payload) + "\n"),
            _Completed(returncode=0, stdout=json.dumps(tree_payload | {"root": "C:/Projects/toy-yard/_exchange/consumer_packets/bodypaint/.incoming/op_remote-handoff-bodypaint-view_abc123/trial-bodypaint-cassia"}) + "\n"),
            _Completed(returncode=0, stdout=json.dumps({"staged_exists": True, "final_existed": False, "backup_path": "", "action": "promoted", "promoted": True}) + "\n"),
            _Completed(returncode=0, stdout=json.dumps(verify_payload) + "\n"),
            _Completed(returncode=0, stdout=json.dumps(tree_payload | {"root": "C:/Projects/toy-yard/_exchange/consumer_packets/bodypaint/trial-bodypaint-cassia"}) + "\n"),
        ]

        result = remote_handoff_bodypaint_view(
            self.conn,
            self.paths,
            source_host_ref="linux-toyyard-01",
            target_host_ref="windows-3070ti-operator-target",
            profile="trial-bodypaint-cassia",
            sample_ref="sample_cassia",
            target_transfer_profile="toy_yard_bodypaint_packets",
        )

        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["transfer"]["target_export_root"], "C:/Projects/toy-yard/_exchange/consumer_packets/bodypaint/trial-bodypaint-cassia")
        self.assertEqual(
            result["transfer"]["staging_operation_root"],
            "C:/Projects/toy-yard/_exchange/consumer_packets/bodypaint/.incoming/" + result["operation_id"],
        )
        self.assertTrue(result["transfer"]["staged_tree_hash_match"])
        self.assertTrue(result["transfer"]["tree_hash_match"])
        self.assertTrue(all(result["verify_result"]["acceptance"].values()))
        self.assertTrue(result["promote_result"]["payload"]["promoted"])


if __name__ == "__main__":
    unittest.main()
