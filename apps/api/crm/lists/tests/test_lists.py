import json
import uuid

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from audit.actors import Actor
from conftest import make_user
from core.errors import ConflictError, ValidationFailedError
from crm.consent import services as consent
from crm.contacts import services as contacts
from crm.fields import services as fields
from crm.lists import bulk, csvio, imports
from crm.lists.models import BulkJob, ImportJob
from workspaces.models import Membership, Role
from workspaces.services import create_workspace

pytestmark = pytest.mark.django_db
SYSTEM = Actor.system("test")
CSV = (
    "First Name,Last Name,E-mail,Mobile,Tags,Score,Notes\n"
    "Ada,Lovelace,ada@example.com,416-555-0199,vip;warm,90,hello\n"
    "Bob,,bob@example.com,,warm,40,\n"
    "Bad,Row,not-an-email,,,,\n"
    ",,,,,,\n"
    "Ada,Again,ADA@example.com,,,95,\n"
)


def login(user):
    client = Client()
    client.force_login(user)
    return client


def call(client, method, path, workspace, body=None):
    kwargs = {"HTTP_X_WORKSPACE_ID": str(workspace.pk)}
    if body is not None:
        kwargs |= {"data": json.dumps(body), "content_type": "application/json"}
    return getattr(client, method)(f"/api/v1/crm{path}", **kwargs)


def upload(client, workspace, text=CSV, name="people.csv"):
    file = SimpleUploadedFile(name, text.encode(), content_type="text/csv")
    return client.post("/api/v1/crm/imports", {"file": file}, HTTP_X_WORKSPACE_ID=str(workspace.pk))


def membership(workspace, user):
    return Membership.objects.get(workspace=workspace, user=user)


@pytest.fixture
def score_field(workspace):
    return fields.create_definition(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        object_type="contact",
        key="score",
        label="Score",
        field_type="number",
    )


# ---- csv helpers ---------------------------------------------------------------------------


def test_csv_cells_that_look_like_formulas_are_neutralised():
    out = "".join(csvio.write([["=1+1", "+cmd", "-1", "@x", "ok", None, 5]]))
    assert out == "'=1+1,'+cmd,'-1,'@x,ok,,5\r\n"


def test_delimiter_detection_and_bom():
    text = csvio.decode(bytes([0xEF, 0xBB, 0xBF]) + b"a;b\n1;2\n")
    assert list(csvio.read_rows(text)) == [["a", "b"], ["1", "2"]]


# ---- import --------------------------------------------------------------------------------


def test_upload_suggests_a_mapping_and_previews(workspace, score_field, owner):
    response = upload(login(owner), workspace)
    assert response.status_code == 201, response.content
    body = response.json()
    assert body["total_rows"] == 4  # the blank line is not counted
    assert body["mapping"]["First Name"] == "first_name"
    assert body["mapping"]["E-mail"] == "email" or body["mapping"]["E-mail"] == "ignore"
    assert body["mapping"]["Score"] == "custom.score"
    assert body["mapping"]["Notes"] == "ignore"
    assert body["preview"][0]["First Name"] == "Ada"


@pytest.mark.parametrize(
    "text",
    ["", "a,b\n", "a,a\n1,2\n", ",b\n1,2\n"],
)
def test_bad_files_are_rejected(workspace, owner, text):
    assert upload(login(owner), workspace, text).status_code == 422


def test_size_and_encoding_limits(workspace, owner, settings):
    settings.CRM_IMPORT_MAX_BYTES = 10
    assert upload(login(owner), workspace).status_code == 422
    settings.CRM_IMPORT_MAX_BYTES = 5_000_000
    bad = SimpleUploadedFile("x.csv", "a,b\n\xe9,1\n".encode("latin-1"))
    assert (
        login(owner)
        .post("/api/v1/crm/imports", {"file": bad}, HTTP_X_WORKSPACE_ID=str(workspace.pk))
        .status_code
        == 422
    )


