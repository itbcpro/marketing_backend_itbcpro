import uuid

import pytest

from audit.actors import Actor
from audit.models import OutboxMessage
from core.errors import ConflictError, NotFoundError, ValidationFailedError
from crm.common.normalize import InvalidValueError, normalise_email, normalise_phone
from crm.fields import services, templating
from crm.fields.models import FieldDefinition

pytestmark = pytest.mark.django_db
ACTOR = Actor.system("test")


def make(workspace, key="lead_score", field_type="number", object_type="contact", **extra):
    return services.create_definition(
        workspace_id=workspace.pk,
        actor=ACTOR,
        object_type=object_type,
        key=key,
        label=key.title(),
        field_type=field_type,
        **extra,
    )


def put(workspace, record, **values):
    return services.set_values(
        workspace_id=workspace.pk, object_type="contact", record_id=record, values=values
    )


# ---- definitions ---------------------------------------------------------------------------


def test_create_definition_audits_and_publishes(workspace):
    definition = make(workspace)
    assert definition.position == 0
    assert OutboxMessage.objects.filter(topic="crm.field.created").count() == 1


@pytest.mark.parametrize("key", ["Bad Key", "1abc", "a", "x" * 51, "first_name", "email"])
def test_invalid_or_reserved_keys_are_rejected(workspace, key):
    with pytest.raises(ValidationFailedError):
        make(workspace, key=key)


def test_keys_are_unique_per_object_type(workspace):
    make(workspace, key="score")
    with pytest.raises(ConflictError):
        make(workspace, key="score")
    make(workspace, key="score", object_type="company")  # same key on another object is fine


def test_select_fields_need_valid_unique_options(workspace):
    with pytest.raises(ValidationFailedError):
        make(workspace, key="tier", field_type="select")
    with pytest.raises(ValidationFailedError):
        make(workspace, key="tier", field_type="select", options=[{"value": "a"}, {"value": "a"}])
    ok = make(workspace, key="tier", field_type="select", options=[{"value": "gold"}])
    assert ok.options == [{"value": "gold", "label": "gold"}]


def test_unknown_types_are_rejected(workspace):
    with pytest.raises(ValidationFailedError):
        make(workspace, field_type="hologram")
    with pytest.raises(ValidationFailedError):
        make(workspace, object_type="spaceship")


def test_update_changes_label_but_never_key_or_type(workspace):
    d = make(workspace)
    updated = services.update_definition(
        workspace_id=workspace.pk, actor=ACTOR, field_id=d.pk, label="Score", folder="Sales"
    )
    assert (updated.label, updated.folder, updated.key, updated.field_type) == (
        "Score",
        "Sales",
        "lead_score",
        "number",
    )


def test_reorder_requires_every_field(workspace):
    a, b = make(workspace, key="aa"), make(workspace, key="bb")
    services.reorder(workspace_id=workspace.pk, object_type="contact", ordered_ids=[b.pk, a.pk])
    assert [d.key for d in services.list_definitions(workspace.pk, "contact")] == ["bb", "aa"]
    with pytest.raises(ValidationFailedError):
        services.reorder(workspace_id=workspace.pk, object_type="contact", ordered_ids=[a.pk])


# ---- values -------------------------------------------------------------------------------


def test_values_roundtrip_for_every_type(workspace):
    ids = {
        "num": make(workspace, key="num", field_type="number"),
        "money": make(workspace, key="money", field_type="money"),
        "phone": make(workspace, key="phone2", field_type="phone"),
        "mail": make(workspace, key="mail2", field_type="email"),
        "link": make(workspace, key="link", field_type="url"),
        "day": make(workspace, key="day", field_type="date"),
        "one": make(
            workspace, key="one", field_type="select", options=[{"value": "a"}, {"value": "b"}]
        ),
        "many": make(
            workspace,
            key="many",
            field_type="multi_select",
            options=[{"value": "a"}, {"value": "b"}],
        ),
        "flag": make(workspace, key="flag", field_type="checkbox"),
        "note": make(workspace, key="note", field_type="long_text"),
        "name": make(workspace, key="name2", field_type="text"),
    }
    assert len(ids) == 11
    record = uuid.uuid4()
    stored = put(
        workspace,
        record,
        num="12.5",
        money={"amount": "9.99", "currency": "cad"},
        phone2="(416) 555-0199",
        mail2=" Person@Example.COM ",
        link="https://example.com/a",
        day="2026-11-18",
        one="a",
        many=["b", "a", "a"],
        flag=True,
        note="long " * 10,
        name2="  Ada ",
    )
    assert stored["num"] == 12.5
    assert stored["money"] == {"amount": "9.990000", "currency": "CAD"}
    assert stored["phone2"] == "+14165550199"
    assert stored["mail2"] == "person@example.com"
    assert stored["day"] == "2026-11-18"
    assert stored["many"] == ["a", "b"]
    assert stored["flag"] is True
    assert stored["name2"] == "Ada"


