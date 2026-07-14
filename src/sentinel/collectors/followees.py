"""Collect followees' public events incrementally.

Followee list comes from /user/following (needs a PAT). Set GH_USER to use the
public /users/{user}/following endpoint instead (works unauthenticated, handy
for local testing).

The events API keeps ~90 days / 300 events per user, so a daily pull never
loses data. On the first run for a user only the last FIRST_RUN_WINDOW_HOURS
of events are archived, to avoid flooding the archive with weeks of history.

Incremental cursor is `created_at`, never the event id: GitHub allocates event
ids from per-type sequences, so a fresh WatchEvent (~1.1e10) can have a *lower*
id than a week-old PushEvent (~1.4e10). Watermarking on max(id) parked the
cursor in the Push range and silently swallowed every star/fork thereafter.
"""

import datetime as dt
import json
import os
from collections import Counter

from ..gh import GitHub
from ..state import ROOT
from ..util import beijing_day

EVENTS_DIR = ROOT / "data" / "events"
FIRST_RUN_WINDOW_HOURS = 24
MAX_EVENT_PAGES = 3  # API hard cap: 300 events


def get_followees(gh: GitHub) -> list[str]:
    user = os.environ.get("GH_USER")
    if user:
        users = gh.paginate(f"/users/{user}/following")
    else:
        users = gh.paginate("/user/following")
    return [u["login"] for u in users]


def trim(e: dict) -> dict:
    """Keep only the fields downstream analysis needs; raw payloads are huge."""
    t = e["type"]
    p = e.get("payload", {})
    keep: dict = {}
    if t == "ReleaseEvent":
        rel = p.get("release") or {}
        keep = {
            "action": p.get("action"),
            "tag": rel.get("tag_name"),
            "name": rel.get("name"),
            "url": rel.get("html_url"),
            "prerelease": rel.get("prerelease"),
        }
    elif t == "CreateEvent":
        keep = {"ref_type": p.get("ref_type"), "ref": p.get("ref")}
    elif t == "ForkEvent":
        keep = {"forkee": (p.get("forkee") or {}).get("full_name")}
    elif t == "PushEvent":
        keep = {"ref": p.get("ref"), "size": p.get("size")}
    elif t in ("IssuesEvent", "PullRequestEvent"):
        obj = p.get("issue") or p.get("pull_request") or {}
        keep = {"action": p.get("action"), "title": obj.get("title"), "url": obj.get("html_url")}
    elif t == "PublicEvent":
        keep = {}
    elif t == "MemberEvent":
        keep = {"action": p.get("action")}
    return {
        "id": e["id"],
        "type": t,
        "actor": e["actor"]["login"],
        "repo": e["repo"]["name"],
        "created_at": e["created_at"],
        "payload": keep,
    }


def _at(e: dict) -> str:
    """Event timestamp, normalized so plain string compare is a time compare."""
    return e["created_at"].replace("Z", "+00:00")


def _new_events_for(gh: GitHub, login: str, ustate: dict) -> list[dict]:
    last_at = ustate.get("last_event_at") or ""
    # Ids of the events sitting exactly on the watermark second, so a re-run
    # neither re-emits them nor drops a sibling that shares their timestamp.
    boundary = set(ustate.get("boundary_ids") or [])

    # Migrating off the old id cursor: the cached etag is still valid, so a
    # conditional request would 304 and strand us on the poisoned state forever.
    # Force a full fetch once to re-baseline.
    etag = None if (not last_at and "last_event_id" in ustate) else ustate.get("etag")

    status, data, etag = gh.get(f"/users/{login}/events/public", {"per_page": 100}, etag=etag)
    if status == 304 or not data:
        return []

    events = list(data)
    # Rare: >100 new events since last run — page until we're past the watermark.
    page = 2
    while last_at and events and _at(events[-1]) > last_at and page <= MAX_EVENT_PAGES:
        _, more, _ = gh.get(f"/users/{login}/events/public", {"per_page": 100, "page": page})
        if not more:
            break
        events.extend(more)
        page += 1

    if last_at:
        new = [
            e for e in events
            if _at(e) > last_at or (_at(e) == last_at and str(e["id"]) not in boundary)
        ]
    else:
        cutoff = (
            dt.datetime.now(dt.UTC) - dt.timedelta(hours=FIRST_RUN_WINDOW_HOURS)
        ).isoformat()
        new = [e for e in events if _at(e) >= cutoff]

    ustate["etag"] = etag
    ustate.pop("last_event_id", None)  # poisoned by the old max(id) cursor

    newest = max(_at(e) for e in events)
    if newest > last_at:
        ustate["last_event_at"] = newest
        ustate["boundary_ids"] = sorted(str(e["id"]) for e in events if _at(e) == newest)
    elif newest == last_at:
        ustate["boundary_ids"] = sorted(
            boundary | {str(e["id"]) for e in events if _at(e) == newest}
        )
    return [trim(e) for e in new]