def test_mapping_is_validated(workspace, score_field, owner):
    job = imports.create_import(
        workspace_id=workspace.pk, actor=SYSTEM, file_name="p.csv", raw=CSV.encode()
    )
    good = {"First Name": "first_name", "E-mail": "email"}
    with pytest.raises(ValidationFailedError):
        imports.dry_run(
            workspace_id=workspace.pk, job=job, mapping={"First Name": "first_name"}
        )  # no identity
    with pytest.raises(ValidationFailedError):
        imports.dry_run(
            workspace_id=workspace.pk, job=job, mapping={**good, "Last Name": "email"}
        )  # duplicate
    with pytest.raises(ValidationFailedError):
        imports.dry_run(
            workspace_id=workspace.pk, job=job, mapping={**good, "Score": "custom.nope"}
        )


def run_import(workspace, mapping_overrides=None, options=None, text=CSV):
    job = imports.create_import(
        workspace_id=workspace.pk, actor=SYSTEM, file_name="p.csv", raw=text.encode()
    )
    mapping = {**job.mapping, **(mapping_overrides or {})}
    imports.start(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        job_id=job.pk,
        mapping=mapping,
        options=options or {},
    )
    return imports.get_job(workspace.pk, job.pk)


def test_import_creates_updates_and_reports_errors(workspace, score_field):
    job = run_import(workspace, {"E-mail": "email"}, {"tags": ["imported"]})
    assert job.status == "done" and job.content == ""
    assert (job.created_count, job.updated_count, job.failed_count) == (2, 1, 1)
    assert job.errors[0]["row"] == 4 and "email" in job.errors[0]["errors"]
    ada = contacts.find_contact(workspace.pk, "email", "ada@example.com")
    assert (ada.first_name, ada.last_name, ada.phone) == ("Ada", "Again", "+14165550199")
    assert contacts.tag_names_of([ada.pk])[ada.pk] == ["imported", "vip", "warm"]
    assert fields.get_values(workspace.pk, "contact", [ada.pk])[ada.pk] == {"score": 95.0}


def test_rerunning_the_same_file_creates_no_duplicates(workspace, score_field):
    run_import(workspace, {"E-mail": "email"})
    again = run_import(workspace, {"E-mail": "email"})
    assert (again.created_count, again.updated_count) == (0, 3)
    assert (
        contacts.query_contacts(membership(workspace, workspace.memberships.first().user)).count()
        == 2
    )


def test_create_only_mode_reports_existing_as_failures(workspace):
    contacts.create_contact(
        workspace_id=workspace.pk, actor=SYSTEM, data={"email": "ada@example.com"}
    )
    job = run_import(workspace, {"E-mail": "email"}, {"update_existing": False})
    assert (
        job.failed_count == 3 and job.created_count == 1
    )  # Ada twice (existing + repeated) and the bad row


def test_consent_is_recorded_only_when_attested(workspace):
    with pytest.raises(ValidationFailedError):
        run_import(workspace, {"E-mail": "email"}, {"consent": {"channel": "email"}})
    job = run_import(
        workspace,
        {"E-mail": "email"},
        {"consent": {"channel": "email", "purpose": "marketing", "attested": True}},
    )
    ada = contacts.find_contact(workspace.pk, "email", "ada@example.com")
    state = consent.states_of(workspace.pk, ada.pk)[0]
    assert (state.channel, state.status) == ("email", "opted_in")
    assert consent.history(workspace.pk, ada.pk)[0].evidence["import_id"] == str(job.pk)


def test_import_cannot_be_started_twice_and_resumes_after_a_crash(workspace):
    job = run_import(workspace, {"E-mail": "email"})
    with pytest.raises(ConflictError):
        imports.start(
            workspace_id=workspace.pk, actor=SYSTEM, job_id=job.pk, mapping=job.mapping, options={}
        )
    fresh = imports.create_import(
        workspace_id=workspace.pk, actor=SYSTEM, file_name="p.csv", raw=CSV.encode()
    )
    ImportJob.objects.filter(pk=fresh.pk).update(
        mapping={**fresh.mapping, "E-mail": "email"}, status="running", processed_rows=2
    )
    done = imports.run(workspace_id=workspace.pk, job_id=fresh.pk)
    assert done.status == "done" and done.processed_rows == 4


def test_dry_run_counts_and_writes_nothing(workspace):
    job = imports.create_import(
        workspace_id=workspace.pk, actor=SYSTEM, file_name="p.csv", raw=CSV.encode()
    )
    result = imports.dry_run(
        workspace_id=workspace.pk, job=job, mapping={**job.mapping, "E-mail": "email"}
    )
    assert result["valid"] == 2 and result["invalid"] == 2
    reasons = {tuple(p["errors"]) for p in result["problems"]}
    assert ("email",) in reasons and ("row",) in reasons  # bad address, duplicate in file
    assert not contacts.query_contacts(
        membership(workspace, workspace.memberships.first().user)
    ).exists()


