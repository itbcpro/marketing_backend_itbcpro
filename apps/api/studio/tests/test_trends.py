from datetime import timedelta

import pytest
from django.utils import timezone

from content.models import GscQueryDaily, Website
from core.errors import DomainError, PermanentProviderError, ValidationFailedError
from studio import net
from studio.models import TrendItem, TrendSource
from studio.tests.conftest import brand_of, feed_transport, make_config
from studio.trends import collect
from studio.trends.community import OwnSearchParams, OwnSearchSource
from studio.trends.feeds import parse_feed
from studio.trends.ports import get_trend_source, validate_params
from studio.trends.suggest import suggestions

pytestmark = pytest.mark.django_db

RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Blog</title>
<item><title>Ransomware hits a regional clinic</title><link>https://news.example.com/a?utm_source=x</link>
<description>&lt;p&gt;A &lt;b&gt;clinic&lt;/b&gt; lost its patient system.&lt;/p&gt;</description>
<pubDate>Mon, 12 Oct 2026 10:00:00 +0000</pubDate></item>
<item><title>New Windows update breaks printing</title><link>https://news.example.com/b</link></item>
</channel></rss>"""
ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>x</title>
<entry><title>Atom entry</title><link href="https://a.example.com/1"/>
<updated>2026-10-12T10:00:00Z</updated><summary>Short summary</summary></entry></feed>"""
GOOGLE_TRENDS = b"""<?xml version="1.0"?><rss version="2.0" xmlns:ht="https://trends.google.com/trending/rss">
<channel><item><title>ransomware</title><ht:approx_traffic>200,000+</ht:approx_traffic>
<link>https://trends.google.com/trending?geo=CA</link>
<ht:news_item><ht:news_item_title>Clinic hit by ransomware</ht:news_item_title></ht:news_item></item>
<item><title>quiet topic</title><ht:approx_traffic>200+</ht:approx_traffic></item></channel></rss>"""
EVIL = b"""<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY a "aaaa">]><rss><channel><item><title>&a;</title></item></channel></rss>"""


def test_rss_items_are_cleaned_and_ranked():
    items = parse_feed(RSS)
    assert items[0].title == "Ransomware hits a regional clinic"
    assert items[0].summary == "A clinic lost its patient system."
    assert items[0].published_at is not None and items[0].published_at.year == 2026
    assert items[0].score > items[1].score


def test_atom_feeds_are_read_too():
    (item,) = parse_feed(ATOM)
    assert item.url == "https://a.example.com/1" and item.summary == "Short summary"
    assert item.published_at is not None


def test_search_traffic_lifts_the_score_and_news_titles_become_the_summary():
    first, second = parse_feed(GOOGLE_TRENDS)
    assert first.summary == "Clinic hit by ransomware"
    assert first.score > second.score


def test_xml_with_entities_is_refused_and_junk_is_an_error():
    with pytest.raises(PermanentProviderError):
        parse_feed(EVIL)
    with pytest.raises(PermanentProviderError):
        parse_feed(b"<html>not a feed")


def test_source_settings_are_validated():
    with pytest.raises(ValidationFailedError):
        validate_params("rss", {"url": "ftp://x"})
    with pytest.raises(ValidationFailedError):
        validate_params("nope", {})
    assert validate_params("news_search", {"query": "managed IT"}).model_dump()["country"] == "US"


def make_source(workspace, **fields) -> TrendSource:
    defaults = {"kind": "rss", "name": "Blog", "params": {"url": "https://feeds.example.com/a.xml"}}
    defaults.update(fields)
    return TrendSource.objects.create(
        workspace_id=workspace.pk, brand=brand_of(workspace), **defaults
    )


def test_collecting_stores_signals_once(workspace, monkeypatch):
    monkeypatch.setattr(net, "transport", feed_transport({"https://feeds.example.com/": RSS}))
    source = make_source(workspace)
    first = collect.collect_brand(brand_of(workspace).pk)
    again = collect.collect_brand(brand_of(workspace).pk)
    assert first.new_items == 2 and again.new_items == 0
    assert TrendItem.objects.filter(workspace_id=workspace.pk).count() == 2
    source.refresh_from_db()
    assert source.last_count == 2 and source.last_error == "" and source.last_fetched_at


def test_tracking_parameters_do_not_create_duplicates(workspace, monkeypatch):
    first = RSS
    second = RSS.replace(b"?utm_source=x", b"?utm_source=y")
    monkeypatch.setattr(net, "transport", feed_transport({"https://feeds.example.com/": first}))
    make_source(workspace)
    collect.collect_brand(brand_of(workspace).pk)
    monkeypatch.setattr(net, "transport", feed_transport({"https://feeds.example.com/": second}))
    collect.collect_brand(brand_of(workspace).pk)
    assert TrendItem.objects.filter(workspace_id=workspace.pk).count() == 2


def test_a_failing_source_does_not_stop_the_others(workspace, monkeypatch):
    routes = {"https://feeds.example.com/": RSS, "https://down.example.com/": 500}
    monkeypatch.setattr(net, "transport", feed_transport(routes))
    good = make_source(workspace)
    bad = make_source(workspace, name="Down", params={"url": "https://down.example.com/x.xml"})
    result = collect.collect_brand(brand_of(workspace).pk)
    assert result.sources == 2 and result.failed == 1 and result.new_items == 2
    bad.refresh_from_db()
    good.refresh_from_db()
    assert bad.last_error and not good.last_error


