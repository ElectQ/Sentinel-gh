"""Incremental event collection, with a fake GitHub client (no network).

The load-bearing case: GitHub allocates event ids from per-type sequences, so a
brand-new WatchEvent carries a *lower* id than a week-old PushEvent. The old
cursor watermarked on max(id), parked itself in the Push id range, and from then
on silently dropped every star/fork the followee made. Real sample (yj94):

    11700657431  2026-07-13T08:46:59Z  WatchEvent   <- new, but LOWER id
    14471372478  2026-07-07T08:04:35Z  PushEvent    <- old, but HIGHER id

So: never order events by id. The cursor is created_at.
"""

from __future__ import annotations

import datetime as dt

from sentinel.collectors import followees

# Ids drawn from the real, disjoint ranges seen in data/events/2026-07.jsonl:
# Watch/Fork ~1.1e10, Push/Create ~1.4e10.
PUSH_ID = "14471372478"
STAR_ID = "11700657431"
FORK_ID = "11553647477"


def _ts(hours_ago: float) -> str:
    t = dt.datetime.now(dt.UTC) - dt.timedelta(hours=hours_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _ev(eid: str, etype: str, at: str, repo: str = "acme/widget") -> dict:
    return {
        "id": eid,
        "type": etype,
        "actor": {"login": "yj94"},
        "repo": {"name": repo},
        "created_at": at,
        "payload": {},
    }


class FakeGH:
    """Serves one fixed page of events, newest first, like the real API.

    With `known_etag`, replays GitHub's conditional-request behaviour: a matching
    If-None-Match yields 304 and no body.
    """

    def __init__(self, events: list[dict], known_etag: str | None = None):
        self.events = events
        self.known_etag = known_etag
        self.conditional = []

    def get(self, path, params=None, etag=None):
        if (params or {}).get("page", 1) > 1:
            return 200, [], None
        self.conditional.append(etag)
        if etag and etag == self.known_etag:
            return 304, None, etag
        return 200, self.events, 'W/"etag-1"'


def _kinds(evs):
    return [e["type"] for e in evs]


def test_star_newer_than_push_but_lower_id_is_still_collected():
    """The regression. Cursor sits on an old push; a fresh star must get through."""
    old_push = _ev(PUSH_ID, "PushEvent", _ts(150))
    new_star = _ev(STAR_ID, "WatchEvent", _ts(2))

    gh = FakeGH([new_star, old_push])
    ustate = {
        "last_event_at": old_push["created_at"].replace("Z", "+00:00"),
        "boundary_ids": [PUSH_ID],
    }

    new = followees._new_events_for(gh, "yj94", ustate)

    assert _kinds(new) == ["WatchEvent"], "fresh star was swallowed by the id cursor"
    assert new[0]["id"] == STAR_ID
    # And the cursor must now sit on the star's timestamp, not the push's id.
    assert ustate["last_event_at"].startswith(new_star["created_at"][:19])


def test_legacy_last_event_id_state_is_migrated_not_trusted():
    """Existing state carries a poisoned max(id) cursor — it must be dropped."""
    star = _ev(STAR_ID, "WatchEvent", _ts(3))
    fork = _ev(FORK_ID, "ForkEvent", _ts(5))

    gh = FakeGH([star, fork])
    # No last_event_at: state written by the old id-based collector.
    ustate = {"last_event_id": int(PUSH_ID), "etag": 'W/"stale"'}

    new = followees._new_events_for(gh, "yj94", ustate)

    assert "last_event_id" not in ustate, "poisoned id cursor survived migration"
    # Falls back to the first-run window, so both recent events come through.
    assert sorted(_kinds(new)) == ["ForkEvent", "WatchEvent"]


def test_migration_bypasses_a_still_valid_etag():
    """A quiet followee's cached etag still matches, so a conditional request would
    304 and strand us on the poisoned id cursor forever. Migration must force a
    full fetch once."""
    star = _ev(STAR_ID, "WatchEvent", _ts(3))
    gh = FakeGH([star], known_etag='W/"stale"')
    ustate = {"last_event_id": int(PUSH_ID), "etag": 'W/"stale"'}

    new = followees._new_events_for(gh, "yj94", ustate)

    assert gh.conditional == [None], "migration sent If-None-Match and got stranded on 304"
    assert _kinds(new) == ["WatchEvent"]
    assert ustate["last_event_at"].startswith(star["created_at"][:19])


def test_etag_304_short_circuits_once_migrated():
    """Post-migration the conditional request is still used — no wasted quota."""
    star = _ev(STAR_ID, "WatchEvent", _ts(3))
    gh = FakeGH([star], known_etag='W/"etag-1"')
    ustate = {"last_event_at": _ts(1), "etag": 'W/"etag-1"'}

    assert followees._new_events_for(gh, "yj94", ustate) == []
    assert gh.conditional == ['W/"etag-1"']


def test_first_run_keeps_only_the_recent_window():
    recent = _ev(STAR_ID, "WatchEvent", _ts(2))
    ancient = _ev(FORK_ID, "ForkEvent", _ts(followees.FIRST_RUN_WINDOW_HOURS + 10))

    gh = FakeGH([recent, ancient])
    ustate: dict = {}

    new = followees._new_events_for(gh, "yj94", ustate)

    assert _kinds(new) == ["WatchEvent"]


def test_rerun_with_no_new_activity_emits_nothing():
    star = _ev(STAR_ID, "WatchEvent", _ts(2))
    gh = FakeGH([star])
    ustate: dict = {}

    first = followees._new_events_for(gh, "yj94", ustate)
    second = followees._new_events_for(FakeGH([star]), "yj94", ustate)

    assert len(first) == 1
    assert second == [], "same event re-emitted on a re-run"


def test_archive_is_idempotent_when_a_run_dies_before_saving_the_cursor(tmp_path, monkeypatch):
    """run.py archives, then persists the cursor. A run killed in between replays the
    same events next time — the archive must not grow duplicates."""
    monkeypatch.setattr(followees, "EVENTS_DIR", tmp_path)
    evs = [followees.trim(_ev(STAR_ID, "WatchEvent", _ts(2)))]

    followees.archive(evs)
    followees.archive(evs)  # crash → cursor never saved → replayed

    lines = (tmp_path / f"{evs[0]['created_at'][:7]}.jsonl").read_text().splitlines()
    assert len(lines) == 1, "crashed run duplicated events into the archive"


def test_sibling_event_on_the_watermark_second_is_not_dropped():
    """Two events share a timestamp; seeing one must not blind us to the other."""
    at = _ts(2)
    seen = _ev(STAR_ID, "WatchEvent", at, repo="acme/one")
    sibling = _ev(FORK_ID, "ForkEvent", at, repo="acme/two")

    ustate: dict = {}
    followees._new_events_for(FakeGH([seen]), "yj94", ustate)

    # Next run: the API now also surfaces the sibling from that same second.
    new = followees._new_events_for(FakeGH([sibling, seen]), "yj94", ustate)

    assert _kinds(new) == ["ForkEvent"], "same-second sibling was dropped"
    assert set(ustate["boundary_ids"]) == {STAR_ID, FORK_ID}
