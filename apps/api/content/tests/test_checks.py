import pytest

from content import checks
from content.models import ContentItem, ItemSource


def make(**fields):
    defaults = {
        "kind": "post",
        "source": ItemSource.WORDPRESS,
        "title": "A good title that is about the right length",
        "slug": "a-good-title",
        "url": "https://x.example/a-good-title/",
        "status": "publish",
        "in_sitemap": True,
        "seo": {
            "title": "A good title that is about the right length",
            "description": "d" * 120,
            "canonical": "https://x.example/a-good-title/",
            "robots": "index,follow",
        },
        "outline": [{"level": 2, "text": "One"}, {"level": 2, "text": "Two"}],
        "stats": {"word_count": 900, "internal_links": 4, "images": 2, "images_missing_alt": 0},
        "text": ("Short sentence here. " * 300),
    }
    return ContentItem(**{**defaults, **fields})


def status_of(item, key):
    return next(c.status for c in checks.run_checks(item) if c.key == key)


def test_a_good_post_scores_high():
    assert checks.score_of(checks.run_checks(make())) >= 95


@pytest.mark.parametrize(
    ("seo", "expected"),
    [
        ({"title": "", "description": "", "robots": ""}, "fail"),
        ({"title": "Short", "description": "x", "robots": ""}, "warn"),
    ],
)
def test_title_and_description_lengths(seo, expected):
    item = make(seo=seo, title="")
    assert status_of(item, "seo_title") == expected
    assert status_of(item, "meta_description") == expected


def test_description_over_the_limit_warns():
    item = make(seo={"title": "t" * 45, "description": "d" * 200})
    assert status_of(item, "meta_description") == "warn"


def test_thin_post_fails_and_lowers_the_score():
    thin = make(stats={"word_count": 120, "internal_links": 0, "images": 0})
    assert status_of(thin, "word_count") == "fail"
    assert status_of(thin, "internal_links") == "fail"
    assert checks.score_of(checks.run_checks(thin)) < checks.score_of(checks.run_checks(make()))


def test_pages_do_not_need_internal_links_or_h2():
    page = make(kind="page", outline=[], stats={"word_count": 300, "internal_links": 0})
    assert status_of(page, "internal_links") == "na"
    assert status_of(page, "word_count") == "pass"


def test_wordpress_items_have_no_h1_check_but_public_pages_do():
    assert status_of(make(), "h1") == "na"
    public = make(source=ItemSource.SITEMAP, stats={"word_count": 900, "h1_count": 2})
    assert status_of(public, "h1") == "fail"


def test_noindex_and_missing_alt_and_http_errors():
    assert status_of(make(seo={"robots": "noindex,follow"}), "indexable") == "fail"
    assert (
        status_of(
            make(stats={"word_count": 900, "images": 3, "images_missing_alt": 1}), "image_alt"
        )
        == "warn"
    )
    assert status_of(make(http_status=404), "http_status") == "fail"


def test_headings_and_slug_and_canonical_and_sitemap():
    skipped = make(outline=[{"level": 2, "text": "a"}, {"level": 4, "text": "b"}])
    assert status_of(skipped, "headings") == "warn"
    assert status_of(make(outline=[]), "headings") == "fail"
    assert status_of(make(slug="Bad_Slug"), "slug") == "warn"
    assert status_of(make(seo={"canonical": "https://other.example/"}), "canonical") == "warn"
    assert status_of(make(in_sitemap=False), "in_sitemap") == "warn"
    assert status_of(make(status="draft", in_sitemap=False), "in_sitemap") == "na"


def test_score_ignores_checks_that_do_not_apply():
    assert checks.score_of([checks.Check("x", "na", 10, {})]) == 0
