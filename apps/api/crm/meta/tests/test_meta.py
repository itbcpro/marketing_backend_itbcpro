import json
import time

import httpx
import pytest
from django.test import Client

from audit.actors import Actor
from conftest import make_user
from core.errors import ConflictError, NotFoundError, ValidationFailedError
from crm.channels.meta_channels import MetaMessengerAdapter, MetaWhatsAppAdapter
from crm.consent import services as consent
from crm.contacts import services as contacts
from crm.inbox.models import Message
from crm.meta import services, templates
from crm.meta.graph import GraphClient, GraphError, signature_of
from crm.meta.models import WhatsAppTemplate
from crm.meta.simulator import SimulatedGraph
from crm.sending import gateway, registry
from workspaces.models import Role
from workspaces.services import create_workspace

pytestmark = pytest.mark.django_db
SYSTEM = Actor.system("test")
APP_SECRET = "app-secret-value"
PHONE = "+14165550111"


@pytest.fixture
def graph(monkeypatch):
    sim = SimulatedGraph()
    monkeypatch.setattr(services, "transport_factory", sim.transport)
    registry.register("whatsapp", MetaWhatsAppAdapter)
    registry.register("messenger", MetaMessengerAdapter)
    return sim


@pytest.fixture
def account(workspace, graph):
    return services.connect(
        workspace.pk,
        app_secret=APP_SECRET,
        app_id="123",
        whatsapp_token="wa-token",
        page_token="page-token",
        waba_id="W1",
        phone_number_id="P1",
        page_id="PG1",
    )


@pytest.fixture
def person(workspace):
    c = contacts.create_contact(workspace_id=workspace.pk, actor=SYSTEM, data={"phone": PHONE})
    for channel in ("whatsapp", "messenger"):
        consent.record(
            workspace_id=workspace.pk,
            actor=SYSTEM,
            contact_id=c.pk,
            channel=channel,
            purpose="transactional",
            status="opted_in",
            source="form",
        )
    return c


def post(account, payload, *, signature=None):
    body = json.dumps(payload).encode()
    sig = signature if signature is not None else signature_of(APP_SECRET, body)
    return Client().post(
        f"/api/v1/crm-public/meta/{account.public_id}",
        data=body,
        content_type="application/json",
        HTTP_X_HUB_SIGNATURE_256=sig,
    )


def wa_inbound(mid="wamid.IN1", text="Hello", sender="14165550111"):
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [{"wa_id": sender, "profile": {"name": "Ada"}}],
                            "messages": [
                                {
                                    "from": sender,
                                    "id": mid,
                                    "timestamp": str(int(time.time())),
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        }
                    }
                ]
            }
        ],
    }


def send(workspace, person, channel="whatsapp", **extra):
    return gateway.send(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        contact_id=person.pk,
        channel=channel,
        body=extra.pop("body", "Hello there"),
        **extra,
    )


# ---- credentials and status --------------------------------------------------------------------


def test_secrets_are_sealed_and_blank_tokens_keep_the_stored_ones(workspace, account):
    assert b"wa-token" not in bytes(account.secrets_ciphertext)
    first = services.secrets_of(account)
    assert (first.whatsapp_token, first.page_token, first.app_secret) == (
        "wa-token",
        "page-token",
        APP_SECRET,
    )
    again = services.connect(
        workspace.pk, app_secret="new-secret", phone_number_id="", page_id="PG1"
    )
    later = services.secrets_of(again)
    assert later.whatsapp_token == "wa-token" and later.app_secret == "new-secret"
    assert later.verify_token == first.verify_token and again.pk == account.pk
    with pytest.raises(ValidationFailedError):
        services.connect(workspace.pk, app_secret="")
    with pytest.raises(ValidationFailedError):
        services.connect(workspace.pk, app_secret="x", phone_number_id="P2")


def test_status_refresh_reads_quality_and_tier(workspace, account, graph):
    refreshed = services.refresh_status(workspace.pk)
    assert (refreshed.quality_rating, refreshed.messaging_tier) == ("GREEN", "TIER_1K")
    graph.fail_next(401, 190)
    with pytest.raises(ValidationFailedError):
        services.refresh_status(workspace.pk)


