import uuid
from datetime import UTC, datetime

import pytest
from django.test import RequestFactory

from audit import services as audit_services
from audit.actors import Actor
from audit.tasks import dispatch_outbox, verify_chain
from conftest import make_user
from core.errors import ErrorCode, PermissionDeniedError, ValidationFailedError
from identity.models import User
from workspaces.deps import WORKSPACE_HEADER, resolve_workspace
from workspaces.models import Role, Workspace
from workspaces.permissions import Permission

pytestmark = pytest.mark.django_db


def _request(user: object, workspace_id: object | None):
    headers = {}
    if workspace_id is not None:
        headers["HTTP_" + WORKSPACE_HEADER.upper().replace("-", "_")] = str(workspace_id)
    request = RequestFactory().get("/x", **headers)
    request.user = user
    return request


def test_resolves_workspace_for_member(owner: User, workspace: Workspace) -> None:
    membership = resolve_workspace(_request(owner, workspace.pk))
    assert membership.workspace_id == workspace.pk and membership.role == Role.OWNER


@pytest.mark.parametrize("header", [None, "not-a-uuid"])
def test_missing_or_malformed_header_is_a_validation_error(owner: User, header: str | None) -> None:
    with pytest.raises(ValidationFailedError) as info:
        resolve_workspace(_request(owner, header))
    assert info.value.code == ErrorCode.WORKSPACE_REQUIRED


def test_unknown_and_foreign_workspaces_are_indistinguishable(
    owner: User, workspace: Workspace
) -> None:
    stranger = make_user("stranger@example.com")
    for target in (uuid.uuid4(), workspace.pk):
        with pytest.raises(ValidationFailedError) as info:
            resolve_workspace(_request(stranger, target))
        assert info.value.code == ErrorCode.WORKSPACE_REQUIRED


def test_insufficient_role_is_forbidden(workspace: Workspace) -> None:
    viewer = make_user("viewer@example.com", workspace, Role.VIEWER)
    with pytest.raises(PermissionDeniedError):
        resolve_workspace(_request(viewer, workspace.pk), Permission.APPROVE_R1)


def test_anonymous_user_is_forbidden(workspace: Workspace) -> None:
    from django.contrib.auth.models import AnonymousUser

    with pytest.raises(PermissionDeniedError):
        resolve_workspace(_request(AnonymousUser(), workspace.pk))


def test_scheduled_audit_and_outbox_tasks_run() -> None:
    ws = uuid.uuid4()
    audit_services.record_event(action="one", actor=Actor.system("t"), workspace_id=ws)
    assert verify_chain() == 0
    assert dispatch_outbox() == 0


def test_verify_chain_task_reports_broken_chains() -> None:
    from django.db import connection

    ws = uuid.uuid4()
    event = audit_services.record_event(
        action="one", actor=Actor.system("t"), workspace_id=ws, after={"v": 1}
    )
    with connection.cursor() as cursor:
        cursor.execute("ALTER TABLE audit_auditevent DISABLE TRIGGER audit_event_no_update_delete")
        cursor.execute(
            "UPDATE audit_auditevent SET occurred_at = %s WHERE id = %s",
            [datetime(2000, 1, 1, tzinfo=UTC), event.pk],
        )
        cursor.execute("ALTER TABLE audit_auditevent ENABLE TRIGGER audit_event_no_update_delete")
    assert verify_chain() == 1
