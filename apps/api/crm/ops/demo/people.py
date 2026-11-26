"""Custom fields, companies, contacts, tags and consent."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from crm.consent import services as consent
from crm.contacts import companies, contact_service, tags
from crm.contacts.models import Company, Contact
from crm.fields import services as fields
from crm.fields.models import FieldDefinition
from crm.ops.demo.base import DEMO_DOMAIN, DEMO_TAG, Demo
from crm.sending import suppression

FIRST = [
    "Olivia",
    "Liam",
    "Emma",
    "Noah",
    "Ava",
    "Lucas",
    "Mia",
    "Ethan",
    "Sofia",
    "Mason",
    "Layla",
    "Omid",
    "Sara",
    "Daniel",
    "Chloe",
    "Amir",
    "Nina",
    "Jacob",
    "Zoe",
    "Kian",
    "Hana",
    "Felix",
    "Ruby",
    "Arman",
    "Lena",
    "Owen",
    "Mina",
    "Leo",
    "Iris",
    "Reza",
]
LAST = [
    "Tremblay",
    "Nguyen",
    "Singh",
    "Roy",
    "Campbell",
    "Patel",
    "Gagnon",
    "Wilson",
    "Ahmadi",
    "Lee",
    "Martin",
    "Brown",
    "Tanaka",
    "Moradi",
    "Fraser",
    "Costa",
    "Khan",
    "Bouchard",
]
COMPANIES = [
    ("Northwind Dental", "Dentistry"),
    ("Maple Roofing", "Construction"),
    ("Blue Harbor Cafe", "Hospitality"),
    ("Summit Fitness", "Fitness"),
    ("Lakeshore Realty", "Real estate"),
    ("Brightside Law", "Legal"),
    ("Cedar Tech Labs", "Software"),
    ("Pinecrest Clinic", "Health"),
    ("Orchard Bakery", "Food"),
    ("Vertex Auto Group", "Automotive"),
]
CITIES = [
    ("Toronto", "ON"),
    ("Ottawa", "ON"),
    ("Montreal", "QC"),
    ("Calgary", "AB"),
    ("Vancouver", "BC"),
    ("Halifax", "NS"),
    ("Winnipeg", "MB"),
    ("Mississauga", "ON"),
]
SOURCES = ["web form", "facebook lead ad", "referral", "import", "phone call", "instagram"]
STATUS_TAGS = ["hot", "newsletter", "vip", "needs follow-up", "event 2026"]
ZONES = {
    "ON": "America/Toronto",
    "QC": "America/Toronto",
    "AB": "America/Edmonton",
    "BC": "America/Vancouver",
    "NS": "America/Halifax",
    "MB": "America/Winnipeg",
}
CONTACT_COUNT = 60
INDUSTRIES = [{"value": v, "label": v} for v in sorted({i for _, i in COMPANIES})]


def seed_fields(demo: Demo) -> None:
    existing = set(
        FieldDefinition.objects.filter(workspace_id=demo.wid).values_list("object_type", "key")
    )
    specs: list[tuple[str, str, str, str, list[dict[str, str]] | None]] = [
        ("contact", "industry", "Industry", "select", INDUSTRIES),
        ("contact", "budget", "Budget", "money", None),
        ("contact", "renewal_date", "Renewal date", "date", None),
        ("contact", "vip", "VIP client", "checkbox", None),
        ("contact", "linkedin", "LinkedIn", "url", None),
        (
            "contact",
            "interests",
            "Interests",
            "multi_select",
            [{"value": v, "label": v.title()} for v in ("seo", "ads", "social", "email")],
        ),
        ("company", "employees", "Employees", "number", None),
        ("opportunity", "contract_term", "Contract term", "text", None),
    ]
    for object_type, key, label, kind, options in specs:
        if (object_type, key) in existing:
            continue
        fields.create_definition(
            workspace_id=demo.wid,
            actor=demo.actor,
            object_type=object_type,
            key=key,
            label=label,
            field_type=kind,
            options=options,
        )
        demo.count("fields")
    for key, label, value in [
        ("booking_link", "Booking link", "https://cal.example.com/itbcpro/intro"),
        ("support_phone", "Support phone", "+1 416 555 0100"),
    ]:
        fields.set_workspace_value(workspace_id=demo.wid, key=key, label=label, value=value)


def seed_companies(demo: Demo) -> list[Company]:
    rows: list[Company] = []
    for index, (name, industry) in enumerate(COMPANIES):
        slug = name.lower().replace(" ", "")
        found = Company.objects.for_workspace(demo.wid).filter(name=name).first()
        if found is None:
            found = companies.create_company(
                workspace_id=demo.wid,
                actor=demo.actor,
                data={
                    "name": name,
                    "email": f"info@{slug}.{DEMO_DOMAIN}",
                    "phone": demo.phone(9000 + index),
                    "website": f"https://www.{slug}.example.com",
                    "address": f"{100 + index * 7} King St W",
                    "description": f"{industry} business, demo account.",
                    "assigned_to": demo.rng.choice(demo.user_ids()),
                    "custom": {"employees": demo.rng.choice([4, 12, 35, 80, 150])},
                },
            )
            demo.count("companies")
        rows.append(found)
    return rows


def _contact_data(demo: Demo, index: int, firms: list[Company]) -> dict[str, Any]:
    rng = demo.rng
    first, last = FIRST[index % len(FIRST)], LAST[(index * 7) % len(LAST)]
    city, state = CITIES[index % len(CITIES)]
    picked = [t for t in STATUS_TAGS if rng.random() < 0.25] + [DEMO_TAG]
    data: dict[str, Any] = {
        "first_name": first,
        "last_name": last,
        "email": f"{first}.{last}{index}@{DEMO_DOMAIN}".lower(),
        "phone": demo.phone(index),
        "contact_type": "customer" if index % 4 == 0 else "lead",
        "source": rng.choice(SOURCES),
        "city": city,
        "state": state,
        "country": "CA",
        "postal_code": f"M{rng.randint(1, 9)}A {rng.randint(1, 9)}B{rng.randint(1, 9)}",
        "address": f"{rng.randint(10, 999)} Maple Ave",
        "time_zone": ZONES[state],
        "assigned_to": rng.choice([*demo.user_ids(), None]),
        "tags": picked,
        "custom": {
            "industry": rng.choice(INDUSTRIES)["value"],
            "budget": {"amount": str(rng.choice([500, 1500, 4000, 12000])), "currency": "CAD"},
            "renewal_date": (date.today() + timedelta(days=rng.randint(-30, 300))).isoformat(),
            "vip": index % 9 == 0,
            "interests": rng.sample(["seo", "ads", "social", "email"], k=rng.randint(1, 3)),
        },
    }
    if index % 3 != 2 and firms:
        data["company_id"] = firms[index % len(firms)].pk
    return data


def seed_contacts(demo: Demo, firms: list[Company]) -> None:
    demo.contacts.clear()
    for index in range(1, CONTACT_COUNT + 1):
        data = _contact_data(demo, index, firms)
        found = Contact.objects.for_workspace(demo.wid).filter(email=data["email"]).first()
        if found is None:
            found = contact_service.create_contact(
                workspace_id=demo.wid, actor=demo.actor, data=data
            )
            demo.count("contacts")
            if index % 5 == 0:
                contact_service.add_follower(
                    workspace_id=demo.wid, contact_id=found.pk, user_id=demo.owner.user_id
                )
        demo.contacts.append(found)
    # Two special cases for the list filters: one deleted contact and one with no company.
    gone = demo.contacts.pop()
    if gone.deleted_at is None:
        contact_service.delete_contact(workspace_id=demo.wid, actor=demo.actor, contact_id=gone.pk)


def seed_consent(demo: Demo) -> None:
    """Most contacts agreed to email; some also to SMS; a few opted out or are suppressed."""
    for index, contact in enumerate(demo.contacts):
        plan = [("email", "opted_in", "form")]
        if index % 2 == 0:
            plan.append(("sms", "opted_in", "form"))
        if index % 7 == 0:
            plan.append(("whatsapp", "opted_in", "link"))
        if index % 11 == 0:
            plan.append(("email", "opted_out", "link"))
        for channel, status, source in plan:
            consent.record(
                workspace_id=demo.wid,
                actor=demo.actor,
                contact_id=contact.pk,
                channel=channel,
                purpose="marketing",
                status=status,
                source=source,
                evidence={"note": "demo data"},
                occurred_at=demo.ago(days=40 - index % 30),
            )
    for address, reason in [
        (f"bounced@{DEMO_DOMAIN}", "hard_bounce"),
        (f"complainer@{DEMO_DOMAIN}", "complaint"),
        (f"manual@{DEMO_DOMAIN}", "manual"),
    ]:
        suppression.suppress(
            workspace_id=demo.wid,
            actor=demo.actor,
            channel="email",
            address=address,
            reason=reason,
            note="demo data",
        )


def run(demo: Demo) -> None:
    seed_fields(demo)
    firms = seed_companies(demo)
    seed_contacts(demo, firms)
    seed_consent(demo)
    if not tags.list_tags(demo.wid):  # pragma: no cover - tags come from contacts above
        raise RuntimeError("no tags were created")
