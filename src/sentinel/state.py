"""Incremental collection state, persisted in the repo across runs."""

import json
import os
from pathlib import Path

ROOT = Path(os.environ.get("SENTINEL_ROOT", "."))
STATE_FILE = ROOT / "state" / "followees.json"


def load() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"users": {}}


def save(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
