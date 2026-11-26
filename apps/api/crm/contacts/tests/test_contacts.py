import uuid
from datetime import timedelta

import pytest
from django.test import override_settings
from django.utils import timezone

from audit.actors import Actor
from audit.models import OutboxMessage
from conftest import make_user
from core.errors import ConflictError, NotFoundError, ValidationFailedError
from crm.contacts import services
from crm.contacts.models import Contact, ContactIdentity, ContactMerge, IdentityChannel, MatchOrder
from crm.fields import services as fields
from workspaces.models import Role
from workspaces.services import create_workspace

pytestmark = pytest.mark.django_db
ACTOR = Actor.system("test")


def new(workspace, **data):
    return services.create_contact(workspace_id=workspace.pk, actor=ACTOR, data=data)


def topics():
    return list(OutboxMessage.objects.order_by("created_at").values_list("topic", flat=True))


# ---- create ----------------------------------------------------------------------------------


def test_create_normalises_and_records_identities(workspace):
    c = new(workspace, first_name=" Ada ", email=" ADA@Example.com ", phone="416-555-0199")
    assert (c.first_name, c.email, c.phone) == ("Ada", "ada@example.com", "+14165550199")
    rows = {(i.channel, i.value, i.is_primary) for i in c.identities.all()}
    assert rows == {("email", "ada@example.com", True), ("phone", "+14165550199", True)}
    assert "crm.contact.created" in topics()


def test_create_with_nothing_is_allowed_and_blank(workspace):
    assert new(workspace).email == ""


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("email", "nope"),
        ("phone", "12"),
        ("country", "Canada"),
        ("time_zone", "Mars/Base"),
        ("contact_type", "vip"),
        ("date_of_birth", "31-12-1990"),
        ("first_name", "x" * 101),
        ("company_id", uuid.uuid4()),
        ("assigned_to", uuid.uuid4()),
        ("attribution", "text"),
    ],
)
def test_invalid_input_names_the_field(workspace, field, value):
    with pytest.raises(ValidationFailedError) as caught:
        new(workspace, **{field: value})
    assert field in caught.value.params["errors"]


def test_all_problems_are_reported_together(workspace):
    with pytest.raises(ValidationFailedError) as caught:
        new(workspace, email="bad", phone="1", country="XXX")
    assert set(caught.value.params["errors"]) == {"email", "phone", "country"}


def test_duplicate_email_or_phone_is_a_conflict(workspace):
    new(workspace, email="a@example.com", phone="416-555-0100")
    with pytest.raises(ConflictError) as caught:
        new(workspace, email="A@example.com")
    assert caught.value.params["errors"] == {"email": "duplicate"}
    with pytest.raises(ConflictError):
        new(workspace, phone="+1 (416) 555-0100")
    assert Contact.objects.count() == 1


def test_same_email_in_another_workspace_is_fine(workspace, owner):
    other = create_workspace(name="Other", owner=owner)
    new(workspace, email="a@example.com")
    assert new(other, email="a@example.com").pk


def test_extra_addresses_are_identities_and_need_a_primary(workspace):
    c = new(workspace, email="a@example.com", extra_emails=["b@example.com", "a@example.com"])
    assert sorted(i.value for i in c.identities.all()) == ["a@example.com", "b@example.com"]
    with pytest.raises(ValidationFailedError):
        new(workspace, extra_emails=["c@example.com"])
    with pytest.raises(ConflictError):
        new(workspace, email="z@example.com", extra_emails=["b@example.com"])


def test_custom_fields_and_tags_are_saved_with_the_contact(workspace):
    fields.create_definition(
        workspace_id=workspace.pk,
        actor=ACTOR,
        object_type="contact",
        key="score",
        label="Score",
        field_type="number",
    )
    c = new(workspace, email="a@example.com", custom={"score": 7}, tags=["VIP", "vip", "Warm"])
    assert fields.get_values(workspace.pk, "contact", [c.pk])[c.pk] == {"score": 7.0}
    assert services.tag_names_of([c.pk])[c.pk] == ["VIP", "Warm"]


