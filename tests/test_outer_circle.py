"""Outer-circle heat from the dashboard feed.

The value of received_events is the *outer* slice: repos the wider crowd (people
you don't follow) is converging on. The inner circle is already covered by the
followee collector, so it must be excluded here — otherwise this just double-counts
what circle_hot already shows.
"""

from __future__ import annotations

from sentinel.analyzers import project


def _ev(actor, repo, etype="WatchEvent", at="2026-07-14T06:00:00Z"):
    return {"actor": actor, "repo": repo, "type": etype, "created_at": at}


def test_heat_is_distinct_actors_not_event_count():
    """Ten pushes from one account must not outrank three different people."""
    events = [_ev("bot", "a/one", "PushEvent") for _ in range(10)]  # PushEvent ignored
    events += [_ev("bot", "a/one", "IssueCommentEvent") for _ in range(10)]  # 1 actor
    events += [_ev(f"u{i}", "b/two", "WatchEvent") for i in range(3)]  # 3 actors

    hot = project.outer_circle_hot(events, followees=set(), my_repos=set(), min_actors=2)

    by_repo = {h["repo"]: h for h in hot}
    assert "a/one" not in by_repo, "single-actor repo should not qualify at min_actors=2"
    assert by_repo["b/two"]["outer_count"] == 3


def test_followee_activity_is_excluded():
    """A followee starring counts as inner circle, not outer heat."""
    events = [
        _ev("followee_x", "a/one"),
        _ev("stranger1", "a/one"),
        _ev("stranger2", "a/one"),
    ]
    hot = project.outer_circle_hot(
        events, followees={"followee_x"}, my_repos=set(), min_actors=2
    )
    assert hot[0]["outer_count"] == 2, "followee must not be counted as outer"
    assert "followee_x" not in hot[0]["outer_actors"]


def test_own_repos_are_excluded():
    events = [_ev("s1", "me/mine"), _ev("s2", "me/mine")]
    hot = project.outer_circle_hot(
        events, followees=set(), my_repos={"me/mine"}, min_actors=2
    )
    assert hot == []


def test_inner_crossover_qualifies_below_threshold():
    """A repo one outsider touched still surfaces if your own circle also starred it."""
    events = [_ev("stranger", "a/one")]
    hot = project.outer_circle_hot(
        events,
        followees=set(),
        my_repos=set(),
        inner_starred_by={"a/one": {"my_followee"}},
        min_actors=2,
    )
    assert len(hot) == 1
    assert hot[0]["in_circle"] is True
    assert hot[0]["inner_count"] == 1


def test_engagement_types_are_tallied():
    events = [
        _ev("u1", "a/one", "WatchEvent"),
        _ev("u2", "a/one", "PullRequestEvent"),
        _ev("u3", "a/one", "IssuesEvent"),
    ]
    hot = project.outer_circle_hot(events, followees=set(), my_repos=set(), min_actors=2)
    assert hot[0]["by_kind"] == {"star": 1, "pr": 1, "issue": 1}


def test_trending_crossmark():
    events = [_ev("u1", "a/one"), _ev("u2", "a/one")]
    hot = project.outer_circle_hot(
        events,
        followees=set(),
        my_repos=set(),
        trepos={"a/one": {"front_page": True, "languages": ["go"]}},
        min_actors=2,
    )
    assert hot[0]["trending"] is True
    assert hot[0]["trending_front_page"] is True
    assert hot[0]["languages"] == ["go"]
