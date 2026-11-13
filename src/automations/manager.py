"""Automation Manager orchestrating configurable recurring automation systems on top of the workflow engine."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.automations.schemas import AutomationScheduleConfig
from src.automations.templates import get_template
from src.core.workflow_engine import WorkflowEngine
from src.core.workflow_manager import WorkflowManager
from src.core.workflow_scheduler import WorkflowScheduler
from src.database.models.automation import AutomationConfigModel, AutomationType
from src.database.repositories.automation_repo import AutomationRepository
from src.database.repositories.schedule_repo import ScheduleRepository
from src.database.repositories.workflow_repo import WorkflowRepository

logger = logging.getLogger(__name__)


class AutomationManager:
    """Configures, schedules, controls, and observes personal recurring automations.

    Leverages the shared WorkflowManager, WorkflowScheduler, and WorkflowEngine
    without creating divergent execution infrastructure.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        workflow_manager: WorkflowManager,
        workflow_scheduler: WorkflowScheduler,
        workflow_engine: WorkflowEngine,
    ) -> None:
        self.session_factory = session_factory
        self.workflow_manager = workflow_manager
        self.workflow_scheduler = workflow_scheduler
        self.workflow_engine = workflow_engine

    async def create_automation(
        self,
        user_id: int,
        automation_type: str,
        name: str | None = None,
        config: dict[str, Any] | None = None,
        schedule_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a new configurable automation system with workflow definition and schedule."""
        template = get_template(automation_type)
        user_config = config or template.get_default_config()

        wf_name, steps, initial_context = template.build_workflow_definition(user_id, user_config)
        resolved_name = name or wf_name

        # 1. Validate and persist reusable recurring workflow definition
        wf_record = await self.workflow_manager.create_workflow(
            user_id=user_id,
            name=resolved_name,
            steps=steps,
            context_data=initial_context,
            description=template.description,
            is_one_time=False,
        )
        workflow_id = wf_record["workflow_id"]

        # 2. Configure recurring recurrence schedule
        raw_sch = schedule_config or user_config.get("schedule", {})
        sch_obj = AutomationScheduleConfig(**raw_sch) if isinstance(raw_sch, dict) else AutomationScheduleConfig()

        schedule = await self.workflow_scheduler.schedule_workflow(
            workflow_id=workflow_id,
            user_id=user_id,
            name=f"Schedule: {resolved_name}",
            trigger_type=sch_obj.trigger_type,
            trigger_config=sch_obj.trigger_config,
            timezone_str=sch_obj.timezone,
            description=f"Recurrence schedule for {resolved_name}",
            catch_up_policy=sch_obj.catch_up_policy,
            max_runs=sch_obj.max_runs,
            allow_overlap=sch_obj.allow_overlap,
            max_concurrency=sch_obj.max_concurrency,
            max_consecutive_failures=sch_obj.max_consecutive_failures,
            run_timeout_seconds=sch_obj.run_timeout_seconds,
            api_call_budget=sch_obj.api_call_budget,
            execution_policy={
                "notification_preferences": user_config.get("notification_preferences", {}),
                "approval_required": user_config.get("approval_required", True),
            },
        )

        # 3. Persist user automation configuration record
        async with self.session_factory() as session:
            repo = AutomationRepository(session)
            cfg_model = await repo.create_or_update_config(
                user_id=user_id,
                automation_type=template.automation_type.value,
                name=resolved_name,
                workflow_id=workflow_id,
                schedule_id=schedule.id,
                config=user_config,
                description=template.description,
                is_enabled=True,
            )
            await session.commit()
            automation_id = cfg_model.id

        logger.info(
            "Created automation '%s' (id=%s, type=%s, workflow_id=%s, schedule_id=%s) for user %s",
            resolved_name,
            automation_id,
            automation_type,
            workflow_id,
            schedule.id,
            user_id,
        )

        return {
            "automation_id": automation_id,
            "automation_type": automation_type,
            "name": resolved_name,
            "is_enabled": True,
            "workflow_id": workflow_id,
            "schedule_id": schedule.id,
            "next_run_at": schedule.next_run_at.isoformat() if schedule.next_run_at else None,
            "config": user_config,
        }

    async def get_automation(self, user_id: int, automation_id: str) -> dict[str, Any] | None:
        """Fetch automation details, schedule status, and recent runs."""
        async with self.session_factory() as session:
            repo = AutomationRepository(session)
            cfg = await repo.get_config(automation_id, user_id=user_id)
            if not cfg:
                return None

            sch_repo = ScheduleRepository(session)
            schedule = await sch_repo.get_schedule(cfg.schedule_id) if cfg.schedule_id else None

            wf_repo = WorkflowRepository(session)
            recent_runs = await wf_repo.list_runs_for_schedule(cfg.schedule_id, limit=5) if cfg.schedule_id else []

            return {
                "automation_id": cfg.id,
                "automation_type": cfg.automation_type,
                "name": cfg.name,
                "description": cfg.description,
                "is_enabled": cfg.is_enabled,
                "workflow_id": cfg.workflow_id,
                "schedule_id": cfg.schedule_id,
                "schedule_status": schedule.status if schedule else None,
                "next_run_at": schedule.next_run_at.isoformat() if schedule and schedule.next_run_at else None,
                "total_runs_count": schedule.total_runs_count if schedule else 0,
                "consecutive_failures": schedule.consecutive_failures if schedule else 0,
                "config": cfg.config,
                "recent_runs": [
                    {
                        "run_id": r.id,
                        "status": r.status,
                        "started_at": r.started_at.isoformat() if r.started_at else None,
                        "completed_at": r.completed_at.isoformat() if r.completed_at else None,
                        "error": r.error_message,
                    }
                    for r in recent_runs
                ],
            }

    async def list_automations(self, user_id: int, automation_type: str | None = None) -> list[dict[str, Any]]:
        """List all automations owned by user."""
        async with self.session_factory() as session:
            repo = AutomationRepository(session)
            configs = await repo.list_configs(user_id=user_id, automation_type=automation_type)
            result: list[dict[str, Any]] = []

            sch_repo = ScheduleRepository(session)
            for cfg in configs:
                schedule = await sch_repo.get_schedule(cfg.schedule_id) if cfg.schedule_id else None
                result.append({
                    "automation_id": cfg.id,
                    "automation_type": cfg.automation_type,
                    "name": cfg.name,
                    "is_enabled": cfg.is_enabled,
                    "workflow_id": cfg.workflow_id,
                    "schedule_id": cfg.schedule_id,
                    "schedule_status": schedule.status if schedule else None,
                    "next_run_at": schedule.next_run_at.isoformat() if schedule and schedule.next_run_at else None,
                    "total_runs_count": schedule.total_runs_count if schedule else 0,
                })
            return result

    async def pause_automation(self, user_id: int, automation_id: str) -> bool:
        """Pause recurring executions for an automation."""
        async with self.session_factory() as session:
            repo = AutomationRepository(session)
            cfg = await repo.get_config(automation_id, user_id=user_id)
            if not cfg or not cfg.schedule_id:
                return False

            paused = await self.workflow_scheduler.pause_schedule(cfg.schedule_id, user_id=user_id)
            return paused

    async def resume_automation(self, user_id: int, automation_id: str) -> bool:
        """Resume paused recurring schedule for an automation."""
        async with self.session_factory() as session:
            repo = AutomationRepository(session)
            cfg = await repo.get_config(automation_id, user_id=user_id)
            if not cfg or not cfg.schedule_id:
                return False

            resumed = await self.workflow_scheduler.resume_schedule(cfg.schedule_id, user_id=user_id)
            return resumed

    async def disable_automation(self, user_id: int, automation_id: str) -> bool:
        """Disable automation completely and pause its schedule."""
        async with self.session_factory() as session:
            repo = AutomationRepository(session)
            cfg = await repo.get_config(automation_id, user_id=user_id)
            if not cfg:
                return False

            if cfg.schedule_id:
                await self.workflow_scheduler.pause_schedule(cfg.schedule_id, user_id=user_id)

            await repo.update_config_status(automation_id, user_id, is_enabled=False)
            await session.commit()
            return True

    async def enable_automation(self, user_id: int, automation_id: str) -> bool:
        """Enable an automation and resume its recurrence schedule."""
        async with self.session_factory() as session:
            repo = AutomationRepository(session)
            cfg = await repo.get_config(automation_id, user_id=user_id)
            if not cfg:
                return False

            if cfg.schedule_id:
                await self.workflow_scheduler.resume_schedule(cfg.schedule_id, user_id=user_id)

            await repo.update_config_status(automation_id, user_id, is_enabled=True)
            await session.commit()
            return True

    async def trigger_automation_run(self, user_id: int, automation_id: str) -> dict[str, Any]:
        """Manually trigger an immediate execution pass of the automation."""
        async with self.session_factory() as session:
            repo = AutomationRepository(session)
            cfg = await repo.get_config(automation_id, user_id=user_id)
            if not cfg:
                raise ValueError(f"Automation '{automation_id}' not found for user {user_id}.")

            workflow_id = cfg.workflow_id
            schedule_id = cfg.schedule_id

        return await self.workflow_engine.execute_workflow(
            workflow_id=workflow_id,
            user_id=user_id,
            trigger_type="manual_automation_trigger",
            schedule_id=schedule_id,
        )

    # -------------------------------------------------------------------------
    # Findings & Follow-ups
    # -------------------------------------------------------------------------

    async def get_findings(
        self,
        user_id: int,
        automation_type: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Retrieve discovered opportunities, leads, content, or contacts for the user."""
        async with self.session_factory() as session:
            repo = AutomationRepository(session)
            findings = await repo.list_findings(
                user_id=user_id,
                automation_type=automation_type,
                status=status,
                limit=limit,
                offset=offset,
            )
            return [
                {
                    "id": f.id,
                    "automation_type": f.automation_type,
                    "title": f.title,
                    "source_url": f.source_url,
                    "qualification_score": f.qualification_score,
                    "status": f.status,
                    "data": f.data,
                    "generated_draft": f.generated_draft,
                    "follow_up_at": f.follow_up_at.isoformat() if f.follow_up_at else None,
                    "created_at": f.created_at.isoformat() if f.created_at else None,
                }
                for f in findings
            ]

    async def update_finding_status(
        self,
        user_id: int,
        finding_id: str,
        status: str,
        follow_up_at: datetime | None = None,
    ) -> bool:
        """Update finding lifecycle status (e.g. applied, contacted, approved, archived)."""
        async with self.session_factory() as session:
            repo = AutomationRepository(session)
            updated = await repo.update_finding_status(
                finding_id=finding_id,
                user_id=user_id,
                status=status,
                follow_up_at=follow_up_at,
            )
            await session.commit()
            return updated

