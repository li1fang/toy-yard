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


if __name__ == "__main__":
    unittest.main()