def test_disabled_sources_are_skipped(workspace, monkeypatch):
    monkeypatch.setattr(net, "transport", feed_transport({"https://feeds.example.com/": RSS}))
    make_source(workspace, enabled=False)
    assert collect.collect_brand(brand_of(workspace).pk).sources == 0


def test_topics_to_avoid_are_filtered_out(workspace, owner, monkeypatch):
    from studio import config_store

    config_store.save(
        brand_of(workspace), make_config(trends={"avoid_topics": ["printing"]}), owner
    )
    monkeypatch.setattr(net, "transport", feed_transport({"https://feeds.example.com/": RSS}))
    make_source(workspace)
    collect.collect_brand(brand_of(workspace).pk)
    titles = list(
        TrendItem.objects.filter(workspace_id=workspace.pk).values_list("title", flat=True)
    )
    assert titles == ["Ransomware hits a regional clinic"]


def test_old_signals_are_pruned(workspace):
    old = TrendItem.objects.create(
        workspace_id=workspace.pk,
        brand=brand_of(workspace),
        kind="rss",
        title="Old",
        fingerprint="a" * 40,
    )
    TrendItem.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=60))
    fresh = TrendItem.objects.create(
        workspace_id=workspace.pk,
        brand=brand_of(workspace),
        kind="rss",
        title="New",
        fingerprint="b" * 40,
    )
    assert collect.prune(brand_of(workspace).pk, 7) == 1
    assert list(TrendItem.objects.values_list("pk", flat=True)) == [fresh.pk]


def test_recent_items_prefer_strong_signals(workspace):
    TrendItem.objects.create(
        workspace_id=workspace.pk,
        brand=brand_of(workspace),
        kind="rss",
        title="Weak",
        fingerprint="a" * 40,
        score=0.1,
    )
    TrendItem.objects.create(
        workspace_id=workspace.pk,
        brand=brand_of(workspace),
        kind="rss",
        title="Strong",
        fingerprint="b" * 40,
        score=0.9,
    )
    TrendItem.objects.create(
        workspace_id=workspace.pk,
        brand=brand_of(workspace),
        kind="rss",
        title="Hidden",
        fingerprint="c" * 40,
        score=1.0,
        ignored=True,
    )
    assert [i.title for i in collect.recent_items(brand_of(workspace).pk, 7, 10)] == [
        "Strong",
        "Weak",
    ]


def test_rising_searches_on_the_own_site_become_signals(workspace):
    site = Website.objects.create(
        workspace_id=workspace.pk, name="Acme", kind="wordpress", base_url="https://acme.test"
    )
    today = timezone.localdate()
    rows = [
        ("managed it toronto", 3, 300),  # recent
        ("managed it toronto", 20, 40),  # before
        ("steady query", 3, 100),
        ("steady query", 20, 100),
        ("tiny query", 3, 5),
    ]
    for query, days_ago, impressions in rows:
        GscQueryDaily.objects.create(
            workspace_id=workspace.pk,
            website=site,
            date=today - timedelta(days=days_ago),
            query=query,
            page="/x",
            clicks=impressions // 10,
            impressions=impressions,
        )
    found = OwnSearchSource().fetch(workspace.pk, OwnSearchParams())
    assert found[0].title == "managed it toronto"
    assert "tiny query" not in {t.title for t in found}


def test_suggestions_come_from_the_owners_topics():
    config = make_config(trends={"topics": ["managed IT", "cybersecurity"], "geo": "ca"})
    picks = suggestions(config.trends)
    kinds = [p.kind for p in picks]
    assert (
        kinds.count("news_search") == 2 and "trending_searches" in kinds and "own_searches" in kinds
    )
    news = next(p for p in picks if p.kind == "news_search")
    assert news.params["country"] == "CA" and news.name == "News: managed IT"


def test_every_source_kind_has_a_default_name():
    for kind in (
        "rss",
        "news_search",
        "trending_searches",
        "reddit",
        "hacker_news",
        "own_searches",
    ):
        source = get_trend_source(kind)
        params = {
            "rss": {"url": "https://a.example.com/x"},
            "news_search": {"query": "it"},
            "reddit": {"subreddit": "sysadmin"},
        }.get(kind, {})
        assert source.default_name(validate_params(kind, params))


def test_feed_addresses_that_point_inside_the_network_are_refused(workspace, monkeypatch):
    monkeypatch.setattr(net, "resolver", lambda host, port: ["10.0.0.5"])
    monkeypatch.setattr(net, "transport", feed_transport({"http://": RSS}))
    source = make_source(workspace, params={"url": "https://internal.example.com/feed.xml"})
    result = collect.collect_brand(brand_of(workspace).pk)
    source.refresh_from_db()
    assert result.failed == 1 and source.last_error and not TrendItem.objects.exists()
    with pytest.raises(DomainError):
        net.fetch("https://internal.example.com/feed.xml")
