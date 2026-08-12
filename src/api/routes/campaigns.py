"""
Campaign API Routes

Provides CRUD and execution endpoints for LinkedIn campaigns.

Every route here is authenticated and org-scoped through
``get_campaign_service``, which builds the service from the caller's resolved
tenancy context. A campaign belonging to another organization is reported as
404, not 403: telling an unauthorized caller that an id exists is itself a
disclosure.
"""

from fastapi import APIRouter, Depends, HTTPException, Request, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional, List
import uuid

from src.database.session import get_db
from src.campaigns.schemas import (
    CampaignCreate,
    CampaignUpdate,
    CampaignResponse,
    CampaignListResponse,
    CampaignProgress,
    CampaignTaskResponse,
    StartCampaignResponse,
)
from src.campaigns.service import (
    CampaignService,
    CampaignNotFoundError,
    CampaignStateError,
    IdempotencyKeyExistsError,
)
from src.api.middleware.idempotency import (
    get_idempotency_key,
    require_idempotency_key,
)
from src.api.middleware.clerk import RequestContext, get_request_context

router = APIRouter(prefix="/campaigns", tags=["campaigns"])


def get_campaign_service(
    request: Request,
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> CampaignService:
    """
    Dependency to get a CampaignService instance for the calling org.

    Depending on ``get_request_context`` is what authenticates these routes:
    an unauthenticated request never reaches a handler, and an authenticated one
    can only ever be handed a service bound to its own ``org_id``.

    The campaign<->agent task bridge and Redis client are attached to
    ``app.state`` by the lifespan when Redis is reachable; when they are absent
    (e.g. under the test transport, which does not run lifespan) the service
    degrades gracefully to the "orchestrator not configured" path.
    """
    orchestrator = getattr(request.app.state, "task_orchestrator", None)
    redis_client = getattr(request.app.state, "redis", None)
    return CampaignService(
        session=db,
        org_id=uuid.UUID(ctx.org_id),
        user_id=uuid.UUID(ctx.user_id),
        orchestrator=orchestrator,
        redis_client=redis_client,
    )


@router.post(
    "",
    response_model=CampaignResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new campaign",
    description="Create a LinkedIn automation campaign. Requires X-Idempotency-Key header."
)
async def create_campaign(
    campaign_in: CampaignCreate,
    idempotency_key: str = Depends(require_idempotency_key),
    service: CampaignService = Depends(get_campaign_service)
):
    """
    Create a new LinkedIn automation campaign.

    - **name**: Campaign name (required)
    - **description**: Optional description
    - **target_urls**: List of LinkedIn URLs to engage with (required)
    - **account_ids**: LinkedIn account IDs to use (required)
    - **actions**: Like/comment settings
    - **priority**: 1=normal, 2=high, 3=urgent
    """
    try:
        campaign = await service.create_campaign(campaign_in, idempotency_key)
        return service.to_response(campaign)
    except IdempotencyKeyExistsError as e:
        # Return cached response for idempotent replay
        existing = await service.get_campaign(e.resource_id)
        return service.to_response(existing)


@router.get(
    "",
    response_model=CampaignListResponse,
    summary="List all campaigns"
)
async def list_campaigns(
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
    # Named status_filter, exposed as ?status=: a parameter called `status`
    # shadows fastapi.status inside the handler, which turns any
    # `status.HTTP_*` reference into an AttributeError.
    status_filter: Optional[str] = Query(None, alias="status", description="Filter by status"),
    service: CampaignService = Depends(get_campaign_service)
):
    """
    List the calling organization's campaigns with pagination.

    Supports filtering by status: draft, scheduled, running, paused, completed, failed
    """
    campaigns, total = await service.list_campaigns(page, page_size, status_filter)
    return CampaignListResponse(
        campaigns=[service.to_response(c) for c in campaigns],
        total=total,
        page=page,
        page_size=page_size
    )


@router.get(
    "/{campaign_id}",
    response_model=CampaignResponse,
    summary="Get campaign by ID"
)
async def get_campaign(
    campaign_id: uuid.UUID,
    service: CampaignService = Depends(get_campaign_service)
):
    """Get a specific campaign by its ID."""
    try:
        campaign = await service.get_campaign(campaign_id)
        return service.to_response(campaign)
    except CampaignNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Campaign {campaign_id} not found"
        )


