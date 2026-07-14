"""Shared pure projections from day_events / day_follows for feed and pulse."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterator
from typing import Any

GH = "https://github.com"

# Dashboard-feed event type -> the kind of engagement it represents. Anything not
# listed here (PublicEvent, DeleteEvent, MemberEvent, PushEvent, …) is ignored as
# noise for the purpose of "what is the network converging on".
RECEIVED_ENGAGEMENT = {
    "WatchEvent": "star",
    "ForkEvent": "fork",
    "ReleaseEvent": "release",
    "CreateEvent": "created",
    "IssuesEvent": "issue",
    "IssueCommentEvent": "issue",
    "PullRequestEvent": "pr",
    "PullRequestReviewEvent": "pr",
    "PullRequestReviewCommentEvent": "pr",
    "CommitCommentEvent": "comment",
}


def profile_url(login: str | None) -> str | None:
    if not login:
        return None
    return f"{GH}/{login}"


def repo_url(full_name: str | None) -> str | None:
    if not full_name:
        return None
    return f"{GH}/{full_name}"


def release_url(full_name: str | None, tag: str | None, explicit: str | None = None) -> str | None:
    if explicit:
        return explicit
    if full_name and tag:
        return f"{GH}/{full_name}/releases/tag/{tag}"
    return repo_url(full_name)


def star_circle_counts(day_events: list[dict]) -> dict[str, set[str]]:
    """repo -> set of actors who starred it today."""
    starred_by: dict[str, set[str]] = defaultdict(set)
    for ev in day_events:
        if ev.get("type") == "WatchEvent":
            starred_by[ev["repo"]].add(ev["actor"])
    return starred_by


def count_push_excluded(day_events: list[dict]) -> int:
    return sum(1 for e in day_events if e.get("type") == "PushEvent")


def outer_circle_hot(
    received_events: list[dict],
    *,
    followees: set[str],
    my_repos: set[str],
    inner_starred_by: dict[str, set[str]] | None = None,
    trepos: dict[str, dict] | None = None,
    min_actors: int = 2,
) -> list[dict]:
    """Repos the *outer* network is converging on, from the dashboard feed.

    "Outer" = engagement by people you don't follow, on repos you don't own. Your
    followees' activity is already the inner circle (collectors.followees), and
    your own repos are a separate concern; both are excluded here so this stays a
    pure "what's bubbling up around my circle that I'm not tracking yet" signal.

    Heat is distinct-actor count across all engagement types (star / fork / issue
    / PR / comment / release), not raw event count — ten pushes from one bot must
    not outweigh three different people opening issues. A repo qualifies when
    `min_actors` distinct outsiders engaged, OR when someone in your own circle
    also starred it (the crossover is worth surfacing even at low outer count).
    """
    inner_starred_by = inner_starred_by or {}
    trepos = trepos or {}

    actors: dict[str, set[str]] = defaultdict(set)
    kinds: dict[str, Counter] = defaultdict(Counter)
    latest: dict[str, str] = {}
    for ev in received_events:
        actor = ev.get("actor")
        repo = ev.get("repo")
        engagement = RECEIVED_ENGAGEMENT.get(ev.get("type"))
        if not (actor and repo and engagement):
            continue
        if actor in followees or repo in my_repos:
            continue
        actors[repo].add(actor)
        kinds[repo][engagement] += 1
        at = ev.get("created_at") or ""
        if at > latest.get(repo, ""):
            latest[repo] = at

    out = []
    for repo, who in actors.items():
        inner = inner_starred_by.get(repo, set())
        if len(who) < min_actors and not inner:
            continue
        meta = trepos.get(repo)
        out.append(
            {
                "repo": repo,
                "repo_url": repo_url(repo),
                "outer_count": len(who),
                "outer_actors": sorted(who),
                "outer_actor_urls": [profile_url(u) for u in sorted(who)],
                "by_kind": dict(kinds[repo]),
                "inner_count": len(inner),
                "inner_actors": sorted(inner),
                "in_circle": bool(inner),
                "trending": meta is not None,
                "trending_front_page": bool(meta and meta.get("front_page")),
                "languages": (meta or {}).get("languages", []),
                "latest_at": latest.get(repo, ""),
            }
        )
    out.sort(key=lambda x: (-x["outer_count"], -x["inner_count"], x["repo"]))
    return out


def _base(
    *,
    id: str,
    kind: str,
    actor: str,
    created_at: str,
    repo: str | None,
    target_user: str | None,
    payload: dict,
    time_precision: str,
    source_event_id: str | None,
    text: str,
    html_url: str | None,
) -> dict[str, Any]:
    """Common feed-item shape with navigation links."""
    return {
        "id": id,
        "kind": kind,
        "text": text,
        "actor": actor,
        "actor_url": profile_url(actor),
        "created_at": created_at,
        "repo": repo,
        "repo_url": repo_url(repo),
        "target_user": target_user,
        "target_user_url": profile_url(target_user),
        "html_url": html_url,  # primary click-through link
        "payload": payload,
        "time_precision": time_precision,
        "source_event_id": source_event_id,
    }


def iter_stars(day_events: list[dict]) -> Iterator[dict[str, Any]]:
    for ev in day_events:
        if ev.get("type") != "WatchEvent":
            continue
        repo = ev["repo"]
        actor = ev["actor"]
        yield _base(
            id=f"event:{ev['id']}",
            kind="star",
            actor=actor,
            created_at=ev["created_at"],
            repo=repo,
            target_user=None,
            payload={},
            time_precision="exact",
            source_event_id=ev["id"],
            text=f"{actor} starred {repo}",
            html_url=repo_url(repo),
        )


def iter_forks(day_events: list[dict]) -> Iterator[dict[str, Any]]:
    for ev in day_events:
        if ev.get("type") != "ForkEvent":
            continue
        repo = ev["repo"]
        actor = ev["actor"]
        forkee = (ev.get("payload") or {}).get("forkee")
        yield _base(
            id=f"event:{ev['id']}",
            kind="fork",
            actor=actor,
            created_at=ev["created_at"],
            repo=repo,
            target_user=None,
            payload={
                "forkee": forkee,
                "forkee_url": repo_url(forkee),
            },
            time_precision="exact",
            source_event_id=ev["id"],
            text=f"{actor} forked {repo}" + (f" → {forkee}" if forkee else ""),
            html_url=repo_url(forkee) or repo_url(repo),
        )


def iter_releases(day_events: list[dict]) -> Iterator[dict[str, Any]]:
    for ev in day_events:
        if ev.get("type") != "ReleaseEvent":
            continue
        p = ev.get("payload") or {}
        if p.get("action") is not None and p.get("action") != "published":
            continue
        repo = ev["repo"]
        actor = ev["actor"]
        tag = p.get("tag")
        url = release_url(repo, tag, p.get("url"))
        yield _base(
            id=f"event:{ev['id']}",
            kind="release",
            actor=actor,
            created_at=ev["created_at"],
            repo=repo,
            target_user=None,
            payload={
                "tag": tag,
                "name": p.get("name"),
                "url": url,
                "prerelease": p.get("prerelease"),
            },
            time_precision="exact",
            source_event_id=ev["id"],
            text=f"{actor} released {repo}" + (f" {tag}" if tag else ""),
            html_url=url,
        )


def iter_created(day_events: list[dict]) -> Iterator[dict[str, Any]]:
    """CreateEvent for new repositories (kind=created)."""
    for ev in day_events:
        if ev.get("type") != "CreateEvent":
            continue
        p = ev.get("payload") or {}
        if p.get("ref_type") != "repository":
            continue
        repo = ev["repo"]
        actor = ev["actor"]
        yield _base(
            id=f"event:{ev['id']}",
            kind="created",
            actor=actor,
            created_at=ev["created_at"],
            repo=repo,
            target_user=None,
            payload={"ref_type": "repository", "ref": p.get("ref")},
            time_precision="exact",
            source_event_id=ev["id"],
            text=f"{actor} created repository {repo}",
            html_url=repo_url(repo),
        )


iter_new_repos = iter_created


def iter_public_repos(day_events: list[dict]) -> Iterator[dict[str, Any]]:
    for ev in day_events:
        if ev.get("type") != "PublicEvent":
            continue
        repo = ev["repo"]
        actor = ev["actor"]
        yield _base(
            id=f"event:{ev['id']}",
            kind="public_repo",
            actor=actor,
            created_at=ev["created_at"],
            repo=repo,
            target_user=None,
            payload={},
            time_precision="exact",
            source_event_id=ev["id"],
            text=f"{actor} made {repo} public",
            html_url=repo_url(repo),
        )


def iter_follows(
    day_follows: list[dict],
    *,
    actions: set[str] | None = None,
) -> Iterator[dict[str, Any]]:
    actions = actions or {"followed"}
    for rec in day_follows:
        action = rec.get("action")
        if action not in actions:
            continue
        kind = "follow" if action == "followed" else "unfollow"
        actor = rec["actor"]
        target = rec.get("target")
        verb = "followed" if action == "followed" else "unfollowed"
        yield _base(
            id=rec["id"],
            kind=kind,
            actor=actor,
            created_at=rec.get("observed_at") or rec.get("_collected_at"),
            repo=None,
            target_user=target,
            payload={"action": action},
            time_precision="daily_window",
            source_event_id=None,
            text=f"{actor} {verb} {target}",
            # Primary link: the person they newly followed
            html_url=profile_url(target) or profile_url(actor),
        )


def attach_repo_signals(
    item: dict[str, Any],
    *,
    trepos: dict,
    circle: dict[str, set[str]],
) -> dict[str, Any]:
    repo = item.get("repo")
    if not repo:
        item["signals"] = {
            "trending": False,
            "trending_front_page": False,
            "circle_count": None,
        }
        return item
    meta = trepos.get(repo)
    item["signals"] = {
        "trending": meta is not None,
        "trending_front_page": bool(meta and meta.get("front_page")),
        "circle_count": len(circle[repo]) if repo in circle else None,
    }
    return item
