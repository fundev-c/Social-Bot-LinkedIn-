"""
What ran and when.

Two kinds of bookkeeping, both answering "when did this last happen":

**Liveness.** A dead scheduler and a quiet one look identical from the outside,
and that is not a hypothetical here — the warm-up programme has *deliberately*
quiet days. The ``observe`` stage plans likes with ``probability=0.8``, so one day
in five an account is scheduled to do nothing at all, and the planner says as
much: *"A deliberately quiet day — real accounts have them."* (This is also what
made the warm-up tests flaky: they were asserting a coin flip.)

So "no activity today" is not evidence of a problem, which means the absence of
activity can never be the alarm. The scheduler has to assert its own aliveness
separately from its output, and that is what the heartbeat is for.

**Sync cadence.** ``run_today`` and ``run_due`` are safe to call every tick — both
return on a DB-only path when nothing is due. ``sync_account`` is different: it
always issues LinkedIn requests, because finding out whether somebody replied is
the whole point. Running it every five minutes would be both wasteful and a
pattern, so it gets its own slower cadence, tracked here.

Cadence is tracked as a key with a TTL rather than a stored timestamp: the key
existing *means* "synced recently", and it expires into "due again" on its own.
Nothing to compare, no clock skew, and losing the record is harmless — the worst
outcome is one extra sync.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

HEARTBEAT_KEY = "scheduler:heartbeat"

# Kept well past any sane interval so a stopped scheduler leaves evidence of when
# it stopped, rather than the record vanishing with it.
HEARTBEAT_TTL_SECONDS = 7 * 24 * 3600


def _synced_key(account_id: Any) -> str:
    return f"scheduler:synced:{account_id}"


async def sync_is_due(redis: Any, account_id: Any, *, interval_seconds: int) -> bool:
    """
    Has this account's sync cadence elapsed?

    Without Redis every sync reads as due. That only arises in a dry run, where
    nothing is executed, so the answer is reported rather than acted on.
    """
    if redis is None:
        return True
    try:
        return not await redis.exists(_synced_key(account_id))
    except Exception as exc:  # pragma: no cover - depends on runtime Redis
        # Fail towards syncing: a redundant read-only sync is a smaller problem
        # than never noticing a reply, which would let a sequence talk over a
        # real conversation.
        logger.warning("Could not read sync cadence for %s: %s", account_id, exc)
        return True


async def record_sync(redis: Any, account_id: Any, *, interval_seconds: int) -> None:
    """Start this account's sync cooldown."""
    if redis is None:
        return
    try:
        await redis.set(_synced_key(account_id), _now_iso(), ex=interval_seconds)
    except Exception as exc:  # pragma: no cover - depends on runtime Redis
        logger.warning("Could not record sync for %s: %s", account_id, exc)


#: The most recent sweep in this process, regardless of Redis.
#:
#: Redis cannot be the only record, because the one mode guaranteed *not* to have
#: Redis is the dry run — and the dry run is the mode most likely to be watched,
#: since it is how you inspect the scheduler before letting it act. A heartbeat
#: that is missing exactly when somebody is looking for it is not a heartbeat.
_last_tick_in_process: Optional[dict] = None


async def record_tick(redis: Any, summary: dict) -> dict:
    """
    Publish the outcome of a sweep, and return the stamped record.

    Recorded three ways, because each covers a gap the others leave: the log for
    history, this process's memory so ``/healthz`` works without Redis, and Redis
    so a *separate* API process can report on a worker it cannot see.
    """
    global _last_tick_in_process

    stamped = {**summary, "at": _now_iso()}
    _last_tick_in_process = stamped
    logger.info("scheduler tick: %s", json.dumps(stamped, default=str, sort_keys=True))

    if redis is None:
        return stamped
    try:
        await redis.set(
            HEARTBEAT_KEY,
            json.dumps(stamped, default=str),
            ex=HEARTBEAT_TTL_SECONDS,
        )
    except Exception as exc:  # pragma: no cover - depends on runtime Redis
        logger.warning("Could not record the scheduler heartbeat: %s", exc)
    return stamped


async def last_tick(redis: Any) -> Optional[dict]:
    """
    The most recent sweep summary, for ``/healthz``.

    Prefers Redis, because when the scheduler runs as its own process that is the
    only place the API can see it. Falls back to this process's own record, which
    is what makes the in-process and dry-run modes observable at all.
    """
    if redis is not None:
        try:
            raw = await redis.get(HEARTBEAT_KEY)
        except Exception as exc:  # pragma: no cover - depends on runtime Redis
            return {"error": f"could not read heartbeat: {exc}"}
        if raw:
            try:
                return json.loads(raw)
            except (TypeError, ValueError):
                return {"error": "heartbeat is not readable JSON"}

    return _last_tick_in_process


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
