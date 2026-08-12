"""Baseline: the schema as AUTO_CREATE_TABLES has been building it.

This revision reproduces exactly what ``Base.metadata.create_all`` produced
before Alembic existed — deliberately including the campaigns table *without*
``org_id``, which 0002 then adds.

That split is what lets an already-deployed database join the migration history
without being rebuilt:

    alembic stamp 0001     # "this database already looks like the baseline"
    alembic upgrade head   # apply everything since

A fresh database just runs both in order and lands in the same place. Squashing
the two into one revision would make the stamp-and-upgrade path impossible.

Server defaults use ``sa.func.now()`` rather than a literal, so each dialect
renders its own (``now()`` on Postgres, ``CURRENT_TIMESTAMP`` on SQLite).

Revision ID: 0001
Revises:
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "idempotency_keys",
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("resource_type", sa.String(length=50), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=True),
        sa.Column("response_code", sa.Integer(), nullable=True),
        sa.Column("response_body", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )

    op.create_table(
        "organizations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("clerk_org_id", sa.String(length=255), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("plan", sa.String(length=50), nullable=False),
        sa.Column("whatsapp_admin_number", sa.String(length=64), nullable=True),
        sa.Column("settings", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_organizations_clerk_org_id", "organizations", ["clerk_org_id"], unique=True)

    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("clerk_user_id", sa.String(length=255), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column("role", sa.String(length=50), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_users_clerk_user_id", "users", ["clerk_user_id"], unique=True)
    op.create_index("ix_users_org_id", "users", ["org_id"], unique=False)

    # NOTE: no org_id / created_by_user_id here — see 0002.
    op.create_table(
        "campaigns",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("target_urls", sa.JSON(), nullable=False),
        sa.Column("account_ids", sa.JSON(), nullable=False),
        sa.Column("actions", sa.JSON(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("scheduled_start_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scheduled_end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("total_tasks", sa.Integer(), nullable=False),
        sa.Column("completed_tasks", sa.Integer(), nullable=False),
        sa.Column("failed_tasks", sa.Integer(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "connected_accounts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("linkedin_member_urn", sa.String(length=255), nullable=True),
        sa.Column("profile_url", sa.String(length=512), nullable=True),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column("headline", sa.Text(), nullable=True),
        sa.Column("auth_blob", sa.Text(), nullable=True),
        sa.Column("device_fingerprint", sa.JSON(), nullable=True),
        sa.Column("proxy", sa.JSON(), nullable=True),
        sa.Column("daily_caps", sa.JSON(), nullable=True),
        sa.Column("mode", sa.String(length=64), nullable=True),
        sa.Column("active_icp_id", sa.Uuid(), nullable=True),
        sa.Column("last_post_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_active_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_connected_accounts_org_id", "connected_accounts", ["org_id"], unique=False)
    op.create_index("ix_connected_accounts_user_id", "connected_accounts", ["user_id"], unique=False)

    op.create_table(
        "account_activity",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("stage", sa.String(length=32), nullable=True),
        sa.Column("subject_urn", sa.String(length=255), nullable=True),
        sa.Column("target_id", sa.Uuid(), nullable=True),
        sa.Column("variant", sa.String(length=64), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["connected_accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_account_activity_created_at", "account_activity", ["created_at"], unique=False)
    op.create_index("ix_account_activity_org_id", "account_activity", ["org_id"], unique=False)
    op.create_index("ix_account_activity_target_id", "account_activity", ["target_id"], unique=False)
    op.create_index("ix_activity_account_action", "account_activity", ["account_id", "action"], unique=False)
    op.create_index("ix_activity_account_time", "account_activity", ["account_id", "created_at"], unique=False)
    op.create_index("ix_activity_variant", "account_activity", ["account_id", "variant"], unique=False)

    op.create_table(
        "campaign_tasks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column("orchestrator_task_id", sa.String(length=255), nullable=False),
        sa.Column("target_url", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "icp_profiles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("titles", sa.JSON(), nullable=True),
        sa.Column("seniorities", sa.JSON(), nullable=True),
        sa.Column("industries", sa.JSON(), nullable=True),
        sa.Column("keywords", sa.JSON(), nullable=True),
        sa.Column("excluded_keywords", sa.JSON(), nullable=True),
        sa.Column("excluded_titles", sa.JSON(), nullable=True),
        sa.Column("locations", sa.JSON(), nullable=True),
        sa.Column("company_sizes", sa.JSON(), nullable=True),
        sa.Column("value_proposition", sa.Text(), nullable=True),
        sa.Column("instructions", sa.Text(), nullable=True),
        sa.Column("relevance_floor", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["connected_accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_icp_profiles_account_id", "icp_profiles", ["account_id"], unique=False)
    op.create_index("ix_icp_profiles_org_id", "icp_profiles", ["org_id"], unique=False)

    op.create_table(
        "outreach_targets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("icp_id", sa.Uuid(), nullable=True),
        sa.Column("member_urn", sa.String(length=255), nullable=False),
        sa.Column("public_id", sa.String(length=255), nullable=True),
        sa.Column("profile_url", sa.String(length=512), nullable=True),
        sa.Column("full_name", sa.String(length=255), nullable=True),
        sa.Column("first_name", sa.String(length=128), nullable=True),
        sa.Column("headline", sa.Text(), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("company", sa.String(length=255), nullable=True),
        sa.Column("industry", sa.String(length=255), nullable=True),
        sa.Column("location", sa.String(length=255), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=True),
        sa.Column("source_ref", sa.String(length=512), nullable=True),
        sa.Column("context", sa.JSON(), nullable=True),
        sa.Column("relevance_score", sa.Integer(), nullable=False),
        sa.Column("relevance_reasons", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("last_touched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("booked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("variant", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["connected_accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_outreach_targets_org_id", "outreach_targets", ["org_id"], unique=False)
    op.create_index("ix_outreach_targets_status", "outreach_targets", ["status"], unique=False)
    op.create_index("ix_target_account_member", "outreach_targets", ["account_id", "member_urn"], unique=True)
    op.create_index("ix_target_account_status", "outreach_targets", ["account_id", "status"], unique=False)

    op.create_table(
        "outreach_suggestions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("draft_text", sa.Text(), nullable=True),
        sa.Column("final_text", sa.Text(), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("relevance_score", sa.Integer(), nullable=False),
        sa.Column("relevance_reasons", sa.JSON(), nullable=True),
        sa.Column("quality_score", sa.Integer(), nullable=True),
        sa.Column("quality_warnings", sa.JSON(), nullable=True),
        sa.Column("subject_urn", sa.String(length=255), nullable=True),
        sa.Column("generated_by", sa.String(length=128), nullable=True),
        sa.Column("step", sa.String(length=32), nullable=True),
        sa.Column("variant", sa.String(length=64), nullable=True),
        sa.Column("depends_on_id", sa.Uuid(), nullable=True),
        sa.Column("not_before", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_by", sa.Uuid(), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["connected_accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_id"], ["outreach_targets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_outreach_suggestions_org_id", "outreach_suggestions", ["org_id"], unique=False)
    op.create_index("ix_outreach_suggestions_status", "outreach_suggestions", ["status"], unique=False)
    op.create_index("ix_suggestion_account_status", "outreach_suggestions", ["account_id", "status"], unique=False)
    op.create_index("ix_suggestion_due", "outreach_suggestions", ["status", "scheduled_for"], unique=False)
    op.create_index("ix_suggestion_target_action", "outreach_suggestions", ["target_id", "action"], unique=False)


def downgrade() -> None:
    op.drop_table("outreach_suggestions")
    op.drop_table("outreach_targets")
    op.drop_table("icp_profiles")
    op.drop_table("campaign_tasks")
    op.drop_table("account_activity")
    op.drop_table("connected_accounts")
    op.drop_table("campaigns")
    op.drop_table("users")
    op.drop_table("organizations")
    op.drop_table("idempotency_keys")
