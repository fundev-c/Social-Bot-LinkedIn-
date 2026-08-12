"""
Warm-up runner tests: does the programme actually *do* anything, and does it
stay inside its own rules while doing it.

Picking the day a test runs on
------------------------------
These tests used to run against the real clock and a random account id, and
failed about half the time. Both causes were real behaviour, not bugs:

* The daily plan is seeded by ``(account_id, day)``, and volume bands carry a
  quiet-day probability — ``observe`` plans likes with ``probability=0.8``, so
  one day in five is *deliberately* empty. A test asserting "it liked something"
  on a random seed is asserting a coin flip.
* The active window is 08:00-19:00, so an action scattered into the last hour
  is not yet due at the 18:00 the tests used.

Neither is fixed by pinning a lucky seed — the next change to a volume band
would silently un-pin it. Instead ``day_that_plans`` searches forward from a
fixed date for a day on which this account really is scheduled to perform the
action under test, and ``end_of_day`` puts the clock past every scheduled time
so nothing is merely early. Deterministic per account, and self-correcting if
the bands are ever retuned.

A test that wants the opposite — "nothing happens yet" — still uses a day with
actions planned and simply asks *before* the window opens, so it proves pacing
rather than passing because the plan happened to be empty.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pytest

from src.infrastructure.transports.base import TransportChallenge, TransportResult
from src.outreach.models import OutreachSuggestion, SuggestionAction, SuggestionStatus
from src.warmup import planner, program, runner
from src.warmup import service as warmup_service
from src.warmup.models import AccountActivity, ActivityStatus

# Any fixed weekday works; weekends only reduce volume, they don't stop it.
SEARCH_FROM = date(2026, 6, 10)
SEARCH_DAYS = 120


class FeedTransport:
    """A transport that serves a small ICP feed and records engagement."""

    name = "feed"

    def __init__(self, posts=3, fail=False, challenge=False):
        self.calls = []
        self.posts = posts
        self.fail = fail
        self.challenge = challenge

    async def fetch_activity(self, account, member_urn):
        return TransportResult(
            success=True,
            action="fetch_activity",
            via=self.name,
            detail={
                "posts": [
                    {"urn": f"urn:li:activity:{member_urn}-{i}", "text": f"post {i}"}
                    for i in range(self.posts)
                ]
            },
        )

    async def _act(self, action, subject):
        self.calls.append((action, subject))
        if self.challenge:
            raise TransportChallenge("checkpoint")
        return TransportResult(
            success=not self.fail, action=action, via=self.name,
            error="nope" if self.fail else None,
        )

    async def like(self, account, activity_urn):
        return await self._act("like", activity_urn)

    async def follow(self, account, member_urn):
        return await self._act("follow", member_urn)

    async def comment(self, account, activity_urn, text):
        return await self._act("comment", activity_urn)


async def _targets(db, org, account, icp, count=4):
    from src.targeting.schemas import TargetImportItem
    from src.targeting.service import import_targets

    organization, _ = org
    created, _ = await import_targets(
        db,
        org_id=str(organization.id),
        account_id=str(account.id),
        items=[
            TargetImportItem(
                profile_url=f"https://www.linkedin.com/in/person-{i}",
                full_name=f"Person{i} Example",
                title="Head of Growth",
                company=f"Company{i}",
                industry="SaaS",
                headline="Head of Growth | B2B activation",
            )
            for i in range(count)
        ],
        icp=icp,
    )
    return created


async def _stage(db, account, key, *, now, days_ago=1):
    """Put an account in a stage that started ``days_ago`` before ``now``."""
    planner.set_stage(account, key, now=now - timedelta(days=days_ago))
    await db.commit()
    await db.refresh(account)


def day_that_plans(account, stage_key: str, action: str) -> date:
    """
    The first day from SEARCH_FROM on which this account is scheduled to do
    ``action`` during ``stage_key``.

    Uses the same planner the runner will, so what it finds is what the runner
    will see. Searching rather than hard-coding a date means a retuned volume
    band moves the test to a still-valid day instead of breaking it.
    """
    for offset in range(SEARCH_DAYS):
        day = SEARCH_FROM + timedelta(days=offset)
        if planner.plan_day(account, day=day, stage_key=stage_key).for_action(action):
            return day

    raise AssertionError(
        f"no day in {SEARCH_DAYS} plans a {action!r} for account {account.id} during "
        f"{stage_key!r}. Either the stage no longer permits that action, or its "
        f"volume band now allows a zero draw on every day searched."
    )


def end_of_day(day: date) -> datetime:
    """
    Late enough that every action planned for ``day`` is due.

    The runner only performs actions whose scheduled time has passed, so a test
    asking at 18:00 silently skips anything the planner put in the 18:00-19:00
    tail. Asking at the end of the day removes that from the result entirely.
    """
    return datetime.combine(day, time(hour=23, minute=59), tzinfo=timezone.utc)


async def _ready(db, org, account, icp, stage_key, action, *, count=4):
    """Import targets, place the account in a stage, and return a due `now`."""
    await _targets(db, org, account, icp, count=count)
    day = day_that_plans(account, stage_key, action)
    now = end_of_day(day)
    await _stage(db, account, stage_key, now=now)
    return now


# ----------------------------------------------------------------------
# It performs work
# ----------------------------------------------------------------------


async def test_runner_likes_icp_posts(db, org, account, icp, rate_limiter):
    """The core of warm-up: real engagement with the right people's content."""
    now = await _ready(db, org, account, icp, "observe", program.LIKE)

    transport = FeedTransport()
    result = await runner.run_today(
        db,
        account,
        transport=transport,
        rate_limiter=rate_limiter,
        live=object(),
        now=now,
    )

    likes = [c for c in transport.calls if c[0] == "like"]
    assert likes, result
    assert all("activity" in c[1] for c in likes)
    assert result["performed"]


