"""
Scheduler configuration, and the two things it refuses to do.

All environment parsing lives here so the refusal rules are stated once, in one
readable place, rather than being spread across the loop as scattered guards.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

# Ticks are cheap when nothing is due (both entry points return on a DB-only
# path), so the interval is chosen for send responsiveness rather than to save
# work. The warm-up runner itself is happy with anything from 15-30 minutes.
DEFAULT_INTERVAL_SECONDS = 300

# Syncing always costs LinkedIn requests — unlike the other two it cannot return
# early — so it gets its own, slower cadence instead of running every tick.
DEFAULT_SYNC_INTERVAL_SECONDS = 1800

# Spacing between accounts inside one sweep. Every account acting at exactly
# :00 is a pattern, and this programme's entire premise is not being one.
DEFAULT_ACCOUNT_SPACING_SECONDS = 5.0

# Proportion of the interval to jitter each sleep by, for the same reason.
DEFAULT_JITTER_RATIO = 0.1

# Ceiling on suggestions executed per account per tick, passed through to
# ``run_due``. Bounds the blast radius of one bad sweep.
DEFAULT_SEND_LIMIT = 10


class SchedulerDisabled(RuntimeError):
    """
    Raised when the scheduler must not run.

    Carries an operator-facing explanation: this is what the worker prints
    before exiting non-zero, so it has to say what to do, not just what is wrong.
    """


@dataclass(frozen=True)
class SchedulerConfig:
    enabled: bool = False
    dry_run: bool = False
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS
    sync_interval_seconds: int = DEFAULT_SYNC_INTERVAL_SECONDS
    account_spacing_seconds: float = DEFAULT_ACCOUNT_SPACING_SECONDS
    jitter_ratio: float = DEFAULT_JITTER_RATIO
    send_limit: int = DEFAULT_SEND_LIMIT
    lease_ttl_seconds: int = 0  # 0 => derived from the interval
    in_process: bool = False

    @property
    def requires_redis(self) -> bool:
        """
        Live sending needs Redis; a dry run does not.

        The rate limiter is Redis-backed, and an autonomous sender with no caps
        is the specific way accounts get restricted. A dry run executes nothing,
        so there is no budget to exceed and no reason to make local inspection
        depend on infrastructure.
        """
        return self.enabled and not self.dry_run

    @property
    def effective_lease_ttl(self) -> int:
        """
        How long a ticker's claim survives without renewal.

        Longer than the interval, so a slow sweep does not let a second ticker
        in behind it; short enough that a crashed ticker is replaced promptly.
        """
        if self.lease_ttl_seconds:
            return self.lease_ttl_seconds
        return max(60, self.interval_seconds * 3)


def _flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise SchedulerDisabled(
            f"{name}={raw!r} is not an integer. Unset it to use the default ({default})."
        ) from exc
    if value <= 0:
        raise SchedulerDisabled(
            f"{name}={value} must be greater than zero. A non-positive interval "
            "would spin the loop with no delay."
        )
    return value


def _ratio(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise SchedulerDisabled(f"{name}={raw!r} is not a number.") from exc
    if not 0.0 <= value < 1.0:
        raise SchedulerDisabled(
            f"{name}={value} must be at least 0 and less than 1 (it is a "
            "proportion of the tick interval)."
        )
    return value


def _seconds(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise SchedulerDisabled(f"{name}={raw!r} is not a number.") from exc
    if value < 0:
        raise SchedulerDisabled(f"{name}={value} cannot be negative.")
    return value


def load_config() -> SchedulerConfig:
    """Read the scheduler's configuration from the environment."""
    return SchedulerConfig(
        enabled=_flag("SCHEDULER_ENABLED", False),
        dry_run=_flag("SCHEDULER_DRY_RUN", False),
        interval_seconds=_positive_int("SCHEDULER_INTERVAL_SECONDS", DEFAULT_INTERVAL_SECONDS),
        sync_interval_seconds=_positive_int(
            "SCHEDULER_SYNC_INTERVAL_SECONDS", DEFAULT_SYNC_INTERVAL_SECONDS
        ),
        account_spacing_seconds=_seconds(
            "SCHEDULER_ACCOUNT_SPACING_SECONDS", DEFAULT_ACCOUNT_SPACING_SECONDS
        ),
        jitter_ratio=_ratio("SCHEDULER_JITTER_RATIO", DEFAULT_JITTER_RATIO),
        send_limit=_positive_int("SCHEDULER_SEND_LIMIT", DEFAULT_SEND_LIMIT),
        lease_ttl_seconds=int(_seconds("SCHEDULER_LEASE_TTL_SECONDS", 0)),
        in_process=_flag("SCHEDULER_IN_PROCESS", False),
    )


def check_startable(config: SchedulerConfig, *, redis_available: bool) -> None:
    """
    Raise ``SchedulerDisabled`` unless the scheduler may start.

    Two refusals, both deliberate:

    1. **Not enabled.** Off by default. The scheduler drives real writes to real
       LinkedIn accounts, and the mobile transport has not yet been validated
       against a live account, so turning it on has to be a decision somebody
       makes rather than a default they inherit.
    2. **No Redis while live.** The per-account rate caps are enforced through a
       Redis-backed limiter. Without it the endpoints refuse to act unless
       ``ALLOW_UNCAPPED_SENDING`` is set — an escape hatch for a human clicking a
       button, which is a different thing from an unattended loop. The scheduler
       does not honour that override: it refuses instead, because an uncapped
       autonomous sender is precisely how an account gets restricted.
    """
    if not config.enabled:
        raise SchedulerDisabled(
            "The scheduler is disabled (SCHEDULER_ENABLED is not set).\n"
            "\n"
            "It is off by default on purpose: it drives real activity on real "
            "LinkedIn accounts, and the mobile transport has not been validated "
            "against a live account yet.\n"
            "\n"
            "  SCHEDULER_ENABLED=true SCHEDULER_DRY_RUN=true   see what it would do\n"
            "  SCHEDULER_ENABLED=true                          let it act"
        )

    if config.requires_redis and not redis_available:
        raise SchedulerDisabled(
            "The scheduler needs Redis and cannot reach it.\n"
            "\n"
            "Per-account rate caps are enforced through a Redis-backed limiter. "
            "Running without it would mean an unattended loop acting with no "
            "ceiling, which is how a LinkedIn account gets restricted. "
            "ALLOW_UNCAPPED_SENDING is deliberately not honoured here.\n"
            "\n"
            "  Set REDIS_URL to a reachable instance, or\n"
            "  SCHEDULER_DRY_RUN=true to inspect decisions without acting "
            "(needs no Redis)."
        )


def describe(config: SchedulerConfig, *, redis_available: Optional[bool] = None) -> dict:
    """A snapshot for ``/healthz`` and startup logs."""
    payload = {
        "enabled": config.enabled,
        "mode": "dry-run" if config.dry_run else "live",
        "interval_seconds": config.interval_seconds,
        "sync_interval_seconds": config.sync_interval_seconds,
        "send_limit": config.send_limit,
        "in_process": config.in_process,
    }
    if redis_available is not None:
        payload["redis_available"] = redis_available
    return payload
