"""
The loop around ``tick``.

Deliberately dull. All the interesting decisions are in ``tick`` and ``config``;
this module's only jobs are to turn up on an interval, hold the lease while it
works, and refuse to die quietly.

Three properties worth stating:

* **A failed tick does not stop the loop.** The engine is idempotent, so the
  recovery for almost any transient failure is "try again next interval". A loop
  that exits on the first exception would turn a five-minute blip into an outage
  lasting until somebody noticed.
* **The sleep is jittered.** Ticks landing on exact multiples of five minutes,
  forever, is a pattern — and so is every account being acted on at the same
  moment within a tick (which ``tick`` spaces out separately).
* **Shutdown is cooperative.** ``asyncio.CancelledError`` propagates after
  releasing the lease, so a redeploy hands over immediately instead of leaving a
  claim to expire on its TTL.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, Optional

from src.scheduler import heartbeat
from src.scheduler.config import SchedulerConfig, check_startable, describe
from src.scheduler.lease import NullLease, SchedulerLease
from src.scheduler.tick import run_tick

logger = logging.getLogger(__name__)


async def run_once(
    config: SchedulerConfig,
    *,
    redis: Any = None,
    session_factory: Any = None,
    sleep: Optional[Any] = None,
) -> dict:
    """
    Acquire the lease, run one sweep, release it.

    Returns the summary that was recorded as the heartbeat. Losing the lease race
    is reported as ``lease_denied`` rather than raised: another ticker is alive
    and doing the work, which is success from the system's point of view.
    """
    if session_factory is None:
        from src.database.session import AsyncSessionLocal

        session_factory = AsyncSessionLocal

    lease = (
        NullLease()
        if config.dry_run or redis is None
        else SchedulerLease(redis, ttl_seconds=config.effective_lease_ttl)
    )

    if not await lease.acquire():
        holder = await lease.holder()
        logger.info("scheduler: another ticker holds the lease (%s); skipping", holder)
        summary = {"accounts": 0, "errors": 0, "lease_denied": True, "holder": holder}
        return summary

    try:
        rate_limiter = _build_rate_limiter(redis, config)
        async with session_factory() as db:
            result = await run_tick(
                db,
                config,
                redis=redis,
                rate_limiter=rate_limiter,
                sleep=sleep or asyncio.sleep,
            )
        summary = result.as_summary()
        await heartbeat.record_tick(redis, summary)
        return summary
    finally:
        await lease.release()


def ensure_logging_is_visible() -> None:
    """
    Make sure the scheduler's INFO output actually reaches somewhere.

    Not defensive boilerplate — it fixes a real silence. Under uvicorn only the
    ``uvicorn.*`` loggers get handlers, so the root logger has none and Python
    falls back to ``logging.lastResort``, which emits WARNING and above. Every
    tick summary is INFO, so in-process the scheduler logged *nothing*: it
    reported starting and it reported each sweep, and both were dropped.

    Combined with a dry run having no Redis to hold the heartbeat, that produced a
    scheduler with no observable output whatsoever — precisely the "looks alive,
    does nothing" state this package exists to make impossible.

    Only acts when nobody else has configured logging, so a process with its own
    setup (the worker, or an app that called ``basicConfig``) is left alone rather
    than having a second handler bolted on and every line duplicated.
    """
    scheduler_logger = logging.getLogger("src.scheduler")

    if logging.getLogger().handlers or scheduler_logger.handlers:
        return

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    scheduler_logger.addHandler(handler)
    scheduler_logger.setLevel(logging.INFO)


async def run_forever(
    config: SchedulerConfig,
    *,
    redis: Any = None,
    session_factory: Any = None,
    max_ticks: Optional[int] = None,
) -> None:
    """
    Tick until cancelled.

    ``max_ticks`` bounds the loop for tests; production leaves it unset.
    """
    check_startable(config, redis_available=redis is not None)
    ensure_logging_is_visible()
    logger.info(
        "scheduler starting: %s", describe(config, redis_available=redis is not None)
    )

    ticks = 0
    try:
        while max_ticks is None or ticks < max_ticks:
            try:
                await run_once(config, redis=redis, session_factory=session_factory)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Never let one bad sweep end the loop; the next interval retries
                # and the engine is idempotent, so nothing is lost by waiting.
                logger.exception("scheduler: tick failed; continuing")

            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                break
            await asyncio.sleep(_next_delay(config))
    except asyncio.CancelledError:
        logger.info("scheduler: stopping (cancelled)")
        raise


def _next_delay(config: SchedulerConfig) -> float:
    """The interval, jittered, so ticks do not land on a metronome."""
    if not config.jitter_ratio:
        return float(config.interval_seconds)
    spread = config.interval_seconds * config.jitter_ratio
    return max(1.0, config.interval_seconds + random.uniform(-spread, spread))


def _build_rate_limiter(redis: Any, config: SchedulerConfig) -> Any:
    """
    The Redis-backed limiter, or nothing in a dry run.

    ``check_startable`` has already refused a live scheduler without Redis, so
    reaching here with ``redis is None`` means this is a dry run and no action
    will be taken anyway.
    """
    if redis is None:
        return None
    from src.infrastructure.rate_policy import AccountRateLimiter

    return AccountRateLimiter(redis)


async def connect_redis(url: Optional[str] = None) -> Any:
    """
    Connect to Redis, or return None.

    Returning None rather than raising keeps the decision about whether Redis is
    mandatory in ``config.check_startable``, where it is explained, instead of
    splitting that rule across the connection code as well.
    """
    import os

    redis_url = url or os.getenv("REDIS_URL", "redis://localhost:6379/0")
    try:
        from redis import asyncio as aioredis

        client = aioredis.from_url(redis_url, decode_responses=True)
        await client.ping()
        return client
    except Exception as exc:
        logger.warning("scheduler: Redis unavailable at %s (%s)", redis_url, exc)
        return None
