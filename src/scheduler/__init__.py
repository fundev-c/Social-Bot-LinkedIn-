"""
The scheduler: what makes the programme autonomous.

Everything the product does on a schedule was already built and tested —
``warmup.runner.run_today``, ``outreach.sync.sync_account`` and
``outreach.execute.run_due`` — but nothing ever called them on a tick. They were
reachable from exactly one HTTP route each, so a warm-up day only happened if a
human poked an endpoint. This package is the missing caller.

Why a plain loop is enough
--------------------------
The engine is already idempotent and self-pacing, which removes the usual reasons
to reach for cron semantics or a durable job store:

* ``run_today`` performs only actions whose planned time has passed, subtracts
  work already done today, and respects the account's pause flag. Calling it more
  often does not make an account act faster.
* ``run_due`` filters on ``scheduled_for`` in SQL and stops at the first capacity
  refusal per action rather than hammering the limiter.

So a missed tick is not a missed action — the next tick picks it up — and a
duplicated tick is not duplicated activity. What the scheduler owes the system is
therefore modest: turn up regularly, sweep every account, keep one ticker
running at a time, and be honest about whether it is alive.

Structure
---------
``config``    environment parsing and the refusal rules, in one place
``accounts``  the one deliberately cross-tenant reader in the codebase
``lease``     the Redis lease that keeps exactly one ticker running
``tick``      a single sweep; pure enough to test without a loop or a clock
``runner``    the loop around ``tick``
``heartbeat`` what ran and when, so silence can be diagnosed

Run it with ``python -m src.scheduler``, or set ``SCHEDULER_IN_PROCESS=true`` to
host it inside the API for local development.
"""

from src.scheduler.config import SchedulerConfig, SchedulerDisabled, load_config

__all__ = ["SchedulerConfig", "SchedulerDisabled", "load_config"]