def test_bad_custom_value_rolls_the_contact_back(workspace):
    fields.create_definition(
        workspace_id=workspace.pk,
        actor=ACTOR,
        object_type="contact",
        key="score",
        label="Score",
        field_type="number",
    )
    with pytest.raises(ValidationFailedError):
        new(workspace, email="a@example.com", custom={"score": "high"})
    assert Contact.objects.count() == 0


# ---- update ----------------------------------------------------------------------------------


def test_update_changes_only_what_is_sent_and_reports_it(workspace):
    c = new(workspace, first_name="Ada", email="a@example.com", city="Toronto")
    OutboxMessage.objects.all().delete()
    updated = services.update_contact(
        workspace_id=workspace.pk, actor=ACTOR, contact_id=c.pk, data={"city": "Ottawa"}
    )
    assert (updated.first_name, updated.city, updated.email) == ("Ada", "Ottawa", "a@example.com")
    event = OutboxMessage.objects.get(topic="crm.contact.updated")
    assert event.payload["data"]["changed"] == ["city"]


def test_unchanged_update_publishes_nothing(workspace):
    c = new(workspace, first_name="Ada")
    OutboxMessage.objects.all().delete()
    services.update_contact(
        workspace_id=workspace.pk, actor=ACTOR, contact_id=c.pk, data={"first_name": "Ada"}
    )
    assert not OutboxMessage.objects.exists()


def test_changing_email_moves_the_identity(workspace):
    c = new(workspace, email="a@example.com")
    services.update_contact(
        workspace_id=workspace.pk, actor=ACTOR, contact_id=c.pk, data={"email": "new@example.com"}
    )
    assert [i.value for i in ContactIdentity.objects.filter(contact=c)] == ["new@example.com"]
    c.refresh_from_db()
    assert c.email == "new@example.com"
    # the old address is free again
    assert new(workspace, email="a@example.com").pk


def test_cannot_take_another_contacts_address(workspace):
    new(workspace, email="a@example.com")
    other = new(workspace, email="b@example.com")
    with pytest.raises(ConflictError):
        services.update_contact(
            workspace_id=workspace.pk,
            actor=ACTOR,
            contact_id=other.pk,
            data={"email": "a@example.com"},
        )


def test_unknown_contact_is_not_found(workspace):
    with pytest.raises(NotFoundError):
        services.update_contact(
            workspace_id=workspace.pk, actor=ACTOR, contact_id=uuid.uuid4(), data={}
        )


def test_other_workspaces_contacts_are_not_found(workspace, owner):
    other = create_workspace(name="Other", owner=owner)
    c = new(workspace, email="a@example.com")
    with pytest.raises(NotFoundError):
        services.get_contact(other.pk, c.pk)


# ---- upsert and match order -------------------------------------------------------------------


def test_upsert_updates_the_matching_contact(workspace):
    first, created = services.upsert_contact(
        workspace_id=workspace.pk, actor=ACTOR, data={"email": "a@example.com", "first_name": "Ada"}
    )
    again, created_again = services.upsert_contact(
        workspace_id=workspace.pk,
        actor=ACTOR,
        data={"email": "A@example.com", "last_name": "Lovelace"},
    )
    assert created and not created_again and first.pk == again.pk
    assert (again.first_name, again.last_name) == ("Ada", "Lovelace")
    assert Contact.objects.count() == 1


def test_upsert_matches_by_phone_when_email_is_absent(workspace):
    first = new(workspace, phone="416-555-0199")
    again, created = services.upsert_contact(
        workspace_id=workspace.pk,
        actor=ACTOR,
        data={"phone": "+14165550199", "email": "a@example.com"},
    )
    assert not created and again.pk == first.pk and again.email == "a@example.com"


@pytest.mark.parametrize("order", [MatchOrder.EMAIL_FIRST, MatchOrder.PHONE_FIRST])
def test_upsert_refuses_when_email_and_phone_point_to_different_contacts(workspace, order):
    cfg = services.get_settings(workspace.pk)
    cfg.match_order = order
    cfg.save()
    new(workspace, email="a@example.com")
    new(workspace, phone="416-555-0199")
    with pytest.raises(ConflictError):
        services.upsert_contact(
            workspace_id=workspace.pk,
            actor=ACTOR,
            data={"email": "a@example.com", "phone": "416-555-0199"},
        )


