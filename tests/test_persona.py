"""Persona enrichment for follow events, with a fake GitHub client (no network).

Covers: the blob is built from profile + repos (forks skipped, ranked by stars,
languages tallied), only the *target* is fetched (never the actor), the per-run
cache dedupes, failures degrade to no-persona, and — the load-bearing one — the
persona actually survives `feed_item_to_contract`, which is a field whitelist.
"""

from __future__ import annotations

from sentinel.analyzers import bundle, persona


class FakeGH:
    def __init__(self, profiles, repos):
        self._profiles, self._repos = profiles, repos
        self.calls = []

    def get(self, path, params=None, etag=None):
        self.calls.append(path)
        login = path.split("/users/")[1]
        return 200, self._profiles.get(login), None

    def paginate(self, path, params=None, max_pages=20):
        self.calls.append(path)
        login = path.split("/users/")[1].split("/")[0]
        return self._repos.get(login, [])


def _gh():
    return FakeGH(
        profiles={"thecodacus": {"name": "The Codacus", "bio": "AI agent tinkerer",
                                 "followers": 1200, "public_repos": 40,
                                 "html_url": "https://github.com/thecodacus"}},
        repos={"thecodacus": [
            {"full_name": "thecodacus/agent-rce", "description": "agent exploit",
             "language": "Python", "stargazers_count": 900, "pushed_at": "2026-07-10", "fork": False},
            {"full_name": "thecodacus/dotfiles", "description": "", "language": "Shell",
             "stargazers_count": 3, "pushed_at": "2026-07-01", "fork": False},
            {"full_name": "thecodacus/someone-elses", "description": "not theirs",
             "language": "Go", "stargazers_count": 5000, "pushed_at": "2026-07-09", "fork": True},
        ]},
    )


def test_fetch_builds_the_blob_ranked_by_stars_skipping_forks():
    p = persona.fetch(_gh(), "thecodacus")
    assert p["login"] == "thecodacus" and p["followers"] == 1200
    names = [r["name"] for r in p["top_repos"]]
    assert names == ["thecodacus/agent-rce", "thecodacus/dotfiles"], "forks excluded, star-sorted"
    assert p["languages"][0] == "Python"


def test_fetch_never_raises_on_a_missing_user():
    gh = FakeGH(profiles={}, repos={})
    assert persona.fetch(gh, "ghost") is None


def test_enrich_only_touches_the_target_never_the_actor():
    gh = _gh()
    items = [{"kind": "follow", "actor": "g3tsyst3m", "target_user": "thecodacus"}]
    persona.enrich_follows(gh, items)
    assert items[0]["persona"]["login"] == "thecodacus"
    # Only thecodacus was ever fetched — the actor's graph is never touched.
    assert all("g3tsyst3m" not in c for c in gh.calls)


def test_enrich_caches_repeated_targets():
    gh = _gh()
    items = [
        {"kind": "follow", "target_user": "thecodacus"},
        {"kind": "follow", "target_user": "thecodacus"},
    ]
    persona.enrich_follows(gh, items)
    assert gh.calls.count("/users/thecodacus") == 1, "the second follow hits the cache"


def test_a_star_event_is_left_alone():
    gh = _gh()
    items = [{"kind": "star", "repo": "x/y"}]
    persona.enrich_follows(gh, items)
    assert "persona" not in items[0] and gh.calls == []


def test_persona_survives_the_contract_whitelist():
    """The whole point: feed_item_to_contract is a whitelist, and persona must be
    on the allowlist or it never reaches the bundle."""
    item = {
        "id": "follow:a:thecodacus:2026-07-13:followed", "kind": "follow",
        "actor": "a", "target_user": "thecodacus",
        "text": "a followed thecodacus", "created_at": "2026-07-13T00:00:00+00:00",
        "persona": {"login": "thecodacus", "top_repos": []},
    }
    contract = bundle.feed_item_to_contract(item)
    assert contract["persona"]["login"] == "thecodacus"


def test_a_repo_event_contract_has_no_persona_key():
    item = {"id": "e1", "kind": "star", "repo": "x/y", "text": "a starred x/y",
            "created_at": "2026-07-13T00:00:00+00:00"}
    contract = bundle.feed_item_to_contract(item)
    assert "persona" not in contract
