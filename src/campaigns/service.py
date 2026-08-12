"""
Campaign Service - Business logic for campaign management.

Integrates with MessageOrchestrator for task execution.
"""

import uuid
from datetime import datetime
from typing import Optional, List, Tuple
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from .models import Campaign, CampaignTask, CampaignStatusEnum
from .repository import (
    CampaignRepository,
    CampaignTaskRepository,
    IdempotencyRepository
)
from .schemas import (
    CampaignCreate,
    CampaignUpdate,
    CampaignResponse,
    CampaignProgress,
    CampaignActions,
    CampaignStatus,
    StartCampaignResponse,
)

logger = structlog.get_logger()


class CampaignServiceError(Exception):
    """Base exception for campaign service errors."""
    pass


class CampaignNotFoundError(CampaignServiceError):
    """Campaign not found."""
    pass


class CampaignStateError(CampaignServiceError):
    """Invalid campaign state for operation."""
    pass


class IdempotencyKeyExistsError(CampaignServiceError):
    """Idempotency key already exists."""
    def __init__(self, resource_id: uuid.UUID, response_body: dict):
        self.resource_id = resource_id
        self.response_body = response_body
        super().__init__("Request already processed")


class CampaignService:
    """
    Campaign orchestration service, scoped to one organization.

    Manages campaign lifecycle and integrates with MessageOrchestrator
    for task execution.

    ``org_id`` is required and has no default. Everything this service can see
    or touch belongs to that org; a campaign id from another tenant raises
    ``CampaignNotFoundError`` exactly as a made-up id does.

    The orchestrator callback path does not have a request context and so does
    not use this class — see :func:`apply_task_result`.
    """

    def __init__(
        self,
        session: AsyncSession,
        org_id: uuid.UUID,
        user_id: Optional[uuid.UUID] = None,
        orchestrator=None,  # MessageOrchestrator instance
        redis_client=None   # Redis client for rate limiting checks
    ):
        self.session = session
        self.org_id = org_id
        self.user_id = user_id
        self.orchestrator = orchestrator
        self.redis = redis_client
        self.campaign_repo = CampaignRepository(session, org_id=org_id)
        self.task_repo = CampaignTaskRepository(session)
        self.idempotency_repo = IdempotencyRepository(session, org_id=org_id)

    async def create_campaign(
        self,
        data: CampaignCreate,
        idempotency_key: Optional[str] = None
    ) -> Campaign:
        """
        Create a new campaign.

        Args:
            data: Campaign creation data
            idempotency_key: Optional idempotency key to prevent duplicates

        Returns:
            Created campaign

        Raises:
            IdempotencyKeyExistsError: If idempotency key already used
        """
        # Check idempotency key if provided
        if idempotency_key:
            existing = await self.idempotency_repo.get(idempotency_key)
            if existing and existing.resource_id:
                logger.info(
                    "idempotency_key_hit",
                    key=idempotency_key,
                    resource_id=str(existing.resource_id)
                )
                raise IdempotencyKeyExistsError(
                    resource_id=existing.resource_id,
                    response_body=existing.response_body or {}
                )

        # Create campaign
        campaign = await self.campaign_repo.create(data, created_by_user_id=self.user_id)

        # Store idempotency key if provided
        if idempotency_key:
            await self.idempotency_repo.create(
                key=idempotency_key,
                resource_type="campaign",
                resource_id=campaign.id,
                response_code=201,
                response_body={"id": str(campaign.id), "name": campaign.name}
            )

        return campaign

    async def get_campaign(self, campaign_id: uuid.UUID) -> Campaign:
        """Get campaign by ID."""
        campaign = await self.campaign_repo.get_by_id(campaign_id)
        if not campaign:
            raise CampaignNotFoundError(f"Campaign {campaign_id} not found")
        return campaign

    async def list_campaigns(
        self,
        page: int = 1,
        page_size: int = 20,
        status: Optional[str] = None
    ) -> Tuple[List[Campaign], int]:
        """List campaigns with pagination."""
        return await self.campaign_repo.list_campaigns(page, page_size, status)

    async def update_campaign(
        self,
        campaign_id: uuid.UUID,
        data: CampaignUpdate
    ) -> Campaign:
        """Update campaign."""
        campaign = await self.campaign_repo.get_by_id(campaign_id)
        if not campaign:
            raise CampaignNotFoundError(f"Campaign {campaign_id} not found")

        # Only allow updates to draft campaigns
        if campaign.status != CampaignStatusEnum.DRAFT.value:
            raise CampaignStateError(
                f"Cannot update campaign in '{campaign.status}' state. "
                "Only draft campaigns can be modified."
            )

        updated = await self.campaign_repo.update(campaign_id, data)
        return updated

    async def delete_campaign(self, campaign_id: uuid.UUID) -> bool:
        """Soft delete campaign."""
        campaign = await self.campaign_repo.get_by_id(campaign_id)
        if not campaign:
            raise CampaignNotFoundError(f"Campaign {campaign_id} not found")

        # Don't allow deletion of running campaigns
        if campaign.status == CampaignStatusEnum.RUNNING.value:
            raise CampaignStateError(
                "Cannot delete running campaign. Pause it first."
            )

        return await self.campaign_repo.soft_delete(campaign_id)

    async def start_campaign(
        self,
        campaign_id: uuid.UUID,
        idempotency_key: Optional[str] = None
    ) -> StartCampaignResponse:
        """
        Start campaign execution.

        Submits tasks to MessageOrchestrator for each target URL.

        Args:
            campaign_id: Campaign ID
            idempotency_key: Optional idempotency key

        Returns:
            StartCampaignResponse with task count

        Raises:
            CampaignNotFoundError: Campaign doesn't exist
            CampaignStateError: Campaign not in valid state to start
        """
        # Check idempotency
        if idempotency_key:
            existing = await self.idempotency_repo.get(idempotency_key)
            if existing:
                logger.info("idempotency_key_hit_start", key=idempotency_key)
                return StartCampaignResponse(
                    campaign_id=existing.resource_id,
                    status=CampaignStatus.RUNNING,
                    tasks_created=existing.response_body.get("tasks_created", 0),
                    message="Campaign already started (idempotent response)"
                )

        campaign = await self.campaign_repo.get_by_id(campaign_id)
        if not campaign:
            raise CampaignNotFoundError(f"Campaign {campaign_id} not found")

        # Validate state
        valid_start_states = [
            CampaignStatusEnum.DRAFT.value,
            CampaignStatusEnum.PAUSED.value
        ]
        if campaign.status not in valid_start_states:
            raise CampaignStateError(
                f"Cannot start campaign in '{campaign.status}' state. "
                f"Must be in {valid_start_states}"
            )

        # Check if orchestrator is available
        if not self.orchestrator:
            logger.warning("orchestrator_not_configured", campaign_id=str(campaign_id))
            # Update status anyway for testing without orchestrator
            await self.campaign_repo.update_status(
                campaign_id,
                CampaignStatusEnum.RUNNING,
                set_started=True
            )
            return StartCampaignResponse(
                campaign_id=campaign_id,
                status=CampaignStatus.RUNNING,
                tasks_created=0,
                message="Campaign started (orchestrator not configured)"
            )

        # Submit tasks to orchestrator
        tasks_data = []
        for url in campaign.target_urls:
            try:
                task_id = await self.orchestrator.submit_linkedin_automation_task(
                    url=url,
                    account_ids=campaign.account_ids,
                    like=campaign.actions.get("like", True),
                    comment=campaign.actions.get("comment", False),
                    priority=campaign.priority,
                    source=f"campaign:{campaign_id}"
                )
                tasks_data.append((task_id, url))
                logger.debug("task_submitted", task_id=task_id, url=url)
            except Exception as e:
                logger.error(
                    "task_submission_failed",
                    url=url,
                    error=str(e)
                )

        # Create campaign task records
        if tasks_data:
            await self.task_repo.create_bulk(campaign_id, tasks_data)

        # Update campaign status
        await self.campaign_repo.update_status(
            campaign_id,
            CampaignStatusEnum.RUNNING,
            set_started=True
        )

        # Store idempotency key
        if idempotency_key:
            await self.idempotency_repo.create(
                key=idempotency_key,
                resource_type="campaign_start",
                resource_id=campaign_id,
                response_code=200,
                response_body={"tasks_created": len(tasks_data)}
            )

        logger.info(
            "campaign_started",
            campaign_id=str(campaign_id),
            tasks_created=len(tasks_data)
        )

        return StartCampaignResponse(
            campaign_id=campaign_id,
            status=CampaignStatus.RUNNING,
            tasks_created=len(tasks_data),
            message=f"Campaign started with {len(tasks_data)} tasks"
        )

    async def pause_campaign(
        self,
        campaign_id: uuid.UUID,
        idempotency_key: Optional[str] = None
    ) -> Campaign:
        """Pause a running campaign."""
        campaign = await self.campaign_repo.get_by_id(campaign_id)
        if not campaign:
            raise CampaignNotFoundError(f"Campaign {campaign_id} not found")

        if campaign.status != CampaignStatusEnum.RUNNING.value:
            raise CampaignStateError(
                f"Cannot pause campaign in '{campaign.status}' state"
            )

        updated = await self.campaign_repo.update_status(
            campaign_id,
            CampaignStatusEnum.PAUSED
        )
        logger.info("campaign_paused", campaign_id=str(campaign_id))
        return updated

    async def get_campaign_progress(
        self,
        campaign_id: uuid.UUID
    ) -> CampaignProgress:
        """Get campaign execution progress."""
        campaign = await self.campaign_repo.get_by_id(campaign_id)
        if not campaign:
            raise CampaignNotFoundError(f"Campaign {campaign_id} not found")

        return await self.campaign_repo.get_progress(campaign_id)

    async def get_campaign_tasks(
        self,
        campaign_id: uuid.UUID,
        status: Optional[str] = None
    ) -> List[CampaignTask]:
        """Get tasks for a campaign."""
        campaign = await self.campaign_repo.get_by_id(campaign_id)
        if not campaign:
            raise CampaignNotFoundError(f"Campaign {campaign_id} not found")

        return await self.task_repo.get_by_campaign(campaign_id, status)

    def to_response(self, campaign: Campaign) -> CampaignResponse:
        """Convert Campaign model to response schema."""
        return CampaignResponse(
            id=campaign.id,
            name=campaign.name,
            description=campaign.description,
            status=CampaignStatus(campaign.status),
            target_urls=campaign.target_urls,
            account_ids=campaign.account_ids,
            actions=CampaignActions(**campaign.actions),
            priority=campaign.priority,
            progress=CampaignProgress(
                total_tasks=campaign.total_tasks,
                completed_tasks=campaign.completed_tasks,
                failed_tasks=campaign.failed_tasks,
                pending_tasks=max(
                    0,
                    campaign.total_tasks - campaign.completed_tasks - campaign.failed_tasks
                ),
                completion_percent=campaign.completion_percent
            ),
            scheduled_start_at=campaign.scheduled_start_at,
            started_at=campaign.started_at,
            completed_at=campaign.completed_at,
            created_at=campaign.created_at,
            updated_at=campaign.updated_at
        )


