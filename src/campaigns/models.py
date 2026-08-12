"""
SQLAlchemy models for Campaign management.

These models map to the Supabase tables created via SQL.
"""

import uuid
from datetime import datetime
from typing import Optional, List, Any
from sqlalchemy import (
    String, Integer, Text, DateTime, ForeignKey,
    JSON, Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func
import enum

from src.database.models import Base

# Cross-dialect aliases: native UUID/JSONB on Postgres, portable equivalents on
# SQLite (used in tests and lightweight demos). Keeps a single schema everywhere.
UUID = Uuid
JSONB = JSON


class CampaignStatusEnum(str, enum.Enum):
    """Campaign status enum matching PostgreSQL campaign_status type."""
    DRAFT = "draft"
    SCHEDULED = "scheduled"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Campaign(Base):
    """
    Campaign model for LinkedIn automation campaigns.

    A campaign groups multiple LinkedIn URLs to engage with,
    using specified accounts and actions (like/comment).

    Owned by an Organization, like every other tenant-scoped row in the system.
    ``org_id`` is not optional: a campaign with no owner is reachable by every
    tenant, which is how these routes leaked before.
    """
    __tablename__ = "campaigns"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4
    )

    # Tenancy. Every query in CampaignRepository filters on org_id.
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Who created it, for attribution in the admin view. Nullable so removing a
    # team member does not take their campaigns with them.
    created_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)

    # Status using PostgreSQL enum
    status: Mapped[str] = mapped_column(
        String(50),
        default=CampaignStatusEnum.DRAFT.value
    )

    # Campaign configuration (stored as JSONB)
    target_urls: Mapped[List[str]] = mapped_column(JSONB, default=list)
    account_ids: Mapped[List[str]] = mapped_column(JSONB, default=list)
    actions: Mapped[dict] = mapped_column(
        JSONB,
        default=lambda: {"like": True, "comment": False}
    )

    # Priority (1=normal, 2=high, 3=urgent)
    priority: Mapped[int] = mapped_column(Integer, default=1)

    # Scheduling
    scheduled_start_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )
    scheduled_end_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )

    # Execution tracking
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )

    # Progress counters (denormalized for performance)
    total_tasks: Mapped[int] = mapped_column(Integer, default=0)
    completed_tasks: Mapped[int] = mapped_column(Integer, default=0)
    failed_tasks: Mapped[int] = mapped_column(Integer, default=0)

    # Soft delete
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now()
    )

    # Relationships
    tasks: Mapped[List["CampaignTask"]] = relationship(
        back_populates="campaign",
        cascade="all, delete-orphan"
    )

    @property
    def is_active(self) -> bool:
        """Check if campaign is in an active state."""
        return self.status in [
            CampaignStatusEnum.RUNNING.value,
            CampaignStatusEnum.SCHEDULED.value
        ]

    @property
    def completion_percent(self) -> float:
        """Calculate completion percentage."""
        if self.total_tasks == 0:
            return 0.0
        return round(
            (self.completed_tasks / self.total_tasks) * 100,
            2
        )


class CampaignTask(Base):
    """
    Links campaigns to orchestrator tasks.

    Each task represents one URL being processed for one campaign.
    """
    __tablename__ = "campaign_tasks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        nullable=False
    )

    # Link to MessageOrchestrator task
    orchestrator_task_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False
    )

    target_url: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="pending")
    result: Mapped[Optional[dict]] = mapped_column(JSONB)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now()
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )

    # Relationships
    campaign: Mapped["Campaign"] = relationship(back_populates="tasks")


class IdempotencyKey(Base):
    """
    Stores idempotency keys for duplicate request prevention.

    Keys expire after 24 hours to prevent unbounded growth.
    """
    __tablename__ = "idempotency_keys"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    resource_type: Mapped[str] = mapped_column(String(50), nullable=False)
    resource_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    response_code: Mapped[Optional[int]] = mapped_column(Integer)
    response_body: Mapped[Optional[dict]] = mapped_column(JSONB)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now()  # Will be set to +24h by SQL default
    )
