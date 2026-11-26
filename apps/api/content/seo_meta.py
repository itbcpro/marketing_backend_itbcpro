"""Which post meta keys hold the SEO title and description for each SEO plugin."""

from content.models import Website

PLUGINS: dict[str, dict[str, str]] = {
    "Yoast SEO": {"seo_title": "_yoast_wpseo_title", "meta_description": "_yoast_wpseo_metadesc"},
    "Rank Math": {"seo_title": "rank_math_title", "meta_description": "rank_math_description"},
}
CORE_FIELDS = ("title", "slug")
SEO_FIELDS = ("seo_title", "meta_description")
LIMITS = {"title": 200, "slug": 190, "seo_title": 300, "meta_description": 500}

HELPER_SNIPPET = """\
add_action('init', function () {
    $keys = ['_yoast_wpseo_title', '_yoast_wpseo_metadesc',
             'rank_math_title', 'rank_math_description'];
    foreach (get_post_types(['show_in_rest' => true]) as $type) {
        foreach ($keys as $key) {
            register_post_meta($type, $key, [
                'show_in_rest'  => true,
                'single'        => true,
                'type'          => 'string',
                'auth_callback' => function () { return current_user_can('edit_posts'); },
            ]);
        }
    }
});
"""


def keys_for(plugin: str) -> dict[str, str]:
    return PLUGINS.get(plugin, {})


def seo_keys(website: Website) -> dict[str, str]:
    """The meta keys of the site's SEO plugin (empty when none was detected)."""
    return keys_for(str((website.profile.get("detected") or {}).get("seo", "")))


ELEMENTOR_SNIPPET = """add_action('init', function () {
    $keys = ['_elementor_data', '_elementor_edit_mode', '_elementor_template_type',
             '_elementor_version'];
    foreach ($keys as $key) {
        register_post_meta('post', $key, [
            'show_in_rest'  => true,
            'single'        => true,
            'type'          => 'string',
            'auth_callback' => function () { return current_user_can('edit_posts'); },
        ]);
    }
});
"""
