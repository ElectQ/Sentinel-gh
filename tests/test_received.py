"""Dashboard-feed collector: same timestamp cursor as followees, plus contract shape."""

from __future__ import annotations

from sentinel.analyzers import bundle, feed
from sentinel.collectors import received


class FakeGH:
    def __init__(self, events, login="me"):
        self.events = events
        self.login = login

    def get(self, path, params=None, etag=None):
        if path == "/user":
            return 200, {"login": self.login}, None
        if (params or {}).get("page", 1) > 1:
            return 200, [], None
        return 200, self.events, 'W/"e1"'


def _ev(eid, etype, at, actor="stranger", repo="a/one"):
    return {
        "id": eid,
        "type": etype,
        "actor": {"login": actor},
        "repo": {"name": repo},
        "created_at": at,
    }


def test_collect_caches_login_and_advances_cursor():
    gh = FakeGH([_ev("9", "WatchEvent", "2026-07-14T06:00:00Z")])
    st: dict = {}
    new = received.collect(gh, st)
    assert st["login"] == "me"
    assert len(new) == 1
    assert st["last_event_at"] == "2026-07-14T06:00:00+00:00"
    # trimmed to the fields aggregation needs
    assert set(new[0]) == {"id", "type", "actor", "repo", "created_at"}
    assert new[0]["actor"] == "stranger"


def test_rerun_emits_nothing():
    ev = _ev("9", "WatchEvent", "2026-07-14T06:00:00Z")
    st: dict = {}
    received.collect(FakeGH([ev]), st)
    assert received.collect(FakeGH([ev]), st) == []


def test_archive_dedupes(tmp_path, monkeypatch):
    monkeypatch.setattr(received, "RECEIVED_DIR", tmp_path)
    evs = [received.trim(_ev("9", "WatchEvent", "2026-07-14T06:00:00Z"))]
    received.archive(evs)
    received.archive(evs)
    lines = (tmp_path / "2026-07.jsonl").read_text().splitlines()
    assert len(lines) == 1


def test_network_hot_survives_the_contract_whitelist():
    """feed_item_to_contract is a whitelist — the network blob must be carried through."""
    # feed.build receives archived (trimmed) events, where actor is a string.
    received_events = [
        received.trim(_ev("1", "WatchEvent", "2026-07-14T06:00:00Z", actor="s1")),
        received.trim(_ev("2", "WatchEvent", "2026-07-14T06:01:00Z", actor="s2")),
    ]
    f = feed.build(
        [], [], {"repos": {}}, followee_count=0, date="2026-07-14",
        received_events=received_events, followees=set(), my_repos=set(),
    )
    net = [i for i in f["items"] if i["kind"] == "network_hot"]
    assert len(net) == 1

    contract = bundle.feed_item_to_contract(net[0])
    assert contract["action"] == "network_hot"
    assert contract["who"] == ""  # aggregate, no single actor
    assert contract["flags"]["is_aggregate"] is True
    assert contract["network"]["outer_count"] == 2
    assert set(contract["network"]["outer_actors"]) == {"s1", "s2"}
