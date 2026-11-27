"""SPF, DKIM and DMARC judging: what is flagged, what passes, what the API returns."""

import base64
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.test import Client

from conftest import make_user
from crm.sending import api as sending_api
from crm.sending import dns_health
from crm.sending.tests.test_domains import FakeResolver
from workspaces.models import Role

DOMAIN = "mail.acme.test"


def key(bits: int) -> str:
    public = rsa.generate_private_key(public_exponent=65537, key_size=bits).public_key()
    der = public.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return base64.b64encode(der).decode()


KEY_2048, KEY_1024 = key(2048), key(1024)
GOOD = {
    DOMAIN: ["v=spf1 include:_spf.mailer.test -all"],
    "_spf.mailer.test": ["v=spf1 ip4:192.0.2.0/24 -all"],
    f"google._domainkey.{DOMAIN}": [f"v=DKIM1; k=rsa; p={KEY_2048}"],
    f"_dmarc.{DOMAIN}": ["v=DMARC1; p=reject; rua=mailto:dmarc@acme.test"],
}


def run(txt, selectors=None, mx=True):
    resolver = FakeResolver(txt=txt, mx={DOMAIN: ["mx.mailer.test"]} if mx else {})
    return dns_health.check_domain(DOMAIN, resolver, selectors)


def codes(report, check=None):
    return {f.code for f in report.findings if check is None or f.check == check}


def test_a_well_set_up_domain_passes_with_only_suggestions():
    report = run(GOOD)
    assert report.status == "pass"
    assert {"spf_all_ok", "spf_lookups_ok", "dkim_ok", "dmarc_enforced", "mx_ok"} <= codes(report)
    assert {f.severity for f in report.findings} <= {"ok", "info"}


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        (None, "spf_missing"),
        (["v=spf1 -all", "v=spf1 ~all"], "spf_multiple"),
        (["v=spf1 +all"], "spf_allows_everyone"),
        (["v=spf1 ?all"], "spf_neutral"),
        (["v=spf1 ip4:192.0.2.1"], "spf_no_all"),
        (["v=spf1 ptr -all"], "spf_uses_ptr"),
        (["v=spf1 -all ip4:192.0.2.1"], "spf_terms_after_all"),
        (["v=spf1 include:gone.test -all"], "spf_include_missing"),
    ],
)
def test_spf_problems_are_named(record, expected):
    txt = {**GOOD, DOMAIN: record} if record else {k: v for k, v in GOOD.items() if k != DOMAIN}
    assert expected in codes(run(txt), "spf")


def test_spf_lookups_are_counted_through_includes():
    includes = " ".join(f"include:i{n}.test" for n in range(11))
    txt = {**GOOD, DOMAIN: [f"v=spf1 {includes} -all"]}
    txt.update({f"i{n}.test": ["v=spf1 ip4:192.0.2.1 -all"] for n in range(11)})
    report = run(txt)
    flagged = next(f for f in report.findings if f.code == "spf_too_many_lookups")
    assert flagged.severity == "error" and flagged.params["lookups"] == "11"
    nested = {**GOOD, DOMAIN: ["v=spf1 include:a.test -all"], "a.test": ["v=spf1 a mx -all"]}
    ok = next(f for f in run(nested).findings if f.code == "spf_lookups_ok")
    assert ok.params["lookups"] == "3"