def test_import_api_flow_and_error_file(workspace, owner):
    client = login(owner)
    job = upload(client, workspace).json()
    mapping = {**job["mapping"], "E-mail": "email"}
    dry = call(client, "post", f"/imports/{job['id']}/dry-run", workspace, {"mapping": mapping})
    assert dry.json()["valid"] == 2
    started = call(
        client,
        "post",
        f"/imports/{job['id']}/start",
        workspace,
        {"mapping": mapping, "options": {}},
    )
    assert started.status_code == 200 and started.json()["status"] == "done"
    errors = client.get(
        f"/api/v1/crm/imports/{job['id']}/errors.csv", HTTP_X_WORKSPACE_ID=str(workspace.pk)
    )
    rows = list(csvio.read_rows(errors.content.decode()))
    assert rows[0][-2:] == ["row", "errors"] and rows[1][-2] == "4" and "email" in rows[1][-1]


def test_import_permissions_and_tenant_isolation(workspace, owner):
    other = create_workspace(name="Other Co", owner=owner)
    editor = login(make_user("e@example.com", workspace, Role.EDITOR))
    assert upload(editor, workspace).status_code == 403
    admin = login(owner)
    job = upload(admin, workspace).json()
    for method, path, body in (
        ("get", f"/imports/{job['id']}", None),
        ("post", f"/imports/{job['id']}/dry-run", {"mapping": {}}),
        ("post", f"/imports/{job['id']}/start", {"mapping": {}}),
    ):
        assert call(admin, method, path, other, body).status_code == 404


# ---- export --------------------------------------------------------------------------------


def test_export_respects_columns_scope_and_limits(workspace, owner, score_field, settings):
    run_import(workspace, {"E-mail": "email"})
    admin = login(owner)
    full = admin.get(
        "/api/v1/crm/contacts-export.csv?columns=first_name,email,tags,custom.score",
        HTTP_X_WORKSPACE_ID=str(workspace.pk),
    )
    rows = list(csvio.read_rows(b"".join(full.streaming_content).decode()))
    assert rows[0] == ["first_name", "email", "tags", "custom.score"]
    assert sorted(r[0] for r in rows[1:]) == ["Ada", "Bob"]
    assert (
        admin.get(
            "/api/v1/crm/contacts-export.csv?columns=password",
            HTTP_X_WORKSPACE_ID=str(workspace.pk),
        ).status_code
        == 422
    )
    settings.CRM_EXPORT_MAX_ROWS = 1
    assert (
        admin.get(
            "/api/v1/crm/contacts-export.csv", HTTP_X_WORKSPACE_ID=str(workspace.pk)
        ).status_code
        == 422
    )
    viewer = login(make_user("v@example.com", workspace, Role.VIEWER))
    assert (
        viewer.get(
            "/api/v1/crm/contacts-export.csv", HTTP_X_WORKSPACE_ID=str(workspace.pk)
        ).status_code
        == 403
    )


def test_export_neutralises_formulas(workspace, owner):
    contacts.create_contact(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        data={"first_name": "=HYPERLINK(1)", "email": "x@example.com"},
    )
    out = login(owner).get(
        "/api/v1/crm/contacts-export.csv?columns=first_name", HTTP_X_WORKSPACE_ID=str(workspace.pk)
    )
    assert "'=HYPERLINK(1)" in b"".join(out.streaming_content).decode()


# ---- smart lists ---------------------------------------------------------------------------


