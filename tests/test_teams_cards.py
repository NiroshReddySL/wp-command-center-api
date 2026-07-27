"""MS Teams Adaptive Card payloads — must match the Power Automate webhook contract."""
from app.database.models import Alert
from app.services.notification import build_alert_card, build_digest_card, build_test_card


def _content(payload: dict) -> dict:
    return payload["attachments"][0]["content"]


def _assert_envelope(payload: dict) -> None:
    """The message+attachments envelope is what Teams Workflows webhooks expect."""
    assert payload["type"] == "message"
    attachment = payload["attachments"][0]
    assert attachment["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert attachment["content"]["type"] == "AdaptiveCard"


def _alert(severity: str = "critical") -> Alert:
    return Alert(
        site_id="site-1",
        agent="watchdog",
        severity=severity,
        type="broken_links",
        title="12 broken links found",
        description="Multiple 404s detected on key landing pages.",
    )


class TestAlertCard:
    def test_envelope_and_severity_color(self) -> None:
        payload = build_alert_card(_alert(), "CloudFuze")
        _assert_envelope(payload)
        title_block = _content(payload)["body"][0]
        assert title_block["color"] == "Attention"
        assert "CRITICAL" in title_block["text"]
        assert "12 broken links found" in title_block["text"]

    def test_site_and_agent_facts(self) -> None:
        facts = _content(build_alert_card(_alert(), "CloudFuze"))["body"][1]["facts"]
        assert {"title": "Site", "value": "CloudFuze"} in facts
        assert {"title": "Agent", "value": "watchdog"} in facts

    def test_warning_maps_to_warning_color(self) -> None:
        payload = build_alert_card(_alert("warning"), "CloudFuze")
        assert _content(payload)["body"][0]["color"] == "Warning"


class TestDigestCard:
    def test_one_fact_per_site(self) -> None:
        payload = build_digest_card([("CloudFuze", 87), ("Blog", 92)])
        _assert_envelope(payload)
        facts = _content(payload)["body"][2]["facts"]
        assert facts == [
            {"title": "CloudFuze", "value": "Health 87/100"},
            {"title": "Blog", "value": "Health 92/100"},
        ]
        assert "2 site(s)" in _content(payload)["body"][1]["text"]


class TestTestCard:
    def test_envelope(self) -> None:
        _assert_envelope(build_test_card())
