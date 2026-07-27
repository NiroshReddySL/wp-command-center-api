"""Flow category update semantics — a client explicitly clearing
description/color back to empty sends `null` on purpose, and that must
take effect. Without this, an update payload with `null` is
indistinguishable from the field being omitted entirely, and a user
clearing a field in the edit form would find their old value silently
still there.
"""
from app.api.flows import FlowCategoryUpdate, _resolve_update_fields


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