# ---- delete, restore, erase -----------------------------------------------------------------


def test_soft_delete_frees_addresses_and_restore_takes_them_back(workspace):
    c = new(workspace, email="a@example.com")
    services.delete_contact(workspace_id=workspace.pk, actor=ACTOR, contact_id=c.pk)
    with pytest.raises(NotFoundError):
        services.get_contact(workspace.pk, c.pk)
    replacement = new(workspace, email="a@example.com")
    with pytest.raises(ConflictError):
        services.restore_contact(workspace_id=workspace.pk, actor=ACTOR, contact_id=c.pk)
    services.delete_contact(workspace_id=workspace.pk, actor=ACTOR, contact_id=replacement.pk)
    restored = services.restore_contact(workspace_id=workspace.pk, actor=ACTOR, contact_id=c.pk)
    assert restored.deleted_at is None
    assert services.find_contact(workspace.pk, IdentityChannel.EMAIL, "a@example.com").pk == c.pk


@override_settings(CRM_RESTORE_DAYS=1)
def test_restore_window_expires(workspace):
    c = new(workspace, email="a@example.com")
    services.delete_contact(workspace_id=workspace.pk, actor=ACTOR, contact_id=c.pk)
    Contact.objects.filter(pk=c.pk).update(deleted_at=timezone.now() - timedelta(days=2))
    with pytest.raises(ConflictError):
        services.restore_contact(workspace_id=workspace.pk, actor=ACTOR, contact_id=c.pk)


def test_erase_removes_everything_of_the_contact(workspace):
    fields.create_definition(
        workspace_id=workspace.pk,
        actor=ACTOR,
        object_type="contact",
        key="score",
        label="S",
        field_type="number",
    )
    c = new(workspace, email="a@example.com", custom={"score": 1}, tags=["x"])
    services.erase_contact(workspace_id=workspace.pk, actor=ACTOR, contact_id=c.pk)
    assert not Contact.objects.filter(pk=c.pk).exists()
    assert not ContactIdentity.objects.filter(value="a@example.com").exists()
    assert fields.get_values(workspace.pk, "contact", [c.pk]) == {}
    assert "crm.contact.erased" in topics()


# ---- ownership -------------------------------------------------------------------------------


def test_assignment_requires_membership_and_publishes(workspace, owner):
    rep = make_user("rep@example.com", workspace, Role.EDITOR)
    outsider = make_user("out@example.com")
    c = new(workspace, email="a@example.com")
    services.assign(workspace_id=workspace.pk, actor=ACTOR, contact_id=c.pk, user_id=rep.pk)
    assert "crm.contact.assigned" in topics()
    with pytest.raises(ValidationFailedError):
        services.assign(
            workspace_id=workspace.pk, actor=ACTOR, contact_id=c.pk, user_id=outsider.pk
        )


def test_round_robin_cycles_through_candidates(workspace, owner):
    reps = [make_user(f"rep{i}@example.com", workspace, Role.EDITOR) for i in range(3)]
    ids = [r.pk for r in reps]
    chosen = []
    for i in range(6):
        c = new(workspace, email=f"c{i}@example.com")
        done = services.assign_round_robin(
            workspace_id=workspace.pk, actor=ACTOR, contact_id=c.pk, user_ids=ids
        )
        chosen.append(done.assigned_to_id)
    ordered = sorted(ids)
    assert chosen == ordered + ordered


def test_followers_must_be_members(workspace):
    rep = make_user("rep@example.com", workspace, Role.EDITOR)
    c = new(workspace)
    services.add_follower(workspace_id=workspace.pk, contact_id=c.pk, user_id=rep.pk)
    services.add_follower(workspace_id=workspace.pk, contact_id=c.pk, user_id=rep.pk)
    assert c.followers.count() == 1
    with pytest.raises(ValidationFailedError):
        services.add_follower(workspace_id=workspace.pk, contact_id=c.pk, user_id=uuid.uuid4())
    services.remove_follower(workspace_id=workspace.pk, contact_id=c.pk, user_id=rep.pk)
    assert c.followers.count() == 0


