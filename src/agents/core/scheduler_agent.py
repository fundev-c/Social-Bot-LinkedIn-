"""
Scheduler agent — superseded. **Do not implement scheduling here.**

⚠️ The real scheduler is ``src/scheduler`` (``python -m src.scheduler``). This
class remains a no-op skeleton and exists only so the legacy agent orchestrator
can still boot the topology declared in ``src/config/config.py``.

Why the real one lives elsewhere: this agent framework is a separate runtime
driven by ``main.py``, and deployments do not run it. Railway's start command is
``migrate && uvicorn`` — the API process only — so a scheduler built in here
would run in ``docker-compose`` and nowhere else. That is how this file came to
describe cadence and timing in detail while nothing in production ever ticked:
``SkeletonAgent._start_agent_tasks`` returns ``[]``, so the container named
``linkedin-scheduler`` booted healthy and did nothing.

The behaviour originally sketched here (firing due sequence steps, materializing
the content calendar, drafting for accounts that have gone quiet) is partly
covered by ``src/scheduler`` — which drives ``warmup.runner.run_today``,
``outreach.sync.sync_account`` and ``outreach.execute.run_due`` — and partly
still unbuilt. Add it there.
"""

from src.agents.core._skeleton import SkeletonAgent


class SchedulerAgent(SkeletonAgent):
    CAPABILITIES = [
        "sequence_dispatch",
        "content_calendar",
        "quiet_account_posting",
    ]
