"""Persona enrichment for follow events.

A follow event — "A followed B" — is by itself just two names. Its value is B: a
person your circle has newly decided to watch. So for each follow we fetch B's
public profile and top repos and attach a compact `persona` blob, from which the
downstream day page builds a "who is this" board.

Only B (the target) is enriched. A (the actor) is one of *your* followees, and
the fact that A follows anyone is your follow graph — it stays on the private
side and is never fetched or characterised here.

Cheap: follows run 0–1/day, and each target is fetched once per run (cached by
login), so this adds ~2 API calls to a run that already makes thousands. The
GitHub client backs off on rate-limit exhaustion, so the worst case is slower,
never a failed run.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

TOP_N = 5
_BIO_MAX = 280
_DESC_MAX = 200


def _languages(repos: list[dict]) -> list[str]:
    tally = Counter(r.get("language") for r in repos if r.get("language"))
    return [lang for lang, _ in tally.most_common(5)]


def fetch(gh: Any, login: str, top_n: int = TOP_N) -> dict | None:
    """B's profile + top repos as a compact blob, or None if it can't be built.

    Never raises — enrichment is best-effort; a follow with no persona simply
    renders as a bare "newly followed" entry downstream.
    """
    if not login:
        return None
    try:
        _, profile, _ = gh.get(f"/users/{login}")
    except Exception:
        return None
    if not profile:
        return None
    try:
        # `sort=pushed` gets the active repos; we re-rank by stars client-side
        # because the API does not sort repos by star count.
        repos = gh.paginate(f"/users/{login}/repos", {"sort": "pushed"}, max_pages=2)
    except Exception:
        repos = []

    owned = [r for r in repos if not r.get("fork")]
    owned.sort(key=lambda r: r.get("stargazers_count") or 0, reverse=True)
    top_repos = [
        {
            "name": r.get("full_name") or r.get("name") or "",
            "description": (r.get("description") or "")[:_DESC_MAX],
            "language": r.get("language"),
            "stars": r.get("stargazers_count") or 0,
            "pushed_at": r.get("pushed_at"),
        }
        for r in owned[:top_n]
    ]
    return {
        "login": login,
        "name": profile.get("name") or "",
        "bio": (profile.get("bio") or "")[:_BIO_MAX],
        "followers": profile.get("followers") or 0,
        "public_repos": profile.get("public_repos") or 0,
        "html_url": profile.get("html_url") or f"https://github.com/{login}",
        "languages": _languages(owned),
        "top_repos": top_repos,
    }


def enrich_follows(gh: Any, items, *, cache: dict | None = None) -> None:
    """Attach `persona` to each follow item in `items`, in place.

    `cache` (login -> persona|None) dedupes the case where two followees follow
    the same target on the same day.
    """
    if gh is None:
        return
    cache = {} if cache is None else cache
    for item in items:
        if item.get("kind") != "follow":
            continue
        login = item.get("target_user")
        if not login:
            continue
        if login not in cache:
            cache[login] = fetch(gh, login)
        if cache[login]:
            item["persona"] = cache[login]
