"""Unit tests for broken-link classification — the false-positive guard."""
from app.agents.watchdog.link_checker import _classify


class TestLinkClassification:
    def test_success_statuses_are_not_broken(self) -> None:
        for status in (200, 204, 301, 302):
            assert _classify(status, is_internal=True) is None
            assert _classify(status, is_internal=False) is None

    def test_bot_protection_statuses_are_not_broken(self) -> None:
        # Cloudflare/LinkedIn/rate limiters answer these to bots while the
        # page works fine in a browser — flagging them caused false positives
        for status in (401, 403, 405, 429, 503, 999):
            assert _classify(status, is_internal=False) is None

    def test_missing_pages_are_critical(self) -> None:
        assert _classify(404, is_internal=True) == "critical"
        assert _classify(410, is_internal=False) == "critical"

    def test_unreachable_internal_is_critical_external_is_warning(self) -> None:
        assert _classify(0, is_internal=True) == "critical"
        assert _classify(0, is_internal=False) == "warning"

    def test_persistent_server_errors_are_warnings(self) -> None:
        assert _classify(500, is_internal=False) == "warning"
        assert _classify(502, is_internal=True) == "warning"
