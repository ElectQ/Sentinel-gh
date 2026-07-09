"""Thin adapter over antonkomarev/github-trending-archive.

We deliberately do not scrape github.com/trending ourselves — the archive
repo commits one JSON per language per day. `(null).json` is the front page
(no language filter). Raw fetches don't consume API rate limit; the single
directory listing goes through the API.
"""

import datetime as dt

import httpx

from ..gh import GitHub

ARCHIVE_REPO = "antonkomarev/github-trending-archive"
RAW = "https://raw.githubusercontent.com/{repo}/master/{path}"


def _day_files(gh: GitHub, date: dt.date) -> list[str]:
    path = f"archive/repository/{date.year}/{date.isoformat()}"
    try:
        _, listing, _ = gh.get(f"/repos/{ARCHIVE_REPO}/contents/{path}")
    except httpx.HTTPStatusError:
        return []
    return [item["path"] for item in listing if item["name"].endswith(".json")]


def fetch(gh: GitHub, date: dt.date | None = None, fallback_days: int = 2) -> dict:
    """Union of all per-language trending lists for the given day.

    Returns {"available", "date", "repos": {full_name: {"languages": [...],
    "front_page": bool}}}. Falls back up to `fallback_days` earlier days if
    the archive hasn't committed today's data yet; empty result if none found.
    """
    date = date or dt.datetime.now(dt.UTC).date()
    for delta in range(fallback_days + 1):
        day = date - dt.timedelta(days=delta)
        files = _day_files(gh, day)
        if not files:
            continue
        repos: dict[str, dict] = {}
        with httpx.Client(timeout=30) as http:
            for path in files:
                r = http.get(RAW.format(repo=ARCHIVE_REPO, path=path))
                if r.status_code != 200:
                    continue
                data = r.json()
                lang = data.get("language")  # None == front page
                for full_name in data.get("list", []):
                    entry = repos.setdefault(full_name, {"languages": [], "front_page": False})
                    if lang is None:
                        entry["front_page"] = True
                    else:
                        entry["languages"].append(lang)
        if repos:
            return {"available": True, "date": day.isoformat(), "repos": repos}
    return {"available": False, "date": None, "repos": {}}


if __name__ == "__main__":
    result = fetch(GitHub())
    print(f"available: {result['available']}, date: {result['date']}, repos: {len(result['repos'])}")
    front = [r for r, m in result["repos"].items() if m["front_page"]]
    print(f"front page ({len(front)}): {front[:10]}")
