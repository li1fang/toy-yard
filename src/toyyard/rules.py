from __future__ import annotations

import tomllib
from pathlib import Path


def load_rules(rules_dir: Path) -> dict[str, dict]:
    generic: dict = {"signals": {}}
    games: dict[str, dict] = {}

    for path in sorted(rules_dir.glob("*.toml")):
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
        if path.stem == "generic":
            generic = payload
        else:
            games[path.stem] = payload
    return {"generic": generic, "games": games}
