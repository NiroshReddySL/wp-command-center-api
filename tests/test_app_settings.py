"""Agent toggle merging + notification defaults."""
from app.services.app_settings import (
    AGENT_DEFINITIONS,
    KNOWN_AGENT_KEYS,
    NOTIFICATION_DEFAULTS,
    merge_agent_toggles,
)


class TestMergeAgentToggles:
    def test_empty_store_defaults_all_on(self) -> None:
        merged = merge_agent_toggles({})
        assert set(merged) == KNOWN_AGENT_KEYS
        assert all(merged.values())

    def test_none_store_defaults_all_on(self) -> None:
        assert all(merge_agent_toggles(None).values())

    def test_stored_false_wins_over_default(self) -> None:
        merged = merge_agent_toggles({"optimizer.content": False})
        assert merged["optimizer.content"] is False
        assert merged["optimizer.seo"] is True

    def test_unknown_stored_keys_dropped(self) -> None:
        merged = merge_agent_toggles({"autopilot.repurpose": False, "bogus": True})
        assert "autopilot.repurpose" not in merged
        assert "bogus" not in merged

    def test_definitions_match_known_keys(self) -> None:
        assert {d["key"] for d in AGENT_DEFINITIONS} == KNOWN_AGENT_KEYS
        assert all(d["label"] and d["description"] for d in AGENT_DEFINITIONS)


class TestNotificationDefaults:
    def test_shape(self) -> None:
        assert NOTIFICATION_DEFAULTS == {
            "teams_webhook_url": "",
            "notify_critical": True,
            "weekly_digest": True,
        }
