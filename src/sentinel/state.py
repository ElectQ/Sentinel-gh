"""Incremental collection state, persisted in the repo across runs."""

import json
import os
from pathlib import Path

ROOT = Path(os.environ.get("SENTINEL_ROOT", "."))
STATE_DIR = ROOT / "state"
FOLLOWEES_FILE = STATE_DIR / "followees.json"
FOLLOWING_FILE = STATE_DIR / "following.json"


def load() -> dict:
    """Backward-compatible alias for followees state."""
    return load_followees()


def save(state: dict) -> None:
    """Backward-compatible alias for followees state."""
    save_followees(state)


def load_followees() -> dict:
    if FOLLOWEES_FILE.exists():
        return json.loads(FOLLOWEES_FILE.read_text())
    return {"users": {}}


def save_followees(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    FOLLOWEES_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def load_following() -> dict:
    """Per-followee following-list snapshots: {login: {etag, logins, ...}}."""
    if FOLLOWING_FILE.exists():
        return json.loads(FOLLOWING_FILE.read_text())
    return {}


def save_following(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    FOLLOWING_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
