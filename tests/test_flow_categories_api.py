"""Flow category update semantics — a client explicitly clearing
description/color back to empty sends `null` on purpose, and that must
take effect. Without this, an update payload with `null` is
indistinguishable from the field being omitted entirely, and a user
clearing a field in the edit form would find their old value silently
still there.
"""
from app.api.flows import FlowCategoryUpdate, _compute_goal_metrics, _resolve_update_fields


class TestResolveUpdateFields:
    def test_omitted_description_is_left_untouched(self) -> None:
        payload = FlowCategoryUpdate(name="New name")
        assert "description" not in _resolve_update_fields(payload)

    def test_explicit_null_description_clears_it(self) -> None:
        payload = FlowCategoryUpdate(description=None)
        updates = _resolve_update_fields(payload)
        assert "description" in updates
        assert updates["description"] is None

    def test_explicit_null_color_clears_it(self) -> None:
        payload = FlowCategoryUpdate(color=None)
        updates = _resolve_update_fields(payload)
        assert "color" in updates
        assert updates["color"] is None

    def test_non_null_description_is_applied(self) -> None:
        payload = FlowCategoryUpdate(description="A real description")
        assert _resolve_update_fields(payload)["description"] == "A real description"

    def test_name_is_stripped_and_only_applied_when_set(self) -> None:
        assert _resolve_update_fields(FlowCategoryUpdate(name="  Padded  "))["name"] == "Padded"
        assert "name" not in _resolve_update_fields(FlowCategoryUpdate())

    def test_is_active_only_applied_when_explicitly_set(self) -> None:
        assert _resolve_update_fields(FlowCategoryUpdate(is_active=False))["is_active"] is False
        assert "is_active" not in _resolve_update_fields(FlowCategoryUpdate())

    def test_empty_payload_resolves_to_no_updates(self) -> None:
        assert _resolve_update_fields(FlowCategoryUpdate()) == {}

    def test_all_fields_together(self) -> None:
        payload = FlowCategoryUpdate(name="X", description=None, color="danger", is_active=False)
        updates = _resolve_update_fields(payload)
        assert updates == {"name": "X", "description": None, "color": "danger", "is_active": False}


class TestComputeGoalMetrics:
    """A flow's "leads" number only exists when a step is explicitly marked
    is_goal — plain content-journey flows (no goal step) must stay
    completely untouched, per the whole point of making this opt-in rather
    than assuming the last step is always a conversion."""

    def test_no_goal_step_returns_all_none(self) -> None:
        steps = [{"step_index": 0, "is_goal": False}, {"step_index": 1, "is_goal": False}]
        step_results = [
            {"step_index": 0, "active_users": 100},
            {"step_index": 1, "active_users": 10},
        ]
        assert _compute_goal_metrics(steps, step_results, total_entered=100) == (None, None, None)

    def test_goal_step_reports_its_active_users_as_leads(self) -> None:
        steps = [
            {"step_index": 0, "is_goal": False},
            {"step_index": 1, "is_goal": False},
            {"step_index": 2, "is_goal": True},
        ]
        step_results = [
            {"step_index": 0, "active_users": 300},
            {"step_index": 1, "active_users": 20},
            {"step_index": 2, "active_users": 5},
        ]
        goal_step_index, leads, lead_rate = _compute_goal_metrics(steps, step_results, total_entered=300)
        assert goal_step_index == 2
        assert leads == 5
        assert lead_rate == 5 / 300

    def test_goal_step_need_not_be_the_last_step(self) -> None:
        steps = [{"step_index": 0, "is_goal": False}, {"step_index": 1, "is_goal": True}, {"step_index": 2, "is_goal": False}]
        step_results = [
            {"step_index": 0, "active_users": 100},
            {"step_index": 1, "active_users": 40},
            {"step_index": 2, "active_users": 10},
        ]
        goal_step_index, leads, _ = _compute_goal_metrics(steps, step_results, total_entered=100)
        assert goal_step_index == 1
        assert leads == 40

    def test_missing_step_result_for_the_goal_defaults_to_zero_leads(self) -> None:
        # GA4 omits a step's row entirely when nobody reached it that day.
        steps = [{"step_index": 0, "is_goal": False}, {"step_index": 1, "is_goal": True}]
        step_results = [{"step_index": 0, "active_users": 50}]
        goal_step_index, leads, lead_rate = _compute_goal_metrics(steps, step_results, total_entered=50)
        assert goal_step_index == 1
        assert leads == 0
        assert lead_rate == 0.0

    def test_zero_entered_never_divides_by_zero(self) -> None:
        steps = [{"step_index": 0, "is_goal": True}]
        step_results = [{"step_index": 0, "active_users": 0}]
        _, leads, lead_rate = _compute_goal_metrics(steps, step_results, total_entered=0)
        assert leads == 0
        assert lead_rate == 0.0

    def test_first_goal_step_wins_if_more_than_one_is_marked(self) -> None:
        steps = [{"step_index": 0, "is_goal": True}, {"step_index": 1, "is_goal": True}]
        step_results = [
            {"step_index": 0, "active_users": 100},
            {"step_index": 1, "active_users": 10},
        ]
        goal_step_index, leads, _ = _compute_goal_metrics(steps, step_results, total_entered=100)
        assert goal_step_index == 0
        assert leads == 100
