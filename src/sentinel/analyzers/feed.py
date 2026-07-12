"""Build the daily high-signal feed (star / follow / created / fork / release / …)."""

from __future__ import annotations

import datetime as dt
import json
import os
from collections import Counter

from ..state import ROOT
from ..util import env_bool
from . import project

FEED_DIR = ROOT / "data" / "feed"
SCHEMA_VERSION = 1

DEFAULT_KINDS = ("star", "fork", "follow", "release", "created")

# Lower number = higher priority when timestamps tie
KIND_PRIORITY = {
    "release": 0,
    "created": 1,
    "new_repo": 1,
    "star": 2,
    "fork": 3,
    "follow": 4,
    "public_repo": 5,
    "unfollow": 6,
    "member": 7,
}


def _kinds() -> list[str]:
    raw = os.environ.get("FEED_KINDS", ",".join(DEFAULT_KINDS))
    return [k.strip() for k in raw.split(",") if k.strip()]


def _normalize_kinds(kinds: list[str]) -> list[str]:
    """Map new_repo → created; preserve order; unique."""
    seen: list[str] = []
    for k in kinds:
        if k == "new_repo":
            k = "created"
        if k not in seen:
            seen.append(k)
    return seen


def _publish_follow() -> bool:
    return env_bool("PUBLISH_FOLLOW_EDGES", True)


def _sort_items(items: list[dict]) -> list[dict]:
    """created_at DESC, then kind priority ASC, then id ASC."""
    return sorted(
        items,
        key=lambda it: (
            # Invert ISO timestamps for descending order via string sort of padded inverse
            # Simpler: sort ascending with reverse time key using max-string trick
            it.get("created_at") or "",
            -KIND_PRIORITY.get(it["kind"], 99),
            it["id"],
        ),
        reverse=True,
    )
    # Note: reverse=True also reverses kind priority (because of the - sign, reverse makes
    # higher priority numbers first after negation — wait:
    # KIND star=2 fork=3; -2 > -3, reverse=True puts -2 before -3? 
    # reverse sorts larger first: -2 > -3 so star before fork. Good.
    # For same time: we want lower KIND_PRIORITY first (release before star).
    # release prio 0 → -0=0; star prio 2 → -2.
    # reverse=True: larger first → 0 then -2? 0 > -2, so release first. Good.


def build(
    day_events: list[dict],
    day_follows: list[dict],
    trending: dict,
    followee_count: int,
    *,
    date: str | None = None,
) -> dict:
    now = dt.datetime.now(dt.UTC)
    day = date or now.date().isoformat()
    kinds = _normalize_kinds(_kinds())
    kind_set = set(kinds)
    trepos = trending.get("repos") or {}
    circle = project.star_circle_counts(day_events)

    raw_items: list[dict] = []
    if "star" in kind_set:
        raw_items.extend(project.iter_stars(day_events))
    if "fork" in kind_set:
        raw_items.extend(project.iter_forks(day_events))
    if "release" in kind_set:
        raw_items.extend(project.iter_releases(day_events))
    if "created" in kind_set:
        raw_items.extend(project.iter_created(day_events))
    if "public_repo" in kind_set:
        raw_items.extend(project.iter_public_repos(day_events))
    if "follow" in kind_set and _publish_follow():
        raw_items.extend(project.iter_follows(day_follows, actions={"followed"}))
    if "unfollow" in kind_set and _publish_follow():
        raw_items.extend(project.iter_follows(day_follows, actions={"unfollowed"}))

    by_id: dict[str, dict] = {}
    for item in raw_items:
        project.attach_repo_signals(item, trepos=trepos, circle=circle)
        item.pop("source_event_id", None)
        by_id[item["id"]] = item

    items = _sort_items(list(by_id.values()))
    by_kind = dict(Counter(i["kind"] for i in items))

    return {
        "schema_version": SCHEMA_VERSION,
        "date": day,
        "generated_at": now.isoformat(timespec="seconds"),
        "followee_count": followee_count,
        "item_count": len(items),
        "kinds_included": kinds,
        "trending_available": bool(trending.get("available")),
        "trending_source_date": trending.get("date"),
        "items": items,
        "summary": {
            "by_kind": by_kind,
            "push_events_excluded": project.count_push_excluded(day_events),
            "events_in_window": len(day_events),
            "follows_in_window": len(day_follows),
        },
    }


def write(feed: dict) -> None:
    FEED_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(feed, ensure_ascii=False, indent=2) + "\n"
    (FEED_DIR / f"{feed['date']}.json").write_text(payload)
    (FEED_DIR / "latest.json").write_text(payload)


if __name__ == "__main__":
    from ..collectors import followees, following

    today = dt.datetime.now(dt.UTC).date().isoformat()
    date = os.environ.get("FEED_DATE", today)
    day_events = followees.collected_on(date)
    day_follows = following.collected_on(date)
    actors = {e["actor"] for e in day_events}
    # Prefer state followee count when available
    from .. import state as state_mod

    st = state_mod.load_followees()
    n = len(st.get("users") or {}) or len(actors)
    trend = {"available": False, "date": None, "repos": {}}
    feed = build(day_events, day_follows, trend, followee_count=n, date=date)
    write(feed)
    print(f"feed {date}: items={feed['item_count']} by_kind={feed['summary']['by_kind']}")
