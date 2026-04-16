from __future__ import annotations

import json
import tempfile
import unittest
import wave
from pathlib import Path

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


def _write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


class AudioSessionTests(unittest.TestCase):
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

    def test_import_audio_session_creates_audio_sample_package_and_artifacts(self) -> None:
        session_root = Path(self.temp_dir.name) / "foundry-sessions" / "audio" / "audio-session-001"
        source_audio = session_root / "input" / "source_audio.wav"
        normalized_audio = session_root / "input" / "normalized_audio.wav"
        transcript_path = session_root / "artifacts" / "transcript.txt"
        segments_path = session_root / "artifacts" / "segments.json"
        _write_wav(source_audio)
        _write_wav(normalized_audio)
        transcript_path.parent.mkdir(parents=True, exist_ok=True)
        transcript_path.write_text("hello from audio lane", encoding="utf-8")
        segments_path.write_text(json.dumps([{"id": 0, "start": 0.0, "end": 0.1, "text": "hello"}]), encoding="utf-8")

        manifest_path = session_root / "session_manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "session_id": "audio-session-001",
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
                        "sha256": "abc123",
                    },
                    "downloaded_artifacts": {
                        "transcript_text": str(transcript_path),
                        "segments_json": str(segments_path),
                    },
                    "result_summary": {
                        "text": "hello from audio lane",
                        "language": "en",
                        "duration_sec": 0.1,
                    },
                }
            ),
            encoding="utf-8",
        )

        result = import_audio_session(self.conn, self.paths, manifest_path=manifest_path)
        sample_row = self.conn.execute("SELECT * FROM samples WHERE item_family = 'audio'").fetchone()
        package_row = self.conn.execute("SELECT * FROM packages WHERE content_bucket = 'audio'").fetchone()
        artifact_rows = self.conn.execute(
            "SELECT artifact_kind, path FROM artifacts WHERE owner_type = 'package' AND owner_id = ? ORDER BY artifact_kind",
            (package_row["id"],),
        ).fetchall()

        self.assertEqual(result["contract_type"], "transcribed_audio_v0")
        self.assertEqual(sample_row["canonical_sample_id"], result["canonical_sample_id"])
        self.assertEqual(package_row["canonical_package_id"], result["canonical_package_id"])
        self.assertTrue(
            {row["artifact_kind"] for row in artifact_rows}
            >= {"normalized_audio", "transcript_text", "segments_json", "promotion_manifest"}
        )

        catalog_rows = audio_catalog_rows(self.conn)
        self.assertEqual(len(catalog_rows), 1)
        self.assertEqual(catalog_rows[0]["language"], "en")

    def test_import_audio_session_preserves_non_wav_normalized_extension(self) -> None:
        session_root = Path(self.temp_dir.name) / "foundry-sessions" / "audio" / "audio-session-002"
        normalized_audio = session_root / "input" / "normalized_audio.mp3"
        _write_bytes(normalized_audio, b"fake-mp3-data")

        manifest_path = session_root / "session_manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "session_id": "audio-session-002",
                    "session_root": str(session_root),
                    "normalized_audio": {
                        "path": str(normalized_audio),
                        "sample_rate": 44100,
                        "channels": 2,
                        "duration_sec": 1.2,
                        "sha256": "def456",
                    },
                    "result_summary": {
                        "language": "ja",
                        "duration_sec": 1.2,
                    },
                }
            ),
            encoding="utf-8",
        )

        result = import_audio_session(self.conn, self.paths, manifest_path=manifest_path)
        promotion_manifest = json.loads(Path(result["promotion_manifest_path"]).read_text(encoding="utf-8"))
        package_row = self.conn.execute(
            "SELECT * FROM packages WHERE canonical_package_id = ?",
            (result["canonical_package_id"],),
        ).fetchone()
        artifact_row = self.conn.execute(
            """
            SELECT * FROM artifacts
            WHERE owner_type = 'package' AND owner_id = ? AND artifact_kind = 'normalized_audio'
            """,
            (package_row["id"],),
        ).fetchone()

        self.assertEqual(result["contract_type"], "single_utterance_audio_v0")
        self.assertTrue(promotion_manifest["normalized_audio_path"].endswith(".mp3"))
        self.assertEqual(Path(promotion_manifest["normalized_audio_path"]).read_bytes(), b"fake-mp3-data")
        self.assertEqual(artifact_row["format"], ".mp3")

    def test_reimport_audio_session_refreshes_canonical_copies(self) -> None:
        session_root = Path(self.temp_dir.name) / "foundry-sessions" / "audio" / "audio-session-003"
        normalized_audio = session_root / "input" / "normalized_audio.wav"
        transcript_path = session_root / "artifacts" / "transcript.txt"
        _write_wav(normalized_audio)
        transcript_path.parent.mkdir(parents=True, exist_ok=True)
        transcript_path.write_text("version-one", encoding="utf-8")

        manifest_path = session_root / "session_manifest.json"
        payload = {
            "session_id": "audio-session-003",
            "session_root": str(session_root),
            "normalized_audio": {
                "path": str(normalized_audio),
                "sample_rate": 16000,
                "channels": 1,
                "duration_sec": 0.1,
                "sha256": "ghi789",
            },
            "downloaded_artifacts": {
                "transcript_text": str(transcript_path),
            },
            "result_summary": {
                "text": "version-one",
                "language": "en",
                "duration_sec": 0.1,
            },
        }
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")

        first = import_audio_session(self.conn, self.paths, manifest_path=manifest_path)
        first_manifest = json.loads(Path(first["promotion_manifest_path"]).read_text(encoding="utf-8"))

        _write_bytes(normalized_audio, b"replacement-audio")
        transcript_path.write_text("version-two", encoding="utf-8")
        payload["result_summary"]["text"] = "version-two"
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")

        second = import_audio_session(self.conn, self.paths, manifest_path=manifest_path)
        second_manifest = json.loads(Path(second["promotion_manifest_path"]).read_text(encoding="utf-8"))

        self.assertEqual(first["canonical_package_id"], second["canonical_package_id"])
        self.assertEqual(Path(second_manifest["normalized_audio_path"]).read_bytes(), b"replacement-audio")
        self.assertEqual(Path(second_manifest["transcript_text_path"]).read_text(encoding="utf-8"), "version-two")
        self.assertEqual(first_manifest["normalized_audio_path"], second_manifest["normalized_audio_path"])

    def test_raw_audio_session_upgrades_to_transcribed_without_new_package(self) -> None:
        session_root = Path(self.temp_dir.name) / "foundry-sessions" / "audio" / "audio-session-004"
        normalized_audio = session_root / "input" / "normalized_audio.wav"
        transcript_path = session_root / "artifacts" / "transcript.txt"
        _write_wav(normalized_audio)
        transcript_path.parent.mkdir(parents=True, exist_ok=True)

        manifest_path = session_root / "session_manifest.json"
        raw_payload = {
            "session_id": "audio-session-004",
            "session_root": str(session_root),
            "normalized_audio": {
                "path": str(normalized_audio),
                "sample_rate": 16000,
                "channels": 1,
                "duration_sec": 0.1,
                "sha256": "jkl012",
            },
            "result_summary": {
                "language": "",
                "duration_sec": 0.1,
            },
        }
        manifest_path.write_text(json.dumps(raw_payload), encoding="utf-8")

        first = import_audio_session(self.conn, self.paths, manifest_path=manifest_path)
        package_rows_before = self.conn.execute("SELECT * FROM packages WHERE content_bucket = 'audio'").fetchall()

        transcript_path.write_text("upgraded transcript", encoding="utf-8")
        upgraded_payload = {
            **raw_payload,
            "downloaded_artifacts": {
                "transcript_text": str(transcript_path),
            },
            "result_summary": {
                "text": "upgraded transcript",
                "language": "en",
                "duration_sec": 0.1,
            },
        }
        manifest_path.write_text(json.dumps(upgraded_payload), encoding="utf-8")

        second = import_audio_session(self.conn, self.paths, manifest_path=manifest_path)
        package_rows_after = self.conn.execute("SELECT * FROM packages WHERE content_bucket = 'audio'").fetchall()
        package_row = self.conn.execute(
            "SELECT * FROM packages WHERE canonical_package_id = ?",
            (second["canonical_package_id"],),
        ).fetchone()
        alias_rows = self.conn.execute(
            """
            SELECT * FROM aliases
            WHERE entity_type = 'package' AND external_system = 'comfyui_remote_foundry' AND alias_key = 'session_id'
            """,
        ).fetchall()

        self.assertEqual(first["canonical_package_id"], second["canonical_package_id"])
        self.assertEqual(len(package_rows_before), 1)
        self.assertEqual(len(package_rows_after), 1)
        self.assertEqual(package_row["contract_type"], "transcribed_audio_v0")
        self.assertEqual(len(alias_rows), 1)


if __name__ == "__main__":
    unittest.main()
