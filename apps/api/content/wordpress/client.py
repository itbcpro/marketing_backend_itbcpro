"""Read client for the WordPress REST API, authenticated with an application password.

All requests go through ``safe_fetch`` (public https hosts only, pinned IP, size cap). Failures
become domain errors with a stable ``reason`` so the setup screen can say what to fix.
"""

import base64
import json
import re
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx

from connections.models import Connection, CredentialKind
from connections.services import read_secret
from content import net
from core.errors import (
    AuthExpiredError,
    PermanentProviderError,
    RetryableProviderError,
)
from core.security import UnsafeUrlError

PROVIDER = "wordpress"
HTTP_ERROR = 400
HTTP_NOT_FOUND = 404
HTTP_TOO_MANY = 429
HTTP_SERVER_ERROR = 500
TIMEOUT = 30.0
UPLOAD_TIMEOUT = 120.0
MAX_BYTES = 4_000_000
ELEMENTOR_SLUG = "elementor/elementor"

SEO_NAMESPACES = {
    "rankmath": "Rank Math",
    "yoast": "Yoast SEO",
    "seopress": "SEOPress",
    "aioseo": "All in One SEO",
}
MULTILINGUAL_NAMESPACES = {"pll": "Polylang", "wpml": "WPML", "translatepress": "TranslatePress"}
BUILDER_NAMESPACES = {"elementor": "Elementor", "wp-block-editor": "Block editor"}
SKIPPED_TYPES = {
    "attachment", "nav_menu_item", "wp_block", "wp_template", "wp_template_part",
    "wp_navigation", "wp_global_styles", "wp_font_family", "wp_font_face", "revision",
}  # fmt: skip
PAGE_SIZE = 20
ID_PAGE_SIZE = 100
MEDIA_FIELDS = "id,alt_text,source_url,mime_type"
EDIT_FIELDS = "id,type,slug,link,status,modified_gmt,title,excerpt,meta,yoast_head_json"
POST_FIELDS = (
    "id,slug,link,type,status,date_gmt,modified_gmt,author,categories,tags,lang,"
    "title,content,excerpt,yoast_head_json"
)
COUNTED_COLLECTIONS = ("posts", "pages", "categories", "tags", "media", "comments")


JSON_START = re.compile(r'\[\s*\{\s*"|\{\s*"|\[\s*\]')
MAX_JSON_STARTS = 25


def parse_json(content: bytes) -> Any:
    """Parse a REST body, tolerating output a plugin printed before the JSON.

    Page builders such as Elementor can echo ``<style>`` blocks ahead of the response while
    rendering content, which makes the body invalid JSON although the data is intact.
    """
    try:
        return json.loads(content)
    except ValueError:
        pass
    text = content.decode("utf-8", errors="replace")
    decoder = json.JSONDecoder()
    for match in list(JSON_START.finditer(text))[:MAX_JSON_STARTS]:
        try:
            value, _ = decoder.raw_decode(text, match.start())
        except ValueError:
            continue
        return value
    raise ValueError("no JSON found")


def normalize_site_url(value: str) -> str:
    """The site origin without a trailing slash or path: ``https://example.com``."""
    text = value.strip()
    if "://" not in text:
        text = f"https://{text}"
    parts = urlsplit(text)
    if parts.scheme != "https" or not parts.hostname:
        raise PermanentProviderError("the site address must be an https URL", provider=PROVIDER)
    return f"{parts.scheme}://{parts.netloc.lower()}"


