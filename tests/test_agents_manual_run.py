"""Manual "Run agents" agent-selection — added so a manual run can target a
chosen subset instead of always running every agent, with the picklist
defaulting to each agent's Agent Configuration toggle where one exists.
"""
from app.api.agents import AGENT_STEPS, AGENT_TOGGLE_KEY, _select_steps
from app.services.app_settings import KNOWN_AGENT_KEYS
from app.services.job_executor import AGENT_MODULES
from app.services.job_executor import AGENT_STEPS as EXEC_AGENT_STEPS


class TestSelectSteps:
    def test_none_runs_everything(self) -> None:
        assert _select_steps(AGENT_STEPS, None) == AGENT_STEPS

    def test_filters_to_requested_names_only(self) -> None:
        result = _select_steps(AGENT_STEPS, ["ContentScorer", "LinkChecker"])
        assert [s[1] for s in result] == ["ContentScorer", "LinkChecker"]

    def test_preserves_agent_steps_order_not_request_order(self) -> None:
        # Requested out of order — output must still follow AGENT_STEPS order.
        result = _select_steps(AGENT_STEPS, ["LinkChecker", "ContentScorer"])
        assert [s[1] for s in result] == ["ContentScorer", "LinkChecker"]

    def test_unknown_name_is_silently_ignored(self) -> None:
        result = _select_steps(AGENT_STEPS, ["NotARealAgent"])
        assert result == []

    def test_empty_list_selects_nothing(self) -> None:
        assert _select_steps(AGENT_STEPS, []) == []


class TestAgentTogglesConsistency:
    """AGENT_TOGGLE_KEY drives the manual-run picklist's defaults — every
    value it points at must actually exist as a toggle, or the default would
    silently fall back to enabled (see `list_manual_options`'s `.get(key, "")`
    against an unknown key)."""

    def test_every_toggle_key_value_is_a_known_agent_key(self) -> None:
        for class_name, toggle_key in AGENT_TOGGLE_KEY.items():
            assert toggle_key in KNOWN_AGENT_KEYS, f"{class_name} -> {toggle_key!r} is not a real toggle"

    def test_every_mapped_class_name_is_a_real_manual_agent(self) -> None:
        step_class_names = {s[1] for s in AGENT_STEPS}
        for class_name in AGENT_TOGGLE_KEY:
            assert class_name in step_class_names


class TestAgentModules:
    def test_covers_every_agent_step(self) -> None:
        for _, class_name, _, _ in EXEC_AGENT_STEPS:
            assert class_name in AGENT_MODULES

    def test_maps_to_the_correct_module_path(self) -> None:
        for module_path, class_name, _, _ in EXEC_AGENT_STEPS:
            assert AGENT_MODULES[class_name] == module_path
