import pytest

from ads import providers
from ads.meta import checks, sync
from ads.models import AdChange, AdEntity, AdMetricDaily, AdSyncState
from core.errors import AuthExpiredError

pytestmark = pytest.mark.django_db


def test_sync_maps_structure_into_the_shared_levels(meta_connection, fake):
    counts = sync.sync_connection(meta_connection)
    assert counts == {"campaigns": 1, "ad_groups": 1, "ads": 1, "metric_days": 3}
    campaign = AdEntity.objects.get(level="campaign")
    group = AdEntity.objects.get(level="ad_group")
    ad = AdEntity.objects.get(level="ad")
    assert ad.parent == group and group.parent == campaign
    assert campaign.data["budget"] == {"daily_spend_limit_micros": 20_000_000}
    assert campaign.data["objective"] == "OUTCOME_LEADS"
    assert group.data["learning_stage"] == "learning"
    assert group.data["targeting"]["geo_locations"]["countries"] == ["CA"]
    assert ad.data["creative"]["title"] == "Stop worrying about backups"
    assert ad.data["creative"]["target_url"] == "https://acme.example/backups"
    assert ad.review_status == "rejected"
    assert any(i["code"] == "policy_personal_attributes" for i in ad.data["serving_issues"])


def test_results_count_each_action_once_and_spend_is_in_micros(meta_connection, fake):
    sync.sync_connection(meta_connection)
    rows = AdMetricDaily.objects.filter(entity__level="ad")
    assert rows.count() == 3
    day = rows.first()
    assert day.spend_micros == 2_500_000 and day.impressions == 100 and day.clicks == 5
    assert day.conversions == 2  # "lead" only; the pixel variant repeats it


def test_account_facts_are_kept_for_the_checks(meta_connection, fake):
    fake.pixels = [{"id": "p1", "name": "Site pixel", "last_fired_time": None}]
    sync.sync_connection(meta_connection)
    account = AdSyncState.objects.get(connection=meta_connection).account
    assert account["currency_code"] == "USD" and account["status"] == "active"
    assert account["spend_cap_micros"] == 1_000_000_000
    assert account["amount_spent_micros"] == 950_000_000
    assert account["pixels"][0]["id"] == "p1"


def test_a_second_sync_records_changes_and_marks_missing_items(meta_connection, fake):
    sync.sync_connection(meta_connection)
    fake.learning = "SUCCESS"
    fake.created_campaigns.append(
        {"id": "11", "name": "Temp", "status": "PAUSED", "effective_status": "PAUSED"}
    )
    sync.sync_connection(meta_connection)
    assert AdEntity.objects.filter(level="campaign").count() == 2
    fake.created_campaigns.clear()
    sync.sync_connection(meta_connection)
    assert AdEntity.objects.get(external_id="11").missing_since is not None
    assert AdChange.objects.filter(entity__level="ad_group").exists()


def test_platform_errors_are_recorded_on_the_sync_state(meta_connection, fake):
    fake.fail["x"] = (500, {})
    from ads.meta import factory

    original = factory.client_factory

    def broken(connection):
        client = original(connection)

        def boom(*_a, **_k):
            raise AuthExpiredError("expired", provider="meta")

        client.get_account = boom
        return client

    factory.client_factory = broken
    try:
        with pytest.raises(AuthExpiredError):
            sync.sync_connection(meta_connection)
    finally:
        factory.client_factory = original
    assert AdSyncState.objects.get(connection=meta_connection).last_error


def test_the_shared_registry_knows_meta(meta_connection):
    provider = providers.for_connection(meta_connection)
    assert provider.action_prefix == "ads.meta." and provider.label == "Meta Ads"


def test_checks_flag_review_learning_and_missing_tracking(meta_connection, fake):
    sync.sync_connection(meta_connection)
    account = AdSyncState.objects.get(connection=meta_connection).account
    ad = AdEntity.objects.get(level="ad")
    codes = {c["code"] for c in checks.run_checks(ad, account)}
    assert {"review_rejected", "serving_issues"} <= codes
    campaign = AdEntity.objects.get(level="campaign")
    campaign_codes = {c["code"]: c for c in checks.run_checks(campaign, account)}
    assert "no_pixel" in campaign_codes and "spend_cap_near" in campaign_codes
    fake.learning = "FAIL"
    sync.sync_connection(meta_connection)
    group = AdEntity.objects.get(level="ad_group")
    assert "learning_limited" in {c["code"] for c in checks.run_checks(group, account)}
