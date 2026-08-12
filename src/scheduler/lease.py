"""
The scheduler lease: exactly one ticker, whatever the deployment does.

Why this exists
---------------
The rate limiter makes a single sweep safe. It does not make two *concurrent*
sweeps safe: both can read the same usage window, both can conclude there is
budget, and both can spend it. The result is an account acting at twice its
configured ceiling, which is the specific failure that gets a LinkedIn account
restricted rather than merely producing a confusing log.

Two concurrent sweeps are easy to arrange by accident:

* the API is scaled past one replica while ``SCHEDULER_IN_PROCESS`` is set, so
  every web process brings its own ticker;
* the worker is deployed while an in-process scheduler is still enabled;
* a deploy overlaps, and the old process is still finishing a sweep.

None of those are misconfigurations anyone would notice from the outside, because
the symptom is *more activity*, not an error. So the guard is not optional and it
is not a deployment convention — it is enforced here, in code, for every mode.

How it works
------------
``SET key owner NX EX ttl`` is an atomic claim. The owner token means release and
renewal can verify they still hold the lease rather than stamping on a successor
that took over after a stall. Release is a compare-and-delete via Lua, because
checking and deleting as two round trips is the same race one level down.

The TTL is the failure story: a ticker that dies mid-sweep cannot release, so the
claim simply expires and the next process takes over. Nothing needs cleaning up,
which is the point — recovery is the default rather than a procedure.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)

LEASE_KEY = "scheduler:lease"

# Delete only if we still own it. Two round trips (GET then DEL) would let a
# successor's claim be deleted in the gap.
_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""

# Extend only if we still own it, for the same reason.
_RENEW_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('expire', KEYS[1], ARGV[2])
end
return 0
"""


class SchedulerLease:
    """A single-holder, expiring claim on the right to run a sweep."""

    def __init__(self, redis: Any, *, ttl_seconds: int, key: str = LEASE_KEY):
        self._redis = redis
        self._ttl = ttl_seconds
        self._key = key
        self._owner = str(uuid.uuid4())
        self._held = False

    @property
    def owner(self) -> str:
        return self._owner

    @property
    def held(self) -> bool:
        return self._held

    async def acquire(self) -> bool:
        """
        Claim the lease, or report that somebody else holds it.

        A refusal is normal operation, not an error: it means another ticker is
        alive and doing the work.
        """
        acquired = await self._redis.set(self._key, self._owner, nx=True, ex=self._ttl)
        self._held = bool(acquired)
        return self._held

    async def renew(self) -> bool:
        """
        Push the expiry out while a sweep is still running.

        Returns False if the lease was lost — the sweep took longer than the TTL
        and somebody else has legitimately taken over. The caller should stop
        rather than carry on acting without a claim.
        """
        if not self._held:
            return False
        result = await self._redis.eval(_RENEW_SCRIPT, 1, self._key, self._owner, self._ttl)
        self._held = bool(result)
        return self._held

    async def release(self) -> None:
        """Give up the lease if we still hold it. Never raises."""
        if not self._held:
            return
        try:
            await self._redis.eval(_RELEASE_SCRIPT, 1, self._key, self._owner)
        except Exception as exc:  # pragma: no cover - depends on runtime Redis
            # The TTL is the backstop, so a failed release costs one idle
            # interval at worst. Not worth failing a completed sweep over.
            logger.warning("Could not release the scheduler lease: %s", exc)
        finally:
            self._held = False

    async def holder(self) -> Optional[str]:
        """Who holds the lease right now, for diagnostics."""
        return await self._redis.get(self._key)

    async def __aenter__(self) -> "SchedulerLease":
        await self.acquire()
        return self

    async def __aexit__(self, *_exc_info) -> None:
        await self.release()


class NullLease:
    """
    A lease that is always granted, for dry runs.

    A dry run performs no actions, so there is no budget for a second sweep to
    double-spend and no reason to require Redis just to look at what the
    scheduler would do. Deliberately a separate class rather than a flag inside
    ``SchedulerLease``: the real lease should have no branch in it that can be
    turned off by configuration.
    """

    owner = "dry-run"
    held = True

    async def acquire(self) -> bool:
        return True

    async def renew(self) -> bool:
        return True

    async def release(self) -> None:
        return None

    async def holder(self) -> Optional[str]:
        return self.owner

    async def __aenter__(self) -> "NullLease":
        return self

    async def __aexit__(self, *_exc_info) -> None:
        return None
