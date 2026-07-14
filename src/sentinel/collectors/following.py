"""Snapshot each followee's following list and emit day-level follow diffs.

GitHub removed FollowEvent from the public timeline, so "A newly followed B"
is recovered only by set-diff of GET /users/{A}/following across runs.

K13: emit diffs only when both previous and current snapshots are complete
(full↔full). Truncation on either side → zero emits (still update state).
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

from ..gh import GitHub
from ..state import ROOT
from ..util import beijing_day, env_bool, env_int

FOLLOWS_DIR = ROOT / "data" / "follows"
DEFAULT_MAX_PAGES = 10


def _max_pages() -> int:
    return env_int("FOLLOWING_MAX_PAGES", DEFAULT_MAX_PAGES)


def _publish_edges() -> bool:
    return env_bool("PUBLISH_FOLLOW_EDGES", True)


def _only_active() -> bool:
    return env_bool("FOLLOWING_ONLY_ACTIVE", False)


def _diff_id(actor: str, target: str, date: str, action: str) -> str:
    return f"follow:{actor}:{target}:{date}:{action}"


def diff_following(
    prev: dict | None,
    set_now: set[str],
    truncated_now: bool,
    *,
    actor: str,
    date: str,
    observed_at: str,
) -> list[dict]:
    """Return edge diffs to archive. Empty unless full↔full after baseline."""
    if prev is None or "baselined_at" not in prev:
        return []
    truncated_prev = bool(prev.get("truncated"))
    if truncated_prev or truncated_now:
        return []
    set_prev = set(prev.get("logins") or [])
    diffs: list[dict] = []
    for target in sorted(set_now - set_prev):
        diffs.append(
            {
                "id": _diff_id(actor, target, date, "followed"),
                "actor": actor,
                "target": target,
                "action": "followed",
                "observed_at": observed_at,
                "incomplete_context": False,
            }
        )
    for target in sorted(set_prev - set_now):
        diffs.append(
            {
                "id": _diff_id(actor, target, date, "unfollowed"),
                "actor": actor,
                "target": target,
                "action": "unfollowed",
                "observed_at": observed_at,
                "incomplete_context": False,
            }
        )
    return diffs


def _write_snapshot(
    ustate: dict,
    logins: list[str],
    etag: str | None,
    truncated: bool,
    observed_at: str,
    *,
    set_baseline: bool,
) -> None:
    ustate["logins"] = sorted(logins)
    ustate["count"] = len(logins)
    ustate["etag"] = etag
    ustate["truncated"] = truncated
    ustate["updated_at"] = observed_at
    if set_baseline and "baselined_at" not in ustate:
        ustate["baselined_at"] = observed_at


def collect(gh: GitHub, state: dict, followees: list[str], *, active: set[str] | None = None) -> list[dict]:
    """Pull following lists, mutate state, return this-run diffs (not yet archived).

    `state` is a dict keyed by followee login. Prunes users no longer followed.
    """
    if not env_bool("FOLLOWING_ENABLED", True):
        return []

    max_pages = _max_pages()
    observed_at = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    date = observed_at[:10]
    new_diffs: list[dict] = []
    targets = followees
    if _only_active() and active is not None:
        targets = [u for u in followees if u in active]

    for login in targets:
        prev = state.get(login)
        status, items, etag, truncated = gh.get_list_with_etag(
            f"/users/{login}/following",
            etag=(prev or {}).get("etag"),
            max_pages=max_pages,
        )
        if status == "not_modified":
            continue
        if status == "error" or items is None:
            print(f"following: soft-skip {login} (fetch error)")
            continue

        logins = [u["login"] for u in items if isinstance(u, dict) and u.get("login")]
        set_now = set(logins)
        baselined = prev is not None and "baselined_at" in prev

        if not baselined:
            _write_snapshot(
                state.setdefault(login, {}),
                logins,
                etag,
                truncated,
                observed_at,
                set_baseline=True,
            )
            continue

        diffs = diff_following(
            prev, set_now, truncated, actor=login, date=date, observed_at=observed_at
        )
        new_diffs.extend(diffs)
        _write_snapshot(
            state.setdefault(login, {}),
            logins,
            etag,
            truncated,
            observed_at,
            set_baseline=False,
        )

    # Prune people no longer followed.
    keep = set(followees)
    for gone in [u for u in state if u not in keep]:
        del state[gone]

    return new_diffs


def _existing_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    ids: set[str] = set()
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ids.add(json.loads(line)["id"])
            except (json.JSONDecodeError, KeyError):
                continue
    return ids


def archive(diffs: list[dict]) -> None:
    """Append diffs to data/follows/YYYY-MM.jsonl; skip ids already present."""
    if not diffs:
        return
    if not _publish_edges() and not env_bool("FOLLOWING_ARCHIVE_ALWAYS", False):
        # P-C default publishes; kill-switch can skip product archive.
        # Still allow archiving when explicitly forced.
        pass
    # Under P-C we always archive when edges are computed (needed for collected_on).
    # PUBLISH_FOLLOW_EDGES only gates product surfaces (feed/pulse), not state/archive
    # recovery — except when 0, still archive so same-day replay works if re-enabled mid-day.
    FOLLOWS_DIR.mkdir(parents=True, exist_ok=True)
    collected_at = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    by_month: dict[str, list[dict]] = {}
    for d in diffs:
        rec = dict(d)
        rec.setdefault("_collected_at", collected_at)
        by_month.setdefault(rec["observed_at"][:7], []).append(rec)

    for month, recs in by_month.items():
        path = FOLLOWS_DIR / f"{month}.jsonl"
        existing = _existing_ids(path)
        with open(path, "a") as f:
            for rec in sorted(recs, key=lambda r: r["id"]):
                if rec["id"] in existing:
                    continue
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                existing.add(rec["id"])


def follows_on(bj_date: str) -> list[dict]:
    """Follow records observed on the given Beijing calendar day; dedupe by id.

    Follow edges have no event timestamp — `observed_at` is the daily-diff window
    they were spotted in, which is the best "when it happened" we have.
    """
    day = dt.date.fromisoformat(bj_date)
    months = {(day - dt.timedelta(days=1)).strftime("%Y-%m"), day.strftime("%Y-%m")}
    by_id: dict[str, dict] = {}
    for month in months:
        path = FOLLOWS_DIR / f"{month}.jsonl"
        if not path.exists():
            continue
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                if beijing_day(rec["observed_at"]) == bj_date:
                    by_id[rec["id"]] = rec
    return list(by_id.values())


def collected_on(date: str) -> list[dict]:
    """All follow records with _collected_at on date; dedupe by id."""
    month_files = {FOLLOWS_DIR / f"{date[:7]}.jsonl"}
    prev = dt.date.fromisoformat(date).replace(day=1) - dt.timedelta(days=1)
    month_files.add(FOLLOWS_DIR / f"{prev.isoformat()[:7]}.jsonl")
    by_id: dict[str, dict] = {}
    for path in month_files:
        if not path.exists():
            continue
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                if rec.get("_collected_at", "")[:10] != date:
                    continue
                by_id[rec["id"]] = rec
    return list(by_id.values())


def truncated_actors(state: dict) -> list[str]:
    return sorted(u for u, s in state.items() if s.get("truncated"))


if __name__ == "__main__":
    from .. import state as state_mod
    from . import followees

    gh = GitHub()
    st_users = state_mod.load_followees()
    st_following = state_mod.load_following()
    logins = followees.get_followees(gh)
    diffs = collect(gh, st_following, logins)
    archive(diffs)
    state_mod.save_following(st_following)
    print(f"followees={len(logins)} this_run_diffs={len(diffs)}")
    for d in diffs[:20]:
        print(f"  {d['actor']} {d['action']} {d['target']}")
