"""A stand-in for Google's token, userinfo and Search Console endpoints."""

import json
from datetime import date, timedelta
from typing import Any

import httpx

SITE = "sc-domain:wp.example.com"
BASE = "https://wp.example.com"
EMAIL = "owner@example.com"

# (query, page path, clicks, impressions, position) shown every day
QUERY_ROWS = [
    ("managed it services", "/hello-world/", 5, 40, 3.0),
    ("it support", "/hello-world/", 2, 20, 6.0),
    ("about acme", "/about/", 4, 40, 7.0),
]


class FakeGoogle:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.queries: list[dict[str, Any]] = []
        self.token = "tok-1"
        self.refreshed_token = "tok-2"
        self.refreshes = 0
        self.revoked = False
        self.sites = [{"siteUrl": SITE, "permissionLevel": "siteOwner"}]
        self.sitemaps: list[dict[str, Any]] = [
            {
                "path": f"{BASE}/sitemap_index.xml",
                "isSitemapsIndex": True,
                "isPending": False,
                "lastSubmitted": "2026-09-01T10:00:00.000Z",
                "lastDownloaded": "2026-09-28T10:00:00.000Z",
                "warnings": "1",
                "errors": "0",
                "contents": [{"type": "web", "submitted": "105", "indexed": "98"}],
            }
        ]
        self.overrides: dict[tuple[str, str], dict[str, float]] = {}
        self.status_for_queries = 200

    # --- data ----------------------------------------------------------------------------

    def _rows(self, day: date, dims: list[str]) -> list[dict[str, Any]]:
        iso = day.isoformat()
        queries = [(q, p, c, i, pos) for q, p, c, i, pos in QUERY_ROWS]
        if "query" in dims:
            return [
                self._row([iso, q, f"{BASE}{p}"], c, i, pos, (q, iso))
                for q, p, c, i, pos in queries
            ]
        if "page" in dims:
            pages: dict[str, list[float]] = {}
            for _, p, c, i, pos in queries:
                acc = pages.setdefault(p, [0, 0, 0.0])
                acc[0] += c
                acc[1] += i
                acc[2] += pos * i
            return [
                self._row([iso, f"{BASE}{p}"], a[0], a[1], a[2] / a[1], (p, iso))
                for p, a in pages.items()
            ]
        clicks = sum(q[2] for q in queries)
        impressions = sum(q[3] for q in queries)
        weighted = sum(q[4] * q[3] for q in queries)
        return [self._row([iso], clicks, impressions, weighted / impressions, ("", iso))]

    def _row(
        self,
        keys: list[str],
        clicks: float,
        impressions: float,
        position: float,
        tag: tuple[str, str],
    ) -> dict[str, Any]:
        change = self.overrides.get(tag, {})
        clicks = change.get("clicks", clicks)
        return {
            "keys": keys,
            "clicks": clicks,
            "impressions": impressions,
            "ctr": clicks / impressions,
            "position": position,
        }

    # --- http ----------------------------------------------------------------------------

    @staticmethod
    def _json(status: int, body: object) -> httpx.Response:
        return httpx.Response(status, content=json.dumps(body).encode())

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        if request.url.host == "oauth2.googleapis.com":
            self.refreshes += 1
            self.token = self.refreshed_token
            return self._json(
                200,
                {
                    "access_token": self.refreshed_token,
                    "expires_in": 3600,
                    "scope": "openid email https://www.googleapis.com/auth/webmasters.readonly",
                },
            )
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return self._json(401, {"error": {"message": "invalid credentials"}})
        if request.url.host == "openidconnect.googleapis.com":
            return self._json(200, {"email": EMAIL})
        if path == "/webmasters/v3/sites":
            return self._json(200, {"siteEntry": self.sites})
        if path.endswith("/sitemaps"):
            return self._json(200, {"sitemap": self.sitemaps})
        if path.endswith("/searchAnalytics/query"):
            if self.status_for_queries != 200:
                return self._json(self.status_for_queries, {"error": {"message": "nope"}})
            return self._query(json.loads(request.content))
        return self._json(404, {})

    def _query(self, body: dict[str, Any]) -> httpx.Response:
        self.queries.append(body)
        start = date.fromisoformat(body["startDate"])
        end = date.fromisoformat(body["endDate"])
        rows: list[dict[str, Any]] = []
        day = start
        while day <= end:
            rows.extend(self._rows(day, body["dimensions"]))
            day += timedelta(days=1)
        offset, limit = int(body.get("startRow", 0)), int(body.get("rowLimit", 25000))
        return self._json(200, {"rows": rows[offset : offset + limit]})
