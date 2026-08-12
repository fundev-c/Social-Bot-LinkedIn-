"""
Scheduler tests.

The scheduler is the piece that turns a built programme into an autonomous one,
which means its failure modes are the expensive kind: acting twice, acting
uncapped, acting on accounts that should be left alone, or appearing to work while
doing nothing. Those are what these tests are about — not the loop mechanics.

The engine functions themselves (``run_today``, ``sync_account``, ``run_due``) are
covered by their own suites and are stubbed here. What is under test is the
scheduler's judgement: who it sweeps, in what order, what it refuses, and what it
reports.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from src.accounts.models import AccountStatus, ConnectedAccount
from src.scheduler import tick as tick_module
from src.scheduler.config import SchedulerConfig, SchedulerDisabled, check_startable, load_config
from src.scheduler.lease import NullLease, SchedulerLease


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeRedis:
    """Enough of redis.asyncio for the lease and the heartbeat."""

    def __init__(self):
        self.store: dict = {}

    async def set(self, key, value, *, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def get(self, key):
        return self.store.get(key)

    async def exists(self, key):
        return 1 if key in self.store else 0

    async def eval(self, script, numkeys, key, *args):
        # Both scripts are compare-and-act on the owner token.
        owner = args[0]
        if self.store.get(key) != owner:
            return 0
        if "del" in script:
            del self.store[key]
        return 1


class FakeSession:
    """A stand-in AsyncSession that returns a fixed account list."""

    def __init__(self, accounts):
        self._accounts = accounts

    async def execute(self, _stmt):
        accounts = self._accounts

        class Result:
            def scalars(self):
                class Scalars:
                    def all(self_inner):
                        return accounts

                return Scalars()

        return Result()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None


def make_account(status=AccountStatus.ACTIVE, org_id=None) -> ConnectedAccount:
    account = ConnectedAccount()
    account.id = uuid.uuid4()
    account.org_id = org_id or uuid.uuid4()
    account.user_id = uuid.uuid4()
    account.status = status
    account.daily_caps = {}
    return account


@pytest.fixture
def live_config():
    return SchedulerConfig(enabled=True, account_spacing_seconds=0.0)


@pytest.fixture
def dry_config():
    return SchedulerConfig(enabled=True, dry_run=True, account_spacing_seconds=0.0)


# ---------------------------------------------------------------------------
# Refusals — the two things the scheduler will not do
# ---------------------------------------------------------------------------


def test_disabled_by_default():
    """
    Off unless explicitly enabled.

    It drives real writes to real accounts and the mobile transport has not been
    validated against a live one yet, so being on must be a decision.
    """
    with pytest.raises(SchedulerDisabled, match="SCHEDULER_ENABLED"):
        check_startable(SchedulerConfig(enabled=False), redis_available=True)


def test_live_scheduler_refuses_to_run_without_redis(live_config):
    """No limiter means no caps, and an uncapped autonomous sender is the risk."""
    with pytest.raises(SchedulerDisabled, match="needs Redis"):
        check_startable(live_config, redis_available=False)


def test_dry_run_does_not_need_redis(dry_config):
    """Nothing is executed, so there is no budget to exceed."""
    check_startable(dry_config, redis_available=False)


def test_uncapped_sending_override_is_not_honoured(monkeypatch, live_config):
    """
    ALLOW_UNCAPPED_SENDING is an escape hatch for a human pressing a button.

    Extending it to an unattended loop would be a different decision than the one
    that flag was introduced for, so the scheduler ignores it.
    """
    monkeypatch.setenv("ALLOW_UNCAPPED_SENDING", "true")
    with pytest.raises(SchedulerDisabled, match="needs Redis"):
        check_startable(live_config, redis_available=False)


def test_bad_interval_is_rejected_with_an_explanation(monkeypatch):
    """A zero interval would spin the loop with no delay."""
    monkeypatch.setenv("SCHEDULER_INTERVAL_SECONDS", "0")
    with pytest.raises(SchedulerDisabled, match="greater than zero"):
        load_config()


def test_defaults_are_off_and_live(monkeypatch):
    for name in (
        "SCHEDULER_ENABLED",
        "SCHEDULER_DRY_RUN",
        "SCHEDULER_IN_PROCESS",
        "SCHEDULER_INTERVAL_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)

    config = load_config()
    assert config.enabled is False
    assert config.dry_run is False
    assert config.in_process is False
    assert config.requires_redis is False  # because it is not enabled


# ---------------------------------------------------------------------------
# The lease — the guard against acting twice
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_only_one_ticker_can_hold_the_lease():
    """
    Two concurrent sweeps can both see budget and both spend it.

    That is the failure that gets an account restricted, and it is easy to arrange
    by accident (a second web replica, or a worker deployed alongside an
    in-process scheduler), so the lease is enforced rather than assumed.
    """
    redis = FakeRedis()
    first = SchedulerLease(redis, ttl_seconds=60)
    second = SchedulerLease(redis, ttl_seconds=60)

    assert await first.acquire() is True
    assert await second.acquire() is False

    await first.release()
    assert await second.acquire() is True


@pytest.mark.asyncio
async def test_releasing_does_not_delete_a_successors_claim():
    """
    Release is a compare-and-delete.

    If a ticker stalls past its TTL, a successor takes over legitimately. The
    stalled process waking up and releasing must not remove the new holder's
    claim, which is why release checks the owner token atomically.
    """
    redis = FakeRedis()
    stalled = SchedulerLease(redis, ttl_seconds=60)
    await stalled.acquire()

    # The TTL expires and a successor claims it.
    redis.store.clear()
    successor = SchedulerLease(redis, ttl_seconds=60)
    await successor.acquire()

    await stalled.release()

    assert await successor.holder() == successor.owner


@pytest.mark.asyncio
async def test_renew_reports_a_lost_lease():
    """A sweep that outlives its claim must learn about it, not act regardless."""
    redis = FakeRedis()
    lease = SchedulerLease(redis, ttl_seconds=60)
    await lease.acquire()

    redis.store.clear()  # expired
    SchedulerLease(redis, ttl_seconds=60)
    await redis.set("scheduler:lease", "somebody-else")

    assert await lease.renew() is False
    assert lease.held is False


@pytest.mark.asyncio
async def test_dry_run_uses_a_lease_that_is_always_granted():
    lease = NullLease()
    assert await lease.acquire() is True
    assert await lease.acquire() is True


# ---------------------------------------------------------------------------
# Who gets swept
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_only_active_accounts_are_swept(dry_config, monkeypatch):
    """
    Acting on a non-ACTIVE account is wrong, not merely unproductive.

    AUTH_REQUIRED needs new cookies (acting yields failures that look like
    transport bugs), SUSPENDED and ERROR need a human, and RATE_LIMITED means
    LinkedIn already pushed back.
    """
    from src.scheduler import accounts as accounts_module

    assert accounts_module.SCHEDULABLE_STATUSES == (AccountStatus.ACTIVE,)


@pytest.mark.asyncio
async def test_sweep_covers_every_organization(dry_config):
    """
    The scheduler has no request and no tenant, so it must cross orgs.

    This is the one deliberately unscoped reader in the codebase; the test states
    the intent so nobody "fixes" it into an org-scoped query that would silently
    stop serving every organization but one.
    """
    accounts = [make_account(), make_account(), make_account()]
    org_ids = {str(a.org_id) for a in accounts}
    db = FakeSession(accounts)

    result = await tick_module.run_tick(db, dry_config, sleep=None)

    assert len(result.accounts) == 3
    assert {o.org_id for o in result.accounts} == org_ids


# ---------------------------------------------------------------------------
# Ordering and isolation — the two properties that protect the product
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_runs_before_sending(live_config, monkeypatch):
    """
    Sync cancels a sequence when somebody has replied.

    Sending first would let a tick deliver a follow-up into a conversation that
    already had an answer waiting — visible to the prospect and unmistakably
    robotic. One tick's delay noticing a reply is acceptable; talking over it is
    not.
    """
    calls: list = []

    async def fake_sync(db, account, **kwargs):
        calls.append("sync")
        return {"replies": 0}

    async def fake_warmup(db, account, **kwargs):
        calls.append("warmup")
        return {"performed": []}

    async def fake_run_due(db, account, **kwargs):
        calls.append("send")
        return {"sent": [], "blocked": {}, "considered": 0}

    _patch_engine(monkeypatch, fake_sync, fake_warmup, fake_run_due)

    db = FakeSession([make_account()])
    await tick_module.run_tick(db, live_config, redis=None, sleep=None)

    assert calls == ["sync", "warmup", "send"]


@pytest.mark.asyncio
async def test_one_failing_account_does_not_stop_the_sweep(live_config, monkeypatch):
    """
    An account with expired cookies must not cost every other account its day.

    Without isolation, the first broken account in creation order would silently
    become a global outage.
    """
    seen: list = []

    async def fake_sync(db, account, **kwargs):
        seen.append(str(account.id))
        raise RuntimeError("voyager request failed: redirects followed")

    async def fake_warmup(db, account, **kwargs):
        return {"performed": []}

    async def fake_run_due(db, account, **kwargs):
        return {"sent": [], "blocked": {}, "considered": 0}

    _patch_engine(monkeypatch, fake_sync, fake_warmup, fake_run_due)

    accounts = [make_account(), make_account(), make_account()]
    db = FakeSession(accounts)

    result = await tick_module.run_tick(db, live_config, redis=None, sleep=None)

    assert len(seen) == 3, "every account should have been attempted"
    assert len(result.accounts) == 3
    assert result.error_count == 3
    for outcome in result.accounts:
        assert "sync" in outcome.errors
        # The stage that failed must not take the others down with it.
        assert outcome.warmup is not None
        assert outcome.sent is not None


@pytest.mark.asyncio
async def test_a_failing_stage_does_not_cost_the_account_its_other_stages(
    live_config, monkeypatch
):
    """Warm-up failing must not prevent approved outreach from going out."""

    async def fake_sync(db, account, **kwargs):
        return {"replies": 0}

    async def fake_warmup(db, account, **kwargs):
        raise RuntimeError("planner exploded")

    async def fake_run_due(db, account, **kwargs):
        return {"sent": ["one"], "blocked": {}, "considered": 1}

    _patch_engine(monkeypatch, fake_sync, fake_warmup, fake_run_due)

    db = FakeSession([make_account()])
    result = await tick_module.run_tick(db, live_config, redis=None, sleep=None)

    outcome = result.accounts[0]
    assert "warmup" in outcome.errors
    assert outcome.sent == {"sent": ["one"], "blocked": {}, "considered": 1}


@pytest.mark.asyncio
async def test_errors_name_the_exception_type(live_config, monkeypatch):
    """
    A transport failure, an expired cookie and a bug can all arrive as a bare
    string. The type is what distinguishes them, so it is recorded.
    """

    async def fake_sync(db, account, **kwargs):
        raise ValueError("something opaque")

    async def noop(db, account, **kwargs):
        return {}

    _patch_engine(monkeypatch, fake_sync, noop, noop)

    db = FakeSession([make_account()])
    result = await tick_module.run_tick(db, live_config, redis=None, sleep=None)

    assert result.accounts[0].errors["sync"].startswith("ValueError:")


# ---------------------------------------------------------------------------
# Cadence and pacing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_is_skipped_while_its_cadence_has_not_elapsed(live_config, monkeypatch):
    """
    Sync always costs LinkedIn requests — it cannot return early the way the other
    two can — so it runs on its own slower clock rather than every tick.
    """
    syncs: list = []

    async def fake_sync(db, account, **kwargs):
        syncs.append(1)
        return {"replies": 0}

    async def noop(db, account, **kwargs):
        return {}

    _patch_engine(monkeypatch, fake_sync, noop, noop)

    redis = FakeRedis()
    account = make_account()
    db = FakeSession([account])

    await tick_module.run_tick(db, live_config, redis=redis, sleep=None)
    second = await tick_module.run_tick(db, live_config, redis=redis, sleep=None)

    assert len(syncs) == 1, "the second tick should not have synced again"
    assert any("sync" in s for s in second.accounts[0].skipped)


@pytest.mark.asyncio
async def test_accounts_are_spaced_out_within_a_sweep(monkeypatch):
    """
    Every account acting the instant a tick begins is a signature, and looking
    human is the entire premise of the warm-up programme.
    """
    slept: list = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    async def noop(db, account, **kwargs):
        return {}

    _patch_engine(monkeypatch, noop, noop, noop)

    config = SchedulerConfig(enabled=True, account_spacing_seconds=5.0)
    db = FakeSession([make_account(), make_account(), make_account()])

    await tick_module.run_tick(db, config, redis=None, sleep=fake_sleep)

    # Spacing goes *between* accounts, so three accounts means two waits.
    assert slept == [5.0, 5.0]


def test_the_tick_interval_is_jittered():
    """Ticks landing on exact multiples of the interval forever is a pattern."""
    from src.scheduler.runner import _next_delay

    config = SchedulerConfig(enabled=True, interval_seconds=300, jitter_ratio=0.1)
    delays = {_next_delay(config) for _ in range(25)}

    assert len(delays) > 1, "the delay should vary"
    assert all(270 <= d <= 330 for d in delays)


def test_jitter_can_be_switched_off():
    from src.scheduler.runner import _next_delay

    config = SchedulerConfig(enabled=True, interval_seconds=300, jitter_ratio=0.0)
    assert _next_delay(config) == 300.0


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_executes_nothing(dry_config, monkeypatch):
    """
    The whole point: mergeable and inspectable before the transport is proven.

    If a dry run could reach the engine, it would be acting on a live LinkedIn
    account with request shapes change 5 has not yet validated.
    """
    called: list = []

    async def tripwire(db, account, **kwargs):
        called.append("executed")
        return {}

    _patch_engine(monkeypatch, tripwire, tripwire, tripwire)

    db = FakeSession([make_account()])
    result = await tick_module.run_tick(db, dry_config, redis=None, sleep=None)

    assert called == [], "a dry run must not reach the engine"
    assert result.dry_run is True
    assert result.accounts[0].sent == {"would_run_due": True, "limit": dry_config.send_limit}


@pytest.mark.asyncio
async def test_dry_run_reports_what_warmup_would_do(dry_config, monkeypatch):
    """
    The preview reads the same assessment ``run_today`` reads.

    A parallel reimplementation would drift, and a dry run that lies is worse
    than no dry run at all.
    """

    async def fake_today(db, account, **kwargs):
        return {
            "paused": False,
            "stage": "observe",
            "plan": {"actions": [{"action": "like"}, {"action": "follow"}]},
        }

    import src.warmup.service as warmup_service

    monkeypatch.setattr(warmup_service, "today", fake_today)

    db = FakeSession([make_account()])
    result = await tick_module.run_tick(db, dry_config, redis=None, sleep=None)

    assert result.accounts[0].warmup == {
        "stage": "observe",
        "planned_today": 2,
        "would_perform": ["like", "follow"],
    }


@pytest.mark.asyncio
async def test_dry_run_reports_a_paused_account_as_paused(dry_config, monkeypatch):
    async def fake_today(db, account, **kwargs):
        return {"paused": True}

    import src.warmup.service as warmup_service

    monkeypatch.setattr(warmup_service, "today", fake_today)

    db = FakeSession([make_account()])
    result = await tick_module.run_tick(db, dry_config, redis=None, sleep=None)

    assert result.accounts[0].warmup == {"would_perform": [], "paused": True}


# ---------------------------------------------------------------------------
# Reporting — so silence can be diagnosed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_sweep_records_a_heartbeat(live_config, monkeypatch):
    """
    The warm-up programme has deliberately quiet days, so "nothing happened" is
    normal and can never be the alarm. Aliveness is asserted separately from
    output, which is what makes a dead scheduler distinguishable from a quiet one.
    """

    async def noop(db, account, **kwargs):
        return {}

    _patch_engine(monkeypatch, noop, noop, noop)

    redis = FakeRedis()
    account = make_account()

    from src.scheduler import heartbeat
    from src.scheduler.runner import run_once

    def session_factory():
        return FakeSession([account])

    await run_once(live_config, redis=redis, session_factory=session_factory, sleep=None)

    recorded = await heartbeat.last_tick(redis)
    assert recorded is not None
    assert recorded["accounts"] == 1
    assert recorded["mode"] == "live"
    assert "at" in recorded, "the heartbeat must carry a timestamp"


@pytest.mark.asyncio
async def test_the_heartbeat_survives_having_no_redis(dry_config, monkeypatch):
    """
    Redis cannot be the only record of a sweep.

    The one mode guaranteed not to have Redis is the dry run — which is the mode
    most likely to be watched, since it is how you inspect the scheduler before
    letting it act. A heartbeat that is missing exactly when somebody looks for it
    is not a heartbeat.
    """

    async def noop(db, account, **kwargs):
        return {}

    _patch_engine(monkeypatch, noop, noop, noop)

    from src.scheduler import heartbeat
    from src.scheduler.runner import run_once

    monkeypatch.setattr(heartbeat, "_last_tick_in_process", None)

    await run_once(
        dry_config,
        redis=None,
        session_factory=lambda: FakeSession([make_account()]),
        sleep=None,
    )

    recorded = await heartbeat.last_tick(None)
    assert recorded is not None, "a dry run must still leave evidence it ran"
    assert recorded["mode"] == "dry-run"


def test_scheduler_logs_are_not_dropped_under_uvicorn(monkeypatch):
    """
    Uvicorn gives handlers only to its own loggers, so the root has none and
    Python's lastResort fallback emits WARNING and above. Every tick summary is
    INFO, which meant the in-process scheduler logged nothing at all.
    """
    import logging as logging_module

    from src.scheduler.runner import ensure_logging_is_visible

    scheduler_logger = logging_module.getLogger("src.scheduler")
    monkeypatch.setattr(logging_module.getLogger(), "handlers", [])
    monkeypatch.setattr(scheduler_logger, "handlers", [])

    ensure_logging_is_visible()

    assert scheduler_logger.handlers, "INFO output would go nowhere"
    assert scheduler_logger.level <= logging_module.INFO


def test_existing_logging_configuration_is_left_alone(monkeypatch):
    """A second handler would duplicate every line in the worker, which does
    configure logging for itself."""
    import logging as logging_module

    from src.scheduler.runner import ensure_logging_is_visible

    scheduler_logger = logging_module.getLogger("src.scheduler")
    monkeypatch.setattr(scheduler_logger, "handlers", [])
    monkeypatch.setattr(
        logging_module.getLogger(), "handlers", [logging_module.StreamHandler()]
    )

    ensure_logging_is_visible()

    assert scheduler_logger.handlers == []


@pytest.mark.asyncio
async def test_losing_the_lease_race_is_reported_not_raised(live_config):
    """
    Another ticker holding the lease is success, not failure: the work is being
    done. It still has to be visible, or a permanently stuck lease would look
    like a healthy idle scheduler.
    """
    from src.scheduler.runner import run_once

    redis = FakeRedis()
    await redis.set("scheduler:lease", "somebody-else")

    summary = await run_once(
        live_config,
        redis=redis,
        session_factory=lambda: FakeSession([make_account()]),
        sleep=None,
    )

    assert summary["lease_denied"] is True
    assert summary["holder"] == "somebody-else"


@pytest.mark.asyncio
async def test_the_loop_survives_a_failing_tick(live_config, monkeypatch):
    """
    A loop that exits on the first exception turns a transient blip into an
    outage lasting until somebody notices. The engine is idempotent, so the
    correct response to almost any failure is to try again next interval.
    """
    from src.scheduler import runner as runner_module

    attempts: list = []

    async def exploding_tick(*args, **kwargs):
        attempts.append(1)
        raise RuntimeError("database went away")

    monkeypatch.setattr(runner_module, "run_once", exploding_tick)
    monkeypatch.setattr(runner_module.asyncio, "sleep", _instant_sleep)

    await runner_module.run_forever(live_config, redis=FakeRedis(), max_ticks=3)

    assert len(attempts) == 3, "the loop should have kept going"


@pytest.mark.asyncio
async def test_run_forever_refuses_when_not_startable():
    from src.scheduler.runner import run_forever

    with pytest.raises(SchedulerDisabled):
        await run_forever(SchedulerConfig(enabled=False), redis=FakeRedis())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _instant_sleep(_seconds):
    return None


def _patch_engine(monkeypatch, sync_fn, warmup_fn, run_due_fn):
    """Swap the three engine entry points for stubs."""
    import src.outreach.execute as executor
    import src.outreach.sync as sync_module
    import src.warmup.runner as warmup_runner

    monkeypatch.setattr(sync_module, "sync_account", sync_fn)
    monkeypatch.setattr(warmup_runner, "run_today", warmup_fn)
    monkeypatch.setattr(executor, "run_due", run_due_fn)
