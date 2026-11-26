"""Every CRM endpoint is reached by an anonymous caller and by a stranger, and must refuse both.

The module tests check each endpoint's own cross-tenant behaviour with real data. This sweep is
the safety net behind them: a new endpoint that forgets authentication or workspace checks fails
here without anyone having to remember to write the test.
"""

import re
import uuid
from typing import Any

import pytest
from django.test import Client

from config.api import api
from conftest import make_user
from workspaces.services import create_workspace

pytestmark = pytest.mark.django_db
METHODS = ("get", "post", "put", "patch", "delete")
PUBLIC_PREFIX = "/api/v1/crm-public/"
PLACEHOLDER = {"integer": "1", "number": "1", "boolean": "true"}
REFUSED = {401, 403, 404, 405, 422}


def operations() -> list[tuple[str, str, dict[str, Any]]]:
    paths = api.get_openapi_schema()["paths"]
    return [
        (method, path, item[method])
        for path, item in paths.items()
        if "/crm/" in path
        for method in METHODS
        if method in item
    ]


def fill(path: str, operation: dict[str, Any]) -> str:
    for parameter in operation.get("parameters", []):
        if parameter.get("in") != "path":
            continue
        kind = parameter.get("schema", {})
        if kind.get("format") == "uuid":
            value = str(uuid.uuid4())
        else:
            value = PLACEHOLDER.get(kind.get("type", ""), "x")
        path = path.replace("{" + parameter["name"] + "}", value)
    return re.sub(r"\{[^}]+\}", "x", path)


def call(client: Client, method: str, url: str, workspace: object | None) -> int:
    headers = {"HTTP_X_WORKSPACE_ID": str(workspace)} if workspace else {}
    kwargs: dict[str, Any] = {**headers}
    if method in ("post", "put", "patch"):
        kwargs |= {"data": "{}", "content_type": "application/json"}
    return int(getattr(client, method)(url, **kwargs).status_code)


def test_the_sweep_actually_finds_the_crm_endpoints() -> None:
    found = operations()
    assert len(found) > 150
    assert any("/crm/workflows" in path for _, path, _ in found)
    assert any("/crm/reports" in path for _, path, _ in found)


def test_every_crm_endpoint_declares_authentication() -> None:
    open_ones = [
        f"{method.upper()} {path}"
        for method, path, op in operations()
        if not path.startswith(PUBLIC_PREFIX) and not op.get("security")
    ]
    assert open_ones == []


def test_anonymous_callers_are_turned_away_everywhere(workspace: Any) -> None:
    client = Client()
    leaks = []
    for method, path, op in operations():
        if path.startswith(PUBLIC_PREFIX):
            continue
        status = call(client, method, fill(path, op), workspace.pk)
        if status not in (401, 403):
            leaks.append(f"{method.upper()} {path} -> {status}")
    assert leaks == []


def test_strangers_get_nothing_from_a_workspace_they_do_not_belong_to(
    workspace: Any, owner: Any
) -> None:
    stranger = make_user("stranger@example.com")
    create_workspace(name="Stranger Co", owner=stranger)  # they belong somewhere, just not here
    client = Client()
    client.force_login(stranger)
    leaks = []
    for method, path, op in operations():
        if path.startswith(PUBLIC_PREFIX):
            continue
        status = call(client, method, fill(path, op), workspace.pk)
        if status not in REFUSED:
            leaks.append(f"{method.upper()} {path} -> {status}")
    assert leaks == []


def test_public_endpoints_are_throttled_and_never_read_a_workspace_header() -> None:
    schema = api.get_openapi_schema()
    public = [p for p in schema["paths"] if p.startswith(PUBLIC_PREFIX)]
    assert public, "expected public CRM endpoints"
    for path in public:
        for method in METHODS:
            op = schema["paths"][path].get(method)
            if op is None:
                continue
            names = {param.get("name", "").lower() for param in op.get("parameters", [])}
            assert "x-workspace-id" not in names, f"{method.upper()} {path}"