def test_smart_lists_are_dynamic_private_or_shared(workspace, owner):
    rep = make_user("rep@example.com", workspace, Role.EDITOR)
    admin, rep_client = login(owner), login(rep)
    created = call(
        admin,
        "post",
        "/smart-lists",
        workspace,
        {"name": "Torontonians", "filter": {"field": "city", "operator": "eq", "value": "toronto"}},
    )
    assert created.status_code == 201
    list_id = created.json()["id"]
    assert call(rep_client, "get", "/smart-lists", workspace).json() == []  # private
    assert call(rep_client, "get", f"/smart-lists/{list_id}/contacts", workspace).status_code == 404
    call(admin, "patch", f"/smart-lists/{list_id}", workspace, {"visibility": "shared"})
    assert len(call(rep_client, "get", "/smart-lists", workspace).json()) == 1
    assert (
        call(rep_client, "patch", f"/smart-lists/{list_id}", workspace, {"name": "x"}).status_code
        == 403
    )
    assert call(admin, "get", f"/smart-lists/{list_id}/contacts", workspace).json()["items"] == []
    contacts.create_contact(
        workspace_id=workspace.pk, actor=SYSTEM, data={"city": "Toronto", "email": "t@example.com"}
    )
    assert (
        len(call(rep_client, "get", f"/smart-lists/{list_id}/contacts", workspace).json()["items"])
        == 1
    )
    bad = call(
        admin,
        "post",
        "/smart-lists",
        workspace,
        {"name": "bad", "filter": {"field": "password", "operator": "eq", "value": 1}},
    )
    assert bad.status_code == 422
    assert call(admin, "delete", f"/smart-lists/{list_id}", workspace).status_code == 204


def test_smart_lists_do_not_cross_workspaces(workspace, owner):
    other = create_workspace(name="Other Co", owner=owner)
    admin = login(owner)
    list_id = call(
        admin, "post", "/smart-lists", workspace, {"name": "L", "visibility": "shared"}
    ).json()["id"]
    assert call(admin, "get", "/smart-lists", other).json() == []
    for method, path, body in (
        ("get", f"/smart-lists/{list_id}/contacts", None),
        ("patch", f"/smart-lists/{list_id}", {"name": "x"}),
        ("delete", f"/smart-lists/{list_id}", None),
    ):
        assert call(admin, method, path, other, body).status_code == 404


# ---- bulk actions --------------------------------------------------------------------------


def people(workspace, n=5):
    return [
        contacts.create_contact(
            workspace_id=workspace.pk,
            actor=SYSTEM,
            data={"email": f"p{i}@example.com", "city": "Toronto" if i % 2 == 0 else "Ottawa"},
        )
        for i in range(n)
    ]


def test_bulk_tags_assign_and_delete(workspace, owner):
    admin = login(owner)
    made = people(workspace)
    created = call(
        admin,
        "post",
        "/bulk-actions",
        workspace,
        {
            "action": "add_tags",
            "params": {"names": ["spring"]},
            "filter": {"field": "city", "operator": "eq", "value": "toronto"},
        },
    )
    assert created.status_code == 201
    job = created.json()
    assert (job["status"], job["total"], job["succeeded"]) == ("done", 3, 3)
    tagged = contacts.tag_names_of([c.pk for c in made])
    assert sorted(tagged) == sorted(c.pk for c in made[::2])
    removed = call(
        admin,
        "post",
        "/bulk-actions",
        workspace,
        {
            "action": "remove_tags",
            "params": {"names": ["spring"]},
            "contact_ids": [str(made[0].pk)],
        },
    ).json()
    assert removed["succeeded"] == 1 and made[0].pk not in contacts.tag_names_of(
        [c.pk for c in made]
    )
    assigned = call(
        admin,
        "post",
        "/bulk-actions",
        workspace,
        {
            "action": "assign",
            "params": {"user_id": str(owner.pk)},
            "contact_ids": [str(made[1].pk)],
        },
    ).json()
    assert assigned["status"] == "done"
    deleted = call(
        admin,
        "post",
        "/bulk-actions",
        workspace,
        {"action": "delete", "contact_ids": [str(c.pk) for c in made[:2]]},
    ).json()
    assert deleted["succeeded"] == 2
    assert contacts.query_contacts(membership(workspace, owner)).count() == 3


def test_bulk_validation_permissions_and_limits(workspace, owner, settings):
    people(workspace, 3)
    admin, editor = login(owner), login(make_user("e@example.com", workspace, Role.EDITOR))
    assert call(admin, "post", "/bulk-actions", workspace, {"action": "explode"}).status_code == 422
    assert (
        call(
            admin, "post", "/bulk-actions", workspace, {"action": "add_tags", "params": {}}
        ).status_code
        == 422
    )
    assert call(editor, "post", "/bulk-actions", workspace, {"action": "delete"}).status_code == 403
    assert (
        call(
            admin,
            "post",
            "/bulk-actions",
            workspace,
            {"action": "delete", "filter": {"field": "city", "operator": "eq", "value": "nowhere"}},
        ).status_code
        == 422
    )
    settings.CRM_BULK_MAX_RECORDS = 2
    assert (
        call(
            admin,
            "post",
            "/bulk-actions",
            workspace,
            {"action": "add_tags", "params": {"names": ["x"]}},
        ).status_code
        == 422
    )


