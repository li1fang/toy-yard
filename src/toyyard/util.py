from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def slugify(value: str) -> str:
    text = value.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")
    return text or "package"


def normalize_ext(path: str | Path) -> str:
    return Path(path).suffix.lower()


def json_dumps(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True)


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json_dumps(payload) + "\n", encoding="utf-8")


def print_rows(rows: Iterable[dict[str, object]]) -> None:
    rows = list(rows)
    if not rows:
        print("No rows.")
        return
    keys = list(rows[0].keys())
    widths = {
        key: max(len(str(key)), *(len(str(row.get(key, ""))) for row in rows))
        for key in keys
    }
    print(" | ".join(f"{key:{widths[key]}}" for key in keys))
    print("-+-".join("-" * widths[key] for key in keys))
    for row in rows:
        print(" | ".join(f"{str(row.get(key, '')):{widths[key]}}" for key in keys))
