"""Collect the viewer's dashboard feed — /users/{me}/received_events.

This is what you see on the GitHub home page: your followees' activity *plus*
network-recommended activity (people you don't follow, converging on repos your
circle cares about). The followee slice is already covered by collectors.followees;
the value here is the *outer* slice — repos the wider crowd is starring/forking/
discussing that haven't hit your circle yet.

Same timestamp cursor as followees (created_at, never event id — ids are not
comparable across event types). One caveat the followee path doesn't have: this
feed only retains ~300 events, so on a busy account a once-a-day pull covers far
less than 24h. We archive what we can and note the window; it is a best-effort
signal, not a complete timeline.
"""

import datetime as dt
import json

from ..gh import GitHub
from ..state import ROOT
from ..util import beijing_day

RECEIVED_DIR = ROOT / "data" / "received"
FIRST_RUN_WINDOW_HOURS = 24
MAX_EVENT_PAGES = 3  # API hard cap: 300 events


def _at(e: dict) -> str:
    return e["created_at"].replace("Z", "+00:00")


def viewer_login(gh: GitHub, state: dict) -> str | None:
    """The authenticated user's login, cached in state (GET /user once)."""
    if state.get("login"):
        return state["login"]
    try:
        _, data, _ = gh.get("/user")
    except Exception:
        return None
    login = (data or {}).get("login")
    if login:
        state["login"] = login
    return login


def trim(e: dict) -> dict:
    """The feed is only used for repo-level aggregation — keep the minimum."""
    return {
        "id": e["id"],
        "type": e["type"],
        "actor": e["actor"]["login"],
        "repo": e["repo"]["name"],
        "created_at": e["created_at"],
    }


def collect(gh: GitHub, state: dict) -> list[dict]:
    """Pull new dashboard events since the cursor. Mutates state in place."""
    login = viewer_login(gh, state)
    if not login:
        return []

    last_at = state.get("last_event_at") or ""
    boundary = set(state.get("boundary_ids") or [])
    etag = state.get("etag")

    status, data, etag = gh.get(
        f"/users/{login}/received_events", {"per_page": 100}, etag=etag
    )
    if status == 304 or not data:
        state["etag"] = etag
        return []

    events = list(data)
    page = 2
    while last_at and events and _at(events[-1]) > last_at and page <= MAX_EVENT_PAGES:
        _, more, _ = gh.get(
            f"/users/{login}/received_events", {"per_page": 100, "page": page}
        )
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

    state["etag"] = etag
    newest = max(_at(e) for e in events)
    if newest > last_at:
        state["last_event_at"] = newest
        state["boundary_ids"] = sorted(str(e["id"]) for e in events if _at(e) == newest)
    elif newest == last_at:
        state["boundary_ids"] = sorted(
            boundary | {str(e["id"]) for e in events if _at(e) == newest}
        )
    return [trim(e) for e in new]


def _archived_ids(path) -> set[str]:
    if not path.exists():
        return set()
    with open(path) as f:
        return {str(json.loads(line)["id"]) for line in f if line.strip()}


def archive(events: list[dict]) -> None:
    """Append to data/received/YYYY-MM.jsonl, grouped by event month; dedupe by id."""
    if not events:
        return
    RECEIVED_DIR.mkdir(parents=True, exist_ok=True)
    by_month: dict[str, list[dict]] = {}
    for ev in sorted(events, key=lambda e: e["created_at"]):
        by_month.setdefault(ev["created_at"][:7], []).append(ev)
    for month, evs in by_month.items():
        path = RECEIVED_DIR / f"{month}.jsonl"
        seen = _archived_ids(path)
        with open(path, "a") as f:
            for ev in evs:
                if str(ev["id"]) in seen:
                    continue
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
                seen.add(str(ev["id"]))


def received_on(bj_date: str) -> list[dict]:
    """Archived dashboard events whose created_at falls on the given Beijing day."""
    day = dt.date.fromisoformat(bj_date)
    months = {(day - dt.timedelta(days=1)).strftime("%Y-%m"), day.strftime("%Y-%m")}
    by_id: dict[str, dict] = {}
    for month in months:
        path = RECEIVED_DIR / f"{month}.jsonl"
        if not path.exists():
            continue
        with open(path) as f:
            for line in f:
                if not line.strip():
                    continue
                ev = json.loads(line)
                if beijing_day(ev["created_at"]) == bj_date:
                    by_id[str(ev["id"])] = ev
    return list(by_id.values())


if __name__ == "__main__":
    from .. import state as state_mod

    gh = GitHub()
    st = state_mod.load_received()
    events = collect(gh, st)
    archive(events)
    state_mod.save_received(st)
    print(f"received: login={st.get('login')} new={len(events)}")
