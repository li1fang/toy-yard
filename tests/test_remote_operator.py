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
from toyyard.remote_operator import load_ssh_host_profile, register_remote_host, remote_probe, remote_status, remote_toyyard
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


if __name__ == "__main__":
    unittest.main()
