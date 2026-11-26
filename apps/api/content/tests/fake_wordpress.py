"""A small stateful stand-in for a WordPress site: REST API, content and public pages."""

import base64
import json
from datetime import UTC, datetime
from typing import Any

import httpx

HOST = "wp.example.com"
BASE = f"https://{HOST}"
USER = "editor"
PASSWORD = "abcd efgh ijkl mnop"
HOME_HTML = (
    '<html lang="en"><head><title>Acme IT</title>'
    '<meta name="description" content="Managed IT services">'
    '<meta name="viewport" content="width=device-width">'
    '<meta name="generator" content="WordPress 6.8">'
    '<link rel="canonical" href="https://wp.example.com/"></head>'
    "<body><h1>Acme IT</h1></body></html>"
)
LONG_BODY = (
    "<p>" + "word " * 350 + "</p><h2>Sub heading</h2>"
    f'<img class="wp-image-101" src="{BASE}/wp-content/uploads/a.png" alt="%ALT101%">'
    '<img src="b.jpg" alt="ok">'
    '<a href="/about/">about</a><a href="https://other.example.org/">other</a>'
)


SERVICES_BODY = (
    "<p>Our services.</p>"
    '<h2>Backup</h2><img class="wp-image-201 size-full" src="BASE/wp-content/uploads/team.jpg"'
    ' alt="%ALT201%">'
    '<img class="wp-image-202" src="BASE/wp-content/uploads/icon.svg" alt="%ALT202%">'
    '<img src="https://cdn.other.example/hotlink.png" alt="">'
).replace("BASE", BASE)
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000001e221bc330000000049454e44ae426082"
)


def _row(kind: str, row_id: int, slug: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": row_id,
        "type": kind,
        "slug": slug,
        "status": "publish",
        "title": slug.replace("-", " ").title(),
        "content": "<p>Short text.</p>",
        "modified": "2026-09-01T10:00:00",
        "published": "2026-08-01T10:00:00",
        "author": 2,
        "categories": [],
        "tags": [],
        "description": "A description",
        **extra,
    }


