"""Aggregate the day's collected events into the pulse JSON contract (schema v2)."""

from __future__ import annotations

import datetime as dt
import json
import os
from collections import Counter

from ..state import ROOT
from ..util import env_bool, env_int
from . import project

PULSE_DIR = ROOT / "data" / "pulse"
SCHEMA_VERSION = 2


def build(
    day_events: list[dict],
    day_follows: list[dict],
    trending: dict,
    followee_count: int,
    *,
    feed_item_count: int | None = None,
    following_enabled: bool = True,
    truncated_users: list[str] | None = None,
) -> dict:
    now = dt.datetime.now(dt.UTC)
    trepos = trending.get("repos") or {}
    starred_by = project.star_circle_counts(day_events)

    releases = []
    for item in project.iter_releases(day_events):
        releases.append(
            {
                "repo": item["repo"],
                "by": item["actor"],
                "tag": item["payload"].get("tag"),
                "name": item["payload"].get("name"),
                "url": item["payload"].get("url"),
                "prerelease": item["payload"].get("prerelease"),
            }
        )

    new_repos = []
    for item in project.iter_created(day_events):
        new_repos.append(
            {
                "repo": item["repo"],
                "by": item["actor"],
                "created_at": item["created_at"],
            }
        )

    def star_entry(repo: str) -> dict:
        meta = trepos.get(repo)
        return {
            "repo": repo,
            "repo_url": project.repo_url(repo),
            "count": len(starred_by[repo]),
            "starred_by": sorted(starred_by[repo]),
            "starred_by_urls": [project.profile_url(u) for u in sorted(starred_by[repo])],
            "trending": meta is not None,
            "trending_front_page": bool(meta and meta.get("front_page")),
        }

    circle_hot = sorted(
        (star_entry(r) for r, users in starred_by.items() if len(users) >= 2),
        key=lambda x: (-x["count"], x["repo"]),
    )
    trending_overlap = sorted(
        (
            {**star_entry(r), "languages": trepos[r]["languages"]}
            for r in starred_by
            if r in trepos
        ),
        key=lambda x: (-x["count"], x["repo"]),
    )

    # Individual stars for digest
    stars_max = env_int("STARS_ITEMS_MAX", 200)
    star_items = []
    for item in project.iter_stars(day_events):
        meta = trepos.get(item["repo"] or "")
        star_items.append(
            {
                "repo": item["repo"],
                "repo_url": item.get("repo_url") or project.repo_url(item["repo"]),
                "by": item["actor"],
                "by_url": item.get("actor_url") or project.profile_url(item["actor"]),
                "html_url": item.get("html_url") or project.repo_url(item["repo"]),
                "text": item.get("text"),
                "created_at": item["created_at"],
                "trending": meta is not None,
                "trending_front_page": bool(meta and meta.get("front_page")),
            }
        )
    star_items.sort(key=lambda x: (x["created_at"], x["repo"], x["by"]), reverse=True)
    stars_truncated = len(star_items) > stars_max
    stars_block = {
        "total": len(star_items),
        "items": star_items[:stars_max],
    }
    if stars_truncated:
        stars_block["truncated"] = True

    forks = []
    for item in project.iter_forks(day_events):
        forkee = item["payload"].get("forkee")
        forks.append(
            {
                "repo": item["repo"],
                "repo_url": item.get("repo_url") or project.repo_url(item["repo"]),
                "by": item["actor"],
                "by_url": item.get("actor_url") or project.profile_url(item["actor"]),
                "forkee": forkee,
                "forkee_url": item["payload"].get("forkee_url") or project.repo_url(forkee),
                "html_url": item.get("html_url") or project.repo_url(forkee) or project.repo_url(item["repo"]),
                "text": item.get("text"),
                "created_at": item["created_at"],
            }
        )
    forks.sort(key=lambda x: (x["created_at"], x["repo"], x["by"]), reverse=True)

    # Follows
    publish = env_bool("PUBLISH_FOLLOW_EDGES", True)
    follows_max = env_int("FOLLOWS_ITEMS_MAX", 500)
    followed = [f for f in day_follows if f.get("action") == "followed"]
    unfollowed = [f for f in day_follows if f.get("action") == "unfollowed"]
    followed.sort(key=lambda x: x.get("observed_at") or "", reverse=True)
    unfollowed.sort(key=lambda x: x.get("observed_at") or "", reverse=True)

    degraded = not following_enabled
    if not publish:
        follow_items: list[dict] = []
        unfollow_items: list[dict] = []
        new_count = 0
        unfollow_count = 0
    else:
        new_count = len(followed)
        unfollow_count = len(unfollowed)
        follow_items = [
            {
                "id": f["id"],
                "by": f["actor"],
                "by_url": project.profile_url(f["actor"]),
                "target": f["target"],
                "target_url": project.profile_url(f["target"]),
                "html_url": project.profile_url(f["target"]),
                "text": f"{f['actor']} followed {f['target']}",
                "observed_at": f.get("observed_at"),
            }
            for f in followed[:follows_max]
        ]
        unfollow_items = [
            {
                "id": f["id"],
                "by": f["actor"],
                "by_url": project.profile_url(f["actor"]),
                "target": f["target"],
                "target_url": project.profile_url(f["target"]),
                "html_url": project.profile_url(f["target"]),
                "text": f"{f['actor']} unfollowed {f['target']}",
                "observed_at": f.get("observed_at"),
            }
            for f in unfollowed[:follows_max]
        ]

    trunc = sorted(set(truncated_users or []))
    follows_block = {
        "new_count": new_count,
        "unfollow_count": unfollow_count,
        "items": follow_items,
        "unfollows": unfollow_items,
        "truncated_users": trunc,
        "incomplete": bool(trunc),
        "degraded": degraded,
    }

    out: dict = {
        "schema_version": SCHEMA_VERSION,
        "date": now.date().isoformat(),
        "generated_at": now.isoformat(timespec="seconds"),
        "followee_count": followee_count,
        "events_collected": len(day_events),
        "trending_available": bool(trending.get("available")),
        "trending_source_date": trending.get("date"),
        "circle_hot": circle_hot,
        "trending_overlap": trending_overlap,
        "releases": releases,
        "new_repos": new_repos,
        "starred_repos_total": len(starred_by),
        "raw_counts": dict(Counter(e["type"] for e in day_events)),
        "follows": follows_block,
        "stars": stars_block,
        "forks": forks,
    }
    if feed_item_count is not None:
        out["feed_item_count"] = feed_item_count
    return out


def write(pulse: dict) -> None:
    PULSE_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(pulse, ensure_ascii=False, indent=2) + "\n"
    (PULSE_DIR / f"{pulse['date']}.json").write_text(payload)
    (PULSE_DIR / "latest.json").write_text(payload)
