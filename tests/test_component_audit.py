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
    LATEST_MANUAL,
    LATEST_UNKNOWN,
    LATEST_WPORG,
    SOURCE_MANUAL,
    SOURCE_WORDPRESS,
    UNAVAILABLE_TYPE,
    WPSCAN_AUTH_TYPE,
    Component,
    PluginAuditor,
    VulnLookup,
    _status_to_active,
    _version_lt,
    active_vulnerabilities,
    plugin_component,
    theme_component,
)
from app.api.watchdog import (
    BUCKET_PREFIXES,
    ComponentUpdate,
    _bucket,
    _normalize_slug,
)


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


class TestLatestVersionResolution:
    """`latest_version` equal to `installed_version` means two opposite
    things, and conflating them is what made a premium plugin sit at "up to
    date" forever: either the directory confirmed nothing newer exists, or
    the directory has never heard of it and nobody checked.
    """

    @staticmethod
    def _component(**over):
        base = {
            "component_type": COMPONENT_PLUGIN, "slug": "avada", "name": "Avada",
            "version": "7.10.0", "is_active": True, "source": SOURCE_MANUAL,
        }
        base.update(over)
        return Component(**base)

    def test_directory_version_wins_when_it_exists(self) -> None:
        latest, source = PluginAuditor._resolve_latest(self._component(), "7.11.0")
        assert (latest, source) == ("7.11.0", LATEST_WPORG)

    def test_operator_version_is_used_when_the_directory_has_none(self) -> None:
        # Avada is sold through ThemeForest; wp.org will never answer for it.
        c = self._component(latest_override="7.11.6")
        latest, source = PluginAuditor._resolve_latest(c, None)
        assert (latest, source) == ("7.11.6", LATEST_MANUAL)

    def test_directory_still_outranks_an_operator_value(self) -> None:
        # If wp.org does know the slug it is fresher than a hand-typed note.
        c = self._component(latest_override="1.0.0")
        latest, source = PluginAuditor._resolve_latest(c, "7.11.0")
        assert (latest, source) == ("7.11.0", LATEST_WPORG)

    def test_neither_source_marks_it_unknown_not_current(self) -> None:
        latest, source = PluginAuditor._resolve_latest(self._component(), None)
        # latest mirrors installed so the comparison is a deliberate no-op...
        assert latest == "7.10.0"
        # ...but it is flagged, so nothing can read it as "up to date".
        assert source == LATEST_UNKNOWN

    def test_a_custom_in_house_component_is_supported(self) -> None:
        c = self._component(slug="cloudfuze-internal", latest_override="2.4.0", version="2.3.0")
        latest, source = PluginAuditor._resolve_latest(c, None)
        assert (latest, source) == ("2.4.0", LATEST_MANUAL)
        assert _version_lt("2.3.0", latest) is True  # correctly reads as outdated


class TestComponentUpdateSchema:
    """Editing a hand-recorded component. The subtlety is that `None` has two
    meanings on the wire — "leave alone" and "set to not-known" — and only
    `model_fields_set` can tell them apart."""

    def test_omitted_fields_are_distinguishable_from_explicit_nulls(self) -> None:
        omitted = ComponentUpdate()
        explicit = ComponentUpdate(is_active=None)
        assert "is_active" not in omitted.model_fields_set
        assert "is_active" in explicit.model_fields_set
        # Both read as None, which is exactly why the check cannot be `is None`.
        assert omitted.is_active is None and explicit.is_active is None

    def test_clearing_the_latest_version_is_expressible(self) -> None:
        # An empty string hands authority back to the directory lookup; the
        # field being absent must leave the stored value alone.
        cleared = ComponentUpdate(latest_version="")
        assert "latest_version" in cleared.model_fields_set
        assert cleared.latest_version == ""

    def test_partial_edits_carry_only_what_changed(self) -> None:
        patch = ComponentUpdate(installed_version="3.13.0")
        assert patch.model_fields_set == {"installed_version"}

    def test_versions_are_length_bounded(self) -> None:
        import pytest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            ComponentUpdate(installed_version="")        # min_length=1
        with pytest.raises(ValidationError):
            ComponentUpdate(latest_version="x" * 51)     # column is String(50)
