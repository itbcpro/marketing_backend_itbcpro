"""Keys that standard fields already use, per object type. Custom fields may not reuse them."""

from crm.fields.models import ObjectType

_COMMON = {"id", "owner", "created_at", "updated_at"}

RESERVED_KEYS: dict[str, frozenset[str]] = {
    ObjectType.CONTACT: frozenset(
        _COMMON
        | {
            "first_name",
            "last_name",
            "full_name",
            "email",
            "phone",
            "company",
            "company_name",
            "address",
            "city",
            "state",
            "postal_code",
            "country",
            "timezone",
            "source",
            "type",
            "tags",
        }
    ),
    ObjectType.COMPANY: frozenset(_COMMON | {"name", "email", "phone", "website", "address"}),
    ObjectType.OPPORTUNITY: frozenset(
        _COMMON | {"name", "value", "pipeline", "stage", "status", "source"}
    ),
}