def basic_header(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {token}"


class WordPressClient:
    def __init__(
        self,
        site_url: str,
        *,
        username: str = "",
        password: str = "",
    ) -> None:
        self.site_url = normalize_site_url(site_url)
        self._headers = {"Accept": "application/json"}
        if username and password:
            self._headers["Authorization"] = basic_header(username, password)

    @classmethod
    def for_connection(cls, connection: Connection) -> WordPressClient:
        form = connection.config.get("form", {})
        return cls(
            str(form.get("site_url") or connection.external_account_id),
            username=str(form.get("username", "")),
            password=read_secret(connection, CredentialKind.ACCESS_TOKEN),
        )

    # --- transport ---------------------------------------------------------------------------

    def _get(self, path: str, **params: Any) -> httpx.Response:
        return self._request("GET", path, params)

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any],
        body: Any = None,
        *,
        raw: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        query = f"?{urlencode(params)}" if params else ""
        url = f"{self.site_url}/wp-json{path}{query}"
        try:
            response = net.fetch(
                url,
                headers={**self._headers, **(headers or {})},
                timeout=UPLOAD_TIMEOUT if raw is not None else TIMEOUT,
                max_bytes=MAX_BYTES,
                method=method,
                json_body=body,
                content=raw,
            )
        except UnsafeUrlError as exc:
            raise PermanentProviderError(str(exc), provider=PROVIDER) from exc
        except httpx.TimeoutException as exc:
            raise RetryableProviderError(
                "the site did not answer in time", provider=PROVIDER
            ) from exc
        except httpx.HTTPError as exc:
            raise RetryableProviderError(
                "the site could not be reached", provider=PROVIDER
            ) from exc
        if response.status_code < HTTP_ERROR:
            return response
        raise self._error(response)

    @staticmethod
    def _error(response: httpx.Response) -> Exception:
        code = ""
        try:
            body = json.loads(response.content)
            code = str(body.get("code", "")) if isinstance(body, dict) else ""
        except ValueError:
            pass
        status = response.status_code
        if status in (401, 403) or code in {"incorrect_password", "invalid_username"}:
            return AuthExpiredError(code or "unauthorised", provider=PROVIDER)
        if status == HTTP_NOT_FOUND or code == "rest_no_route":
            return PermanentProviderError("rest_not_found", provider=PROVIDER)
        if status == HTTP_TOO_MANY or status >= HTTP_SERVER_ERROR:
            return RetryableProviderError(f"site returned {status}", provider=PROVIDER)
        return PermanentProviderError(code or f"site returned {status}", provider=PROVIDER)

    def _json(self, path: str, **params: Any) -> Any:
        try:
            return parse_json(self._get(path, **params).content)
        except ValueError as exc:
            raise PermanentProviderError(
                "the site returned invalid JSON", provider=PROVIDER
            ) from exc

    # --- reads -------------------------------------------------------------------------------

    def root(self) -> dict[str, Any]:
        """Public index of the REST API: name, timezone and the installed route namespaces."""
        data = self._json("/")
        if not isinstance(data, dict) or "namespaces" not in data:
            raise PermanentProviderError("rest_not_found", provider=PROVIDER)
        return data

    def me(self) -> dict[str, Any]:
        data = self._json("/wp/v2/users/me", context="edit")
        return data if isinstance(data, dict) else {}

    def settings(self) -> dict[str, Any]:
        data = self._json("/wp/v2/settings")
        return data if isinstance(data, dict) else {}

    def plugins(self) -> list[dict[str, Any]]:
        data = self._json("/wp/v2/plugins", per_page=100)
        return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []

    def total(self, collection: str) -> int:
        """Item count of a collection, read from the ``X-WP-Total`` header of a one-row page."""
        params: dict[str, Any] = {"per_page": 1, "_fields": "id"}
        if collection in {"posts", "pages", "media"}:
            params["status"] = "any" if collection != "media" else "inherit"
        response = self._get(f"/wp/v2/{collection}", **params)
        return int(response.headers.get("x-wp-total", "0") or 0)

    def item(self, rest_base: str, item_id: int) -> dict[str, Any]:
        """One item as the editor sees it (raw title and excerpt, registered meta, SEO data)."""
        data = self._json(f"/wp/v2/{rest_base}/{item_id}", context="edit", _fields=EDIT_FIELDS)
        return data if isinstance(data, dict) else {}

    def item_body(self, rest_base: str, item_id: int) -> dict[str, Any]:
        """One item with its raw body (as stored), status and page-builder markers."""
        data = self._json(
            f"/wp/v2/{rest_base}/{item_id}",
            context="edit",
            _fields="id,status,modified_gmt,content,meta",
        )
        return data if isinstance(data, dict) else {}

    def item_full(self, rest_base: str, item_id: int) -> dict[str, Any]:
        """One item with its rendered content, as the inventory sync reads it."""
        data = self._json(f"/wp/v2/{rest_base}/{item_id}", _fields=POST_FIELDS)
        return data if isinstance(data, dict) else {}

    def media(self, media_id: int) -> dict[str, Any]:
        """One media library entry (address, type and alt text)."""
        data = self._json(f"/wp/v2/media/{media_id}", context="edit", _fields=MEDIA_FIELDS)
        return data if isinstance(data, dict) else {}

    def find_media(self, **query: str) -> list[dict[str, Any]]:
        """Media library entries matching ``slug=`` or ``search=`` (address and alt text)."""
        rows, _pages = self._paged("/wp/v2/media", 1, 20, _fields=MEDIA_FIELDS, **query)
        return [r for r in rows if isinstance(r, dict)]

    def set_media_alt(self, media_id: int, alt_text: str) -> dict[str, Any]:
        response = self._request(
            "POST",
            f"/wp/v2/media/{media_id}",
            {"context": "edit", "_fields": MEDIA_FIELDS},
            {"alt_text": alt_text},
        )
        try:
            data = parse_json(response.content)
        except ValueError as exc:
            raise PermanentProviderError(
                "the site returned invalid JSON", provider=PROVIDER
            ) from exc
        return data if isinstance(data, dict) else {}

    def update_item(self, rest_base: str, item_id: int, body: dict[str, Any]) -> dict[str, Any]:
        """Change fields of one item (title, slug, excerpt, meta). Returns the saved item."""
        response = self._request(
            "POST",
            f"/wp/v2/{rest_base}/{item_id}",
            {"context": "edit", "_fields": EDIT_FIELDS},
            body,
        )
        try:
            data = parse_json(response.content)
        except ValueError as exc:
            raise PermanentProviderError(
                "the site returned invalid JSON", provider=PROVIDER
            ) from exc
        return data if isinstance(data, dict) else {}

    def _object(self, response: httpx.Response) -> dict[str, Any]:
        try:
            data = parse_json(response.content)
        except ValueError as exc:
            raise PermanentProviderError(
                "the site returned invalid JSON", provider=PROVIDER
            ) from exc
        return data if isinstance(data, dict) else {}

    def upload_media(self, filename: str, mime_type: str, data: bytes) -> dict[str, Any]:
        """Add a file to the media library. Returns the new entry (id and address)."""
        response = self._request(
            "POST",
            "/wp/v2/media",
            {"context": "edit", "_fields": MEDIA_FIELDS},
            raw=data,
            headers={
                "Content-Type": mime_type,
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )
        return self._object(response)

    def update_media(self, media_id: int, body: dict[str, Any]) -> dict[str, Any]:
        """Change alt text, caption or title of a media library entry."""
        response = self._request(
            "POST", f"/wp/v2/media/{media_id}", {"context": "edit", "_fields": MEDIA_FIELDS}, body
        )
        return self._object(response)

    def create_item(self, rest_base: str, body: dict[str, Any]) -> dict[str, Any]:
        """Create a post or page. Returns the saved item."""
        response = self._request(
            "POST", f"/wp/v2/{rest_base}", {"context": "edit", "_fields": EDIT_FIELDS}, body
        )
        return self._object(response)

    def trash_item(self, rest_base: str, item_id: int) -> None:
        """Move an item to the trash (it can still be restored in WordPress)."""
        self._request("DELETE", f"/wp/v2/{rest_base}/{item_id}", {})

    def elementor_version(self) -> str:
        for row in self.plugins():
            if row.get("plugin") == ELEMENTOR_SLUG:
                return str(row.get("version", ""))
        return ""

    def exposed_meta_keys(self, rest_base: str) -> set[str]:
        """Meta keys a REST client can read and write for this content type."""
        rows = self._json(f"/wp/v2/{rest_base}", context="edit", per_page=1, _fields="id,meta")
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            meta = rows[0].get("meta")
            return set(meta) if isinstance(meta, dict) else set()
        return set()

    def post_types(self) -> list[tuple[str, str]]:
        """``(type, rest_base)`` of every readable content type that holds public pages."""
        data = self._json("/wp/v2/types")
        found: list[tuple[str, str]] = []
        for slug, row in data.items() if isinstance(data, dict) else []:
            base = str((row or {}).get("rest_base") or "")
            if base and slug not in SKIPPED_TYPES and not slug.startswith("elementor_"):
                found.append((slug, base))
        return sorted(found)

    def _paged(self, path: str, page: int, per_page: int, **params: Any) -> tuple[list[Any], int]:
        response = self._get(path, page=page, per_page=per_page, **params)
        try:
            rows = parse_json(response.content)
        except ValueError as exc:
            raise PermanentProviderError(
                "the site returned invalid JSON", provider=PROVIDER
            ) from exc
        pages = int(response.headers.get("x-wp-totalpages", "1") or 1)
        return (rows if isinstance(rows, list) else []), pages

    def list_ids(self, rest_base: str) -> set[int]:
        """Every id of a collection that is not in the trash (used to detect deletions)."""
        found: set[int] = set()
        page, pages = 1, 1
        while page <= pages:
            rows, pages = self._paged(
                f"/wp/v2/{rest_base}", page, ID_PAGE_SIZE, _fields="id", status="any"
            )
            found.update(int(r["id"]) for r in rows if isinstance(r, dict) and "id" in r)
            page += 1
        return found

    def posts_page(
        self, rest_base: str, page: int, *, modified_after: str = ""
    ) -> tuple[list[dict[str, Any]], int]:
        params: dict[str, Any] = {"_fields": POST_FIELDS, "status": "any", "orderby": "modified"}
        if modified_after:
            params["modified_after"] = modified_after
        rows, pages = self._paged(f"/wp/v2/{rest_base}", page, PAGE_SIZE, **params)
        return [r for r in rows if isinstance(r, dict)], pages

    def names(self, rest_base: str) -> dict[int, str]:
        """``{id: name}`` of a taxonomy or of the users who wrote content."""
        found: dict[int, str] = {}
        page, pages = 1, 1
        try:
            while page <= pages:
                rows, pages = self._paged(
                    f"/wp/v2/{rest_base}", page, ID_PAGE_SIZE, _fields="id,name"
                )
                found.update({int(r["id"]): str(r.get("name", "")) for r in rows if "id" in r})
                page += 1
        except AuthExpiredError, PermanentProviderError:
            pass
        return found

    def counts(self) -> dict[str, int | None]:
        """Counts per collection; ``None`` when this account cannot read the collection."""
        out: dict[str, int | None] = {}
        for name in COUNTED_COLLECTIONS:
            try:
                out[name] = self.total(name)
            except AuthExpiredError, PermanentProviderError:
                out[name] = None
        return out


def detect_plugins(namespaces: list[str]) -> dict[str, str]:
    """Which well-known plugin families the REST namespaces reveal."""
    found: dict[str, str] = {}
    roots = {ns.split("/")[0].lower() for ns in namespaces}
    for group, table in (
        ("seo", SEO_NAMESPACES),
        ("multilingual", MULTILINGUAL_NAMESPACES),
        ("builder", BUILDER_NAMESPACES),
    ):
        for key, label in table.items():
            if key in roots:
                found[group] = label
                break
    return found
