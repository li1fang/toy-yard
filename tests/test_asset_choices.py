from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from toyyard.asset_choices import (
    answer_membership, ask_membership, current_choice, init_choices, membership_questions, record_decision, related,
)
from toyyard.db import init_db
from toyyard.warehouse import ensure_package, ensure_sample


class AssetChoicesTest(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(TemporaryDirectory())
        self.db = self.enterContext(closing(init_db(Path(folder) / "warehouse.sqlite3")))
        init_choices(self.db)
        sample = ensure_sample(self.db, canonical_sample_id="fixture", display_name="Review fixture")
        self.parts = {key: ensure_package(self.db, sample_id=sample["id"], canonical_package_id=key)["id"]
                      for key in ("old", "new", "unknown-part")}

    def test_pending_question_becomes_a_relationship_after_confirmation(self):
        child, old, new = (self.parts[key] for key in ("unknown-part", "old", "new"))
        question = ask_membership(self.db, child, [old, new], "mount", "Which mount contains this part?", "artist")
        self.assertEqual({"old", "new"}, set(membership_questions(self.db, open_only=True)[0]["candidate_versions"]))
        self.assertEqual([], related(self.db, child))
        answer_membership(self.db, question, new, "reviewer", "Matched the socket in the reference")
        self.assertEqual([], membership_questions(self.db, open_only=True))
        self.assertEqual("new", membership_questions(self.db, question_id=question)[0]["parent_version"])
        self.assertEqual("new", related(self.db, child)[0]["canonical_package_id"])
        with self.assertRaises(ValueError):
            answer_membership(self.db, question, old, "second reviewer")

    def test_adoption_selects_one_current_version_per_purpose(self):
        record_decision(self.db, self.parts["old"], "cut", "adopt", "reviewer")
        record_decision(self.db, self.parts["new"], "match", "adopt", "reviewer")
        record_decision(self.db, self.parts["new"], "cut", "changes_requested", "reviewer")
        self.assertEqual("old", current_choice(self.db, "cut")["canonical_package_id"])
        self.assertEqual("new", current_choice(self.db, "match")["canonical_package_id"])

    def test_standalone_answer_is_recorded_without_a_parent(self):
        child = self.parts["unknown-part"]
        question = ask_membership(self.db, child, [], "", "Is this reusable?", "artist")
        answer_membership(self.db, question, None, "reviewer", "Standalone reference")
        self.assertEqual([], related(self.db, child))
        self.assertEqual([], membership_questions(self.db, open_only=True))
