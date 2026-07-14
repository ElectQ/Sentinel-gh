"""Soundwave-style day bundles — Megatron-facing contract surface.

Mirrors ElectQ/Soundwave:
  bundles/index.json          <- entry + readiness marker
  bundles/YYYY-MM-DD.json     <- one file per Beijing day

Internal raw archives stay under data/{events,follows,feed,pulse}.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import Any

from ..state import ROOT

SCHEMA_VERSION = 1
SOURCE_ID = "github_followee_feed"
CST = timezone(timedelta(hours=8))
BUNDLES_DIR = ROOT / "bundles"


def beijing_date(dt: datetime | None = None) -> str:
    dt = dt or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(CST).strftime("%Y-%m-%d")


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, str) and value:
        try:
            s = value.replace("Z", "+00:00")
            return datetime.fromisoformat(s).astimezone(timezone.utc).isoformat()
        except ValueError:
            return value
    return ""


def _producer() -> dict[str, Any]:
    try:
        ver = pkg_version("sentinel")
    except Exception:
        ver = "0.1.0"
    return {
        "name": "sentinel-gh",
        "version": ver,
        "run_id": os.environ.get("GITHUB_RUN_ID", ""),
        "commit": (os.environ.get("GITHUB_SHA") or "")[:7],
    }


def feed_item_to_contract(item: dict[str, Any], *, collected_at: str | None = None) -> dict[str, Any]:
    """Map an internal feed item to the daily contract Item.

    Minimal model (preferred for analysis):
      id, who, who_url, action, target, target_url, text, at

    Also keeps Soundwave-compatible aliases:
      external_id, author, author_url, url, content, published_at, tags, links, …
    """
    kind = item.get("kind") or ""
    actor = item.get("actor") or ""
    actor_url = item.get("actor_url") or (f"https://github.com/{actor}" if actor else "")
    repo = item.get("repo")
    repo_url = item.get("repo_url")
    target_user = item.get("target_user")
    target_user_url = item.get("target_user_url")
    payload = item.get("payload") or {}
    forkee = payload.get("forkee")
    forkee_url = payload.get("forkee_url")
    primary = item.get("html_url") or repo_url or target_user_url or actor_url or ""

    # Human "target": repo full_name for repo events, login for follow.
    if kind in ("follow", "unfollow"):
        target_label = target_user
        target_link = target_user_url or primary
    elif kind == "fork" and forkee:
        target_label = forkee
        target_link = forkee_url or primary
    else:
        target_label = repo
        target_link = repo_url or primary

    links: list[str] = []
    for u in (actor_url, repo_url, target_user_url, forkee_url, primary, payload.get("url")):
        if u and u not in links:
            links.append(u)

    refs: dict[str, Any] = {
        "actor": {"login": actor, "url": actor_url} if actor else None,
        "repo": {"full_name": repo, "url": repo_url} if repo else None,
        "target_user": {"login": target_user, "url": target_user_url} if target_user else None,
        "forkee": {"full_name": forkee, "url": forkee_url} if forkee else None,
    }

    signals = item.get("signals") or {}
    eid = str(item.get("id") or "")
    text = item.get("text") or ""
    at = _iso(item.get("created_at"))
    collected = _iso(collected_at or item.get("created_at"))

    return {
        # --- minimal analysis shape ---
        "id": eid,
        "who": actor,
        "who_url": actor_url,
        "action": kind,
        "target": target_label,
        "target_url": target_link,
        "text": text,
        "at": at,
        # --- Soundwave-compatible aliases ---
        "external_id": eid,
        "url": primary,
        "content": text,
        "author": actor,
        "author_name": actor,
        "author_url": actor_url,
        "published_at": at,
        "collected_at": collected,
        "tags": [f"kind:{kind}"] if kind else [],
        "hashtags": [],
        "links": links,
        "refs": refs,
        "media": {"photos": [], "videos": []},
        "metrics": {
            "circle_count": signals.get("circle_count"),
            "trending": bool(signals.get("trending")),
            "trending_front_page": bool(signals.get("trending_front_page")),
        },
        "flags": {
            "kind": kind,
            "time_precision": item.get("time_precision") or "exact",
            "is_follow": kind in ("follow", "unfollow"),
            "is_repo_event": kind in ("star", "fork", "release", "created", "public_repo"),
        },
        # Persona of the newly-followed target, when enrichment ran (follow items
        # only). Explicitly copied because this builder is a whitelist — a key on
        # the internal item that is not named here never reaches the bundle.
        **({"persona": item["persona"]} if item.get("persona") else {}),
    }


class BundleStore:
    """Day bundles: the contract surface downstream (e.g. Megatron) pulls."""

    def __init__(self, bundle_dir: Path | str | None = None, source_id: str = SOURCE_ID):
        self.dir = Path(bundle_dir) if bundle_dir else BUNDLES_DIR
        self.dir.mkdir(parents=True, exist_ok=True)
        self.source_id = source_id

    def path_for(self, date: str) -> Path:
        return self.dir / f"{date}.json"

    def write(
        self,
        date: str,
        items: list[dict[str, Any]],
        *,
        window_start: str | datetime,
        window_end: str | datetime,
        window_hours: int | None = None,
        stats_extra: dict[str, Any] | None = None,
        merge: bool = True,
    ) -> Path:
        path = self.path_for(date)
        existing = json.loads(path.read_text()) if path.exists() and merge else {}

        merged: dict[str, dict[str, Any]] = {
            it["external_id"]: it for it in existing.get("items", []) if it.get("external_id")
        }
        merged.update({it["external_id"]: it for it in items if it.get("external_id")})

        start, end = _iso(window_start), _iso(window_end)
        old_window = existing.get("collect_window") or {}
        if old_window.get("start") and start:
            start = min(start, old_window["start"])
        if old_window.get("end") and end:
            end = max(end, old_window["end"])

        ordered = sorted(
            merged.values(),
            key=lambda it: it.get("at") or it.get("published_at") or "",
            reverse=True,
        )

        by_kind: dict[str, int] = {}
        for it in ordered:
            for tag in it.get("tags") or []:
                if tag.startswith("kind:"):
                    k = tag[5:]
                    by_kind[k] = by_kind.get(k, 0) + 1

        hours = window_hours
        if hours is None and start and end:
            try:
                hours = int(
                    (
                        datetime.fromisoformat(end) - datetime.fromisoformat(start)
                    ).total_seconds()
                    // 3600
                )
            except ValueError:
                hours = 24

        stats = {
            "total": len(ordered),
            "by_kind": by_kind,
            "failed_lists": [],
        }
        if stats_extra:
            stats.update(stats_extra)

        bundle = {
            "schema_version": SCHEMA_VERSION,
            "source_id": self.source_id,
            "collect_date": date,
            "collect_window": {"start": start, "end": end, "hours": hours or 24},
            "producer": _producer(),
            "stats": stats,
            "items": ordered,
        }
        path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2) + "\n")
        return path

    def rebuild_index(self) -> Path:
        """Rewrite index.json — entry point and readiness marker."""
        days = []
        watermark = ""

        for path in sorted(self.dir.glob("*.json"), reverse=True):
            if path.name == "index.json":
                continue
            data = json.loads(path.read_text())
            body = path.read_bytes()
            days.append(
                {
                    "date": data["collect_date"],
                    "count": data["stats"]["total"],
                    "sha256": hashlib.sha256(body).hexdigest(),
                    "window_end": (data.get("collect_window") or {}).get("end") or "",
                }
            )
            we = (data.get("collect_window") or {}).get("end") or ""
            if we:
                watermark = max(watermark, we)

        # days currently reverse-alpha by filename (newest first if ISO dates)
        days_sorted = sorted(days, key=lambda d: d["date"], reverse=True)
        index = {
            "source_id": self.source_id,
            "schema_version": SCHEMA_VERSION,
            "latest": days_sorted[0]["date"] if days_sorted else "",
            "watermark": watermark,
            "updated_at": _iso(datetime.now(timezone.utc)),
            "days": days_sorted,
        }
        path = self.dir / "index.json"
        path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n")
        return path

    def write_from_feed(
        self,
        feed: dict[str, Any],
        *,
        collect_date: str | None = None,
        collected_at: str | None = None,
        merge: bool = True,
    ) -> Path:
        """Project a feed document into a day bundle + refresh index."""
        date = collect_date or feed.get("date") or beijing_date()
        now = collected_at or datetime.now(timezone.utc).isoformat()
        items = [
            feed_item_to_contract(it, collected_at=now) for it in feed.get("items") or []
        ]
        # window: min/max published_at among items, fallback to day
        pubs = [it["published_at"] for it in items if it.get("published_at")]
        if pubs:
            window_start, window_end = min(pubs), max(pubs)
        else:
            window_start = f"{date}T00:00:00+00:00"
            window_end = now

        path = self.write(
            date,
            items,
            window_start=window_start,
            window_end=window_end,
            stats_extra={
                "followee_count": feed.get("followee_count"),
                "push_events_excluded": (feed.get("summary") or {}).get("push_events_excluded"),
            },
            merge=merge,
        )
        self.rebuild_index()
        return path


def write_bundle_from_feed(feed: dict[str, Any], **kwargs: Any) -> Path:
    return BundleStore().write_from_feed(feed, **kwargs)


if __name__ == "__main__":
    # Rebuild all bundles from existing data/feed/YYYY-MM-DD.json
    feed_dir = ROOT / "data" / "feed"
    store = BundleStore()
    for path in sorted(feed_dir.glob("20*.json")):
        feed = json.loads(path.read_text())
        # Historical feeds used UTC date as filename; keep that as collect_date
        # for backfill continuity (Soundwave-style Beijing dating applies to new runs).
        out = store.write_from_feed(feed, collect_date=feed["date"], merge=False)
        print(f"wrote {out.name} items={feed.get('item_count')}")
    store.rebuild_index()
    print("index:", (store.dir / "index.json").read_text()[:400])
