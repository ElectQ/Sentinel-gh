"""Aggregate the day's collected events into the pulse JSON contract."""

import datetime as dt
import json
from collections import Counter, defaultdict

from ..state import ROOT

PULSE_DIR = ROOT / "data" / "pulse"
SCHEMA_VERSION = 1


def build(new_events: list[dict], trending: dict, followee_count: int) -> dict:
    now = dt.datetime.now(dt.UTC)
    trepos = trending["repos"]

    starred_by: dict[str, set[str]] = defaultdict(set)
    releases, new_repos = [], []
    for ev in new_events:
        if ev["type"] == "WatchEvent":
            starred_by[ev["repo"]].add(ev["actor"])
        elif ev["type"] == "ReleaseEvent" and ev["payload"].get("action") == "published":
            releases.append(
                {
                    "repo": ev["repo"],
                    "by": ev["actor"],
                    "tag": ev["payload"].get("tag"),
                    "name": ev["payload"].get("name"),
                    "url": ev["payload"].get("url"),
                    "prerelease": ev["payload"].get("prerelease"),
                }
            )
        elif ev["type"] == "CreateEvent" and ev["payload"].get("ref_type") == "repository":
            new_repos.append({"repo": ev["repo"], "by": ev["actor"], "created_at": ev["created_at"]})

    def star_entry(repo: str) -> dict:
        meta = trepos.get(repo)
        return {
            "repo": repo,
            "count": len(starred_by[repo]),
            "starred_by": sorted(starred_by[repo]),
            "trending": meta is not None,
            "trending_front_page": bool(meta and meta["front_page"]),
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

    return {
        "schema_version": SCHEMA_VERSION,
        "date": now.date().isoformat(),
        "generated_at": now.isoformat(timespec="seconds"),
        "followee_count": followee_count,
        "events_collected": len(new_events),
        "trending_available": trending["available"],
        "trending_source_date": trending["date"],
        "circle_hot": circle_hot,
        "trending_overlap": trending_overlap,
        "releases": releases,
        "new_repos": new_repos,
        "starred_repos_total": len(starred_by),
        "raw_counts": dict(Counter(e["type"] for e in new_events)),
    }


def write(pulse: dict) -> None:
    PULSE_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(pulse, ensure_ascii=False, indent=2) + "\n"
    (PULSE_DIR / f"{pulse['date']}.json").write_text(payload)
    (PULSE_DIR / "latest.json").write_text(payload)
