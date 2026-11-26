import uuid

import pytest

from audit.actors import Actor
from conftest import make_user
from core.errors import ValidationFailedError
from crm.contacts import services
from crm.fields import services as fields
from workspaces.models import Membership, RecordScope, Role

pytestmark = pytest.mark.django_db
ACTOR = Actor.system("test")


@pytest.fixture
def membership(workspace, owner):
    return Membership.objects.get(workspace=workspace, user=owner)


@pytest.fixture
def book(workspace, owner):
    """Four contacts with a mix of attributes."""
    for key, field_type in (
        ("score", "number"),
        ("tier", "select"),
        ("joined", "date"),
        ("vip", "checkbox"),
        ("hobbies", "multi_select"),
    ):
        fields.create_definition(
            workspace_id=workspace.pk,
            actor=ACTOR,
            object_type="contact",
            key=key,
            label=key,
            field_type=field_type,
            options=[{"value": "gold"}, {"value": "silver"}]
            if key == "tier"
            else ([{"value": "ski"}, {"value": "golf"}] if key == "hobbies" else None),
        )
    rows = {
        "ada": {
            "first_name": "Ada",
            "email": "ada@acme.test",
            "city": "Toronto",
            "source": "web",
            "tags": ["vip", "warm"],
            "custom": {
                "score": 90,
                "tier": "gold",
                "joined": "2026-01-10",
                "vip": True,
                "hobbies": ["ski"],
            },
        },
        "bob": {
            "first_name": "Bob",
            "email": "bob@other.test",
            "city": "Ottawa",
            "source": "ads",
            "tags": ["warm"],
            "custom": {"score": 40, "tier": "silver", "vip": False},
            "assigned_to": owner.pk,
        },
        "cy": {
            "first_name": "Cy",
            "phone": "416-555-0100",
            "city": "toronto",
            "contact_type": "customer",
        },
        "dee": {"first_name": "Dee", "email": "dee@acme.test"},
    }
    return {
        k: services.create_contact(workspace_id=workspace.pk, actor=ACTOR, data=v)
        for k, v in rows.items()
    }


def names(membership, spec=None, **kwargs):
    rows = services.query_contacts(membership, spec=spec, **kwargs)
    return sorted(c.first_name for c in rows)


def cond(field, operator, value=None):
    return {"field": field, "operator": operator, "value": value}


def test_empty_filter_matches_everything(membership, book):
    assert names(membership) == ["Ada", "Bob", "Cy", "Dee"]
    assert names(membership, {}) == ["Ada", "Bob", "Cy", "Dee"]


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        (cond("city", "eq", "TORONTO"), ["Ada", "Cy"]),
        (cond("city", "neq", "toronto"), ["Bob", "Dee"]),
        (cond("email", "contains", "acme"), ["Ada", "Dee"]),
        (cond("first_name", "starts_with", "b"), ["Bob"]),
        (cond("email", "empty"), ["Cy"]),
        (cond("email", "not_empty"), ["Ada", "Bob", "Dee"]),
        (cond("contact_type", "eq", "customer"), ["Cy"]),
        (cond("contact_type", "in", ["lead", "customer"]), ["Ada", "Bob", "Cy", "Dee"]),
        (cond("assigned_to", "empty"), ["Ada", "Cy", "Dee"]),
        (cond("tags", "has", "VIP"), ["Ada"]),
        (cond("tags", "not_has", "warm"), ["Cy", "Dee"]),
        (cond("tags", "has_any", ["vip", "warm"]), ["Ada", "Bob"]),
        (cond("custom.score", "gt", 50), ["Ada"]),
        (cond("custom.score", "lte", 40), ["Bob"]),
        (cond("custom.score", "empty"), ["Cy", "Dee"]),
        (cond("custom.tier", "eq", "Gold"), ["Ada"]),
        (cond("custom.tier", "neq", "gold"), ["Bob", "Cy", "Dee"]),
        (cond("custom.joined", "after", "2026-01-01"), ["Ada"]),
        (cond("custom.joined", "before", "2026-01-01"), []),
        (cond("custom.vip", "is_true"), ["Ada"]),
        (cond("custom.vip", "is_false"), ["Bob", "Cy", "Dee"]),
        (cond("custom.hobbies", "has", "ski"), ["Ada"]),
    ],
)
def test_conditions(membership, book, spec, expected):
    assert names(membership, spec) == expected


