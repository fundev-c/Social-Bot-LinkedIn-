"""Give campaigns an owning organization.

Campaigns predate tenancy in this codebase and had no owner column, which is why
their routes could not be org-scoped: there was nothing to scope them by. This
adds ``org_id`` (required) and ``created_by_user_id`` (optional attribution).

Backfilling rows that never had an owner
----------------------------------------
The column is NOT NULL, so existing rows need a value, and the correct one was
never recorded. The rule here:

* If any organization exists, orphans are assigned to the **oldest** one. In
  every deployment so far that is the only organization, so this is exact rather
  than a guess.
* If **no** organization exists, the migration **aborts**. There is no correct
  owner to pick and this migration will not guess by destroying rows. The deploy
  fails with the row count and the two ways forward, and the operator chooses.

This migration never deletes data. An aborted deploy is a conversation; a wrong
delete is unrecoverable.

**The check runs before any DDL, and that ordering is load-bearing.** SQLite has
no transactional DDL (Alembic says as much in its log), so columns added before
an abort survive the rollback and the re-run then fails on ``duplicate column
name`` instead of succeeding — turning the documented recovery step into a dead
end. Checking first makes the abort a genuine no-op on every backend. Postgres
would have rolled it back cleanly; local development runs on SQLite, which is
where this would have been found the hard way.

At revision 0001 the ``org_id`` column does not exist yet, so every campaign
present is by definition unowned — the pre-check counts rows in ``campaigns``,
which is exactly the orphan count.

Row counts are printed on the adoption path too, so the deploy log records
exactly what happened rather than leaving you to infer it.

Revision ID: 0002
Revises: 0001
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()

    # 1. Decide whether this can succeed *before* changing the schema, so an
    #    abort leaves the database untouched on backends without transactional
    #    DDL. See the module docstring — this ordering is not cosmetic.
    #    org_id does not exist yet, so every campaign here is unowned.
    orphans = conn.execute(sa.text("SELECT COUNT(*) FROM campaigns")).scalar_one()
    owner = None

    if orphans:
        owner = conn.execute(
            sa.text("SELECT id FROM organizations ORDER BY created_at, id LIMIT 1")
        ).scalar_one_or_none()

        if owner is None:
            raise RuntimeError(
                f"[0002] cannot migrate: {orphans} campaign(s) have no owning "
                "organization and no organization exists to adopt them.\n"
                "\n"
                "campaigns.org_id is NOT NULL, and this migration will not guess an "
                "owner by deleting rows. Nothing has been changed. Choose one:\n"
                "\n"
                "  1. Create the organization that should own them, then re-deploy. "
                "The oldest organization will adopt them automatically.\n"
                "  2. If they are pre-tenancy test data nobody needs, remove them "
                "deliberately and re-deploy:\n"
                "       DELETE FROM campaigns;\n"
                "     Unqualified on purpose: org_id does not exist yet at this "
                "revision, so every campaign in the table is one of these rows.\n"
                "\n"
                "Inspect them first: SELECT id, name, created_at FROM campaigns;"
            )

    # 2. Safe to proceed. Add both columns nullable so existing rows survive the
    #    DDL, then give them the owner found above.
    op.add_column("campaigns", sa.Column("org_id", sa.Uuid(), nullable=True))
    op.add_column("campaigns", sa.Column("created_by_user_id", sa.Uuid(), nullable=True))

    if orphans:
        conn.execute(
            sa.text("UPDATE campaigns SET org_id = :owner WHERE org_id IS NULL"),
            {"owner": owner},
        )
        print(f"[0002] adopted {orphans} pre-tenancy campaign(s) into organization {owner}")

    # 3. Now the column can be required, and the constraints can go on.
    #    Batch mode is required on SQLite (no in-place ALTER) and is a
    #    passthrough on Postgres.
    with op.batch_alter_table("campaigns", schema=None) as batch_op:
        batch_op.alter_column("org_id", existing_type=sa.Uuid(), nullable=False)
        batch_op.create_foreign_key(
            "fk_campaigns_org_id_organizations",
            "organizations",
            ["org_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_foreign_key(
            "fk_campaigns_created_by_user_id_users",
            "users",
            ["created_by_user_id"],
            ["id"],
            ondelete="SET NULL",
        )

    op.create_index("ix_campaigns_org_id", "campaigns", ["org_id"], unique=False)
    op.create_index(
        "ix_campaigns_created_by_user_id", "campaigns", ["created_by_user_id"], unique=False
    )


def downgrade() -> None:
    """
    Reversible in schema only.

    Dropping org_id discards which organization each campaign belonged to; the
    rows survive but re-running 0002 afterwards will re-adopt them all into the
    oldest org, which is wrong for anyone who had more than one. Take a backup
    before downgrading a database with real campaigns in it.
    """
    op.drop_index("ix_campaigns_created_by_user_id", table_name="campaigns")
    op.drop_index("ix_campaigns_org_id", table_name="campaigns")

    with op.batch_alter_table("campaigns", schema=None) as batch_op:
        batch_op.drop_constraint("fk_campaigns_created_by_user_id_users", type_="foreignkey")
        batch_op.drop_constraint("fk_campaigns_org_id_organizations", type_="foreignkey")
        batch_op.drop_column("created_by_user_id")
        batch_op.drop_column("org_id")