@pytest.mark.parametrize(
    ("field_type", "raw"),
    [
        ("number", "abc"),
        ("number", True),
        ("number", "NaN"),
        ("phone", "123"),
        ("email", "nope"),
        ("url", "javascript:alert(1)"),
        ("date", "2026-13-40"),
        ("checkbox", "yes"),
        ("text", 5),
        ("money", {"amount": "1", "currency": "TOOLONG"}),
    ],
)
def test_invalid_values_are_reported_per_field(workspace, field_type, raw):
    make(workspace, key="val", field_type=field_type)
    with pytest.raises(ValidationFailedError) as caught:
        put(workspace, uuid.uuid4(), val=raw)
    assert "val" in caught.value.params["errors"]


def test_select_rejects_unknown_option(workspace):
    make(workspace, key="tier", field_type="select", options=[{"value": "gold"}])
    with pytest.raises(ValidationFailedError):
        put(workspace, uuid.uuid4(), tier="bronze")


def test_unknown_field_and_all_or_nothing(workspace):
    make(workspace, key="ok")
    record = uuid.uuid4()
    with pytest.raises(ValidationFailedError) as caught:
        put(workspace, record, ok="1", missing="x")
    assert caught.value.params["errors"] == {"missing": "unknown_field"}
    assert services.get_values(workspace.pk, "contact", [record]) == {}


def test_none_clears_a_value(workspace):
    make(workspace, key="note", field_type="text")
    record = uuid.uuid4()
    put(workspace, record, note="hi")
    assert put(workspace, record, note=None) == {}


def test_archived_fields_stay_readable_but_not_writable(workspace):
    d = make(workspace, key="old", field_type="text")
    record = uuid.uuid4()
    put(workspace, record, old="keep")
    services.archive_definition(workspace_id=workspace.pk, actor=ACTOR, field_id=d.pk)
    assert services.get_values(workspace.pk, "contact", [record])[record] == {"old": "keep"}
    with pytest.raises(ValidationFailedError):
        put(workspace, record, old="new")
    assert services.list_definitions(workspace.pk, "contact") == []
    assert len(services.list_definitions(workspace.pk, "contact", include_archived=True)) == 1
    assert OutboxMessage.objects.filter(topic="crm.field.archived").count() == 1


def test_values_are_scoped_to_their_workspace(workspace, owner):
    from workspaces.services import create_workspace

    other = create_workspace(name="Other", owner=owner)
    make(workspace, key="shared")
    with pytest.raises(NotFoundError):
        services.get_definition(other.pk, FieldDefinition.objects.get(key="shared").pk)
    with pytest.raises(ValidationFailedError):
        services.set_values(
            workspace_id=other.pk,
            object_type="contact",
            record_id=uuid.uuid4(),
            values={"shared": "x"},
        )


# ---- workspace values and templating ---------------------------------------------------------


def test_workspace_values_and_merge_fields(workspace):
    services.set_workspace_value(
        workspace_id=workspace.pk,
        key="booking_link",
        label="Booking",
        value="https://x.test/b?a=1&b=2",
    )
    values = services.workspace_values(workspace.pk)
    text = "Hi {{ contact.first_name }}, book: {{workspace_values.booking_link}} {{contact.nope}}"
    context = {"contact": {"first_name": "<Ada>"}, "workspace_values": values}
    assert templating.render(text, context) == "Hi <Ada>, book: https://x.test/b?a=1&b=2 "
    assert "&lt;Ada&gt;" in templating.render(text, context, html=True)
    assert templating.render(text, context, fallback="-").endswith(" -")
    services.delete_workspace_value(workspace_id=workspace.pk, key="booking_link")
    with pytest.raises(NotFoundError):
        services.delete_workspace_value(workspace_id=workspace.pk, key="booking_link")


def test_templates_do_not_evaluate_code():
    context = {"contact": {"first_name": "Ada"}}
    # Only plain keys resolve; anything else stays as text or falls back, and nothing is evaluated.
    rendered = templating.render("{{ contact.first_name.upper() }} {{__class__.x}}", context)
    assert rendered == "{{ contact.first_name.upper() }} "
    assert templating.referenced_keys("{{contact.a}} {{company.b}}") == {
        ("contact", "a"),
        ("company", "b"),
    }


def test_normalisers():
    assert normalise_email(" A@B.CO ") == "a@b.co"
    assert normalise_phone("416-555-0199") == "+14165550199"
    assert normalise_phone("+44 20 7946 0958") == "+442079460958"
    for bad in ("", "x@", "a b@c.de"):
        with pytest.raises(InvalidValueError):
            normalise_email(bad)
    with pytest.raises(InvalidValueError):
        normalise_phone("abc")