async def apply_task_result(
    session: AsyncSession,
    orchestrator_task_id: str,
    status: str,
    result: Optional[dict] = None,
) -> None:
    """
    Record a task outcome reported by the orchestrator, and roll it up.

    This is the one campaign write with no request context behind it: the
    orchestrator reports on its own schedule, long after the HTTP call that
    started the campaign has returned. It is a module function rather than a
    ``CampaignService`` method precisely so that class can keep ``org_id``
    mandatory — the alternative was an ``org_id=None`` escape hatch on the
    service, which is the door this whole change closes.

    The tenant is *derived* from the task's campaign rather than supplied, so
    there is still no path from caller input to a cross-org write.
    """
    task = await CampaignTaskRepository(session).update_status(
        orchestrator_task_id, status, result
    )

    if not task:
        logger.warning(
            "task_not_found_for_status_update",
            orchestrator_task_id=orchestrator_task_id
        )
        return

    owner = await session.execute(
        select(Campaign.org_id).where(Campaign.id == task.campaign_id)
    )
    org_id = owner.scalar_one_or_none()
    if org_id is None:
        logger.warning(
            "campaign_missing_for_task",
            orchestrator_task_id=orchestrator_task_id,
            campaign_id=str(task.campaign_id),
        )
        return

    repo = CampaignRepository(session, org_id=org_id)

    # Update campaign counters
    if status == "completed":
        await repo.increment_task_count(task.campaign_id, completed=1)
    elif status == "failed":
        await repo.increment_task_count(task.campaign_id, failed=1)

    # Check if campaign is complete
    progress = await repo.get_progress(task.campaign_id)
    if progress.pending_tasks == 0:
        # All tasks processed
        await repo.update_status(
            task.campaign_id,
            CampaignStatusEnum.COMPLETED,
            set_completed=True
        )
        logger.info("campaign_completed", campaign_id=str(task.campaign_id))
