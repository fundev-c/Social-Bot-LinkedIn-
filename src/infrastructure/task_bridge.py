"""
Campaign <-> agent runtime bridge.

The API process and the agent runtime are two separate processes that share
Redis. ``CampaignService`` already calls
``orchestrator.submit_linkedin_automation_task(...)`` to hand work to the
agents, but no such object existed. ``MessageOrchestrator`` is that object.

It is deliberately thin and Redis-only (never touches the in-memory
``AgentOrchestrator.agents`` map): for each (url x account) it publishes a
``queue_interaction`` command on the ``agent_type:interaction`` channel that the
interaction agents subscribe to, records the task metadata under
``task:{task_id}``, and returns the task id. Completion flows back the other way
as ``events:interaction_completed`` / ``events:interaction_failed`` events,
which the API-side consumer maps to ``campaigns.service.apply_task_result`` (a
module function, not a ``CampaignService`` method, because this path has no
request context and so no org to scope a service to — it derives the tenant from
the task's own campaign).
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import List, Optional, Sequence

INTERACTION_CHANNEL = "agent_type:interaction"
TASK_KEY_PREFIX = "task:"
TASK_TTL_SECONDS = 7 * 24 * 3600  # keep task->campaign mapping for a week


@dataclass
class InteractionResult:
    """Parsed interaction completion/failure event."""
    task_id: str
    status: str  # "completed" | "failed"
    result: Optional[dict] = None


class MessageOrchestrator:
    """Redis-backed task submitter the campaign service depends on."""

    def __init__(self, redis_client, channel: str = INTERACTION_CHANNEL):
        self.redis = redis_client
        self.channel = channel

    async def submit_linkedin_automation_task(
        self,
        url: str,
        account_ids: Sequence[str],
        like: bool = True,
        comment: bool = False,
        priority: int = 1,
        source: str = "",
    ) -> str:
        """
        Submit one target URL for automation across the given accounts.

        Publishes one ``queue_interaction`` command per account and returns a
        single ``task_id`` identifying this URL submission (one CampaignTask row
        maps to one task_id).
        """
        task_id = str(uuid.uuid4())
        actions = {"like": bool(like), "comment": bool(comment)}
        accounts = [str(a) for a in account_ids]

        # Persist task metadata so the result consumer can resolve it later.
        await self.redis.hset(
            f"{TASK_KEY_PREFIX}{task_id}",
            mapping={
                "url": url,
                "actions": json.dumps(actions),
                "accounts": json.dumps(accounts),
                "priority": str(priority),
                "source": source,
                "status": "submitted",
                "created_at": str(time.time()),
            },
        )
        await self.redis.expire(f"{TASK_KEY_PREFIX}{task_id}", TASK_TTL_SECONDS)

        # Fan the work out to the interaction agents, one command per account.
        for account_id in accounts:
            command = {
                "type": "queue_interaction",
                "task_id": task_id,
                "url": url,
                "account_id": account_id,
                "actions": actions,
                "priority": priority,
                "source": source,
            }
            await self.redis.publish(self.channel, json.dumps(command))

        return task_id

    @staticmethod
    def parse_result_event(payload: str) -> Optional[InteractionResult]:
        """
        Parse an ``events:interaction_*`` payload into an InteractionResult.

        Returns None if the payload is malformed or missing a task_id, so the
        consumer can skip it safely.
        """
        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            return None
        task_id = data.get("task_id")
        if not task_id:
            return None
        status = data.get("status") or ("failed" if data.get("error") else "completed")
        return InteractionResult(task_id=task_id, status=status, result=data.get("result"))

    async def get_task(self, task_id: str) -> Optional[dict]:
        """Read stored task metadata (decoded), or None if absent/expired."""
        raw = await self.redis.hgetall(f"{TASK_KEY_PREFIX}{task_id}")
        if not raw:
            return None
        out = dict(raw)
        for k in ("actions", "accounts"):
            if k in out:
                try:
                    out[k] = json.loads(out[k])
                except (json.JSONDecodeError, TypeError):
                    pass
        return out
