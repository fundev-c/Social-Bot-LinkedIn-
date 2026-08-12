"""
Campaign Management Module

Provides campaign orchestration for LinkedIn automation.
"""

from .models import Campaign, CampaignTask, IdempotencyKey
from .schemas import (
    CampaignCreate,
    CampaignUpdate,
    CampaignResponse,
    CampaignActions,
    CampaignProgress,
    CampaignStatus,
)
from .service import CampaignService, apply_task_result
from .repository import CampaignRepository

__all__ = [
    "Campaign",
    "CampaignTask",
    "IdempotencyKey",
    "CampaignCreate",
    "CampaignUpdate",
    "CampaignResponse",
    "CampaignActions",
    "CampaignProgress",
    "CampaignStatus",
    "CampaignService",
    "CampaignRepository",
    "apply_task_result",
]
