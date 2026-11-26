import uuid

import pytest
from django.apps import apps
from django.db import ProgrammingError, connection, models, transaction
from django.db.migrations import CreateModel
from django.db.migrations.state import ProjectState
from django.test import Client

from conftest import make_user
from core import tenancy
from core.db_roles import ensure_runtime_role
from core.migration_ops import EnableRowLevelSecurity
from core.models import TenantModel
from core.rls_legacy import LEGACY_TABLES_WITHOUT_RLS
from core.tasks import tenant_task
from workspaces.models import Organization, Workspace
from workspaces.services import create_workspace

pytestmark = [pytest.mark.django_db]
TABLE = "rls_probe"


@pytest.fixture(autouse=True)
def _clean_context():
    tenancy.clear()
    yield
    tenancy.clear()


@pytest.fixture
def probe():
    """A throwaway tenant table protected by the same operation real migrations use."""
    with connection.cursor() as cursor:
        role = ensure_runtime_role(cursor)
    empty = ProjectState()
    create = CreateModel(
        "RlsProbe",
        [
            ("id", models.UUIDField(primary_key=True)),
            ("workspace_id", models.UUIDField()),
            ("note", models.TextField(default="")),
        ],
        options={"db_table": TABLE},
    )
    after = empty.clone()
    create.state_forwards("core_probe", after)
    with connection.schema_editor() as editor:
        create.database_forwards("core_probe", editor, empty, after)
        EnableRowLevelSecurity("RlsProbe").database_forwards("core_probe", editor, after, after)
    with connection.cursor() as cursor:
        cursor.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON "{TABLE}" TO "{role}"')
    return role


def _as_runtime(role: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute(f'SET ROLE "{role}"')


def _owner() -> None:
    with connection.cursor() as cursor:
        cursor.execute("RESET ROLE")


def _insert(workspace_id: uuid.UUID, note: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            f'INSERT INTO "{TABLE}" (id, workspace_id, note) VALUES (%s, %s, %s)',
            [uuid.uuid4(), workspace_id, note],
        )


def _visible() -> list[str]:
    with connection.cursor() as cursor:
        cursor.execute(f'SELECT note FROM "{TABLE}" ORDER BY note')
        return [row[0] for row in cursor.fetchall()]


def test_no_context_means_no_rows(probe):
    a, b = uuid.uuid4(), uuid.uuid4()
    with tenancy.use_workspace(a):
        _as_runtime(probe)
        _insert(a, "a1")
    _as_runtime(probe)
    try:
        assert _visible() == []
        with tenancy.use_workspace(b):
            assert _visible() == []
    finally:
        _owner()


def test_each_workspace_sees_only_its_rows(probe):
    a, b = uuid.uuid4(), uuid.uuid4()
    _as_runtime(probe)
    try:
        with tenancy.use_workspace(a):
            _insert(a, "a1")
            _insert(a, "a2")
        with tenancy.use_workspace(b):
            _insert(b, "b1")
        with tenancy.use_workspace(a):
            assert _visible() == ["a1", "a2"]
        with tenancy.use_workspace(b):
            assert _visible() == ["b1"]
    finally:
        _owner()


def test_cannot_write_rows_of_another_workspace(probe):
    a, b = uuid.uuid4(), uuid.uuid4()
    _as_runtime(probe)
    try:
        with tenancy.use_workspace(a), pytest.raises(ProgrammingError), transaction.atomic():
            _insert(b, "stolen")
    finally:
        _owner()


def test_cannot_move_a_row_into_another_workspace(probe):
    a, b = uuid.uuid4(), uuid.uuid4()
    _as_runtime(probe)
    try:
        with tenancy.use_workspace(a):
            _insert(a, "a1")
            with pytest.raises(ProgrammingError), transaction.atomic(), connection.cursor() as c:
                c.execute(f'UPDATE "{TABLE}" SET workspace_id = %s', [b])
    finally:
        _owner()


def test_context_is_restored_after_the_block():
    a, b = uuid.uuid4(), uuid.uuid4()
    with tenancy.use_workspace(a):
        with tenancy.use_workspace(b):
            assert tenancy.current_workspace_id() == b
        assert tenancy.current_workspace_id() == a
    assert tenancy.current_workspace_id() is None
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_setting(%s, true)", [tenancy.SESSION_VARIABLE])
        assert cursor.fetchone()[0] in {"", None}


def test_middleware_never_leaks_a_workspace_between_requests(owner, workspace):
    client = Client()
    client.force_login(owner)
    response = client.get("/api/v1/actions/inbox", HTTP_X_WORKSPACE_ID=str(workspace.pk))
    assert response.status_code == 200
    assert tenancy.current_workspace_id() is None


def test_tenant_task_requires_workspace_and_enters_context():
    seen: list[uuid.UUID | None] = []

    @tenant_task("core.tests.tenant_probe")
    def probe_task(self, *, workspace_id):
        seen.append(tenancy.current_workspace_id())

    ws = uuid.uuid4()
    probe_task.apply(kwargs={"workspace_id": str(ws)}).get()
    assert seen == [ws]
    assert tenancy.current_workspace_id() is None
    with pytest.raises(ValueError, match="workspace_id"):
        probe_task.apply()


def test_every_new_tenant_table_must_enable_rls():
    """A table that extends TenantModel needs RLS unless it predates it (frozen list)."""
    unprotected: list[str] = []
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT c.relname FROM pg_class c WHERE c.relrowsecurity AND c.relforcerowsecurity"
        )
        protected = {row[0] for row in cursor.fetchall()}
    for model in apps.get_models():
        table = model._meta.db_table
        if (
            issubclass(model, TenantModel)
            and table not in LEGACY_TABLES_WITHOUT_RLS
            and table not in protected
        ):
            unprotected.append(table)
    assert unprotected == [], f"enable row level security for: {unprotected}"


def test_legacy_list_has_no_stale_entries():
    tables = {m._meta.db_table for m in apps.get_models() if issubclass(m, TenantModel)}
    assert tables >= LEGACY_TABLES_WITHOUT_RLS


def test_workspace_creation_creates_its_organization(owner):
    workspace = create_workspace(name="Fresh Studio", owner=owner)
    assert workspace.organization is not None
    assert workspace.organization.name == "Fresh Studio"
    assert workspace.organization.plan_key == "free"


def test_workspaces_can_share_one_organization(owner):
    first = create_workspace(name="Brand One", owner=owner)
    second = create_workspace(name="Brand Two", owner=owner, organization=first.organization)
    assert second.organization_id == first.organization_id
    assert Organization.objects.filter(pk=first.organization_id).count() == 1
    assert Workspace.objects.filter(organization=first.organization).count() == 2


def test_organization_slugs_stay_unique(owner):
    make_user("second-owner@example.com")
    a = create_workspace(name="Same Name", owner=owner, slug="same-name")
    assert Organization.objects.filter(slug="same-name").exists()
    Workspace.objects.filter(pk=a.pk).delete()
    b = create_workspace(name="Same Name", owner=owner, slug="same-name")
    assert b.organization.slug == "same-name-2"