async def test_performed_activity_is_recorded_for_graduation(
    db, org, account, icp, rate_limiter
):
    now = await _ready(db, org, account, icp, "observe", program.LIKE)

    await runner.run_today(
        db, account, transport=FeedTransport(), rate_limiter=rate_limiter,
        live=object(), now=now,
    )

    totals = await warmup_service.totals_for(db, account)
    assert totals.get("like", 0) > 0


async def test_the_same_post_is_never_engaged_with_twice(
    db, org, account, icp, rate_limiter
):
    now = await _ready(db, org, account, icp, "observe", program.LIKE, count=1)

    transport = FeedTransport(posts=2)
    await runner.run_today(
        db, account, transport=transport, rate_limiter=rate_limiter,
        live=object(), now=now,
    )
    first = {c[1] for c in transport.calls if c[0] == "like"}

    transport2 = FeedTransport(posts=2)
    await runner.run_today(
        db, account, transport=transport2, rate_limiter=rate_limiter,
        live=object(), now=now,
    )
    second = {c[1] for c in transport2.calls if c[0] == "like"}

    # Without this the test would also pass on a day that liked nothing at all.
    assert first, "nothing was liked, so there is no repeat to detect"
    assert not (first & second), "re-liked a post it had already liked"


# ----------------------------------------------------------------------
# It stays inside the rules
# ----------------------------------------------------------------------


async def test_runner_never_performs_a_locked_action(
    db, org, account, icp, rate_limiter
):
    """An observing account must not comment, follow, connect or message."""
    now = await _ready(db, org, account, icp, "observe", program.LIKE)

    transport = FeedTransport()
    await runner.run_today(
        db, account, transport=transport, rate_limiter=rate_limiter,
        live=object(), now=now,
    )

    performed = {c[0] for c in transport.calls}
    assert performed <= {"like"}, performed


async def test_runner_respects_the_pause_switch(db, org, account, icp, rate_limiter):
    now = await _ready(db, org, account, icp, "observe", program.LIKE)
    planner.set_paused(account, True, "manual")
    await db.commit()

    transport = FeedTransport()
    result = await runner.run_today(
        db, account, transport=transport, rate_limiter=rate_limiter,
        live=object(), now=now,
    )

    # The day had likes planned and they were due; only the pause stopped them.
    assert result["performed"] == []
    assert not [c for c in transport.calls if c[0] in ("like", "follow")]