def test_embedded_signup_trades_the_code_and_subscribes(workspace, account, graph):
    done = services.finish_embedded_signup(
        workspace.pk, code="abc", waba_id="W9", phone_number_id="P9"
    )
    assert (done.waba_id, done.phone_number_id) == ("W9", "P9")
    assert services.secrets_of(done).whatsapp_token == graph.exchanged
    assert any(path.endswith("/W9/subscribed_apps") for _, path, _ in graph.requests)
    with pytest.raises(ValidationFailedError):
        services.finish_embedded_signup(workspace.pk, code="", waba_id="W9", phone_number_id="P9")


# ---- sending -----------------------------------------------------------------------------------


def open_window(account, workspace):
    assert post(account, wa_inbound()).status_code == 200


def test_free_text_goes_out_inside_the_window(workspace, account, person, graph):
    open_window(account, workspace)
    message = Message.objects.get(pk=send(workspace, person).message.pk)
    assert message.status == "sent" and message.provider_message_id.startswith("wamid.")
    _, path, body = graph.requests[-1]
    assert path.endswith("/P1/messages") and body["to"] == "14165550111"
    assert body["type"] == "text" and body["biz_opaque_callback_data"] == str(message.pk)


def test_free_text_is_blocked_outside_the_window_but_templates_pass(
    workspace, account, person, graph
):
    blocked = send(workspace, person)
    assert blocked.blocked and blocked.reason == "window_closed"
    template = {"kind": "template", "name": "order_ready", "language": "en", "variables": ["Ada"]}
    outcome = send(workspace, person, attachments=[template], body="Your order is ready, Ada")
    message = Message.objects.get(pk=outcome.message.pk)
    assert not outcome.blocked and message.status == "sent"
    _, _, body = graph.requests[-1]
    assert body["type"] == "template" and body["template"]["name"] == "order_ready"
    assert body["template"]["components"][0]["parameters"] == [{"type": "text", "text": "Ada"}]


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [
        (400, 131047, "window_closed"),
        (400, 132001, "wa_template_missing"),
        (401, 190, "meta_token_invalid"),
        (400, 99999, "meta_failed"),
    ],
)
def test_meta_refusals_become_stable_codes(
    workspace, account, person, graph, status, code, expected
):
    open_window(account, workspace)
    graph.fail_next(status, code)
    message = Message.objects.get(pk=send(workspace, person).message.pk)
    assert (message.status, message.error_code) == ("failed", expected)


def test_a_lost_response_is_never_retried_blindly(workspace, account):
    def broken(request):
        raise httpx.ReadTimeout("slow", request=request)

    client = GraphClient("t", transport=httpx.MockTransport(broken))
    with pytest.raises(GraphError) as caught:
        client.post("/P1/messages", {})
    assert caught.value.code == "meta_unknown_outcome" and not caught.value.retryable


def test_without_an_account_the_send_fails_clearly(workspace, person):
    registry.register("whatsapp", MetaWhatsAppAdapter)
    message = Message.objects.get(
        pk=send(
            workspace, person, attachments=[{"kind": "template", "name": "x", "language": "en"}]
        ).message.pk
    )
    assert message.error_code == "meta_not_connected"


def test_messenger_replies_and_tags(workspace, account, person, graph):
    # A reply inside 24 hours is a normal send; a tagged one may use the longer window.
    payload = {
        "object": "page",
        "entry": [
            {
                "messaging": [
                    {
                        "sender": {"id": "PSID1"},
                        "timestamp": int(time.time() * 1000),
                        "message": {"mid": "m_in1", "text": "Hi page"},
                    }
                ]
            }
        ],
    }
    assert post(account, payload).status_code == 200
    stored = Message.objects.get(provider_message_id="m_in1")
    assert stored.direction == "inbound" and stored.body == "Hi page"
    contact = stored.conversation.contact
    consent.record(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        contact_id=contact.pk,
        channel="messenger",
        purpose="transactional",
        status="opted_in",
        source="form",
    )
    tag = {"kind": "tag", "tag": "HUMAN_AGENT"}
    outcome = send(workspace, contact, channel="messenger", attachments=[tag])
    assert not outcome.blocked
    _, path, body = graph.requests[-1]
    assert path.endswith("/PG1/messages") and body["tag"] == "HUMAN_AGENT"
    assert body["recipient"] == {"id": "PSID1"}


