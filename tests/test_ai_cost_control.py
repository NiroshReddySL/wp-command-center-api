"""AI cost controls for ContentScorer — an enterprise site can have thousands
of posts needing a recommendation on a first crawl. Two levers keep that
affordable and predictable:
  - a cheaper model tier for the high-volume per-post recommendation call
  - a hard per-run budget, spent on the neediest content first, with the
    rest carrying over (untouched) to the next run
"""
from app.agents.optimizer.content_scorer import _prioritize_ai_candidates
from app.ai.engine import FAST_MODEL, MODEL


def _candidate(name: str, score: int, traffic: int) -> dict:
    return {"post": name, "title": name, "score": score, "traffic": traffic}


class TestPrioritizeAiCandidates:
    def test_worst_health_score_goes_first(self) -> None:
        candidates = [_candidate("good", 90, 0), _candidate("bad", 10, 0), _candidate("mid", 50, 0)]
        to_process, deferred = _prioritize_ai_candidates(candidates, budget=2)
        assert [c["post"] for c in to_process] == ["bad", "mid"]
        assert [c["post"] for c in deferred] == ["good"]

    def test_ties_broken_by_higher_traffic_first(self) -> None:
        candidates = [_candidate("low-traffic", 30, 5), _candidate("high-traffic", 30, 5000)]
        to_process, _ = _prioritize_ai_candidates(candidates, budget=1)
        assert to_process[0]["post"] == "high-traffic"

    def test_budget_over_count_processes_everything(self) -> None:
        candidates = [_candidate("a", 10, 0), _candidate("b", 20, 0)]
        to_process, deferred = _prioritize_ai_candidates(candidates, budget=100)
        assert len(to_process) == 2
        assert deferred == []

    def test_zero_budget_defers_everything(self) -> None:
        candidates = [_candidate("a", 10, 0)]
        to_process, deferred = _prioritize_ai_candidates(candidates, budget=0)
        assert to_process == []
        assert len(deferred) == 1

    def test_negative_budget_treated_as_zero_not_a_crash(self) -> None:
        to_process, deferred = _prioritize_ai_candidates([_candidate("a", 10, 0)], budget=-5)
        assert to_process == []
        assert len(deferred) == 1

    def test_empty_candidates_is_safe(self) -> None:
        assert _prioritize_ai_candidates([], budget=50) == ([], [])

    def test_original_list_not_mutated(self) -> None:
        candidates = [_candidate("b", 90, 0), _candidate("a", 10, 0)]
        original_order = list(candidates)
        _prioritize_ai_candidates(candidates, budget=1)
        assert candidates == original_order


class TestModelTiering:
    def test_fast_model_is_distinct_from_flagship(self) -> None:
        assert FAST_MODEL != MODEL

    def test_fast_model_is_a_mini_tier(self) -> None:
        assert "mini" in FAST_MODEL