class FakeWordPress:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.queries: list[dict[str, str]] = []
        self.password = PASSWORD
        self.capabilities = {"edit_posts": True, "edit_others_posts": True, "publish_posts": True}
        self.namespaces = ["oembed/1.0", "wp/v2", "rankmath/v1", "elementor/v1"]
        self.rest_disabled = False
        self.rows: dict[str, list[dict[str, Any]]] = {
            "post": [
                _row("post", 1, "hello-world", content=LONG_BODY, categories=[3], tags=[4]),
                _row("post", 2, "short-post", description=""),
                _row("post", 3, "draft-post", status="draft"),
            ],
            "page": [
                _row("page", 10, "about"),
                _row("page", 11, "services", content=SERVICES_BODY),
            ],
        }
        self.extra_totals = {"categories": 5, "tags": 9, "media": 120, "comments": 3}
        self.terms = {"categories": {3: "News"}, "tags": {4: "Security"}}
        self.users = {2: "Editor"}
        self.sitemap_excludes = {"short-post"}
        self.junk_prefix = ""
        self.media: dict[int, dict[str, str]] = {
            101: {"alt": "", "type": "image/png", "file": "a.png"},
            201: {"alt": "", "type": "image/jpeg", "file": "team.jpg"},
            202: {"alt": "", "type": "image/svg+xml", "file": "icon.svg"},
        }
        self.meta_exposed = True
        self.elementor_exposed = True
        self.uploads: list[dict[str, Any]] = []
        self.next_id = 500
        self.stale_seo = False
        self.posted: list[dict[str, Any]] = []
        self.sitemap: str | None = None
        self.public_pages: dict[str, str] = {"/": HOME_HTML}

    # --- test helpers ------------------------------------------------------------------------

    def edit(self, kind: str, row_id: int, **changes: Any) -> None:
        """Change a row and bump its modified time to now, like WordPress does."""
        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        for row in self.rows[kind]:
            if row["id"] == row_id:
                row.update({"modified": stamp, **changes})

    def remove(self, kind: str, row_id: int) -> None:
        self.rows[kind] = [r for r in self.rows[kind] if r["id"] != row_id]

    def add(self, kind: str, row_id: int, slug: str, **extra: Any) -> None:
        self.rows[kind].append(_row(kind, row_id, slug, **extra))

    def totals(self) -> dict[str, int]:
        return {
            "posts": len(self.rows["post"]),
            "pages": len(self.rows["page"]),
            **self.extra_totals,
        }

    # --- http --------------------------------------------------------------------------------

    def _authed(self, request: httpx.Request) -> bool:
        header = request.headers.get("authorization", "")
        if not header.startswith("Basic "):
            return False
        decoded = base64.b64decode(header[6:]).decode()
        return decoded == f"{USER}:{self.password}"

    @staticmethod
    def _json(status: int, body: object, headers: dict[str, str] | None = None) -> httpx.Response:
        return httpx.Response(status, content=json.dumps(body).encode(), headers=headers or {})

    def _public_urls(self) -> list[str]:
        pages = [
            f"{BASE}/{row['slug']}/"
            for kind in ("post", "page")
            for row in self.rows[kind]
            if row["status"] == "publish" and row["slug"] not in self.sitemap_excludes
        ]
        return [f"{BASE}/", *pages, f"{BASE}/category/news/"]

    def _sitemap_xml(self) -> str:
        if self.sitemap is not None:
            return self.sitemap
        entries = "".join(
            f"<url><loc>{u}</loc><lastmod>2026-09-01T10:00:00+00:00</lastmod></url>"
            for u in self._public_urls()
        )
        return f"<urlset>{entries}</urlset>"

    def _public(self, path: str) -> httpx.Response:
        if path.startswith("/wp-content/uploads/"):
            name = path.rsplit("/", 1)[-1]
            media = next((m for m in self.media.values() if m["file"] == name), None)
            if media is None:
                return httpx.Response(404)
            body = b"<svg/>" if media["type"] == "image/svg+xml" else PNG
            return httpx.Response(200, content=body, headers={"content-type": media["type"]})
        if path == "/robots.txt":
            return httpx.Response(
                200, content=f"User-agent: *\nSitemap: {BASE}/sitemap.xml\n".encode()
            )
        if path == "/sitemap.xml":
            return httpx.Response(200, content=self._sitemap_xml().encode())
        if path in self.public_pages:
            return httpx.Response(200, content=self.public_pages[path].encode())
        return httpx.Response(404)

    def _collection(self, collection: str, params: dict[str, str]) -> httpx.Response:
        kind = {"posts": "post", "pages": "page"}.get(collection)
        if collection in self.terms:
            rows: list[Any] = [{"id": i, "name": n} for i, n in self.terms[collection].items()]
        elif collection == "users":
            rows = [{"id": i, "name": n} for i, n in self.users.items()]
        elif kind:
            rows = [r for r in self.rows[kind] if r["status"] != "trash"]
            after = params.get("modified_after")
            if after:
                rows = [r for r in rows if r["modified"] >= after]
            if params.get("_fields") == "id":
                rows = [{"id": r["id"]} for r in self.rows[kind]]
            else:
                rows = [self._post_json(r) for r in rows]
        elif collection == "media" and ("slug" in params or "search" in params):
            term = (params.get("slug") or params.get("search", "")).lower()
            rows = [
                {
                    "id": i,
                    "alt_text": m["alt"],
                    "mime_type": m["type"],
                    "source_url": f"{BASE}/wp-content/uploads/{m['file']}",
                }
                for i, m in self.media.items()
                if term in m["file"].lower()
            ]
        elif collection in self.extra_totals:
            return self._json(200, [], {"x-wp-total": str(self.extra_totals[collection])})
        else:
            return self._json(404, {"code": "rest_no_route"})
        per_page = int(params.get("per_page", "10"))
        page = int(params.get("page", "1"))
        pages = max(1, -(-len(rows) // per_page))
        headers = {"x-wp-total": str(len(rows)), "x-wp-totalpages": str(pages)}
        response = self._json(200, rows[(page - 1) * per_page : page * per_page], headers)
        if self.junk_prefix and kind:
            return httpx.Response(
                200, content=self.junk_prefix.encode() + response.content, headers=headers
            )
        return response

    def _rendered(self, row: dict[str, Any]) -> str:
        text = str(row["content"])
        for media_id, media in self.media.items():
            text = text.replace(f"%ALT{media_id}%", media["alt"])
        return text

    def _post_json(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "type": row["type"],
            "slug": row["slug"],
            "link": f"{BASE}/{row['slug']}/",
            "status": row["status"],
            "date_gmt": row["published"],
            "modified_gmt": row["modified"],
            "author": row["author"],
            "categories": row["categories"],
            "tags": row["tags"],
            "title": {"rendered": row["title"]},
            "content": {"rendered": self._rendered(row)},
            "excerpt": {"rendered": ""},
            "yoast_head_json": {
                "title": row.get("_stale", {}).get("title") or row.get("seo_title") or row["title"],
                "description": row.get("_stale", {}).get("description", row["description"]),
                "canonical": f"{BASE}/{row['slug']}/",
                "robots": {"index": "index", "follow": "follow"},
            },
        }

    def _meta(self, row: dict[str, Any]) -> dict[str, str]:
        if not self.meta_exposed:
            return {}
        meta = {
            "rank_math_title": row.get("seo_title", ""),
            "rank_math_description": row.get("description", ""),
        }
        if self.elementor_exposed:
            meta.update(
                _elementor_data=row.get("_elementor_data", ""),
                _elementor_edit_mode=row.get("_elementor_edit_mode", ""),
                _elementor_template_type=row.get("_elementor_template_type", ""),
                _elementor_version=row.get("_elementor_version", ""),
            )
        return meta

    def _edit_json(self, row: dict[str, Any]) -> dict[str, Any]:
        data = self._post_json(row)
        data["title"] = {"raw": row["title"], "rendered": row["title"]}
        data["content"] = {"raw": str(row["content"]), "rendered": self._rendered(row)}
        data["meta"] = self._meta(row)
        return data

    def _upload(self, request: httpx.Request) -> httpx.Response:
        self.next_id += 1
        name = request.headers.get("content-disposition", "").split('filename="')[-1].rstrip('"')
        self.media[self.next_id] = {
            "alt": "",
            "type": request.headers.get("content-type", ""),
            "file": name,
        }
        self.uploads.append({"id": self.next_id, "file": name, "size": len(request.content)})
        return self._json(
            201,
            {
                "id": self.next_id,
                "alt_text": "",
                "mime_type": request.headers.get("content-type", ""),
                "source_url": f"{BASE}/wp-content/uploads/{name}",
            },
        )

    def _create_post(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        self.next_id += 1
        row = _row("post", self.next_id, body.get("slug") or f"post-{self.next_id}")
        row["content"] = ""
        self.rows["post"].append(row)
        self.created = getattr(self, "created", [])
        self.created.append(body)
        self._apply_post(row, body)
        return self._json(201, self._edit_json(row))

    def _apply_post(self, row: dict[str, Any], body: dict[str, Any]) -> None:
        for key in ("title", "slug", "status", "content"):
            if key in body:
                row[key] = body[key]
        row["featured_media"] = body.get("featured_media", row.get("featured_media"))
        meta = body.get("meta", {})
        for key in ("_elementor_data", "_elementor_edit_mode", "_elementor_template_type"):
            if key in meta:
                row[key] = meta[key]
        if "rank_math_title" in meta:
            row["seo_title"] = meta["rank_math_title"]
        if "rank_math_description" in meta:
            row["description"] = meta["rank_math_description"]
        row["modified"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")

    def _media(self, media_id: int, request: httpx.Request) -> httpx.Response:
        media = self.media.get(media_id)
        if media is None:
            return self._json(404, {"code": "rest_post_invalid_id"})
        if request.method == "POST":
            body = json.loads(request.content or b"{}")
            self.posted.append(body)
            if "alt_text" in body:
                media["alt"] = body["alt_text"]
        return self._json(
            200,
            {
                "id": media_id,
                "alt_text": media["alt"],
                "mime_type": media["type"],
                "source_url": f"{BASE}/wp-content/uploads/{media['file']}",
            },
        )

    def _single(  # noqa: PLR0912 - a test double that mirrors the REST routes
        self, collection: str, request: httpx.Request
    ) -> httpx.Response | None:
        """GET or POST of one item: ``posts/<id>``."""
        base, _, raw_id = collection.partition("/")
        if base == "media" and raw_id.isdigit():
            return self._media(int(raw_id), request)
        kind = {"posts": "post", "pages": "page"}.get(base)
        if kind is None or not raw_id.isdigit():
            return None
        row = next((r for r in self.rows[kind] if r["id"] == int(raw_id)), None)
        if row is None:
            return self._json(404, {"code": "rest_post_invalid_id"})
        if request.method == "DELETE":
            row["status"] = "trash"
            return self._json(200, {"deleted": False, "previous": self._edit_json(row)})
        if request.method == "POST":
            body = json.loads(request.content or b"{}")
            self.posted.append(body)
            for key in ("title", "slug", "status", "content"):
                if key in body:
                    row[key] = body[key]
            meta = body.get("meta", {})
            for key in ("_elementor_data", "_elementor_edit_mode", "_elementor_template_type"):
                if key in meta:
                    row[key] = meta[key]
            if self.stale_seo and "_stale" not in row:
                row["_stale"] = {
                    "title": row.get("seo_title") or row["title"],
                    "description": row["description"],
                }
            if self.meta_exposed:
                if "rank_math_title" in meta:
                    row["seo_title"] = meta["rank_math_title"]
                if "rank_math_description" in meta:
                    row["description"] = meta["rank_math_description"]
            row["modified"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        return self._json(200, self._edit_json(row))

    def handler(  # noqa: PLR0912 - a test double that mirrors the REST routes
        self, request: httpx.Request
    ) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        self.queries.append(dict(request.url.params))
        if not path.startswith("/wp-json"):
            return self._public(path)
        if self.rest_disabled:
            return self._json(404, {"code": "rest_no_route"})
        route = path[len("/wp-json") :] or "/"
        if route == "/":
            return self._json(
                200,
                {
                    "name": "Acme IT",
                    "description": "Managed IT",
                    "home": BASE,
                    "timezone_string": "America/Toronto",
                    "gmt_offset": -5,
                    "namespaces": self.namespaces,
                },
            )
        if not self._authed(request):
            return self._json(401, {"code": "rest_not_logged_in"})
        if route == "/wp/v2/users/me":
            return self._json(
                200,
                {
                    "id": 2,
                    "name": "Editor",
                    "roles": ["editor"],
                    "capabilities": self.capabilities,
                },
            )
        if route == "/wp/v2/settings":
            return self._json(
                200, {"title": "Acme IT", "language": "en_CA", "timezone": "America/Toronto"}
            )
        if route == "/wp/v2/plugins":
            return self._json(
                200,
                [
                    {"name": "Rank Math SEO", "version": "1.0.230", "status": "active"},
                    {"plugin": "elementor/elementor", "version": "3.25.4", "status": "active"},
                ],
            )
        if route == "/wp/v2/types":
            return self._json(
                200,
                {
                    "post": {"rest_base": "posts"},
                    "page": {"rest_base": "pages"},
                    "attachment": {"rest_base": "media"},
                    "elementor_library": {"rest_base": "elementor_library"},
                },
            )
        collection = route.removeprefix("/wp/v2/")
        if collection == "media" and request.method == "POST":
            return self._upload(request)
        if collection == "posts" and request.method == "POST":
            return self._create_post(request)
        single = self._single(collection, request)
        if single is not None:
            return single
        params = request.url.params
        if (
            collection in {"posts", "pages"}
            and params.get("per_page") == "1"
            and "meta" in params.get("_fields", "")
        ):
            row = self.rows["post" if collection == "posts" else "page"][0]
            return self._json(200, [{"id": row["id"], "meta": self._meta(row)}])
        if collection in {"posts", "pages"} and request.url.params.get("per_page") == "1":
            return self._json(200, [], {"x-wp-total": str(self.totals()[collection])})
        return self._collection(collection, dict(request.url.params))