# ---- tags ------------------------------------------------------------------------------------


def test_tags_are_case_insensitive_and_removable(workspace):
    c = new(workspace)
    added = services.add_tags(
        workspace_id=workspace.pk, actor=ACTOR, contact=c, names=["VIP", "vip", " Hot "]
    )
    assert [t.name for t in added] == ["VIP", "Hot"]
    assert services.add_tags(workspace_id=workspace.pk, actor=ACTOR, contact=c, names=["vip"]) == []
    removed = services.remove_tags(workspace_id=workspace.pk, actor=ACTOR, contact=c, names=["hot"])
    assert [t.name for t in removed] == ["Hot"]
    assert services.tag_names_of([c.pk])[c.pk] == ["VIP"]


def test_tag_limits(workspace):
    c = new(workspace)
    with pytest.raises(ValidationFailedError):
        services.add_tags(workspace_id=workspace.pk, actor=ACTOR, contact=c, names=["x" * 101])
    with override_settings(CRM_MAX_TAGS_PER_REQUEST=2), pytest.raises(ValidationFailedError):
        services.add_tags(workspace_id=workspace.pk, actor=ACTOR, contact=c, names=["a", "b", "c"])


def test_rename_and_merge_tags(workspace):
    a, b = new(workspace), new(workspace)
    t1 = services.add_tags(workspace_id=workspace.pk, actor=ACTOR, contact=a, names=["old"])[0]
    services.add_tags(workspace_id=workspace.pk, actor=ACTOR, contact=b, names=["new", "old"])
    t2 = next(t for t in services.list_tags(workspace.pk) if t.name == "new")
    with pytest.raises(ConflictError):
        services.rename_tag(workspace_id=workspace.pk, tag_id=t1.pk, name="NEW")
    assert (
        services.rename_tag(workspace_id=workspace.pk, tag_id=t1.pk, name="older").name == "older"
    )
    services.merge_tags(workspace_id=workspace.pk, source_id=t1.pk, target_id=t2.pk)
    assert [t.name for t in services.list_tags(workspace.pk)] == ["new"]
    assert services.tag_names_of([a.pk, b.pk]) == {a.pk: ["new"], b.pk: ["new"]}


# ---- merge and undo --------------------------------------------------------------------------


def test_merge_moves_everything_and_undo_restores_it(workspace):
    fields.create_definition(
        workspace_id=workspace.pk,
        actor=ACTOR,
        object_type="contact",
        key="score",
        label="S",
        field_type="number",
    )
    winner = new(workspace, first_name="Ada", email="w@example.com", tags=["a"])
    loser = new(
        workspace,
        first_name="A.",
        last_name="Lovelace",
        email="l@example.com",
        phone="416-555-0199",
        city="Toronto",
        tags=["a", "b"],
        custom={"score": 9},
    )
    merge = services.merge_contacts(
        workspace_id=workspace.pk, actor=ACTOR, winner_id=winner.pk, loser_id=loser.pk
    )
    winner.refresh_from_db()
    loser.refresh_from_db()
    assert winner.first_name == "Ada"  # winner keeps its own value
    assert (winner.last_name, winner.city) == ("Lovelace", "Toronto")  # empty ones are filled
    assert (winner.email, winner.phone) == ("w@example.com", "+14165550199")
    assert sorted(i.value for i in winner.identities.all()) == [
        "+14165550199",
        "l@example.com",
        "w@example.com",
    ]
    assert services.tag_names_of([winner.pk])[winner.pk] == ["a", "b"]
    assert fields.get_values(workspace.pk, "contact", [winner.pk])[winner.pk] == {"score": 9.0}
    assert loser.deleted_at is not None and loser.merged_into_id == winner.pk
    assert "crm.contact.merged" in topics()

    services.undo_merge(workspace_id=workspace.pk, actor=ACTOR, merge_id=merge.pk)
    winner.refresh_from_db()
    loser.refresh_from_db()
    assert loser.deleted_at is None and loser.merged_into_id is None
    assert (loser.email, loser.phone) == ("l@example.com", "+14165550199")
    assert (winner.last_name, winner.city, winner.email, winner.phone) == (
        "",
        "",
        "w@example.com",
        "",
    )
    assert [i.value for i in winner.identities.all()] == ["w@example.com"]
    assert services.tag_names_of([winner.pk, loser.pk]) == {winner.pk: ["a"], loser.pk: ["a", "b"]}
    assert fields.get_values(workspace.pk, "contact", [loser.pk])[loser.pk] == {"score": 9.0}
    assert "crm.contact.merge_undone" in topics()


