"""
One sweep.

A tick enumerates every schedulable account and, for each, does up to three
things in a fixed order:

1. **sync** — pull acceptance and reply state back from LinkedIn (slow cadence)
2. **warm-up** — perform whatever the day's plan says is due now
3. **send** — execute approved outreach suggestions that are due

**The order matters and is not alphabetical.** Sync runs first because it is what
cancels a sequence when somebody has replied. Sending first would mean a tick
could deliver a follow-up into a conversation that already had an answer waiting
— the single worst thing this product can do, because it is visible to the
prospect and unmistakably robotic. One tick's delay in noticing a reply is
acceptable; one message sent over the top of it is not.

Warm-up sits between them because it is the cheapest and least consequential:
likes and follows, with comments going to the approval queue.

Failure isolation
-----------------
Every account is wrapped, and every stage within an account is wrapped. One
account with expired cookies must not stop the other nineteen from running, and a
sync failure must not cost that account its warm-up day. Failures are counted and
reported in the summary rather than raised, so a partial sweep is still a
successful tick that tells you what went wrong.

This mirrors how the engine already behaves — ``sync_account``'s docstring says
transport failures are "reported rather than raised: a sync that can't reach
LinkedIn should leave the system in its previous (safe) state, not crash the
scheduler." This module is the scheduler that docstring was written for.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from src.scheduler import heartbeat
from src.scheduler.accounts import due_accounts
from src.scheduler.config import SchedulerConfig

logger = logging.getLogger(__name__)


@dataclass
class AccountOutcome:
    """What happened for one account in one tick."""

    account_id: str
    org_id: str
    synced: Optional[dict] = None
    warmup: Optional[dict] = None
    sent: Optional[dict] = None
    skipped: List[str] = field(default_factory=list)
    errors: dict = field(default_factory=dict)

    def as_summary(self) -> dict:
        payload: dict = {"account_id": self.account_id, "org_id": self.org_id}
        if self.synced is not None:
            payload["synced"] = self.synced
        if self.warmup is not None:
            payload["warmup"] = self.warmup
        if self.sent is not None:
            payload["sent"] = self.sent
        if self.skipped:
            payload["skipped"] = self.skipped
        if self.errors:
            payload["errors"] = self.errors
        return payload


@dataclass
class TickResult:
    """The whole sweep."""

    accounts: List[AccountOutcome] = field(default_factory=list)
    dry_run: bool = False
    lease_denied: bool = False

    @property
    def error_count(self) -> int:
        return sum(len(outcome.errors) for outcome in self.accounts)

    def as_summary(self) -> dict:
        return {
            "accounts": len(self.accounts),
            "errors": self.error_count,
            "mode": "dry-run" if self.dry_run else "live",
            "lease_denied": self.lease_denied,
            "detail": [outcome.as_summary() for outcome in self.accounts],
        }


async def run_tick(
    db: AsyncSession,
    config: SchedulerConfig,
    *,
    redis: Any = None,
    rate_limiter: Any = None,
    sleep: Optional[Callable] = None,
) -> TickResult:
    """
    Sweep every schedulable account once.

    ``sleep`` is injected so the inter-account spacing can be asserted on in
    tests without actually waiting; the runner passes ``asyncio.sleep``.
    """
    result = TickResult(dry_run=config.dry_run)
    accounts = await due_accounts(db)

    if not accounts:
        logger.info("scheduler: no schedulable accounts")
        return result

    for index, account in enumerate(accounts):
        if index and config.account_spacing_seconds and sleep is not None:
            # Spread the sweep out. All accounts acting the moment a tick starts
            # is a signature, and looking human is the entire premise.
            await sleep(config.account_spacing_seconds)

        outcome = await _run_account(
            db,
            account,
            config,
            redis=redis,
            rate_limiter=rate_limiter,
        )
        result.accounts.append(outcome)

    return result


async def _run_account(
    db: AsyncSession,
    account: Any,
    config: SchedulerConfig,
    *,
    redis: Any,
    rate_limiter: Any,
) -> AccountOutcome:
    """Run all three stages for one account, isolating each failure."""
    outcome = AccountOutcome(
        account_id=str(getattr(account, "id", "unknown")),
        org_id=str(getattr(account, "org_id", "unknown")),
    )

    # ---- 1. Sync (slow cadence; always costs LinkedIn requests) ----
    if await heartbeat.sync_is_due(
        redis, account.id, interval_seconds=config.sync_interval_seconds
    ):
        if config.dry_run:
            outcome.synced = {"would_sync": True}
        else:
            try:
                from src.outreach import sync as sync_module

                outcome.synced = await sync_module.sync_account(db, account)
                await heartbeat.record_sync(
                    redis, account.id, interval_seconds=config.sync_interval_seconds
                )
            except Exception as exc:
                _record_error(outcome, "sync", exc)
    else:
        outcome.skipped.append("sync: cadence not elapsed")

    # ---- 2. Warm-up (self-pacing; cheap when nothing is due) ----
    if config.dry_run:
        outcome.warmup = await _warmup_preview(db, account)
    else:
        try:
            from src.warmup import runner as warmup_runner

            outcome.warmup = await warmup_runner.run_today(
                db, account, rate_limiter=rate_limiter
            )
        except Exception as exc:
            _record_error(outcome, "warmup", exc)

    # ---- 3. Send approved outreach that is due ----
    if config.dry_run:
        outcome.sent = {"would_run_due": True, "limit": config.send_limit}
    else:
        try:
            from src.outreach import execute as executor

            outcome.sent = await executor.run_due(
                db,
                account,
                rate_limiter=rate_limiter,
                limit=config.send_limit,
            )
        except Exception as exc:
            _record_error(outcome, "send", exc)

    return outcome


async def _warmup_preview(db: AsyncSession, account: Any) -> dict:
    """
    What warm-up would do, without doing it.

    Reads the same assessment ``run_today`` reads, so a dry run reflects the real
    decision rather than a parallel reimplementation of it that can drift.
    """
    try:
        from src.warmup import service as warmup_service

        assessment = await warmup_service.today(db, account)
        if assessment.get("paused"):
            return {"would_perform": [], "paused": True}

        actions = assessment.get("plan", {}).get("actions", []) or []
        return {
            "stage": assessment.get("stage"),
            "planned_today": len(actions),
            "would_perform": [item.get("action") for item in actions],
        }
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def _record_error(outcome: AccountOutcome, stage: str, exc: BaseException) -> None:
    """
    Record a stage failure and carry on.

    The type name is included because the message alone is often ambiguous: a
    transport failure, an expired cookie and a programming error can all arrive
    as a bare string, and they call for completely different responses.
    """
    outcome.errors[stage] = f"{type(exc).__name__}: {exc}"
    logger.warning(
        "scheduler: %s failed for account %s: %s",
        stage,
        outcome.account_id,
        exc,
        exc_info=True,
    )
