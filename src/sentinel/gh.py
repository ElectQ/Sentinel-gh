"""GitHub REST API client: auth, pagination, rate-limit backoff, conditional requests."""

import os
import time

import httpx

API = "https://api.github.com"


class GitHub:
    def __init__(self, token: str | None = None):
        self.token = (
            token
            or os.environ.get("GH_PAT")
            or os.environ.get("GH_TOKEN")
            or os.environ.get("GITHUB_TOKEN")
        )
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "sentinel-gh",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        self.http = httpx.Client(headers=headers, timeout=30, follow_redirects=True)

    def get(self, path: str, params: dict | None = None, etag: str | None = None):
        """Single GET against the API. Returns (status, data, etag).

        With an etag, an unchanged resource yields (304, None, etag) and does
        not count against the rate limit.
        """
        headers = {"If-None-Match": etag} if etag else {}
        r = None
        for attempt in range(4):
            r = self.http.get(f"{API}{path}", params=params, headers=headers)
            if r.status_code in (403, 429) and r.headers.get("x-ratelimit-remaining") == "0":
                reset = int(r.headers.get("x-ratelimit-reset", time.time() + 60))
                time.sleep(min(max(reset - time.time() + 1, 1), 300))
                continue
            if r.status_code >= 500:
                time.sleep(2**attempt)
                continue
            break
        if r.status_code == 304:
            return 304, None, etag
        r.raise_for_status()
        return r.status_code, r.json(), r.headers.get("etag")

    def paginate(self, path: str, params: dict | None = None, max_pages: int = 20) -> list:
        params = dict(params or {}, per_page=100)
        out: list = []
        for page in range(1, max_pages + 1):
            _, data, _ = self.get(path, {**params, "page": page})
            out.extend(data)
            if len(data) < 100:
                break
        return out
