#!/usr/bin/env python
"""
Bring the database up to date, including the first time.

Run before the web process starts:

    python scripts/migrate.py && uvicorn src.api.main:app ...

Why this exists rather than a bare ``alembic upgrade head``
-----------------------------------------------------------
This project provisioned its schema with ``AUTO_CREATE_TABLES`` (SQLAlchemy's
``create_all``) before it had migrations. Those databases have all the tables but
no ``alembic_version`` row, so ``upgrade head`` tries to re-create tables that
already exist and fails — on every boot, forever, until somebody runs a manual
``alembic stamp``. A deploy that requires a remembered manual step is a deploy
that breaks the first time it is done by someone who wasn't told.

So this decides which of three situations it is:

* **Already managed** (``alembic_version`` exists) → upgrade. The normal case.
* **Pre-Alembic database** (no ``alembic_version``, but ``campaigns`` exists) →
  stamp 0001 to record "this already matches the baseline", then upgrade. That
  is what makes 0001 a faithful copy of the create_all schema load-bearing.
* **Empty database** → upgrade from scratch.

It is idempotent: running it twice in a row is a no-op the second time.

Concurrency: Alembic does not take a lock, so two replicas booting at the same
instant could both try to migrate. With a single web replica (the current
deployment) that cannot happen; if you scale out, move this to a release/pre-
deploy step that runs once instead of into the start command.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import sqlalchemy as sa  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

from src.database.session import DATABASE_URL  # noqa: E402

# A table that exists in the 0001 baseline. If it is present without an
# alembic_version table, the schema was built by create_all.
BASELINE_TABLE = "campaigns"
BASELINE_REVISION = "0001"


def _sync_url(url: str) -> str:
    """Inspection is a sync operation; drop the async driver for it."""
    return url.replace("+asyncpg", "").replace("+aiosqlite", "")


def main() -> int:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))

    engine = sa.create_engine(_sync_url(DATABASE_URL))
    try:
        inspector = sa.inspect(engine)
        tables = set(inspector.get_table_names())
    finally:
        engine.dispose()

    if "alembic_version" in tables:
        print("[migrate] database is under Alembic control; upgrading to head")
    elif BASELINE_TABLE in tables:
        print(
            f"[migrate] found a pre-Alembic schema ({BASELINE_TABLE!r} exists with no "
            f"alembic_version); stamping {BASELINE_REVISION} and upgrading from there"
        )
        command.stamp(config, BASELINE_REVISION)
    else:
        print("[migrate] empty database; creating the schema from scratch")

    command.upgrade(config, "head")
    print("[migrate] done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
