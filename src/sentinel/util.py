"""Small shared helpers."""

import datetime as dt
import os

CST = dt.timezone(dt.timedelta(hours=8))


def beijing_day(ts: str) -> str:
    """Beijing calendar date (YYYY-MM-DD) of an ISO-8601 timestamp.

    The contract day. Bundles are keyed by when an event *happened*, not by when
    we happened to collect it, so a re-run or a backfill can never shuffle an
    event into a different day.
    """
    return (
        dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        .astimezone(CST)
        .strftime("%Y-%m-%d")
    )


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)