async def test_runner_refuses_to_act_without_a_rate_limiter(
    db, org, account, icp, monkeypatch
):
    """No limiter means caps can't be proven, so nothing happens."""
    monkeypatch.delenv("ALLOW_UNCAPPED_SENDING", raising=False)
    now = await _ready(db, org, account, icp, "observe", program.LIKE)

    transport = FeedTransport()
    await runner.run_today(
        db, account, transport=transport, rate_limiter=None, live=object(),
        now=now,
    )

    assert not [c for c in transport.calls if c[0] == "like"]


async def test_only_actions_that_are_due_are_performed(
    db, org, account, icp, rate_limiter
):
    """
    Calling the runner early must not pull the whole day forward.

    The day is chosen to have likes planned, so this proves pacing rather than
    passing because there was nothing to do.
    """
    await _targets(db, org, account, icp)
    day = day_that_plans(account, "react", program.LIKE)
    # 06:00 is before the 08:00 activity window, so nothing is due yet.
    early = datetime.combine(day, time(hour=6), tzinfo=timezone.utc)
    await _stage(db, account, "react", now=early)

    transport = FeedTransport()
    result = await runner.run_today(
        db, account, transport=transport, rate_limiter=rate_limiter, live=object(),
        now=early,
    )

    assert result["performed"] == []
    assert "paced" in result["message"] or result["skipped"]


async def test_a_challenge_during_warmup_pauses_the_account(
    db, org, account, icp, rate_limiter
):
    now = await _ready(db, org, account, icp, "observe", program.LIKE)

    await runner.run_today(
        db, account, transport=FeedTransport(challenge=True),
        rate_limiter=rate_limiter, live=object(),
        now=now,
    )

    assert account.status == "rate_limited"


async def test_failures_are_recorded_rather_than_silently_dropped(
    db, org, account, icp, rate_limiter
):
    from sqlalchemy import select

    now = await _ready(db, org, account, icp, "observe", program.LIKE)

    await runner.run_today(
        db, account, transport=FeedTransport(fail=True), rate_limiter=rate_limiter,
        live=object(), now=now,
    )

    rows = list(
        (
            await db.execute(
                select(AccountActivity).where(
                    AccountActivity.account_id == account.id,
                    AccountActivity.status == ActivityStatus.FAILED,
                )
            )
        )
        .scalars()
        .all()
    )
    assert rows
    # A failed action must not count toward graduation.
    totals = await warmup_service.totals_for(db, account)
    assert totals.get("like", 0) == 0


# ----------------------------------------------------------------------
# Comments go through approval
# ----------------------------------------------------------------------


async def test_comments_are_queued_for_approval_not_posted(
    db, org, account, icp, rate_limiter
):
    """Comments are published under the user's name, so a human sees them first."""
    from sqlalchemy import select

    now = await _ready(db, org, account, icp, "converse", program.COMMENT)

    transport = FeedTransport()
    result = await runner.run_today(
        db, account, transport=transport, rate_limiter=rate_limiter, live=object(),
        now=now,
    )

    # Nothing was actually commented on LinkedIn.
    assert not [c for c in transport.calls if c[0] == "comment"]

    queued = list(
        (
            await db.execute(
                select(OutreachSuggestion).where(
                    OutreachSuggestion.account_id == account.id,
                    OutreachSuggestion.action == SuggestionAction.COMMENT,
                )
            )
        )
        .scalars()
        .all()
    )
    assert queued, result
    assert queued[0].status in (SuggestionStatus.PENDING, SuggestionStatus.BLOCKED)
    assert queued[0].step == "warmup_comment"
    assert queued[0].subject_urn


async def test_queued_comment_references_the_post_it_replies_to(
    db, org, account, icp, rate_limiter
):
    from sqlalchemy import select

    now = await _ready(db, org, account, icp, "converse", program.COMMENT)

    await runner.run_today(
        db, account, transport=FeedTransport(), rate_limiter=rate_limiter,
        live=object(), now=now,
    )

    queued = (
        await db.execute(
            select(OutreachSuggestion).where(
                OutreachSuggestion.action == SuggestionAction.COMMENT
            )
        )
    ).scalars().first()

    assert queued is not None
    assert queued.draft_text
    assert queued.rationale and "post" in queued.rationale.lower()
