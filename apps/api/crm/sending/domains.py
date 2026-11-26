"""Sending domains: which DNS records to publish, and live verification of them.

The records come from a deployment profile (``APP_CRM_EMAIL_DNS_PROFILE``), because they depend on
the mail provider in use. Nothing provider specific is written in code.

Profile keys, all optional (a missing key means that record is not required):
``spf_include``, ``dkim = {selector, cname_target}``, ``return_path = {host, target}``,
``tracking = {host, target}``, ``mx = {host, target, priority}``, ``dmarc = {policy, report_to}``.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from audit.actors import Actor
from audit.services import record_event
from core.errors import ConflictError, NotFoundError, ValidationFailedError
from crm.sending.dns import DnsResolver, ResolverUnavailableError, SystemResolver
from crm.sending.models import (
    DomainRecord,
    DomainStatus,
    RecordStatus,
    SendingDomain,
)

DOMAIN_PATTERN = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
TXT, CNAME, MX = "TXT", "CNAME", "MX"


@dataclass(frozen=True)
class Expected:
    purpose: str
    record_type: str
    name: str
    value: str
    required: bool = True
    priority: int | None = None


def plan(domain: str, profile: dict[str, Any]) -> list[Expected]:
    """The records to publish for ``domain`` under the deployment profile."""
    records: list[Expected] = []
    if include := profile.get("spf_include"):
        records.append(Expected("spf", TXT, domain, f"v=spf1 include:{include} ~all"))
    dkim = profile.get("dkim") or {}
    if dkim.get("selector") and dkim.get("cname_target"):
        records.append(
            Expected("dkim", CNAME, f"{dkim['selector']}._domainkey.{domain}", dkim["cname_target"])
        )
    for purpose in ("return_path", "tracking"):
        item = profile.get(purpose) or {}
        if item.get("host") and item.get("target"):
            records.append(Expected(purpose, CNAME, f"{item['host']}.{domain}", item["target"]))
    mx = profile.get("mx") or {}
    if mx.get("host") and mx.get("target"):
        records.append(
            Expected(
                "mx",
                MX,
                f"{mx['host']}.{domain}",
                mx["target"],
                priority=int(mx.get("priority", 10)),
            )
        )
    dmarc = profile.get("dmarc") or {}
    policy = dmarc.get("policy", "none")
    report = f"; rua=mailto:{dmarc['report_to']}" if dmarc.get("report_to") else ""
    records.append(
        Expected("dmarc", TXT, f"_dmarc.{domain}", f"v=DMARC1; p={policy}{report}", required=False)
    )
    return records


def normalise(domain: str) -> str:
    clean = domain.strip().lower().rstrip(".")
    if not DOMAIN_PATTERN.match(clean):
        raise ValidationFailedError("invalid domain", errors={"domain": "invalid_domain"})
    return clean


def add_domain(*, workspace_id: uuid.UUID, actor: Actor, domain: str) -> SendingDomain:
    clean = normalise(domain)
    records = plan(clean, settings.CRM_EMAIL_DNS_PROFILE)
    try:
        with transaction.atomic():
            row = SendingDomain.objects.create(workspace_id=workspace_id, domain=clean)
            DomainRecord.objects.bulk_create(
                DomainRecord(
                    workspace_id=workspace_id,
                    domain=row,
                    purpose=r.purpose,
                    record_type=r.record_type,
                    name=r.name,
                    value=r.value,
                    priority=r.priority,
                    required=r.required,
                )
                for r in records
            )
            record_event(
                action="crm.sending_domain.added",
                actor=actor,
                workspace_id=workspace_id,
                target_type="crm_sending_domain",
                target_id=row.pk,
                after={"domain": clean},
            )
    except IntegrityError as exc:
        raise ConflictError("domain already in use", errors={"domain": "domain_taken"}) from exc
    return row


def get_domain(workspace_id: uuid.UUID, domain_id: uuid.UUID) -> SendingDomain:
    found = SendingDomain.objects.for_workspace(workspace_id).filter(pk=domain_id).first()
    if found is None:
        raise NotFoundError("domain not found")
    return found


def delete_domain(*, workspace_id: uuid.UUID, actor: Actor, domain_id: uuid.UUID) -> None:
    row = get_domain(workspace_id, domain_id)
    row.delete()
    record_event(
        action="crm.sending_domain.removed",
        actor=actor,
        workspace_id=workspace_id,
        target_type="crm_sending_domain",
        target_id=domain_id,
    )


# ---- checking -------------------------------------------------------------------------------


def _same_host(a: str, b: str) -> bool:
    return a.rstrip(".").lower() == b.rstrip(".").lower()


def _spf_ok(expected: str, observed: list[str]) -> bool:
    wanted = re.search(r"include:(\S+)", expected)
    return any(
        o.lower().startswith("v=spf1")
        and (wanted is None or f"include:{wanted.group(1).lower()}" in o.lower())
        for o in observed
    )


def _check(record: DomainRecord, resolver: DnsResolver) -> tuple[str, str]:
    """Return (status, what was seen) for one record."""
    if record.record_type == TXT:
        seen = resolver.txt(record.name)
        if record.purpose == "spf":
            good = _spf_ok(record.value, seen)
        elif record.purpose == "dmarc":
            good = any(s.lower().startswith("v=dmarc1") for s in seen)
        else:
            good = record.value in seen
    elif record.record_type == CNAME:
        seen = resolver.cname(record.name)
        good = any(_same_host(s, record.value) for s in seen)
    else:
        seen = resolver.mx(record.name)
        good = any(_same_host(s, record.value) for s in seen)
    if good:
        return RecordStatus.OK, "; ".join(seen)
    return (RecordStatus.MISMATCH if seen else RecordStatus.MISSING), "; ".join(seen)


def verify(
    *, workspace_id: uuid.UUID, domain_id: uuid.UUID, resolver: DnsResolver | None = None
) -> SendingDomain:
    """Look every record up now. Required records decide the domain's status."""
    domain = get_domain(workspace_id, domain_id)
    resolver = resolver or SystemResolver()
    now = timezone.now()
    records = list(domain.records.all())
    for record in records:
        try:
            record.status, record.observed = _check(record, resolver)
        except ResolverUnavailableError:
            record.status = RecordStatus.UNKNOWN  # a DNS outage must not unverify a domain
        record.checked_at = now
        record.save(update_fields=["status", "observed", "checked_at", "updated_at"])
    required = [r for r in records if r.required]
    if any(r.status == RecordStatus.UNKNOWN for r in required):
        outcome = domain.status
    elif required and all(r.status == RecordStatus.OK for r in required):
        outcome = DomainStatus.VERIFIED
    else:
        outcome = (
            DomainStatus.FAILED if domain.status == DomainStatus.VERIFIED else DomainStatus.PENDING
        )
    if outcome == DomainStatus.VERIFIED and domain.status != DomainStatus.VERIFIED:
        domain.verified_at = now
    domain.status, domain.last_checked_at = outcome, now
    domain.save(update_fields=["status", "verified_at", "last_checked_at", "updated_at"])
    return domain


def is_verified(workspace_id: uuid.UUID, domain: str) -> bool:
    return (
        SendingDomain.objects.for_workspace(workspace_id)
        .filter(domain=domain.strip().lower(), status=DomainStatus.VERIFIED)
        .exists()
    )


def due_for_recheck() -> list[tuple[uuid.UUID, uuid.UUID]]:
    """(workspace, domain) pairs the periodic job should look at again."""
    rows = SendingDomain.objects.values_list("workspace_id", "pk")
    return list(rows)
