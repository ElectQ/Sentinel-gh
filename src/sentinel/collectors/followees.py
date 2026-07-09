"""Collect followees' public events incrementally.

Followee list comes from /user/following (needs a PAT). Set GH_USER to use the
public /users/{user}/following endpoint instead (works unauthenticated, handy
for local testing).

The events API keeps ~90 days / 300 events per user, so a daily pull never
loses data. On the first run for a user only the last FIRST_RUN_WINDOW_HOURS
of events are archived, to avoid flooding the archive with weeks of history.
"""

import datetime as dt
import json
import os
from collections import Counter

from ..gh import GitHub
from ..state import ROOT

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
    return {
        "id": e["id"],
        "type": t,
        "actor": e["actor"]["login"],
        "repo": e["repo"]["name"],
        "created_at": e["created_at"],
        "payload": keep,
    }


def _new_events_for(gh: GitHub, login: str, ustate: dict) -> list[dict]:
    last_id = int(ustate.get("last_event_id", 0))
    status, data, etag = gh.get(
        f"/users/{login}/events/public", {"per_page": 100}, etag=ustate.get("etag")
    )
    if status == 304 or not data:
        return []

    events = list(data)
    # Rare: >100 new events since last run — page until we pass last_id.
    page = 2
    while last_id and events and int(events[-1]["id"]) > last_id and page <= MAX_EVENT_PAGES:
        _, more, _ = gh.get(f"/users/{login}/events/public", {"per_page": 100, "page": page})
        if not more:
            break
        events.extend(more)
        page += 1

    if last_id:
        new = [e for e in events if int(e["id"]) > last_id]
    else:
        cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(hours=FIRST_RUN_WINDOW_HOURS)
        new = [
            e for e in events
            if dt.datetime.fromisoformat(e["created_at"].replace("Z", "+00:00")) >= cutoff
        ]

    ustate["etag"] = etag
    if events:
        ustate["last_event_id"] = max(int(e["id"]) for e in events[:100])
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


def archive(events: list[dict]) -> None:
    """Append events to data/events/YYYY-MM.jsonl, grouped by event month."""
    EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    by_month: dict[str, list[dict]] = {}
    for ev in sorted(events, key=lambda e: int(e["id"])):
        by_month.setdefault(ev["created_at"][:7], []).append(ev)
    for month, evs in by_month.items():
        with open(EVENTS_DIR / f"{month}.jsonl", "a") as f:
            for ev in evs:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")


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
