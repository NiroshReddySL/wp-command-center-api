"""AI recommendation return contract — must not leave stale advice on screen
after the underlying issue is fixed.

Regression for: rescanning a post whose FAQ-schema issue was resolved kept
showing "add FAQPage schema" forever, because callers only overwrote
`ai_recommendation` when the AI returned truthy text, never when it
legitimately had nothing left to say.
"""
from unittest.mock import AsyncMock, patch

import pytest

from app.agents.optimizer.content_scorer import _generate_ai_recommendation
from app.ai.engine import FAST_MODEL


class TestGenerateAiRecommendationContract:
    @pytest.mark.asyncio
    async def test_clean_post_returns_empty_string_not_none(self) -> None:
        """No issues + high score must short-circuit to "" (clear-signal), not None (failure-signal)."""
        result = await _generate_ai_recommendation("Title", 92, 1200, [], {})
        assert result == ""

    @pytest.mark.asyncio
    async def test_successful_generation_returns_joined_text(self) -> None:
        with patch(
            "app.agents.optimizer.content_scorer.ai_engine.generate_json",
            new=AsyncMock(return_value={"recommendations": ["Fix A", "Fix B", "Fix C"]}),
        ):
            result = await _generate_ai_recommendation(
                "Title", 40, 300, ["Short content"], {}
            )
        assert result == "Fix A\nFix B\nFix C"

    @pytest.mark.asyncio
    async def test_empty_recs_from_model_returns_empty_string(self) -> None:
        with patch(
            "app.agents.optimizer.content_scorer.ai_engine.generate_json",
            new=AsyncMock(return_value={"recommendations": []}),
        ):
            result = await _generate_ai_recommendation("Title", 40, 300, ["Short content"], {})
        assert result == ""

    @pytest.mark.asyncio
    async def test_api_failure_returns_none_not_empty(self) -> None:
        """A transient failure must be distinguishable from 'genuinely clean' —
        callers use this to avoid erasing still-valid existing advice."""
        with patch(
            "app.agents.optimizer.content_scorer.ai_engine.generate_json",
            new=AsyncMock(side_effect=RuntimeError("API down")),
        ):
            result = await _generate_ai_recommendation("Title", 40, 300, ["Short content"], {})
        assert result is None

    @pytest.mark.asyncio
    async def test_none_and_empty_string_are_distinguishable(self) -> None:
        """The bug hinges on this: `if ai_rec:` treats "" and None identically.
        Callers must use `is not None` so a clean post's "" is acted on."""
        clean = await _generate_ai_recommendation("Title", 95, 1200, [], {})
        with patch(
            "app.agents.optimizer.content_scorer.ai_engine.generate_json",
            new=AsyncMock(side_effect=RuntimeError("down")),
        ):
            failed = await _generate_ai_recommendation("Title", 40, 300, ["issue"], {})
        assert clean == "" and failed is None
        assert clean is not failed

    @pytest.mark.asyncio
    async def test_uses_the_cheap_model_tier(self) -> None:
        """Per-post recommendations run at enterprise-site volume — this call
        must go through the cheap tier, not the flagship model."""
        mock = AsyncMock(return_value={"recommendations": ["Fix A", "Fix B", "Fix C"]})
        with patch("app.agents.optimizer.content_scorer.ai_engine.generate_json", new=mock):
            await _generate_ai_recommendation("Title", 40, 300, ["Short content"], {})
        assert mock.call_args.kwargs.get("model") == FAST_MODEL
