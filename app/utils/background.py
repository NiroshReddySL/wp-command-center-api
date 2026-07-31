"""Fire-and-forget task launching that survives the garbage collector.

`asyncio.create_task` hands back the only *strong* reference to a running
task — the event loop keeps just a weak one. So a detached task whose handle
is discarded on the very next line can be collected mid-await, silently
cancelling real work with no error raised anywhere. Both background jobs in
this app are exactly that shape (a sync run and an agent job, each outliving
the request that started it), so they launch through here instead.

The second reason to centralise it: an exception inside a detached task is
otherwise surfaced only when the task object is finalised, by which point the
traceback is disconnected from anything that explains it. Every task spawned
here logs its own failure at the moment it happens.
"""
import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

logger = logging.getLogger(__name__)

# Strong references to in-flight detached tasks, held until each completes.
_TASKS: set[asyncio.Task[Any]] = set()


def _log_failure(task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            "Background task %s failed: %s", task.get_name(), exc, exc_info=exc
        )


def spawn(coro: Coroutine[Any, Any, Any], *, name: str | None = None) -> asyncio.Task[Any]:
    """Run `coro` detached from the caller, keeping it referenced until it ends."""
    task = asyncio.create_task(coro, name=name)
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    task.add_done_callback(_log_failure)
    return task


def pending_count() -> int:
    """How many detached tasks are still in flight — for tests and diagnostics."""
    return len(_TASKS)


__all__ = ["pending_count", "spawn"]
