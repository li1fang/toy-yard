"""CLI for version choices and unresolved component membership."""

from contextlib import closing
import json

from toyyard.asset_choices import (
    answer_membership, ask_membership, current_choice, init_choices, membership_questions, record_decision,
)
from toyyard.db import init_db


def add_review_commands(subparsers):
    review = subparsers.add_parser("review", help="Resolve package membership and select versions by purpose")
    actions = review.add_subparsers(dest="review_command", required=True)
    questions = actions.add_parser("questions", help="List component membership questions")
    questions.add_argument("--open", action="store_true")
    ask = actions.add_parser("ask", help="Ask which parent version contains a component")
    ask.add_argument("component")
    ask.add_argument("question")
    ask.add_argument("--candidate", action="append", default=[])
    ask.add_argument("--slot", default="")
    ask.add_argument("--actor", required=True)
    answer = actions.add_parser("answer", help="Confirm a component's parent version")
    answer.add_argument("id", type=int)
    answer.add_argument("parent", help="Canonical parent package, or '-' for a standalone component")
    answer.add_argument("--note", default="")
    answer.add_argument("--actor", required=True)
    choose = actions.add_parser("choose", help="Choose a package version for a purpose")
    choose.add_argument("purpose")
    choose.add_argument("version")
    choose.add_argument("--note", default="")
    choose.add_argument("--actor", required=True)
    current = actions.add_parser("current", help="Read the current package for a purpose")
    current.add_argument("purpose")


def package_id(db, name):
    row = db.execute("SELECT id FROM packages WHERE canonical_package_id = ?", (name,)).fetchone()
    if row is None:
        raise ValueError(f"Unknown canonical package: {name}")
    return row[0]


def run_review(db_path, args):
    try:
        with closing(init_db(db_path)) as db:
            init_choices(db)
            if args.review_command == "questions":
                result = membership_questions(db, open_only=args.open)
            elif args.review_command == "ask":
                candidates = [package_id(db, key) for key in args.candidate]
                result = {"id": ask_membership(db, package_id(db, args.component), candidates,
                                               args.slot, args.question, args.actor)}
            elif args.review_command == "answer":
                parent = package_id(db, args.parent) if args.parent != "-" else None
                answer_membership(db, args.id, parent, args.actor, args.note)
                result = {"id": args.id, "parent": args.parent if parent else None}
            elif args.review_command == "choose":
                result = {"id": record_decision(db, package_id(db, args.version), args.purpose,
                                                 "adopt", args.actor, args.note)}
            else:
                choice = current_choice(db, args.purpose)
                result = dict(choice) if choice else None
    except ValueError as exc:
        print(str(exc))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0
