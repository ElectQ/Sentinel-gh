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
        self.last_rate_limit_remaining: int | None = None

    def _track_rate(self, r: httpx.Response) -> None:
        rem = r.headers.get("x-ratelimit-remaining")
        if rem is not None:
            try:
                self.last_rate_limit_remaining = int(rem)
            except ValueError:
                pass

    def get(self, path: str, params: dict | None = None, etag: str | None = None):
        """Single GET against the API. Returns (status, data, etag).

        With an etag, an unchanged resource yields (304, None, etag) and does
        not count against the rate limit.
        """
        headers = {"If-None-Match": etag} if etag else {}
        r = None
        for attempt in range(4):
            r = self.http.get(f"{API}{path}", params=params, headers=headers)
            self._track_rate(r)
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

    def get_list_with_etag(
        self,
        path: str,
        *,
        etag: str | None,
        max_pages: int,
        per_page: int = 100,
    ) -> tuple[str, list | None, str | None, bool]:
        """Fetch a paginated list with page-1 conditional request.

        Returns (status, items_or_None, new_etag, truncated).
        status: "not_modified" | "ok" | "error"
        """
        headers = {"If-None-Match": etag} if etag else {}
        r = None
        for attempt in range(4):
            r = self.http.get(
                f"{API}{path}",
                params={"per_page": per_page, "page": 1},
                headers=headers,
            )
            self._track_rate(r)
            if r.status_code in (403, 429) and r.headers.get("x-ratelimit-remaining") == "0":
                reset = int(r.headers.get("x-ratelimit-reset", time.time() + 60))
                time.sleep(min(max(reset - time.time() + 1, 1), 300))
                continue
            if r.status_code >= 500:
                time.sleep(2**attempt)
                continue
            break

        if r.status_code == 304:
            return "not_modified", None, etag, False
        if r.status_code in (401, 403, 404):
            return "error", None, etag, False
        if r.status_code >= 400:
            return "error", None, etag, False

        items = list(r.json())
        new_etag = r.headers.get("etag")
        page = 1
        while len(items) == page * per_page and page < max_pages:
            page += 1
            status, more, _ = self.get(path, {"per_page": per_page, "page": page})
            if status != 200 or not more:
                break
            items.extend(more)
            if len(more) < per_page:
                break

        # Truncated if we filled max_pages and the last page was full.
        last_page_full = len(items) >= max_pages * per_page and len(items) % per_page == 0
        # More precisely: if we stopped because of max_pages while last fetch was full.
        truncated = page >= max_pages and len(items) == max_pages * per_page
        if last_page_full and page == max_pages:
            truncated = True
        return "ok", items, new_etag, truncated
