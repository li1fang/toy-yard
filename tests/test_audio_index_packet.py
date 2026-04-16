from __future__ import annotations

import json
import tempfile
import unittest
import wave
from pathlib import Path

from toyyard.audio_index_packet import export_audio_index_packet, import_audio_index_packet
from toyyard.audio_session import import_audio_session
from toyyard.db import init_db
from toyyard.paths import ProjectPaths
from toyyard.reports import audio_catalog_rows
from toyyard.warehouse import seed_canonical_root


def _write_wav(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * 1600)


class AudioIndexPacketTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

        self.linux_paths = ProjectPaths(self.root / "toy-yard-linux")
        self.linux_paths.ensure_layout()
        self.linux_conn = init_db(self.linux_paths.db_path)
        seed_canonical_root(self.linux_conn, self.linux_paths.root)

        self.windows_paths = ProjectPaths(self.root / "toy-yard-windows")
        self.windows_paths.ensure_layout()
        self.windows_conn = init_db(self.windows_paths.db_path)
        seed_canonical_root(self.windows_conn, self.windows_paths.root)

    def tearDown(self) -> None:
        self.linux_conn.close()
        self.windows_conn.close()
        self.temp_dir.cleanup()

    def _create_audio_session(self, session_id: str) -> Path:
        session_root = self.root / "foundry-sessions" / session_id
        source_audio = session_root / "input" / "source_audio.wav"
        normalized_audio = session_root / "input" / "normalized_audio.wav"
        transcript_path = session_root / "artifacts" / "transcript.txt"
        _write_wav(source_audio)
        _write_wav(normalized_audio)
        transcript_path.parent.mkdir(parents=True, exist_ok=True)
        transcript_path.write_text("hello from replicated audio", encoding="utf-8")

        manifest_path = session_root / "session_manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "session_id": session_id,
                    "session_root": str(session_root),
                    "source": {
                        "kind": "local_path",
                        "original_path": str(source_audio),
                        "copied_path": str(source_audio),
                    },
                    "normalized_audio": {
                        "path": str(normalized_audio),
                        "sample_rate": 16000,
                        "channels": 1,
                        "duration_sec": 0.1,
                        "sha256": f"sha-{session_id}",
                    },
                    "downloaded_artifacts": {
                        "transcript_text": str(transcript_path),
                    },
                    "result_summary": {
                        "text": "hello from replicated audio",
                        "language": "en",
                        "duration_sec": 0.1,
                    },
                }
            ),
            encoding="utf-8",
        )
        return manifest_path

    def test_export_and_import_audio_index_packet_keeps_remote_only_audio_index(self) -> None:
        manifest_path = self._create_audio_session("audio-session-repl-001")
        import_audio_session(self.linux_conn, self.linux_paths, manifest_path=manifest_path)

        exported = export_audio_index_packet(
            self.linux_conn,
            self.linux_paths,
            session_id="audio-session-repl-001",
            node_id="linux-audio-01",
        )
        packet_path = Path(exported["packet_path"])
        imported = import_audio_index_packet(
            self.windows_conn,
            self.windows_paths,
            packet_path=packet_path,
        )

        self.assertEqual(imported["origin_node_id"], "linux-audio-01")
        self.assertEqual(imported["availability"], "remote_index_only")

        catalog_rows = audio_catalog_rows(self.windows_conn)
        self.assertEqual(len(catalog_rows), 1)
        self.assertEqual(catalog_rows[0]["availability"], "remote_index_only")
        self.assertEqual(catalog_rows[0]["replicated_from_node"], "linux-audio-01")

        artifacts = self.windows_conn.execute(
            """
            SELECT path, status, metadata_json
            FROM artifacts
            WHERE owner_type = 'package'
            ORDER BY id
            """
        ).fetchall()
        self.assertTrue(any(row["status"] == "remote_indexed" for row in artifacts))
        self.assertTrue(any(json.loads(row["metadata_json"] or "{}").get("remote_only") for row in artifacts))

    def test_import_audio_index_packet_is_idempotent(self) -> None:
        manifest_path = self._create_audio_session("audio-session-repl-002")
        import_audio_session(self.linux_conn, self.linux_paths, manifest_path=manifest_path)
        exported = export_audio_index_packet(
            self.linux_conn,
            self.linux_paths,
            session_id="audio-session-repl-002",
            node_id="linux-audio-01",
        )
        packet_path = Path(exported["packet_path"])

        first = import_audio_index_packet(self.windows_conn, self.windows_paths, packet_path=packet_path)
        second = import_audio_index_packet(self.windows_conn, self.windows_paths, packet_path=packet_path)

        packages = self.windows_conn.execute("SELECT * FROM packages WHERE content_bucket = 'audio'").fetchall()
        reports = self.windows_conn.execute("SELECT * FROM report_imports").fetchall()
        aliases = self.windows_conn.execute(
            """
            SELECT * FROM aliases
            WHERE entity_type = 'package' AND external_system = 'comfyui_remote_foundry' AND alias_key = 'session_id'
            """
        ).fetchall()

        self.assertEqual(first["packet_id"], second["packet_id"])
        self.assertEqual(len(packages), 1)
        self.assertEqual(len(reports), 1)
        self.assertEqual(len(aliases), 1)


if __name__ == "__main__":
    unittest.main()
