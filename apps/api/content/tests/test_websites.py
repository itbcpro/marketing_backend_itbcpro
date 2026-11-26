import pytest

from audit.actors import Actor
from connections import lifecycle
from connections.models import ConnectionStatus
from content import services
from content.models import Website, WebsiteKind, WebsiteStatus
from content.tests.fake_wordpress import HOST
from content.wordpress.client import normalize_site_url
from core.errors import ConflictError, NotFoundError, ValidationFailedError

SYSTEM = Actor.system("test")


def test_normalize_site_url():
    assert normalize_site_url("Example.com/") == "https://example.com"
    with pytest.raises(Exception, match="https"):
        normalize_site_url("http://example.com")


def test_health_check_active(wp_connection):
    result = lifecycle.check_connection(wp_connection)
    assert result.status == ConnectionStatus.ACTIVE


def test_health_check_rejected_password(wp_connection, wp):
    wp.password = "changed on the server"
    result = lifecycle.check_connection(wp_connection)
    assert result.status == ConnectionStatus.EXPIRED


def test_health_check_read_only_user(wp_connection, wp):
    wp.capabilities = {"edit_posts": False}
    result = lifecycle.check_connection(wp_connection)
    assert result.status == ConnectionStatus.DEGRADED
    assert "Editor" in result.detail


def test_health_check_rest_disabled(wp_connection, wp):
    wp.rest_disabled = True
    result = lifecycle.check_connection(wp_connection)
    assert result.status == ConnectionStatus.DEGRADED


def test_create_wordpress_website_probes_site(workspace, wp_connection):
    site = services.create_website(
        workspace_id=workspace.pk,
        name="Acme",
        kind=WebsiteKind.WORDPRESS,
        connection_id=wp_connection.pk,
        actor=SYSTEM,
    )
    assert site.base_url == f"https://{HOST}"
    assert site.status == WebsiteStatus.HEALTHY
    profile = site.profile
    assert profile["counts"]["posts"] == 3
    assert profile["counts"]["media"] == 120
    assert profile["detected"] == {"seo": "Rank Math", "builder": "Elementor"}
    assert profile["user"]["can_publish"] is True
    assert profile["warnings"] == []


def test_probe_flags_missing_seo_plugin(workspace, wp_connection, wp):
    wp.namespaces = ["wp/v2"]
    site = services.create_website(
        workspace_id=workspace.pk,
        name="",
        kind=WebsiteKind.WORDPRESS,
        connection_id=wp_connection.pk,
        actor=SYSTEM,
    )
    assert "no_seo_plugin_detected" in site.profile["warnings"]
    assert site.name == HOST


def test_probe_marks_site_unreachable_when_credentials_rejected(workspace, wp_connection, wp):
    site = services.create_website(
        workspace_id=workspace.pk,
        name="Acme",
        kind=WebsiteKind.WORDPRESS,
        connection_id=wp_connection.pk,
        actor=SYSTEM,
    )
    wp.password = "rotated"
    site = services.run_probe(site)
    assert site.status == WebsiteStatus.DEGRADED
    assert "credentials_rejected" in site.profile["warnings"]


def test_duplicate_website_is_a_conflict(workspace, wp_connection):
    kwargs = {
        "workspace_id": workspace.pk,
        "name": "Acme",
        "kind": WebsiteKind.WORDPRESS,
        "connection_id": wp_connection.pk,
        "actor": SYSTEM,
    }
    services.create_website(**kwargs)
    with pytest.raises(ConflictError):
        services.create_website(**kwargs)


def test_wordpress_website_needs_a_wordpress_connection(workspace, wp):
    with pytest.raises(NotFoundError):
        services.create_website(
            workspace_id=workspace.pk, name="x", kind=WebsiteKind.WORDPRESS, actor=SYSTEM
        )


def test_git_site_probe_reads_public_pages(workspace, wp):
    site = services.create_website(
        workspace_id=workspace.pk,
        name="AI site",
        kind=WebsiteKind.GIT_SITE,
        base_url=f"https://{HOST}",
        repo="acme/site",
        branch="main",
        actor=SYSTEM,
    )
    profile = site.profile
    assert site.status == WebsiteStatus.HEALTHY
    assert profile["title"] == "Acme IT"
    assert profile["sitemap_entries"] == 5
    assert profile["robots_txt"] is True
    assert profile["canonical"] == "https://wp.example.com/"
    assert profile["warnings"] == []


def test_git_site_probe_reports_problems(workspace, wp):
    wp.sitemap = ""
    site = services.create_website(
        workspace_id=workspace.pk,
        name="AI site",
        kind=WebsiteKind.GIT_SITE,
        base_url=f"https://{HOST}",
        actor=SYSTEM,
    )
    assert "no_sitemap" in site.profile["warnings"]


def test_git_site_rejects_bad_repo(workspace, wp):
    with pytest.raises(ValidationFailedError):
        services.create_website(
            workspace_id=workspace.pk,
            name="x",
            kind=WebsiteKind.GIT_SITE,
            base_url="https://example.com",
            repo="not a repo",
            actor=SYSTEM,
        )


def test_private_hosts_are_refused(workspace, monkeypatch):
    from content import net

    monkeypatch.setattr(net, "transport", None)
    monkeypatch.setattr(net, "resolver", lambda host, port: ["10.0.0.5"])
    site = services.create_website(
        workspace_id=workspace.pk,
        name="internal",
        kind=WebsiteKind.GIT_SITE,
        base_url="https://intranet.example.com",
        actor=SYSTEM,
    )
    assert site.status == WebsiteStatus.UNREACHABLE


def test_delete_website(workspace, wp_connection):
    site = services.create_website(
        workspace_id=workspace.pk,
        name="Acme",
        kind=WebsiteKind.WORDPRESS,
        connection_id=wp_connection.pk,
        actor=SYSTEM,
    )
    services.delete_website(site, actor=SYSTEM)
    assert not Website.objects.filter(pk=site.pk).exists()