# ---- webhooks ----------------------------------------------------------------------------------


def test_subscription_check_needs_the_verify_token(workspace, account):
    token = services.secrets_of(account).verify_token
    url = f"/api/v1/crm-public/meta/{account.public_id}"
    ok = Client().get(
        url, {"hub.mode": "subscribe", "hub.verify_token": token, "hub.challenge": "777"}
    )
    assert ok.status_code == 200 and ok.content == b"777"
    bad = Client().get(
        url, {"hub.mode": "subscribe", "hub.verify_token": "no", "hub.challenge": "1"}
    )
    assert bad.status_code == 403
    assert (
        Client().get("/api/v1/crm-public/meta/nope", {"hub.mode": "subscribe"}).status_code == 404
    )


def test_a_signed_whatsapp_message_is_stored_once(workspace, account):
    assert post(account, wa_inbound()).status_code == 200
    assert post(account, wa_inbound()).status_code == 200
    rows = Message.objects.filter(provider_message_id="wamid.IN1")
    assert rows.count() == 1 and rows.get().body == "Hello"
    assert rows.get().conversation.contact.phone == PHONE


def test_bad_signatures_are_refused(workspace, account):
    for sig in ("", "sha256=00", signature_of("wrong", b"{}")):
        assert post(account, wa_inbound(), signature=sig).status_code == 403
    assert Message.objects.count() == 0


def test_delivery_reports_update_messages_and_failures_get_codes(workspace, account, person, graph):
    open_window(account, workspace)
    message = Message.objects.get(pk=send(workspace, person).message.pk)

    def status(kind, **extra):
        return {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "statuses": [
                                    {
                                        "id": message.provider_message_id,
                                        "status": kind,
                                        "timestamp": str(int(time.time())),
                                        **extra,
                                    }
                                ]
                            }
                        }
                    ]
                }
            ],
        }

    post(account, status("delivered"))
    post(account, status("read"))
    message.refresh_from_db()
    assert message.status == "read"
    post(account, status("failed", errors=[{"code": 131026}]))
    message.refresh_from_db()
    assert message.status == "read"  # a late failure never moves it backwards


def test_a_garbage_body_is_a_client_error(workspace, account):
    body = b"not json"
    res = Client().post(
        f"/api/v1/crm-public/meta/{account.public_id}",
        data=body,
        content_type="application/json",
        HTTP_X_HUB_SIGNATURE_256=signature_of(APP_SECRET, body),
    )
    assert res.status_code == 422


# ---- management API ----------------------------------------------------------------------------


def api(client, method, path, workspace, body=None):
    kwargs = {"HTTP_X_WORKSPACE_ID": str(workspace.pk)}
    if body is not None:
        kwargs |= {"data": json.dumps(body), "content_type": "application/json"}
    return getattr(client, method)(f"/api/v1/crm{path}", **kwargs)


def logged(user):
    c = Client()
    c.force_login(user)
    return c


def test_api_roundtrip_never_returns_secrets(workspace, owner, graph):
    admin = logged(owner)
    assert api(admin, "get", "/meta/account", workspace).json()["connected"] is False
    made = api(
        admin,
        "put",
        "/meta/account",
        workspace,
        {"app_secret": APP_SECRET, "whatsapp_token": "wa-secret", "phone_number_id": "P1"},
    ).json()
    assert made["connected"] and made["has_whatsapp_token"] and made["webhook_url"]
    assert "wa-secret" not in json.dumps(made) and APP_SECRET not in json.dumps(made)
    status = api(admin, "post", "/meta/account/status", workspace).json()
    assert status["quality_rating"] == "GREEN"
    assert api(admin, "delete", "/meta/account", workspace).status_code == 204
    assert api(admin, "get", "/meta/account", workspace).json()["connected"] is False