@router.patch(
    "/{campaign_id}",
    response_model=CampaignResponse,
    summary="Update campaign"
)
async def update_campaign(
    campaign_id: uuid.UUID,
    campaign_update: CampaignUpdate,
    service: CampaignService = Depends(get_campaign_service)
):
    """
    Update a campaign.

    Only draft campaigns can be updated.
    """
    try:
        campaign = await service.update_campaign(campaign_id, campaign_update)
        return service.to_response(campaign)
    except CampaignNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Campaign {campaign_id} not found"
        )
    except CampaignStateError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )


@router.delete(
    "/{campaign_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete campaign"
)
async def delete_campaign(
    campaign_id: uuid.UUID,
    service: CampaignService = Depends(get_campaign_service)
):
    """
    Soft delete a campaign.

    Running campaigns cannot be deleted - pause them first.
    """
    try:
        await service.delete_campaign(campaign_id)
    except CampaignNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Campaign {campaign_id} not found"
        )
    except CampaignStateError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )


@router.post(
    "/{campaign_id}/start",
    response_model=StartCampaignResponse,
    summary="Start campaign execution"
)
async def start_campaign(
    campaign_id: uuid.UUID,
    idempotency_key: str = Depends(require_idempotency_key),
    service: CampaignService = Depends(get_campaign_service)
):
    """
    Start campaign execution.

    Submits tasks to the message orchestrator for each target URL.
    Requires X-Idempotency-Key header to prevent duplicate starts.
    """
    try:
        result = await service.start_campaign(campaign_id, idempotency_key)
        return result
    except CampaignNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Campaign {campaign_id} not found"
        )
    except CampaignStateError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )


@router.post(
    "/{campaign_id}/pause",
    response_model=CampaignResponse,
    summary="Pause campaign execution"
)
async def pause_campaign(
    campaign_id: uuid.UUID,
    idempotency_key: Optional[str] = Depends(get_idempotency_key),
    service: CampaignService = Depends(get_campaign_service)
):
    """
    Pause a running campaign.

    Only running campaigns can be paused.
    """
    try:
        campaign = await service.pause_campaign(campaign_id, idempotency_key)
        return service.to_response(campaign)
    except CampaignNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Campaign {campaign_id} not found"
        )
    except CampaignStateError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )


@router.get(
    "/{campaign_id}/status",
    response_model=CampaignProgress,
    summary="Get campaign execution progress"
)
async def get_campaign_status(
    campaign_id: uuid.UUID,
    service: CampaignService = Depends(get_campaign_service)
):
    """
    Get real-time campaign execution progress.

    Returns task counts and completion percentage.
    """
    try:
        return await service.get_campaign_progress(campaign_id)
    except CampaignNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Campaign {campaign_id} not found"
        )


@router.get(
    "/{campaign_id}/tasks",
    response_model=List[CampaignTaskResponse],
    summary="Get campaign tasks"
)
async def get_campaign_tasks(
    campaign_id: uuid.UUID,
    # See list_campaigns: `status` as a parameter name shadows fastapi.status,
    # which broke the 404 branch below with an AttributeError.
    status_filter: Optional[str] = Query(None, alias="status", description="Filter by task status"),
    service: CampaignService = Depends(get_campaign_service)
):
    """
    Get all tasks for a campaign.

    Tasks are created when a campaign is started.
    """
    try:
        tasks = await service.get_campaign_tasks(campaign_id, status_filter)
        return [CampaignTaskResponse.model_validate(t) for t in tasks]
    except CampaignNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Campaign {campaign_id} not found"
        )
