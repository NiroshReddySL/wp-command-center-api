"""Detached background tasks.

`asyncio.create_task` returns the only strong reference to the task it
creates; the event loop holds a weak one. Dropping that handle — which is
exactly what a fire-and-forget call site does — lets the garbage collector
cancel live work with no error raised anywhere. These tests pin the two
properties that make `spawn` safe: the reference is held for the task's
lifetime, and released afterwards so the set can't grow without bound.
"""
import asyncio
import logging

import pytest

from app.utils.background import pending_count, spawn


class TestSpawn:
    async def test_runs_the_coroutine_to_completion(self) -> None:
        seen: list[str] = []

        async def work() -> None:
            await asyncio.sleep(0)
            seen.append("done")

        await spawn(work())
        assert seen == ["done"]

    async def test_holds_a_reference_while_the_task_is_in_flight(self) -> None:
        # The whole point: without this, the task is collectable mid-await.
        started = asyncio.Event()
        release = asyncio.Event()

        async def work() -> None:
            started.set()
            await release.wait()

        before = pending_count()
        task = spawn(work())
        await started.wait()
        assert pending_count() == before + 1

        release.set()
        await task

    async def test_releases_the_reference_once_finished(self) -> None:
        # Otherwise the set is a slow leak for the life of the process.
        before = pending_count()

        async def work() -> None:
            await asyncio.sleep(0)

        await spawn(work())
        await asyncio.sleep(0)  # let done-callbacks run
        assert pending_count() == before

    async def test_logs_a_failure_instead_of_swallowing_it(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # A detached task's exception is otherwise reported only at
        # finalisation, detached from anything that explains it.
        async def boom() -> None:
            raise ValueError("kaboom")

        with caplog.at_level(logging.ERROR):
            task = spawn(boom(), name="boom-task")
            with pytest.raises(ValueError):
                await task
            await asyncio.sleep(0)

        assert "kaboom" in caplog.text

    async def test_a_failed_task_still_releases_its_reference(self) -> None:
        before = pending_count()

        async def boom() -> None:
            raise ValueError("x")

        task = spawn(boom())
        with pytest.raises(ValueError):
            await task
        await asyncio.sleep(0)
        assert pending_count() == before

    async def test_names_are_preserved_for_diagnostics(self) -> None:
        async def work() -> None:
            await asyncio.sleep(0)

        task = spawn(work(), name="sync-agents-42")
        assert task.get_name() == "sync-agents-42"
        await task
