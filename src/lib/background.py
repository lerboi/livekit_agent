"""Strong-reference holder for fire-and-forget asyncio tasks.

The event loop keeps only WEAK references to tasks: a Task nobody awaits or
stores can be garbage-collected mid-flight (documented footgun in the
asyncio.create_task docs — "Save a reference to the result of this
function"). For fire-and-forget work on the call path (egress start,
calendar push, SMS sends, delayed disconnects, recovery teardowns) a
collected task means the work silently never happens.

create_background_task() pins each task in a module-level set until it
completes, then drops it via done_callback. The set stays tiny — each agent
job runs in its own worker process and produces at most a handful of
background tasks per call.
"""
from __future__ import annotations

import asyncio
from typing import Any, Coroutine

_BACKGROUND_TASKS: set[asyncio.Task] = set()


def create_background_task(
    coro: Coroutine[Any, Any, Any], *, name: str | None = None
) -> asyncio.Task:
    """asyncio.create_task + a strong reference held until the task finishes.

    Drop-in replacement for bare asyncio.create_task at fire-and-forget call
    sites; the returned task can still be awaited or cancelled normally.
    """
    task = asyncio.create_task(coro, name=name)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return task
