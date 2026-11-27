"""Mail authentication health of a domain: SPF, DKIM, DMARC and friends, with what to fix.

Unlike :mod:`crm.sending.domains` (which checks the exact records our provider asks for), this
looks at what the domain publishes and judges it against current practice: one SPF record that
stays within ten DNS lookups and ends in ``-all`` or ``~all``, a DKIM key of at least 2048 bits,
and a DMARC policy that is enforced and reported. Every finding is a stable code with parameters;
the wording lives in the web catalog.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass, field
from typing import Literal

from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
from cryptography.hazmat.primitives.serialization import load_der_public_key

from crm.sending.dns import DnsResolver, ResolverUnavailableError

Severity = Literal["ok", "info", "warning", "error"]
MAX_SPF_LOOKUPS = 10
SPF_LOOKUP_WARN = 9
MAX_SPF_DEPTH = 10
FULL_PERCENT = 100
MIN_RSA_BITS = 1024
GOOD_RSA_BITS = 2048
MAX_SELECTORS = 10
SELECTOR_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9._-]{0,60}[a-z0-9])?$")
# Selectors that big mail providers use by default. DNS cannot list selectors, so we probe these.
COMMON_SELECTORS = (
    "default",
    "google",
    "selector1",
    "selector2",
    "k1",
    "k2",
    "k3",
    "s1",
    "s2",
    "mail",
    "dkim",
    "smtp",
    "mandrill",
    "pm",
    "zoho",
    "mxvault",
)
LOOKUP_TERMS = ("include", "a", "mx", "ptr", "exists", "redirect")


@dataclass(frozen=True)
class Finding:
    check: str
    severity: Severity
    code: str
    params: dict[str, str] = field(default_factory=dict)
    observed: str = ""


@dataclass
class HealthReport:
    domain: str
    findings: list[Finding] = field(default_factory=list)

    @property
    def status(self) -> str:
        severities = {f.severity for f in self.findings}
        return "fail" if "error" in severities else "warn" if "warning" in severities else "pass"

    def add(
        self,
        check: str,
        severity: Severity,
        code: str,
        observed: str = "",
        **params: str | int,
    ) -> None:
        self.findings.append(
            Finding(check, severity, code, {k: str(v) for k, v in params.items()}, observed)
        )


def _txt(resolver: DnsResolver, name: str) -> list[str] | None:
    """TXT strings, or None when DNS could not answer (that is not the same as "absent")."""
    try:
        return resolver.txt(name)
    except ResolverUnavailableError:
        return None


# ---- SPF ----------------------------------------------------------------------------------


def _spf_terms(record: str) -> list[str]:
    return record.split()[1:]


def _term_name(term: str) -> str:
    bare = term.lstrip("+-~?")
    return re.split(r"[:=/]", bare, maxsplit=1)[0].lower()


def _count_lookups(
    resolver: DnsResolver, record: str, seen: set[str], depth: int, report: HealthReport
) -> int:
    """DNS lookups this record causes, following ``include`` and ``redirect`` (RFC 7208 §4.6.4)."""
    total = 0
    for term in _spf_terms(record):
        name = _term_name(term)
        if name not in LOOKUP_TERMS:
            continue
        total += 1
        if name not in {"include", "redirect"}:
            continue
        target = term.split(":" if name == "include" else "=", 1)[-1].lower()
        if target in seen or depth >= MAX_SPF_DEPTH:
            continue
        seen.add(target)
        found = _txt(resolver, target)
        spf = [t for t in found or [] if t.lower().startswith("v=spf1")]
        if found is not None and not spf:
            report.add("spf", "error", "spf_include_missing", target=target)
        elif spf:
            total += _count_lookups(resolver, spf[0], seen, depth + 1, report)
    return total


def check_spf(resolver: DnsResolver, domain: str, report: HealthReport) -> None:
    found = _txt(resolver, domain)
    if found is None:
        report.add("spf", "warning", "dns_unavailable", name=domain)
        return
    records = [t for t in found if t.lower().startswith("v=spf1")]
    if not records:
        report.add("spf", "error", "spf_missing")
        return
    if len(records) > 1:
        report.add("spf", "error", "spf_multiple", " | ".join(records), count=len(records))
        return
    record = records[0]
    terms = _spf_terms(record)
    names = [_term_name(t) for t in terms]
    all_term = next((t for t in terms if _term_name(t) == "all"), "")
    if "all" in names and names.index("all") != len(names) - 1 and "redirect" not in names:
        report.add("spf", "warning", "spf_terms_after_all", record)
    if all_term in {"-all", "~all"}:
        report.add("spf", "ok", "spf_all_ok", record, policy=all_term)
    elif all_term in {"+all", "all"}:
        report.add("spf", "error", "spf_allows_everyone", record)
    elif all_term == "?all":
        report.add("spf", "warning", "spf_neutral", record)
    elif "redirect" not in names:
        report.add("spf", "warning", "spf_no_all", record)
    if "ptr" in names:
        report.add("spf", "warning", "spf_uses_ptr", record)
    _report_spf_lookups(resolver, domain, record, report)


def _report_spf_lookups(
    resolver: DnsResolver, domain: str, record: str, report: HealthReport
) -> None:
    lookups = _count_lookups(resolver, record, {domain}, 0, report)
    if lookups > MAX_SPF_LOOKUPS:
        report.add("spf", "error", "spf_too_many_lookups", record, lookups=lookups)
    elif lookups >= SPF_LOOKUP_WARN:
        report.add("spf", "warning", "spf_near_lookup_limit", record, lookups=lookups)
    else:
        report.add("spf", "ok", "spf_lookups_ok", record, lookups=lookups)


# ---- DKIM ---------------------------------------------------------------------------------


def _tags(record: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in record.split(";"):
        key, sep, value = part.partition("=")
        if sep:
            out[key.strip().lower()] = value.strip()
    return out


def _key_bits(public: str, kind: str) -> int | None:
    """Size in bits of the published key, or None when it cannot be read."""
    try:
        key = load_der_public_key(base64.b64decode(public.replace(" ", ""), validate=False))
    except binascii.Error, ValueError:
        return None
    if isinstance(key, rsa.RSAPublicKey):
        return key.key_size
    if isinstance(key, ed25519.Ed25519PublicKey):
        return 256 if kind == "ed25519" else None
    return None


def _check_dkim_record(report: HealthReport, selector: str, record: str) -> None:
    tags = _tags(record)
    kind = tags.get("k", "rsa").lower()
    public = tags.get("p", "")
    if not public:
        report.add("dkim", "error", "dkim_revoked", record, selector=selector)
        return
    bits = _key_bits(public, kind)
    if bits is None:
        report.add("dkim", "error", "dkim_key_unreadable", record, selector=selector)
    elif kind == "rsa" and bits < MIN_RSA_BITS:
        report.add("dkim", "error", "dkim_weak_key", record, selector=selector, bits=bits)
    elif kind == "rsa" and bits < GOOD_RSA_BITS:
        report.add("dkim", "warning", "dkim_key_1024", record, selector=selector, bits=bits)
    else:
        report.add("dkim", "ok", "dkim_ok", record, selector=selector, bits=bits)
    if tags.get("t", "").lower().startswith("y"):
        report.add("dkim", "warning", "dkim_testing", record, selector=selector)


def check_dkim(
    resolver: DnsResolver, domain: str, selectors: list[str], report: HealthReport
) -> None:
    asked = list(dict.fromkeys(s.lower() for s in selectors))
    guessed = [s for s in COMMON_SELECTORS if s not in asked]
    found_any = False
    for selector in [*asked, *guessed]:
        records = _txt(resolver, f"{selector}._domainkey.{domain}")
        if records is None:
            report.add("dkim", "warning", "dns_unavailable", name=f"{selector}._domainkey")
            return
        record = next((r for r in records if "p=" in r.replace(" ", "")), "")
        if record:
            found_any = True
            _check_dkim_record(report, selector, record)
        elif selector in asked:
            report.add("dkim", "error", "dkim_selector_missing", selector=selector)
    if not found_any and not asked:
        report.add("dkim", "warning", "dkim_not_found")


# ---- DMARC --------------------------------------------------------------------------------


def _parents(domain: str) -> list[str]:
    labels = domain.split(".")
    return [".".join(labels[i:]) for i in range(1, len(labels) - 1)]


def _dmarc_records(resolver: DnsResolver, domain: str) -> tuple[str, list[str]] | None:
    """The DMARC record that applies: the domain's own, else its nearest parent's."""
    for name in [domain, *_parents(domain)]:
        found = _txt(resolver, f"_dmarc.{name}")
        if found is None:
            return None
        records = [t for t in found if t.lower().startswith("v=dmarc1")]
        if records:
            return name, records
    return "", []


def _report_authorised(resolver: DnsResolver, domain: str, address: str) -> bool:
    host = address.rsplit("@", 1)[-1].strip().lower()
    if host == domain or host.endswith(f".{domain}") or domain.endswith(f".{host}"):
        return True
    found = _txt(resolver, f"{domain}._report._dmarc.{host}") or []
    return any(t.lower().startswith("v=dmarc1") for t in found)


def check_dmarc(resolver: DnsResolver, domain: str, report: HealthReport) -> None:
    hit = _dmarc_records(resolver, domain)
    if hit is None:
        report.add("dmarc", "warning", "dns_unavailable", name=f"_dmarc.{domain}")
        return
    where, records = hit
    if not records:
        report.add("dmarc", "error", "dmarc_missing")
        return
    if len(records) > 1:
        report.add("dmarc", "error", "dmarc_multiple", " | ".join(records), count=len(records))
        return
    record = records[0]
    tags = _tags(record)
    if where != domain:
        report.add("dmarc", "info", "dmarc_inherited", record, parent=where)
    policy = tags.get("p", "").lower()
    if policy in {"reject", "quarantine"}:
        report.add("dmarc", "ok", "dmarc_enforced", record, policy=policy)
    elif policy == "none":
        report.add("dmarc", "warning", "dmarc_monitoring_only", record)
    else:
        report.add("dmarc", "error", "dmarc_invalid_policy", record, policy=policy or "-")
    pct = tags.get("pct", "100")
    if pct.isdigit() and int(pct) < FULL_PERCENT and policy in {"quarantine", "reject"}:
        report.add("dmarc", "warning", "dmarc_partial", record, pct=pct)
    _check_dmarc_reports(resolver, where or domain, record, tags, report)


def _check_dmarc_reports(
    resolver: DnsResolver, domain: str, record: str, tags: dict[str, str], report: HealthReport
) -> None:
    reports = [a.strip() for a in tags.get("rua", "").split(",") if a.strip()]
    if not reports:
        report.add("dmarc", "warning", "dmarc_no_reports", record)
    for address in reports:
        mailbox = address.removeprefix("mailto:")
        if "@" in mailbox and not _report_authorised(resolver, domain, mailbox):
            report.add("dmarc", "warning", "dmarc_report_unauthorised", record, address=mailbox)


# ---- the rest -----------------------------------------------------------------------------


def check_extras(resolver: DnsResolver, domain: str, report: HealthReport) -> None:
    try:
        has_mx = bool(resolver.mx(domain))
    except ResolverUnavailableError:
        report.add("mx", "warning", "dns_unavailable", name=domain)
        return
    report.add("mx", "ok" if has_mx else "warning", "mx_ok" if has_mx else "mx_missing")
    sts = _txt(resolver, f"_mta-sts.{domain}") or []
    if not any(t.lower().startswith("v=stsv1") for t in sts):
        report.add("mta_sts", "info", "mta_sts_missing")
    tls = _txt(resolver, f"_smtp._tls.{domain}") or []
    if not any(t.lower().startswith("v=tlsrptv1") for t in tls):
        report.add("tls_rpt", "info", "tls_rpt_missing")


def clean_selectors(selectors: list[str]) -> list[str]:
    """Valid DKIM selector names only, at most :data:`MAX_SELECTORS`."""
    cleaned = [s.strip().lower() for s in selectors if SELECTOR_PATTERN.match(s.strip().lower())]
    return list(dict.fromkeys(cleaned))[:MAX_SELECTORS]


def check_domain(
    domain: str, resolver: DnsResolver, selectors: list[str] | None = None
) -> HealthReport:
    """Run every check for an already validated, lower-case domain."""
    report = HealthReport(domain)
    check_spf(resolver, domain, report)
    check_dkim(resolver, domain, clean_selectors(selectors or []), report)
    check_dmarc(resolver, domain, report)
    check_extras(resolver, domain, report)
    return report
