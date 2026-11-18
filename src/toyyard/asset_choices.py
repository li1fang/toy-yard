"""Version relationships and explicit, scoped choices for warehouse packages."""

from toyyard.util import now_iso


SCHEMA = """
CREATE TABLE IF NOT EXISTS package_relations (
    subject_id INTEGER NOT NULL REFERENCES packages(id),
    object_id INTEGER NOT NULL REFERENCES packages(id),
    kind TEXT NOT NULL CHECK(kind IN ('derived_from', 'includes', 'option', 'supersedes')),
    slot TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    CHECK(subject_id != object_id),
    PRIMARY KEY(subject_id, object_id, kind, slot)
);
CREATE TABLE IF NOT EXISTS package_decisions (
    id INTEGER PRIMARY KEY,
    package_id INTEGER NOT NULL REFERENCES packages(id),
    purpose TEXT NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('adopt', 'changes_requested')),
    actor TEXT NOT NULL,
    note TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE VIEW IF NOT EXISTS package_current_choices AS
    SELECT d.* FROM package_decisions d
    WHERE d.action = 'adopt' AND d.id = (
        SELECT max(id) FROM package_decisions WHERE purpose = d.purpose AND action = 'adopt'
    );
CREATE TABLE IF NOT EXISTS package_membership_questions (
    id INTEGER PRIMARY KEY,
    child_id INTEGER NOT NULL REFERENCES packages(id),
    slot TEXT NOT NULL,
    question TEXT NOT NULL,
    asked_by TEXT NOT NULL,
    asked_at TEXT NOT NULL,
    parent_id INTEGER REFERENCES packages(id),
    answer TEXT,
    answered_by TEXT,
    answered_at TEXT,
    CHECK(child_id <> parent_id)
);
CREATE TABLE IF NOT EXISTS package_membership_candidates (
    question_id INTEGER NOT NULL REFERENCES package_membership_questions(id),
    parent_id INTEGER NOT NULL REFERENCES packages(id),
    PRIMARY KEY(question_id, parent_id)
);
"""


def init_choices(db):
    db.executescript(SCHEMA)


def relate(db, subject_id, object_id, kind, *, slot="", note=""):
    with db:
        _write_relation(db, subject_id, object_id, kind, slot, note)


def _write_relation(db, subject_id, object_id, kind, slot, note):
    db.execute("""INSERT INTO package_relations VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(subject_id, object_id, kind, slot) DO UPDATE SET note = excluded.note""",
               (subject_id, object_id, kind, slot, note))


def related(db, package_id):
    return db.execute("""SELECT r.kind, r.slot, r.note,
        r.subject_id = ? AS outgoing, p.canonical_package_id, p.metadata_json
        FROM package_relations r JOIN packages p ON p.id =
            CASE WHEN r.subject_id = ? THEN r.object_id ELSE r.subject_id END
        WHERE r.subject_id = ? OR r.object_id = ?
        ORDER BY r.kind, r.slot, p.canonical_package_id""", (package_id,) * 4).fetchall()


def record_decision(db, package_id, purpose, action, actor, note=""):
    if not purpose.strip() or not actor.strip():
        raise ValueError("Purpose and actor are required")
    with db:
        return db.execute("""INSERT INTO package_decisions
            (package_id, purpose, action, actor, note, recorded_at) VALUES (?, ?, ?, ?, ?, ?)""",
                          (package_id, purpose, action, actor, note, now_iso())).lastrowid


def current_choice(db, purpose):
    return db.execute("""SELECT d.*, p.canonical_package_id FROM package_current_choices d
        JOIN packages p ON p.id = d.package_id WHERE d.purpose = ?""", (purpose,)).fetchone()


def decision_history(db, package_id):
    return db.execute("SELECT * FROM package_decisions WHERE package_id = ? ORDER BY id DESC",
                      (package_id,)).fetchall()


def ask_membership(db, child_id, candidates, slot, question, actor):
    candidates = set(candidates)
    question, actor = question.strip(), actor.strip()
    if not question or not actor or child_id in candidates:
        raise ValueError("Provide a question and reviewer. A package cannot contain itself.")
    with db:
        question_id = db.execute("""INSERT INTO package_membership_questions
            (child_id, slot, question, asked_by, asked_at) VALUES (?, ?, ?, ?, ?)""",
                                 (child_id, slot, question, actor, now_iso())).lastrowid
        db.executemany("INSERT INTO package_membership_candidates VALUES (?, ?)",
                       ((question_id, parent_id) for parent_id in candidates))
    return question_id


def membership_questions(db, *, open_only=False, question_id=None):
    rows = db.execute("""SELECT q.*, c.canonical_package_id AS child_version, p.canonical_package_id AS parent_version
        FROM package_membership_questions q JOIN packages c ON q.child_id = c.id
        LEFT JOIN packages p ON q.parent_id = p.id
        WHERE (? = 0 OR q.answered_at IS NULL) AND (? IS NULL OR q.id = ?) ORDER BY q.id DESC""",
                      (open_only, question_id, question_id)).fetchall()
    return [dict(row) | {"candidate_versions": [candidate[0] for candidate in db.execute("""SELECT p.canonical_package_id
        FROM package_membership_candidates c JOIN packages p ON c.parent_id = p.id
        WHERE c.question_id = ? ORDER BY p.canonical_package_id""", (row["id"],))]} for row in rows]


def answer_membership(db, question_id, parent_id, actor, note=""):
    if not actor.strip():
        raise ValueError("Reviewer is required.")
    if parent_id is None and not note.strip():
        raise ValueError("Explain why the component is standalone.")
    # The row update and the confirmed edge are one decision, even when two reviewers submit at once.
    with db:
        row = db.execute("""UPDATE package_membership_questions
            SET parent_id=?, answer=?, answered_by=?, answered_at=?
            WHERE id=? AND answered_at IS NULL AND (? IS NULL OR child_id<>?)
            RETURNING child_id, slot""", (parent_id, note, actor, now_iso(), question_id, parent_id, parent_id)).fetchone()
        if row is None:
            raise ValueError("Question already answered, missing, or selected itself.")
        if parent_id is not None:
            _write_relation(db, parent_id, row["child_id"], "includes", row["slot"], note)