def test_groups_combine_with_and_or(membership, book):
    spec = {
        "op": "and",
        "conditions": [
            cond("email", "contains", "acme"),
            {
                "op": "or",
                "conditions": [cond("tags", "has", "vip"), cond("first_name", "eq", "Dee")],
            },
        ],
    }
    assert names(membership, spec) == ["Ada", "Dee"]
    assert names(
        membership,
        {"op": "or", "conditions": [cond("city", "eq", "ottawa"), cond("source", "eq", "web")]},
    ) == ["Ada", "Bob"]


def test_filter_combines_with_search_and_plain_params(membership, book):
    assert names(membership, cond("city", "eq", "toronto"), text="ada") == ["Ada"]
    assert names(membership, params={"source": "ads"}) == ["Bob"]


@pytest.mark.parametrize(
    "spec",
    [
        cond("password", "eq", "x"),
        cond("city", "regex", ".*"),
        cond("custom.nope", "eq", 1),
        cond("custom.score", "contains", "1"),
        cond("custom.score", "gt", "abc"),
        cond("created_at", "before", "not-a-date"),
        cond("assigned_to", "eq", "nope"),
        cond("contact_type", "eq", "vip"),
        cond("tags", "has", ["a", "b"]),
        {"op": "xor", "conditions": []},
        {"op": "and", "conditions": ["nope"]},
        {"field": 5, "operator": "eq"},
    ],
)
def test_bad_filters_are_rejected(membership, book, spec):
    with pytest.raises(ValidationFailedError):
        names(membership, spec)


def test_depth_and_size_are_limited(membership, book, settings):
    deep = cond("city", "eq", "x")
    for _ in range(7):
        deep = {"op": "and", "conditions": [deep]}
    with pytest.raises(ValidationFailedError):
        names(membership, deep)
    settings.CRM_FILTER_MAX_CONDITIONS = 3
    with pytest.raises(ValidationFailedError):
        names(
            membership, {"op": "or", "conditions": [cond("city", "eq", str(i)) for i in range(4)]}
        )


def test_sql_metacharacters_are_only_data(membership, book):
    assert (
        names(membership, cond("first_name", "eq", "x'; DROP TABLE crm_contacts_contact; --")) == []
    )
    assert names(membership, cond("first_name", "contains", "%")) == []
    assert len(names(membership)) == 4


def test_record_scope_always_applies(workspace, owner, book):
    rep = make_user("rep@example.com", workspace, Role.EDITOR)
    Membership.objects.filter(user=rep).update(record_scope=RecordScope.ASSIGNED)
    scoped = Membership.objects.get(user=rep, workspace=workspace)
    services.assign(
        workspace_id=workspace.pk, actor=ACTOR, contact_id=book["dee"].pk, user_id=rep.pk
    )
    assert names(scoped) == ["Dee"]
    assert names(scoped, cond("city", "empty")) == ["Dee"]
    assert names(scoped, cond("email", "contains", "acme")) == ["Dee"]


def test_filters_never_cross_workspaces(workspace, owner, book):
    from workspaces.services import create_workspace

    other = create_workspace(name="Other Co", owner=owner)
    other_membership = Membership.objects.get(workspace=other, user=owner)
    assert names(other_membership) == []
    assert names(other_membership, cond("tags", "has", "vip")) == []
    with pytest.raises(ValidationFailedError):
        names(
            other_membership, cond("custom.score", "gt", 1)
        )  # the field exists only in the first workspace
    assert uuid.UUID(str(other.pk))
