import hashlib
import json
import uuid

import pytest
from django.http import HttpRequest, HttpResponse
from django.test import Client, RequestFactory

from core.api.pagination import decode_cursor, encode_cursor, paginate
from core.errors import ValidationFailedError
from core.middleware import IdempotencyMiddleware
from core.models import IdempotencyRecord
from identity.models import User

pytestmark = pytest.mark.django_db
URL = "/api/v1/auth/logout"


def _post(client: Client, key: str | None, body: dict[str, object] | None = None):
    extra = {"HTTP_IDEMPOTENCY_KEY": key} if key else {}
    return client.post(URL, data=json.dumps(body or {}), content_type="application/json", **extra)


def test_replay_returns_stored_response_and_marks_it(owner: User) -> None:
    client = Client()
    client.force_login(owner)
    first = _post(client, "key-1")
    assert first.status_code == 204
    client.force_login(owner)
    replay = _post(client, "key-1")
    assert replay.status_code == 204 and replay["Idempotent-Replayed"] == "true"
    assert IdempotencyRecord.objects.count() == 1


def test_same_key_with_different_body_is_rejected(owner: User) -> None:
    client = Client()
    client.force_login(owner)
    _post(client, "key-2", {"a": 1})
    client.force_login(owner)
    conflict = _post(client, "key-2", {"a": 2})
    assert conflict.status_code == 422
    assert conflict.json()["code"] == "idempotency_key_reused"


def test_in_progress_key_returns_conflict(owner: User) -> None:
    client = Client()
    client.force_login(owner)
    body = json.dumps({})
    digest = hashlib.sha256(f"POST\n{URL}\n".encode() + body.encode()).hexdigest()
    IdempotencyRecord.objects.create(
        user_id=owner.pk,
        key="key-3",
        method="POST",
        path=URL,
        request_hash=digest,
        state=IdempotencyRecord.State.IN_PROGRESS,
    )
    response = _post(client, "key-3")
    assert response.status_code == 409
    assert response.json()["code"] == "idempotency_in_progress"


def test_keys_are_scoped_per_user(owner: User) -> None:
    other = User.objects.create_user("other@example.com", "another-long-password-1")
    for user in (owner, other):
        client = Client()
        client.force_login(user)
        assert _post(client, "shared-key").status_code == 204
    assert IdempotencyRecord.objects.count() == 2


def test_no_key_means_no_record(owner: User) -> None:
    client = Client()
    client.force_login(owner)
    _post(client, None)
    assert IdempotencyRecord.objects.count() == 0


def test_cursor_round_trip_and_rejects_garbage() -> None:
    value = uuid.uuid7()
    assert decode_cursor(encode_cursor(value)) == value
    with pytest.raises(ValidationFailedError):
        decode_cursor("not-a-cursor!!")


def test_pagination_walks_all_rows_in_creation_order() -> None:
    users = [
        User.objects.create_user(f"u{i}@example.com", "long-password-value-1") for i in range(7)
    ]
    queryset = User.objects.all()
    seen: list[User] = []
    cursor: str | None = None
    while True:
        page, cursor = paginate(queryset, cursor=cursor, limit=3)
        seen.extend(page)
        if cursor is None:
            break
    assert [u.pk for u in seen] == [u.pk for u in users]


def test_crashing_request_releases_the_key(owner: User) -> None:
    factory = RequestFactory()

    def crash(request: HttpRequest) -> HttpResponse:
        raise RuntimeError("simulated crash")

    def request_with_key() -> HttpRequest:
        request = factory.post(URL, data="{}", content_type="application/json")
        request.headers  # noqa: B018 - force header parsing
        request.META["HTTP_IDEMPOTENCY_KEY"] = "crash-key"
        request.user = owner
        return request

    with pytest.raises(RuntimeError, match="simulated crash"):
        IdempotencyMiddleware(crash)(request_with_key())
    assert IdempotencyRecord.objects.filter(key="crash-key").count() == 0

    ok = IdempotencyMiddleware(lambda r: HttpResponse(status=204))(request_with_key())
    assert ok.status_code == 204