def test_dkim_key_strength_and_selectors():
    weak = {**GOOD, f"google._domainkey.{DOMAIN}": [f"v=DKIM1; k=rsa; p={KEY_1024}"]}
    assert "dkim_key_1024" in codes(run(weak), "dkim")
    revoked = {**GOOD, f"google._domainkey.{DOMAIN}": ["v=DKIM1; k=rsa; p="]}
    assert "dkim_revoked" in codes(run(revoked), "dkim")
    testing = {**GOOD, f"google._domainkey.{DOMAIN}": [f"v=DKIM1; t=y; p={KEY_2048}"]}
    assert "dkim_testing" in codes(run(testing), "dkim")
    broken = {**GOOD, f"google._domainkey.{DOMAIN}": ["v=DKIM1; p=bm90IGEga2V5"]}
    assert "dkim_key_unreadable" in codes(run(broken), "dkim")
    none = {k: v for k, v in GOOD.items() if "_domainkey" not in k}
    assert "dkim_not_found" in codes(run(none), "dkim")
    assert "dkim_selector_missing" in codes(run(none, ["mine"]), "dkim")
    found = run(GOOD, ["google"])
    assert "dkim_selector_missing" not in codes(found)
    assert dns_health.clean_selectors(["Good", "bad selector!", "good", "x" * 80]) == ["good"]


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        (None, "dmarc_missing"),
        (["v=DMARC1; p=none; rua=mailto:d@acme.test"], "dmarc_monitoring_only"),
        (["v=DMARC1; p=reject"], "dmarc_no_reports"),
        (["v=DMARC1; p=banana; rua=mailto:d@acme.test"], "dmarc_invalid_policy"),
        (["v=DMARC1; p=reject; pct=25; rua=mailto:d@acme.test"], "dmarc_partial"),
        (["v=DMARC1; p=reject; rua=mailto:d@other.test"], "dmarc_report_unauthorised"),
        (["v=DMARC1; p=none", "v=DMARC1; p=reject"], "dmarc_multiple"),
    ],
)
def test_dmarc_problems_are_named(record, expected):
    name = f"_dmarc.{DOMAIN}"
    txt = {**GOOD, name: record} if record else {k: v for k, v in GOOD.items() if k != name}
    assert expected in codes(run(txt), "dmarc")


def test_dmarc_is_inherited_from_the_parent_domain_and_reports_may_be_authorised():
    own = f"_dmarc.{DOMAIN}"
    txt = {k: v for k, v in GOOD.items() if k != own}
    txt["_dmarc.acme.test"] = ["v=DMARC1; p=reject; rua=mailto:d@acme.test"]
    report = run(txt)
    assert "dmarc_inherited" in codes(report) and "dmarc_missing" not in codes(report)
    other = {**GOOD, own: ["v=DMARC1; p=reject; rua=mailto:d@reports.test"]}
    other[f"{DOMAIN}._report._dmarc.reports.test"] = ["v=DMARC1"]
    assert "dmarc_report_unauthorised" not in codes(run(other))


def test_missing_mx_and_a_dns_outage_do_not_crash():
    assert "mx_missing" in codes(run(GOOD, mx=False))
    down = dns_health.check_domain(DOMAIN, FakeResolver(down=True))
    assert {f.code for f in down.findings} == {"dns_unavailable"}


def test_api_returns_findings_and_is_limited_to_managers(workspace, owner, monkeypatch):
    monkeypatch.setattr(
        sending_api,
        "SystemResolver",
        lambda: FakeResolver(txt=GOOD, mx={DOMAIN: ["mx.mailer.test"]}),
    )
    admin = Client()
    admin.force_login(owner)
    body = json.dumps({"domain": "Mail.Acme.test.", "selectors": ["google"]})
    kwargs = {"HTTP_X_WORKSPACE_ID": str(workspace.pk), "content_type": "application/json"}
    response = admin.post("/api/v1/crm/email/dns-health", body, **kwargs)
    assert response.status_code == 200, response.content
    data = response.json()
    assert data["domain"] == DOMAIN and data["status"] == "pass"
    assert any(f["code"] == "dkim_ok" and f["params"]["bits"] == "2048" for f in data["findings"])
    bad = admin.post("/api/v1/crm/email/dns-health", json.dumps({"domain": "no spaces"}), **kwargs)
    assert bad.status_code == 422
    viewer = Client()
    viewer.force_login(make_user("v@example.com", workspace, Role.VIEWER))
    assert viewer.post("/api/v1/crm/email/dns-health", body, **kwargs).status_code == 403
