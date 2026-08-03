"""Component audit — plugins AND themes, read from WordPress or entered by hand.

Reading `/wp/v2/plugins` and `/wp/v2/themes` needs an Application Password,
so an unconnected site produced no findings at all and the Watchdog page
rendered that as a clean bill of health. These tests pin the parsing, the
version/CVE reasoning and the bucketing that make a hand-entered component
audit exactly like a WordPress-read one.
"""
from app.agents.watchdog.plugin_audit import (
    COMPONENT_PLUGIN,
    COMPONENT_THEME,
    SOURCE_WORDPRESS,
    UNAVAILABLE_TYPE,
    WPSCAN_AUTH_TYPE,
    VulnLookup,
    _status_to_active,
    active_vulnerabilities,
    plugin_component,
    theme_component,
)
from app.api.watchdog import BUCKET_PREFIXES, _bucket, _normalize_slug


class TestPluginParsing:
    def test_reads_slug_version_name_and_state(self) -> None:
        c = plugin_component({
            "plugin": "akismet/akismet", "name": "Akismet Anti-Spam",
            "version": "5.3", "status": "active",
        })
        assert c is not None
        assert (c.slug, c.version, c.name) == ("akismet", "5.3", "Akismet Anti-Spam")
        assert c.is_active is True
        assert c.component_type == COMPONENT_PLUGIN
        assert c.source == SOURCE_WORDPRESS

    def test_inactive_plugin_is_recorded_as_inactive(self) -> None:
        # An installed-but-inactive plugin is still on disk and still carries
        # its CVEs, so it is audited — just labelled honestly.
        c = plugin_component({"plugin": "hello/hello.php", "version": "1.7", "status": "inactive"})
        assert c is not None and c.is_active is False

    def test_a_plugin_with_no_version_is_unauditable(self) -> None:
        # Without a version there is nothing to compare a CVE's fixed_in to.
        assert plugin_component({"plugin": "x/x.php", "version": ""}) is None

    def test_falls_back_to_the_slug_when_unnamed(self) -> None:
        c = plugin_component({"plugin": "wp-super-cache/wp-cache.php", "version": "1.9"})
        assert c is not None and c.name == "wp-super-cache"


class TestThemeParsing:
    def test_reads_stylesheet_as_the_slug(self) -> None:
        c = theme_component({
            "stylesheet": "twentytwentyfour", "template": "twentytwentyfour",
            "name": {"raw": "Twenty Twenty-Four", "rendered": "Twenty Twenty-Four"},
            "version": "1.5", "status": "active",
        })
        assert c is not None
        assert (c.slug, c.version) == ("twentytwentyfour", "1.5")
        assert c.name == "Twenty Twenty-Four"
        assert c.component_type == COMPONENT_THEME
        assert c.is_active is True

    def test_child_theme_is_identified_by_its_own_stylesheet(self) -> None:
        # `template` is the PARENT. Auditing the parent's slug would report on
        # a component that isn't the one installed.
        c = theme_component({
            "stylesheet": "my-child", "template": "twentytwentyfour",
            "name": "My Child", "version": "1.0",
        })
        assert c is not None and c.slug == "my-child"

    def test_name_may_be_a_plain_string(self) -> None:
        c = theme_component({"stylesheet": "astra", "name": "Astra", "version": "4.6"})
        assert c is not None and c.name == "Astra"

    def test_a_theme_with_no_version_is_unauditable(self) -> None:
        assert theme_component({"stylesheet": "astra", "version": ""}) is None


class TestActiveState:
    def test_known_states_map_to_booleans(self) -> None:
        assert _status_to_active("active") is True
        assert _status_to_active("inactive") is False

    def test_anything_else_is_unknown_not_inactive(self) -> None:
        # NULL means "nobody said", which must not render as "deactivated".
        for value in (None, "", "paused", 1, {}):
            assert _status_to_active(value) is None


class TestVulnerabilityFiltering:
    def test_a_vuln_fixed_in_a_later_release_still_applies(self) -> None:
        vulns = [{"title": "XSS", "fixed_in": "2.0.0"}]
        assert len(active_vulnerabilities("1.9.0", vulns)) == 1

    def test_a_vuln_already_patched_is_dropped(self) -> None:
        vulns = [{"title": "XSS", "fixed_in": "2.0.0"}]
        assert active_vulnerabilities("2.0.0", vulns) == []

    def test_an_unpatched_vuln_applies_at_any_version(self) -> None:
        # No fixed_in means nobody shipped a patch — the most serious case,
        # and the one an "is there a newer release?" check misses entirely.
        vulns = [{"title": "RCE", "fixed_in": None}]
        assert len(active_vulnerabilities("9.9.9", vulns)) == 1

    def test_prerelease_installs_are_compared_correctly(self) -> None:
        vulns = [{"title": "XSS", "fixed_in": "2.0.0"}]
        assert len(active_vulnerabilities("2.0.0-rc1", vulns)) == 1


class TestVulnLookupSemantics:
    """A failed lookup is unknown, never clean — the distinction the whole
    module is built around."""

    def test_none_means_unknown(self) -> None:
        assert VulnLookup(None).vulns is None
        assert VulnLookup(None).auth_failed is False

    def test_empty_list_means_confirmed_no_cves(self) -> None:
        assert VulnLookup([]).vulns == []

    def test_auth_failure_is_distinguishable_from_an_outage(self) -> None:
        # A rejected key fails identically forever and silently; it needs its
        # own signal so it can be reported once, loudly.
        rejected = VulnLookup(None, auth_failed=True)
        assert rejected.vulns is None and rejected.auth_failed is True


class TestBucketing:
    """Tab badges and tab rows are both derived from these prefixes, so they
    cannot disagree — they did, when the list used a substring type filter
    that could not express "plugins and themes"."""

    def test_plugins_and_themes_share_one_bucket(self) -> None:
        for alert_type in (
            "plugin_outdated", "plugin_vulnerable",
            "theme_outdated", "theme_vulnerable",
        ):
            assert _bucket(alert_type) == "component"

    def test_audit_notices_bucket_with_their_components(self) -> None:
        # Otherwise "audit unavailable" counts toward All but appears on no tab.
        assert _bucket(UNAVAILABLE_TYPE) == "component"
        assert _bucket(WPSCAN_AUTH_TYPE) == "component"

    def test_other_agents_keep_their_buckets(self) -> None:
        assert _bucket("broken_link") == "broken_link"
        assert _bucket("performance") == "performance"

    def test_unknown_types_fall_through(self) -> None:
        assert _bucket("traffic_drop") == "other"

    def test_every_bucket_has_prefixes(self) -> None:
        assert all(prefixes for prefixes in BUCKET_PREFIXES.values())


class TestSlugNormalisation:
    """The slug is the wp.org / WPScan lookup key. A typo doesn't error — it
    silently finds nothing, which reports the component as up to date."""

    def test_lowercases(self) -> None:
        assert _normalize_slug("Akismet") == "akismet"

    def test_accepts_a_pasted_plugin_path(self) -> None:
        assert _normalize_slug("akismet/akismet.php") == "akismet"

    def test_spaces_become_hyphens(self) -> None:
        assert _normalize_slug("  Contact Form 7 ") == "contact-form-7"

    def test_preserves_legitimate_slug_characters(self) -> None:
        assert _normalize_slug("wp-super-cache") == "wp-super-cache"

    def test_strips_surrounding_hyphens(self) -> None:
        assert _normalize_slug("--yoast--") == "yoast"
