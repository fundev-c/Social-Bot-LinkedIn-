"""
Scheduler entrypoint: ``python -m src.scheduler``.

Runs as its own process, from the same image as the API. Separate rather than
in-process so that a stalled sweep cannot degrade request handling, a scheduler
crash cannot take the API down with it, and — most importantly — scaling the web
service past one replica does not multiply the number of tickers. (The lease
guards that anyway, but a design that relies on the guard to be correct is worse
than one that does not create the situation.)

``SCHEDULER_IN_PROCESS=true`` hosts it inside the API instead, which is the
convenient shape for local development where running two processes to see a warm-up
day happen is friction with no benefit.

Exit codes matter here: a platform restarts a non-zero exit and reports it, which
is what should happen when the scheduler is misconfigured. Refusing to start is a
deliberate outcome, so it exits 2 with the explanation rather than logging a
warning and idling — a process that is up but doing nothing is the state this
whole module is designed to make impossible to reach silently.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from src.scheduler.config import SchedulerDisabled, check_startable, load_config
from src.scheduler.runner import connect_redis, run_forever


def _configure_logging() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


async def _main() -> int:
    _configure_logging()
    log = logging.getLogger("scheduler")

    try:
        config = load_config()
    except SchedulerDisabled as exc:
        print(f"scheduler: {exc}", file=sys.stderr)
        return 2

    redis = await connect_redis()

    try:
        check_startable(config, redis_available=redis is not None)
    except SchedulerDisabled as exc:
        # Printed rather than logged: this is the message an operator reads in a
        # crash-looping container's output, and it should not be filtered out by
        # a log level somebody set to WARNING.
        print(f"scheduler: {exc}", file=sys.stderr)
        if redis is not None:
            await redis.aclose()
        return 2

    try:
        await run_forever(config, redis=redis)
    except asyncio.CancelledError:
        log.info("scheduler: shut down cleanly")
    finally:
        if redis is not None:
            await redis.aclose()

    return 0


def main() -> None:
    try:
        raise SystemExit(asyncio.run(_main()))
    except KeyboardInterrupt:
        raise SystemExit(0)


if __name__ == "__main__":
    main()
