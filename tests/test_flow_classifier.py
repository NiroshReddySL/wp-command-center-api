"""Flow Classifier alert threshold — same "meaningful sample + relative
drop" shape as TrafficAgent's traffic_drop check, applied to a flow
category's day-over-day conversion rate.
"""
from app.agents.flows.flow_classifier import _drop_ratio_if_alertworthy


class TestDropRatioIfAlertworthy:
    def test_no_alert_when_sample_too_small_today(self) -> None:
        assert _drop_ratio_if_alertworthy(5, 0.1, 100, 0.5) is None

    def test_no_alert_when_sample_too_small_yesterday(self) -> None:
        assert _drop_ratio_if_alertworthy(100, 0.1, 5, 0.5) is None

    def test_no_alert_when_prev_conversion_rate_is_zero(self) -> None:
        # Would otherwise divide by zero.
        assert _drop_ratio_if_alertworthy(100, 0.1, 100, 0.0) is None

    def test_no_alert_when_drop_is_below_threshold(self) -> None:
        # 10% relative drop — not enough to cross the -30% threshold.
        assert _drop_ratio_if_alertworthy(100, 0.45, 100, 0.5) is None

    def test_alert_when_drop_crosses_threshold(self) -> None:
        # 0.5 -> 0.3 is a 40% relative drop, past the -30% threshold.
        change = _drop_ratio_if_alertworthy(100, 0.3, 100, 0.5)
        assert change is not None
        assert change < 0
        assert round(change, 2) == -0.4

    def test_no_alert_on_improvement(self) -> None:
        assert _drop_ratio_if_alertworthy(100, 0.6, 100, 0.5) is None

    def test_exactly_at_threshold_triggers(self) -> None:
        # Exactly -30% should trigger (the check is <=, not <).
        change = _drop_ratio_if_alertworthy(100, 0.35, 100, 0.5)
        assert change is not None