def test_merge_choices_pick_the_losers_value(workspace):
    winner = new(workspace, first_name="Ada", city="Toronto")
    loser = new(workspace, first_name="Augusta", city="London")
    services.merge_contacts(
        workspace_id=workspace.pk,
        actor=ACTOR,
        winner_id=winner.pk,
        loser_id=loser.pk,
        choices={"first_name": "loser"},
    )
    winner.refresh_from_db()
    assert (winner.first_name, winner.city) == ("Augusta", "Toronto")


def test_merge_validation(workspace):
    a, b = new(workspace), new(workspace)
    with pytest.raises(ValidationFailedError):
        services.merge_contacts(
            workspace_id=workspace.pk, actor=ACTOR, winner_id=a.pk, loser_id=a.pk
        )
    with pytest.raises(ValidationFailedError):
        services.merge_contacts(
            workspace_id=workspace.pk,
            actor=ACTOR,
            winner_id=a.pk,
            loser_id=b.pk,
            choices={"nope": "winner"},
        )
    with pytest.raises(NotFoundError):
        services.merge_contacts(
            workspace_id=workspace.pk, actor=ACTOR, winner_id=a.pk, loser_id=uuid.uuid4()
        )


@override_settings(CRM_MERGE_UNDO_HOURS=1)
def test_undo_window_and_single_use(workspace):
    a, b = new(workspace), new(workspace)
    merge = services.merge_contacts(
        workspace_id=workspace.pk, actor=ACTOR, winner_id=a.pk, loser_id=b.pk
    )
    services.undo_merge(workspace_id=workspace.pk, actor=ACTOR, merge_id=merge.pk)
    with pytest.raises(ConflictError):
        services.undo_merge(workspace_id=workspace.pk, actor=ACTOR, merge_id=merge.pk)
    again = services.merge_contacts(
        workspace_id=workspace.pk, actor=ACTOR, winner_id=a.pk, loser_id=b.pk
    )
    ContactMerge.objects.filter(pk=again.pk).update(created_at=timezone.now() - timedelta(hours=2))
    with pytest.raises(ConflictError):
        services.undo_merge(workspace_id=workspace.pk, actor=ACTOR, merge_id=again.pk)


# ---- companies -------------------------------------------------------------------------------


def test_company_lifecycle_and_contact_link(workspace):
    company = services.create_company(
        workspace_id=workspace.pk,
        actor=ACTOR,
        data={"name": " Acme ", "website": "https://acme.test", "email": "Hi@Acme.test"},
    )
    assert (company.name, company.email) == ("Acme", "hi@acme.test")
    c = new(workspace, company_id=company.pk)
    assert c.company_id == company.pk
    services.update_company(
        workspace_id=workspace.pk, actor=ACTOR, company_id=company.pk, data={"name": "Acme Inc"}
    )
    services.delete_company(workspace_id=workspace.pk, actor=ACTOR, company_id=company.pk)
    c.refresh_from_db()
    assert c.company_id is None
    with pytest.raises(NotFoundError):
        services.get_company(workspace.pk, company.pk)
    with pytest.raises(ValidationFailedError):
        new(workspace, company_id=company.pk)


def test_company_requires_a_name_and_valid_website(workspace):
    with pytest.raises(ValidationFailedError) as caught:
        services.create_company(workspace_id=workspace.pk, actor=ACTOR, data={"website": "ftp://x"})
    assert set(caught.value.params["errors"]) == {"name", "website"}