def collect(gh: GitHub, state: dict) -> tuple[list[str], list[dict]]:
    """Pull new events for every followee. Mutates state in place."""
    followees = get_followees(gh)
    collected_at = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    new_events: list[dict] = []
    for login in followees:
        ustate = state["users"].setdefault(login, {})
        for ev in _new_events_for(gh, login, ustate):
            ev["_collected_at"] = collected_at
            new_events.append(ev)
    # Drop state for people no longer followed.
    state["users"] = {u: s for u, s in state["users"].items() if u in set(followees)}
    return followees, new_events


def events_on(bj_date: str) -> list[dict]:
    """Archived events whose `created_at` falls on the given Beijing calendar day.

    This is what bundles are built from. Keyed on event time, not collection
    time, so backfilling a missed event lands it in the day it actually belongs
    to instead of the day we noticed it.
    """
    day = dt.date.fromisoformat(bj_date)
    # A Beijing day straddles two UTC days, hence possibly two month files.
    months = {(day - dt.timedelta(days=1)).strftime("%Y-%m"), day.strftime("%Y-%m")}
    by_id: dict[str, dict] = {}
    for month in months:
        path = EVENTS_DIR / f"{month}.jsonl"
        if not path.exists():
            continue
        with open(path) as f:
            for line in f:
                ev = json.loads(line)
                if beijing_day(ev["created_at"]) == bj_date:
                    by_id[str(ev["id"])] = ev  # archive may hold re-collected dupes
    return list(by_id.values())


def collected_on(date: str) -> list[dict]:
    """All archived events collected on the given UTC date (YYYY-MM-DD).

    Same-day re-runs rebuild the pulse from these, so a manual re-dispatch
    aggregates instead of clobbering the day's digest.
    """
    month_files = {EVENTS_DIR / f"{date[:7]}.jsonl"}
    prev = dt.date.fromisoformat(date).replace(day=1) - dt.timedelta(days=1)
    month_files.add(EVENTS_DIR / f"{prev.isoformat()[:7]}.jsonl")
    events = []
    for path in month_files:
        if not path.exists():
            continue
        with open(path) as f:
            for line in f:
                ev = json.loads(line)
                if ev.get("_collected_at", "")[:10] == date:
                    events.append(ev)
    return events


def _archived_ids(path) -> set[str]:
    if not path.exists():
        return set()
    with open(path) as f:
        return {str(json.loads(line)["id"]) for line in f if line.strip()}


def archive(events: list[dict]) -> None:
    """Append events to data/events/YYYY-MM.jsonl, grouped by event month.

    Skips ids already on disk. We archive before the cursor is persisted, so a
    run killed in between (Actions timeout, network blip) is replayed on the next
    run — without this the same events would be appended twice.
    """
    EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    by_month: dict[str, list[dict]] = {}
    for ev in sorted(events, key=lambda e: e["created_at"]):
        by_month.setdefault(ev["created_at"][:7], []).append(ev)
    for month, evs in by_month.items():
        path = EVENTS_DIR / f"{month}.jsonl"
        seen = _archived_ids(path)
        with open(path, "a") as f:
            for ev in evs:
                if str(ev["id"]) in seen:
                    continue
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
                seen.add(str(ev["id"]))


if __name__ == "__main__":
    from .. import state as state_mod

    gh = GitHub()
    st = state_mod.load()
    followees, events = collect(gh, st)
    archive(events)
    state_mod.save(st)
    print(f"followees: {len(followees)}, new events: {len(events)}")
    for t, n in Counter(e["type"] for e in events).most_common():
        print(f"  {t}: {n}")
