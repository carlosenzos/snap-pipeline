from __future__ import annotations

import asyncio


def run_async(coro):
    """Run an async coroutine from a sync Celery task.

    Handles the case where an event loop may or may not exist.
    """
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                return pool.submit(asyncio.run, coro).result()
        return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)