def test_api_permissions_and_tenants(workspace, owner, account):
    viewer = logged(make_user("v@example.com", workspace, Role.VIEWER))
    assert api(viewer, "get", "/meta/account", workspace).status_code == 403
    assert api(viewer, "put", "/meta/account", workspace, {"app_secret": "x"}).status_code == 403
    other = create_workspace(name="Other Co", owner=owner)
    admin = logged(owner)
    assert api(admin, "get", "/meta/account", other).json()["connected"] is False
    assert api(admin, "post", "/meta/account/status", other).status_code == 404
    assert api(admin, "delete", "/meta/account", other).status_code == 404
    with pytest.raises(NotFoundError):
        services.get_account(other.pk)


# ---- templates ---------------------------------------------------------------------------------

DRAFT = {
    "name": "order_ready",
    "language": "en",
    "category": "utility",
    "body": "Hi {{1}}, order {{2}} is ready.",
    "examples": ["Ada", "A100"],
}


def draft(workspace, **over):
    return templates.create_draft(workspace.pk, **{**DRAFT, **over})


@pytest.mark.parametrize(
    "bad",
    [
        {"name": "Order Ready"},
        {"language": "english"},
        {"category": "promo"},
        {"body": ""},
        {"body": "Hi {{2}}"},  # numbering must start at 1
        {"examples": ["only one"]},
        {"examples": ["Ada", " "]},
    ],
)
def test_template_validation(workspace, bad):
    with pytest.raises(ValidationFailedError):
        draft(workspace, **bad)


def test_names_are_unique_per_language(workspace):
    draft(workspace)
    with pytest.raises(ConflictError):
        draft(workspace)
    assert draft(workspace, language="fr").pk


def test_submitting_sends_the_body_and_examples_for_review(workspace, account, graph):
    template = templates.submit(workspace.pk, draft(workspace).pk)
    assert template.status == "pending" and template.meta_template_id.startswith("TPL")
    _, path, body = graph.requests[-1]
    assert path.endswith("/W1/message_templates") and body["category"] == "UTILITY"
    component = body["components"][0]
    assert component["text"] == DRAFT["body"] and component["example"] == {
        "body_text": [DRAFT["examples"]]
    }
    with pytest.raises(ConflictError):
        templates.submit(workspace.pk, template.pk)  # already with Meta


def test_the_verdict_arrives_by_webhook_and_by_sync(workspace, account, graph):
    template = templates.submit(workspace.pk, draft(workspace).pk)
    event = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "field": "message_template_status_update",
                        "value": {
                            "event": "REJECTED",
                            "message_template_id": template.meta_template_id,
                            "message_template_name": "order_ready",
                            "reason": "INVALID_FORMAT",
                        },
                    }
                ]
            }
        ],
    }
    assert post(account, event).status_code == 200
    template.refresh_from_db()
    assert (template.status, template.reason) == ("rejected", "INVALID_FORMAT")
    graph.templates = [
        {
            "id": template.meta_template_id,
            "name": "order_ready",
            "language": "en",
            "status": "APPROVED",
            "category": "UTILITY",
            "components": [{"type": "BODY", "text": DRAFT["body"]}],
        },
        {
            "id": "TPLX",
            "name": "made_in_meta",
            "language": "en",
            "status": "APPROVED",
            "category": "MARKETING",
            "components": [{"type": "BODY", "text": "Sale today"}],
        },
    ]
    assert templates.sync(workspace.pk) == 2
    template.refresh_from_db()
    assert template.status == "approved" and template.reason == ""
    imported = WhatsAppTemplate.objects.get(name="made_in_meta")
    assert imported.body == "Sale today" and imported.category == "marketing"


