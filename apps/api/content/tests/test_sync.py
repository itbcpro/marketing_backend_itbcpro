import pytest

from audit.actors import Actor
from content import selectors, services, sync
from content.models import ContentItem, SyncRun, SyncStatus, WebsiteKind
from content.tests.fake_wordpress import BASE, HOST

SYSTEM = Actor.system("test")


@pytest.fixture(autouse=True)
def no_pause(monkeypatch):
    monkeypatch.setattr(sync, "sleeper", lambda _seconds: None)


@pytest.fixture
def git_site(workspace, wp):
    wp.public_pages.update(
        {
            "/hello-world/": '<html lang="en"><head><title>Hello</title></head><body><h1>Hello</h1>'
            "<p>Some words here.</p></body></html>",
            "/about/": "<html><head><title>About</title></head><body><h1>About</h1></body></html>",
            "/services/": "<html><head><title>Services</title></head><body></body></html>",
            "/category/news/": "<html><head><title>News</title></head><body></body></html>",
        }
    )
    return services.create_website(
        workspace_id=workspace.pk,
        name="AI site",
        kind=WebsiteKind.GIT_SITE,
        base_url=BASE,
        actor=SYSTEM,
    )


def _run(site, *, full=False):
    return sync.execute_run(sync.start_run(site, full=full))


def test_wordpress_full_sync_builds_inventory(wp_site):
    run = _run(wp_site, full=True)
    assert run.status == SyncStatus.DONE, run.error
    assert run.stats["by_type"]["post"]["created"] == 3
    assert run.stats["by_type"]["page"]["created"] == 2
    assert "elementor_library" not in run.stats["by_type"]

    post = ContentItem.objects.get(website=wp_site, key="wp:post:1")
    assert post.title == "Hello World"
    assert post.url == f"{BASE}/hello-world/"
    assert post.author == "Editor"
    assert post.terms == {"categories": ["News"], "tags": ["Security"]}
    assert post.seo["description"] == "A description"
    assert post.stats["word_count"] >= 350
    assert post.stats["internal_links"] == 1
    assert post.stats["external_links"] == 1
    assert post.stats["images"] == 2
    assert post.stats["images_missing_alt"] == 1
    assert post.outline == [{"level": 2, "text": "Sub heading"}]
    assert post.in_sitemap is True
    assert post.language == "en"


def test_sitemap_cross_check(wp_site):
    _run(wp_site, full=True)
    short = ContentItem.objects.get(website=wp_site, key="wp:post:2")
    assert short.in_sitemap is False
    archive = ContentItem.objects.get(website=wp_site, url=f"{BASE}/category/news/")
    assert archive.kind == "other"
    assert archive.in_sitemap is True


def test_summary_matches_wordpress_counts(wp_site):
    _run(wp_site, full=True)
    summary = selectors.summary(wp_site)
    assert summary["by_kind"]["post"] == 3
    assert summary["wordpress_check"]["post"] == {"inventory": 3, "wordpress": 3, "match": True}
    assert summary["wordpress_check"]["page"]["match"] is True
    assert summary["issues"]["thin_content"] == 2
    assert summary["issues"]["no_meta_description"] == 1
    assert summary["issues"]["not_in_sitemap"] == 1


def test_incremental_sync_picks_up_an_edit(wp_site, wp):
    _run(wp_site, full=True)
    wp.edit("post", 1, title="Hello Again")
    run = _run(wp_site)
    assert run.stats["incremental"] is True
    assert run.stats["updated"] == 1
    assert ContentItem.objects.get(website=wp_site, key="wp:post:1").title == "Hello Again"
    assert any("modified_after" in q for q in wp.queries)


def test_unchanged_sync_changes_nothing(wp_site):
    _run(wp_site, full=True)
    again = _run(wp_site, full=True)
    assert again.stats.get("created", 0) == 0
    assert again.stats.get("updated", 0) == 0
    assert again.stats["unchanged"] == 5


def test_deleted_post_is_marked_removed(wp_site, wp):
    _run(wp_site, full=True)
    wp.remove("post", 2)
    run = _run(wp_site)
    assert run.stats["removed"] == 1
    assert ContentItem.objects.get(website=wp_site, key="wp:post:2").removed_at is not None
    assert selectors.summary(wp_site)["by_kind"]["post"] == 2


def test_failed_sync_is_recorded(wp_site, wp):
    wp.password = "rotated"
    run = _run(wp_site, full=True)
    assert run.status == SyncStatus.FAILED
    assert run.error
    assert ContentItem.objects.filter(website=wp_site).count() == 0


def test_git_site_sync_reads_sitemap_pages(git_site):
    run = _run(git_site, full=True)
    assert run.status == SyncStatus.DONE, run.error
    assert run.stats["sitemap_urls"] == 5
    hello = ContentItem.objects.get(website=git_site, url=f"{BASE}/hello-world/")
    assert hello.title == "Hello"
    assert hello.http_status == 200
    assert hello.outline == [{"level": 1, "text": "Hello"}]
    assert hello.stats["h1_count"] == 1
    assert "Some words here." in hello.text
    home = ContentItem.objects.get(website=git_site, url=f"{BASE}/")
    assert home.seo["description"] == "Managed IT services"
    assert home.seo["canonical"] == f"{BASE}/"


def test_git_site_second_sync_skips_unchanged_pages(git_site, wp):
    _run(git_site, full=True)
    wp.calls.clear()
    run = _run(git_site)
    assert run.stats["fetched"] == 0
    assert run.stats["unchanged"] == 5
    assert "/about/" not in wp.calls


def test_git_site_removed_from_sitemap(git_site, wp):
    _run(git_site, full=True)
    wp.sitemap_excludes.add("about")
    run = _run(git_site, full=True)
    assert run.stats["removed"] == 1
    assert ContentItem.objects.get(website=git_site, url=f"{BASE}/about/").removed_at is not None


def test_git_site_missing_page_is_an_error_item(git_site, wp):
    del wp.public_pages["/services/"]
    _run(git_site, full=True)
    item = ContentItem.objects.get(website=git_site, url=f"{BASE}/services/")
    assert item.http_status == 404
    assert "http_error" in selectors.issues_of(item)


def test_runs_are_listed_newest_first(wp_site):
    _run(wp_site, full=True)
    _run(wp_site)
    runs = list(SyncRun.objects.filter(website=wp_site))
    assert [r.full for r in runs] == [False, True]
    assert HOST in wp_site.base_url


def test_output_printed_before_the_json_is_tolerated(wp_site, wp):
    wp.junk_prefix = '<style id="elementor-post-1">.a{--x:1;}.b{color:red}</style>'
    run = _run(wp_site, full=True)
    assert run.status == SyncStatus.DONE, run.error
    assert run.stats["created"] == 5
