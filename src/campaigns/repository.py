"""
Campaign repository for database operations.

Provides async CRUD operations for campaigns and related entities.

Tenancy
-------
``CampaignRepository`` and ``IdempotencyRepository`` are constructed with an
``org_id`` and every query they issue is filtered by it. There is deliberately
no "unscoped" mode and no ``org_id=None`` sentinel: a repository you can
accidentally build without a tenant is the bug this scoping exists to prevent.

A campaign belonging to another org therefore reads as *absent* rather than
*forbidden* — callers raise 404, which avoids confirming that someone else's
campaign id exists.
"""

import uuid
from datetime import datetime, timedelta
from typing import Optional, List, Tuple
from sqlalchemy import select, update, delete, func, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
import structlog

from .models import Campaign, CampaignTask, IdempotencyKey, CampaignStatusEnum
from .schemas import CampaignCreate, CampaignUpdate, CampaignProgress

logger = structlog.get_logger()


class CampaignRepository:
    """Repository for Campaign database operations, scoped to one organization."""

    def __init__(self, session: AsyncSession, org_id: uuid.UUID):
        self.session = session
        self.org_id = org_id

    def _scoped(self, include_deleted: bool = False):
        """Base SELECT restricted to this org. Every read goes through here."""
        query = select(Campaign).where(Campaign.org_id == self.org_id)
        if not include_deleted:
            query = query.where(Campaign.deleted_at.is_(None))
        return query

    async def create(
        self,
        data: CampaignCreate,
        created_by_user_id: Optional[uuid.UUID] = None,
    ) -> Campaign:
        """Create a new campaign owned by this repository's org."""
        campaign = Campaign(
            org_id=self.org_id,
            created_by_user_id=created_by_user_id,
            name=data.name,
            description=data.description,
            target_urls=data.target_urls,
            account_ids=[str(aid) for aid in data.account_ids],
            actions=data.actions.model_dump(),
            priority=data.priority,
            scheduled_start_at=data.scheduled_start_at,
            status=CampaignStatusEnum.DRAFT.value,
            total_tasks=len(data.target_urls),
        )
        self.session.add(campaign)
        await self.session.commit()
        await self.session.refresh(campaign)
        logger.info(
            "campaign_created",
            campaign_id=str(campaign.id),
            org_id=str(self.org_id),
            name=campaign.name,
        )
        return campaign

    async def get_by_id(
        self,
        campaign_id: uuid.UUID,
        include_deleted: bool = False
    ) -> Optional[Campaign]:
        """Get campaign by ID. Another org's campaign resolves to None."""
        query = self._scoped(include_deleted).where(Campaign.id == campaign_id)
        result = await self.session.execute(query)
        return result.scalar_one_or_none()

    async def get_with_tasks(self, campaign_id: uuid.UUID) -> Optional[Campaign]:
        """Get campaign with its tasks loaded."""
        query = (
            self._scoped()
            .options(selectinload(Campaign.tasks))
            .where(Campaign.id == campaign_id)
        )
        result = await self.session.execute(query)
        return result.scalar_one_or_none()

    async def list_campaigns(
        self,
        page: int = 1,
        page_size: int = 20,
        status: Optional[str] = None
    ) -> Tuple[List[Campaign], int]:
        """List this org's campaigns with pagination."""
        # Base query
        query = self._scoped()

        # Filter by status if provided
        if status:
            query = query.where(Campaign.status == status)

        # Get total count
        count_query = select(func.count()).select_from(query.subquery())
        total_result = await self.session.execute(count_query)
        total = total_result.scalar() or 0

        # Apply pagination and ordering
        query = (
            query
            .order_by(Campaign.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )

        result = await self.session.execute(query)
        campaigns = list(result.scalars().all())

        return campaigns, total

    async def update(
        self,
        campaign_id: uuid.UUID,
        data: CampaignUpdate
    ) -> Optional[Campaign]:
        """Update campaign by ID."""
        campaign = await self.get_by_id(campaign_id)
        if not campaign:
            return None

        update_data = data.model_dump(exclude_unset=True)

        # Convert account_ids to strings if present
        if "account_ids" in update_data and update_data["account_ids"]:
            update_data["account_ids"] = [
                str(aid) for aid in update_data["account_ids"]
            ]

        # Convert actions to dict if present
        if "actions" in update_data and update_data["actions"]:
            update_data["actions"] = update_data["actions"].model_dump()

        # Update total_tasks if target_urls changed
        if "target_urls" in update_data:
            update_data["total_tasks"] = len(update_data["target_urls"])

        for field, value in update_data.items():
            setattr(campaign, field, value)

        await self.session.commit()
        await self.session.refresh(campaign)
        logger.info("campaign_updated", campaign_id=str(campaign_id))
        return campaign

    async def soft_delete(self, campaign_id: uuid.UUID) -> bool:
        """Soft delete campaign by ID."""
        campaign = await self.get_by_id(campaign_id)
        if not campaign:
            return False

        campaign.deleted_at = datetime.utcnow()
        campaign.status = CampaignStatusEnum.CANCELLED.value
        await self.session.commit()
        logger.info("campaign_deleted", campaign_id=str(campaign_id))
        return True

    async def update_status(
        self,
        campaign_id: uuid.UUID,
        status: CampaignStatusEnum,
        set_started: bool = False,
        set_completed: bool = False
    ) -> Optional[Campaign]:
        """Update campaign status."""
        campaign = await self.get_by_id(campaign_id)
        if not campaign:
            return None

        campaign.status = status.value

        if set_started:
            campaign.started_at = datetime.utcnow()
        if set_completed:
            campaign.completed_at = datetime.utcnow()

        await self.session.commit()
        await self.session.refresh(campaign)
        logger.info(
            "campaign_status_updated",
            campaign_id=str(campaign_id),
            status=status.value
        )
        return campaign

    async def increment_task_count(
        self,
        campaign_id: uuid.UUID,
        completed: int = 0,
        failed: int = 0
    ) -> None:
        """Increment task counters for a campaign."""
        stmt = (
            update(Campaign)
            .where(Campaign.id == campaign_id)
            .where(Campaign.org_id == self.org_id)
            .values(
                completed_tasks=Campaign.completed_tasks + completed,
                failed_tasks=Campaign.failed_tasks + failed
            )
        )
        await self.session.execute(stmt)
        await self.session.commit()

    async def get_progress(self, campaign_id: uuid.UUID) -> CampaignProgress:
        """Get campaign progress."""
        campaign = await self.get_by_id(campaign_id)
        if not campaign:
            return CampaignProgress()

        pending = campaign.total_tasks - campaign.completed_tasks - campaign.failed_tasks
        completion_percent = 0.0
        if campaign.total_tasks > 0:
            completion_percent = round(
                (campaign.completed_tasks / campaign.total_tasks) * 100,
                2
            )

        return CampaignProgress(
            total_tasks=campaign.total_tasks,
            completed_tasks=campaign.completed_tasks,
            failed_tasks=campaign.failed_tasks,
            pending_tasks=max(0, pending),
            completion_percent=completion_percent
        )


class CampaignTaskRepository:
    """
    Repository for CampaignTask database operations.

    Not org-scoped, and deliberately so: tasks are only ever reached through a
    campaign whose ownership the caller has already checked, or by
    ``orchestrator_task_id`` — an opaque id minted by the orchestrator that no
    API client supplies. Adding a tenant filter here would imply user input
    reaches it, which it must not.
    """

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_bulk(
        self,
        campaign_id: uuid.UUID,
        tasks: List[Tuple[str, str]]  # [(orchestrator_task_id, target_url), ...]
    ) -> List[CampaignTask]:
        """Create multiple campaign tasks at once."""
        campaign_tasks = []
        for orchestrator_task_id, target_url in tasks:
            task = CampaignTask(
                campaign_id=campaign_id,
                orchestrator_task_id=orchestrator_task_id,
                target_url=target_url,
                status="pending"
            )
            self.session.add(task)
            campaign_tasks.append(task)

        await self.session.commit()

        # Refresh all tasks
        for task in campaign_tasks:
            await self.session.refresh(task)

        logger.info(
            "campaign_tasks_created",
            campaign_id=str(campaign_id),
            count=len(campaign_tasks)
        )
        return campaign_tasks

    async def get_by_campaign(
        self,
        campaign_id: uuid.UUID,
        status: Optional[str] = None
    ) -> List[CampaignTask]:
        """Get all tasks for a campaign."""
        query = select(CampaignTask).where(
            CampaignTask.campaign_id == campaign_id
        )
        if status:
            query = query.where(CampaignTask.status == status)
        query = query.order_by(CampaignTask.created_at)

        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def update_status(
        self,
        orchestrator_task_id: str,
        status: str,
        result: Optional[dict] = None
    ) -> Optional[CampaignTask]:
        """Update task status by orchestrator task ID."""
        query = select(CampaignTask).where(
            CampaignTask.orchestrator_task_id == orchestrator_task_id
        )
        result_obj = await self.session.execute(query)
        task = result_obj.scalar_one_or_none()

        if not task:
            return None

        task.status = status
        if result:
            task.result = result
        if status in ["completed", "failed"]:
            task.completed_at = datetime.utcnow()

        await self.session.commit()
        await self.session.refresh(task)
        return task


class IdempotencyRepository:
    """
    Repository for idempotency key operations, scoped to one organization.

    Keys are chosen by the client, so two orgs can pick the same one. The stored
    primary key is therefore namespaced as ``{org_id}:{key}``: without that, org
    B replaying org A's key would be handed org A's campaign as the "cached
    response" — the same leak as an unscoped read, arriving by a quieter route.
    """

    def __init__(self, session: AsyncSession, org_id: uuid.UUID):
        self.session = session
        self.org_id = org_id

    def _namespaced(self, key: str) -> str:
        return f"{self.org_id}:{key}"

    async def get(self, key: str) -> Optional[IdempotencyKey]:
        """Get idempotency key if exists and not expired."""
        query = select(IdempotencyKey).where(
            and_(
                IdempotencyKey.key == self._namespaced(key),
                IdempotencyKey.expires_at > datetime.utcnow()
            )
        )
        result = await self.session.execute(query)
        return result.scalar_one_or_none()

    async def create(
        self,
        key: str,
        resource_type: str,
        resource_id: Optional[uuid.UUID] = None,
        response_code: Optional[int] = None,
        response_body: Optional[dict] = None
    ) -> IdempotencyKey:
        """Create or update idempotency key."""
        existing = await self.get(key)
        if existing:
            return existing

        idempotency_key = IdempotencyKey(
            key=self._namespaced(key),
            resource_type=resource_type,
            resource_id=resource_id,
            response_code=response_code,
            response_body=response_body,
            expires_at=datetime.utcnow() + timedelta(hours=24)
        )
        self.session.add(idempotency_key)
        await self.session.commit()
        await self.session.refresh(idempotency_key)
        return idempotency_key

    async def cleanup_expired(self) -> int:
        """Delete expired idempotency keys."""
        stmt = delete(IdempotencyKey).where(
            IdempotencyKey.expires_at < datetime.utcnow()
        )
        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.rowcount
