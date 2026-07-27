"""Enterprise-scale batching for ContentScorer.

Regression context: on a large site (thousands of posts + pages), a single
ContentScorer run tried to fully analyze EVERY item in one pass, with
results only committed once at the very end. Any interruption (the job
executor's hard timeout, a transient error) discarded every bit of
progress, and pages — always appended after all posts in the item list —
were the last items reached and so were effectively never analyzed on any
site large enough to blow the timeout. Two pure pieces fix this:
  - `_interleave` spreads the smaller collection (pages) evenly through the
    combined list instead of leaving it clustered at the end.
  - `_analysis_priority_key` makes sure that even after batching caps how
    much work one run attempts, never-analyzed items always sort first —
    so a big backlog of one content type can't starve the other forever.
"""
from datetime import datetime, timezone

from app.agents.optimizer.content_scorer import _analysis_priority_key, _interleave


class TestInterleave:
    def test_equal_length_alternates(self) -> None:
        posts = [{"id": 1}, {"id": 2}]
        pages = [{"id": 10}, {"id": 20}]
        assert _interleave(posts, pages) == [{"id": 1}, {"id": 10}, {"id": 2}, {"id": 20}]

    def test_smaller_collection_spread_through_larger_not_clustered_at_end(self) -> None:
        posts = [{"id": i} for i in range(6)]
        pages = [{"id": 100}, {"id": 200}]
        merged = _interleave(posts, pages)
        page_positions = [i for i, item in enumerate(merged) if item["id"] >= 100]
        # With 6 posts and 2 pages, the two pages must land early (indices 1
        # and 3) rather than both being pushed to the tail (indices 6, 7) —
        # the exact bug that starved pages when a batch cap was later added.
        assert page_positions == [1, 3]

    def test_empty_second_list_returns_first_unchanged(self) -> None:
        posts = [{"id": 1}, {"id": 2}]
        assert _interleave(posts, []) == posts

    def test_both_empty_returns_empty(self) -> None:
        assert _interleave([], []) == []

    def test_total_length_preserved(self) -> None:
        posts = [{"id": i} for i in range(7)]
        pages = [{"id": i} for i in range(3)]
        assert len(_interleave(posts, pages)) == 10


class TestAnalysisPriorityKey:
    def test_never_analyzed_sorts_before_analyzed(self) -> None:
        analyzed = datetime(2026, 1, 1, tzinfo=timezone.utc)
        assert _analysis_priority_key(None) < _analysis_priority_key(analyzed)

    def test_older_analysis_sorts_before_newer(self) -> None:
        older = datetime(2026, 1, 1, tzinfo=timezone.utc)
        newer = datetime(2026, 6, 1, tzinfo=timezone.utc)
        assert _analysis_priority_key(older) < _analysis_priority_key(newer)

    def test_sorting_a_mixed_list_puts_never_analyzed_first_oldest_next(self) -> None:
        never = None
        old = datetime(2026, 1, 1, tzinfo=timezone.utc)
        recent = datetime(2026, 6, 1, tzinfo=timezone.utc)
        items = [("recent", recent), ("never", never), ("old", old)]
        ordered = sorted(items, key=lambda pair: _analysis_priority_key(pair[1]))
        assert [name for name, _ in ordered] == ["never", "old", "recent"]