def test_only_approved_templates_are_sent_and_variables_must_match(
    workspace, account, person, graph
):
    template = templates.submit(workspace.pk, draft(workspace).pk)
    with pytest.raises(ConflictError):
        templates.send(
            workspace.pk,
            SYSTEM,
            template_id=template.pk,
            contact_id=person.pk,
            variables=["a", "b"],
        )
    templates.apply_verdict(
        workspace.pk, meta_id=template.meta_template_id, name=template.name, status="APPROVED"
    )
    with pytest.raises(ValidationFailedError):
        templates.send(
            workspace.pk, SYSTEM, template_id=template.pk, contact_id=person.pk, variables=["a"]
        )
    outcome = templates.send(
        workspace.pk,
        SYSTEM,
        template_id=template.pk,
        contact_id=person.pk,
        variables=["Ada", "A100"],
    )
    assert not outcome.blocked  # no open window needed
    assert outcome.message.body == "Hi Ada, order A100 is ready."
    _, _, body = graph.requests[-1]
    assert body["template"]["components"][0]["parameters"][1] == {"type": "text", "text": "A100"}


def test_deleting_a_submitted_template_removes_it_at_meta_too(workspace, account, graph):
    template = templates.submit(workspace.pk, draft(workspace).pk)
    templates.delete(workspace.pk, template.pk)
    assert graph.requests[-1][0] == "DELETE"
    assert not WhatsAppTemplate.objects.exists()


def test_templates_api_and_scoping(workspace, owner, account, person, graph):
    admin = logged(owner)
    made = api(admin, "post", "/meta/templates", workspace, DRAFT)
    assert made.status_code == 201 and made.json()["variables"] == 2
    tid = made.json()["id"]
    assert (
        api(admin, "post", f"/meta/templates/{tid}/submit", workspace).json()["status"] == "pending"
    )
    assert api(admin, "post", "/meta/templates/sync", workspace).json() == {"synced": 0}
    bad = {"contact_id": str(person.pk), "variables": ["a", "b"]}
    assert api(admin, "post", f"/meta/templates/{tid}/send", workspace, bad).status_code == 409
    viewer = logged(make_user("v@example.com", workspace, Role.VIEWER))
    assert api(viewer, "get", "/meta/templates", workspace).status_code == 200
    assert api(viewer, "post", "/meta/templates", workspace, DRAFT).status_code == 403
    other = create_workspace(name="Other Co", owner=owner)
    assert api(admin, "get", "/meta/templates", other).json() == []
    for method, path in (
        ("post", f"/meta/templates/{tid}/submit"),
        ("delete", f"/meta/templates/{tid}"),
    ):
        assert api(admin, method, path, other).status_code == 404, path
    assert api(admin, "delete", f"/meta/templates/{tid}", workspace).status_code == 204


def test_whatsapp_is_priced_by_template_category_and_replies_as_service(
    workspace, account, person, graph
):
    from datetime import timedelta

    from django.utils import timezone

    from crm.usage.models import RateCard, UsageRecord

    start = timezone.now() - timedelta(days=1)
    for category, price in (("", 100), ("service", 0), ("utility", 4_000), ("marketing", 9_000)):
        RateCard.objects.create(
            channel="whatsapp",
            category=category,
            currency="CAD",
            cost_micros=price // 2,
            price_micros=price,
            effective_from=start,
        )
    utility = templates.submit(workspace.pk, draft(workspace).pk)
    templates.apply_verdict(
        workspace.pk, meta_id=utility.meta_template_id, name=utility.name, status="APPROVED"
    )
    sent = templates.send(
        workspace.pk, SYSTEM, template_id=utility.pk, contact_id=person.pk, variables=["Ada", "A1"]
    )
    reply_open = post(account, wa_inbound())
    assert reply_open.status_code == 200
    free = send(workspace, person)
    from crm.usage import charging

    first = charging.charge_message(workspace_id=workspace.pk, message_id=sent.message.pk)
    second = charging.charge_message(workspace_id=workspace.pk, message_id=free.message.pk)
    assert (first.price_micros, second.price_micros) == (4_000, 0)
    assert UsageRecord.objects.count() == 2
