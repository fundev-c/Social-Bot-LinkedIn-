"""
Migration tests.

Two things can go wrong with Alembic and both are silent until production:

1. **Drift** — someone edits a model and forgets the migration. The schema the
   tests run against (built by ``create_all``) then stops matching the schema a
   real deploy gets (built by migrations), and everything passes right up until
   the column is missing in production. ``test_migrations_match_models`` runs the
   migrations for real and asks Alembic's own autogenerate comparison whether
   anything is left over. Anything but "no changes" fails.

2. **A data step that was never executed** — 0002 backfills campaigns that
   predate tenancy. That branch runs exactly once per deployment, and by then
   it is too late to find out it was wrong, so both of its paths are exercised
   here.

These are sync tests on purpose: Alembic's env.py owns its event loop, and
nesting that inside pytest-asyncio's would deadlock.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def alembic_config(db_path: Path) -> Config:
    """Alembic configured to run against a throwaway SQLite file."""
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path.as_posix()}")
    return config


@pytest.fixture
def db_path(tmp_path) -> Path:
    return tmp_path / "migrations.db"


@pytest.fixture
def sync_engine(db_path):
    """A plain sync engine on the same file, for asserting on the result."""
    engine = sa.create_engine(f"sqlite:///{db_path.as_posix()}")
    yield engine
    engine.dispose()


def test_migrations_match_models(db_path, sync_engine):
    """
    A database built by migrations is identical to one built from the models.

    This is the guard against drift: add a column to a model without a
    migration and this test fails with the missing operation named.
    """
    from src.database.models import Base, import_all_models

    command.upgrade(alembic_config(db_path), "head")
    import_all_models()

    with sync_engine.connect() as conn:
        context = MigrationContext.configure(conn, opts={"compare_type": True})
        diff = compare_metadata(context, Base.metadata)

    assert diff == [], f"models and migrations have drifted apart: {diff}"


def test_downgrade_and_upgrade_round_trip(db_path, sync_engine):
    """head -> base -> head leaves the schema where it started."""
    config = alembic_config(db_path)
    command.upgrade(config, "head")
    command.downgrade(config, "base")

    inspector = sa.inspect(sync_engine)
    remaining = set(inspector.get_table_names()) - {"alembic_version"}
    assert remaining == set(), f"downgrade left tables behind: {remaining}"

    command.upgrade(config, "head")
    assert "campaigns" in sa.inspect(sync_engine).get_table_names()


def _seed_pre_tenancy_campaign(engine, *, with_org: bool) -> uuid.UUID | None:
    """Insert a campaign at the 0001 schema, as AUTO_CREATE_TABLES would have."""
    org_id = uuid.uuid4() if with_org else None
    with engine.begin() as conn:
        if with_org:
            conn.execute(
                sa.text(
                    "INSERT INTO organizations (id, name, plan, settings, created_at, updated_at)"
                    " VALUES (:id, 'Acme', 'free', '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {"id": str(org_id)},
            )
        conn.execute(
            sa.text(
                "INSERT INTO campaigns"
                " (id, name, status, target_urls, account_ids, actions, priority,"
                "  total_tasks, completed_tasks, failed_tasks, created_at, updated_at)"
                " VALUES (:id, 'Legacy campaign', 'draft', '[]', '[]', '{}', 1,"
                "         0, 0, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"id": str(uuid.uuid4())},
        )
    return org_id


def test_backfill_adopts_orphans_into_the_existing_org(db_path, sync_engine):
    """An unowned campaign is adopted, not destroyed, when an org exists."""
    config = alembic_config(db_path)
    command.upgrade(config, "0001")
    org_id = _seed_pre_tenancy_campaign(sync_engine, with_org=True)

    command.upgrade(config, "0002")

    with sync_engine.connect() as conn:
        rows = conn.execute(sa.text("SELECT name, org_id FROM campaigns")).fetchall()

    assert len(rows) == 1, "the campaign should have survived the migration"
    assert rows[0].name == "Legacy campaign"
    assert str(rows[0].org_id) == str(org_id)


def test_backfill_aborts_when_no_org_can_own_the_orphans(db_path, sync_engine):
    """
    With no organization at all, the migration refuses to run and keeps the row.

    0002 is non-destructive by decision: there is no correct owner to pick, so it
    fails the deploy and lets a human choose rather than deleting data. Asserted
    here so a later edit cannot quietly turn this back into a DELETE.
    """
    config = alembic_config(db_path)
    command.upgrade(config, "0001")
    _seed_pre_tenancy_campaign(sync_engine, with_org=False)

    with pytest.raises(RuntimeError, match="no organization exists to adopt them"):
        command.upgrade(config, "0002")

    # The abort must leave the campaign intact, not half-migrated.
    with sync_engine.connect() as conn:
        count = conn.execute(sa.text("SELECT COUNT(*) FROM campaigns")).scalar_one()
        version = conn.execute(
            sa.text("SELECT version_num FROM alembic_version")
        ).scalar_one()

    assert count == 1, "the campaign must survive an aborted migration"
    assert version == "0001", "a failed 0002 must not be recorded as applied"

    # And it must leave the *schema* untouched, not just the rows. SQLite has no
    # transactional DDL, so a column added before the abort would survive the
    # rollback and make the re-run below fail on "duplicate column name" —
    # exactly the dead end this ordering exists to prevent.
    columns = {c["name"] for c in sa.inspect(sync_engine).get_columns("campaigns")}
    assert "org_id" not in columns, "the abort must happen before any DDL is applied"
    assert "created_by_user_id" not in columns


def test_upgrade_succeeds_once_an_org_exists_to_adopt_them(db_path, sync_engine):
    """
    The documented recovery path works: create an org, re-run, orphans adopted.

    This is the instruction the abort message gives the operator, so it is worth
    proving rather than assuming.
    """
    config = alembic_config(db_path)
    command.upgrade(config, "0001")
    _seed_pre_tenancy_campaign(sync_engine, with_org=False)

    with pytest.raises(RuntimeError):
        command.upgrade(config, "0002")

    # Recovery: the operator creates the owning organization and re-deploys.
    org_id = uuid.uuid4()
    with sync_engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO organizations (id, name, plan, settings, created_at, updated_at)"
                " VALUES (:id, 'Acme', 'free', '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"id": str(org_id)},
        )

    command.upgrade(config, "0002")

    with sync_engine.connect() as conn:
        rows = conn.execute(sa.text("SELECT name, org_id FROM campaigns")).fetchall()

    assert len(rows) == 1
    assert str(rows[0].org_id) == str(org_id)