def test_bulk_respects_record_scope(workspace, owner):
    rep = make_user("rep@example.com", workspace, Role.EDITOR)
    Membership.objects.filter(user=rep).update(record_scope="assigned")
    made = people(workspace, 4)
    contacts.assign(workspace_id=workspace.pk, actor=SYSTEM, contact_id=made[0].pk, user_id=rep.pk)
    job = call(
        login(rep),
        "post",
        "/bulk-actions",
        workspace,
        {"action": "add_tags", "params": {"names": ["mine"]}},
    ).json()
    assert job["total"] == 1
    assert (
        call(
            login(rep),
            "post",
            "/bulk-actions",
            workspace,
            {"action": "add_tags", "params": {"names": ["x"]}, "contact_ids": [str(made[1].pk)]},
        ).status_code
        == 422
    )


def test_bulk_records_per_contact_failures_and_can_be_canceled(workspace, owner):
    made = people(workspace, 3)
    job = BulkJob.objects.create(
        workspace_id=workspace.pk,
        action="assign",
        params={"user_id": str(uuid.uuid4())},
        contact_ids=[str(c.pk) for c in made],
        total=3,
    )
    done = bulk.run(workspace_id=workspace.pk, job_id=job.pk)
    assert (done.failed, done.succeeded, done.failures[0]["error"]) == (3, 0, "validation_failed")
    queued = BulkJob.objects.create(
        workspace_id=workspace.pk, action="delete", contact_ids=[str(made[0].pk)], total=1
    )
    canceled = bulk.cancel(workspace_id=workspace.pk, job_id=queued.pk)
    assert canceled.status == "canceled"
    assert bulk.run(workspace_id=workspace.pk, job_id=queued.pk).processed == 0
    with pytest.raises(ConflictError):
        bulk.cancel(workspace_id=workspace.pk, job_id=queued.pk)
    running = BulkJob.objects.create(
        workspace_id=workspace.pk,
        action="delete",
        contact_ids=[str(made[1].pk)],
        total=1,
        cancel_requested=True,
    )
    assert bulk.run(workspace_id=workspace.pk, job_id=running.pk).status == "canceled"
    assert contacts.get_contact(workspace.pk, made[1].pk)


def test_bulk_jobs_are_isolated_by_workspace(workspace, owner):
    other = create_workspace(name="Other Co", owner=owner)
    people(workspace, 2)
    admin = login(owner)
    job = call(
        admin,
        "post",
        "/bulk-actions",
        workspace,
        {"action": "add_tags", "params": {"names": ["x"]}},
    ).json()
    assert call(admin, "get", f"/bulk-actions/{job['id']}", other).status_code == 404
    assert call(admin, "post", f"/bulk-actions/{job['id']}/cancel", other).status_code == 404
    assert (
        call(
            admin,
            "post",
            "/bulk-actions",
            other,
            {"action": "delete", "contact_ids": [str(uuid.uuid4())]},
        ).status_code
        == 422
    )


def test_downloads_work_from_a_plain_link_with_the_workspace_in_the_query(workspace, owner):
    run_import(workspace, {"E-mail": "email"})
    admin = login(owner)
    out = admin.get(f"/api/v1/crm/contacts-export.csv?workspace={workspace.pk}&columns=first_name")
    assert out.status_code == 200 and b"Ada" in b"".join(out.streaming_content)
    job = ImportJob.objects.filter(workspace=workspace).first()
    errors = admin.get(f"/api/v1/crm/imports/{job.pk}/errors.csv?workspace={workspace.pk}")
    assert errors.status_code == 200
    viewer = login(make_user("v2@example.com", workspace, Role.VIEWER))
    assert (
        viewer.get(f"/api/v1/crm/contacts-export.csv?workspace={workspace.pk}").status_code == 403
    )
