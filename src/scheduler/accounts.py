"""
Account enumeration for the scheduler.

⚠️ **This is the one reader in the codebase that deliberately crosses tenants.**

Everything else that touches ``connected_accounts`` filters by ``org_id``, and
that is load-bearing: the campaign routes shipped without it and any
unauthenticated caller could read every organization's data. ``list_accounts``
in ``src/accounts/service.py`` takes a required ``org_id`` for that reason, and
there is intentionally no unscoped variant there — a function that can be called
without a tenant is the defect itself.

The scheduler is the legitimate exception. It runs with no request and no user,
on behalf of every organization at once, so it cannot have a tenant to filter by.
Rather than weaken the scoped API with an optional ``org_id``, the unscoped query
lives here, alone, named for what it does and used by exactly one caller.

**If you are reading this because you want cross-org access somewhere else: you
almost certainly do not.** Ask instead whose request it is, and scope it to them.
"""

from __future__ import annotations

from typing import List, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.accounts.models import AccountStatus, ConnectedAccount

#: Only accounts in these states are swept.
#:
#: ``ACTIVE`` is what a successfully verified account becomes. The others are all
#: states where acting would be wrong rather than merely unproductive:
#: ``AUTH_REQUIRED`` needs new cookies (acting produces failures that look like
#: transport bugs), ``SUSPENDED`` and ``ERROR`` need a human, ``RATE_LIMITED``
#: means LinkedIn already pushed back, and ``INACTIVE`` is a connected-but-not-yet
#: verified or a disconnected account.
SCHEDULABLE_STATUSES: Sequence[str] = (AccountStatus.ACTIVE,)


async def due_accounts(db: AsyncSession) -> List[ConnectedAccount]:
    """
    Every account the scheduler should consider this tick, across all orgs.

    Ordered by ``created_at`` so a sweep is stable and reproducible; the runner
    is what varies the order, since ordering is a pacing concern rather than a
    query one.

    Soft-deleted accounts are excluded — ``disconnect_account`` sets
    ``deleted_at`` and leaves the row, so filtering on status alone would keep
    acting as an account the user believes they removed.
    """
    stmt = (
        select(ConnectedAccount)
        .where(
            ConnectedAccount.deleted_at.is_(None),
            ConnectedAccount.status.in_(tuple(SCHEDULABLE_STATUSES)),
        )
        .order_by(ConnectedAccount.created_at.asc(), ConnectedAccount.id.asc())
    )
    return list((await db.execute(stmt)).scalars().all())
