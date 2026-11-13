"""Repository for managing automation template configurations and persistent findings."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.database.models.automation import AutomationConfigModel, AutomationFindingModel, AutomationFindingStatus

logger = logging.getLogger(__name__)


class AutomationRepository:
    """Handles persistence and isolation for automation configurations and discovered findings."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # -------------------------------------------------------------------------
    # 1. Automation Configurations
    # -------------------------------------------------------------------------

    async def create_or_update_config(
        self,
        user_id: int,
        automation_type: str,
        name: str,
        workflow_id: str,
        config: dict[str, Any],
        schedule_id: str | None = None,
        description: str | None = None,
        is_enabled: bool = True,
        config_id: str | None = None,
    ) -> AutomationConfigModel:
        """Create or update an automation configuration for a user."""
        existing = None
        if config_id:
            stmt = select(AutomationConfigModel).where(
                AutomationConfigModel.id == config_id,
                AutomationConfigModel.user_id == user_id,
            )
            res = await self.session.execute(stmt)
            existing = res.scalar_one_or_none()

        if existing:
            existing.automation_type = automation_type
            existing.name = name
            existing.description = description
            existing.is_enabled = is_enabled
            existing.workflow_id = workflow_id
            existing.schedule_id = schedule_id
            existing.config = config
            existing.updated_at = datetime.now(timezone.utc)
            await self.session.flush()
            return existing

        new_config = AutomationConfigModel(
            user_id=user_id,
            automation_type=automation_type,
            name=name,
            description=description,
            is_enabled=is_enabled,
            workflow_id=workflow_id,
            schedule_id=schedule_id,
            config=config,
        )
        self.session.add(new_config)
        await self.session.flush()
        return new_config

    async def get_config(self, config_id: str, user_id: int | None = None) -> AutomationConfigModel | None:
        """Fetch automation configuration by id, strictly verifying user ownership when user_id is provided."""
        stmt = select(AutomationConfigModel).where(AutomationConfigModel.id == config_id)
        if user_id is not None:
            stmt = stmt.where(AutomationConfigModel.user_id == user_id)
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def get_config_by_type(self, user_id: int, automation_type: str) -> AutomationConfigModel | None:
        """Fetch active automation configuration for a specific type and user."""
        stmt = (
            select(AutomationConfigModel)
            .where(
                AutomationConfigModel.user_id == user_id,
                AutomationConfigModel.automation_type == automation_type,
            )
            .order_by(AutomationConfigModel.created_at.desc())
        )
        res = await self.session.execute(stmt)
        return res.scalars().first()

    async def list_configs(self, user_id: int, automation_type: str | None = None) -> list[AutomationConfigModel]:
        """List all automation configurations owned by a user."""
        stmt = select(AutomationConfigModel).where(AutomationConfigModel.user_id == user_id)
        if automation_type:
            stmt = stmt.where(AutomationConfigModel.automation_type == automation_type)
        stmt = stmt.order_by(AutomationConfigModel.created_at.desc())
        res = await self.session.execute(stmt)
        return list(res.scalars().all())

    async def update_config_status(self, config_id: str, user_id: int, is_enabled: bool) -> bool:
        """Enable or disable an automation configuration."""
        stmt = (
            update(AutomationConfigModel)
            .where(
                AutomationConfigModel.id == config_id,
                AutomationConfigModel.user_id == user_id,
            )
            .values(is_enabled=is_enabled, updated_at=datetime.now(timezone.utc))
        )
        res = await self.session.execute(stmt)
        await self.session.flush()
        return (res.rowcount or 0) > 0

    async def delete_config(self, config_id: str, user_id: int) -> bool:
        """Delete an automation configuration belonging to a user."""
        stmt = delete(AutomationConfigModel).where(
            AutomationConfigModel.id == config_id,
            AutomationConfigModel.user_id == user_id,
        )
        res = await self.session.execute(stmt)
        await self.session.flush()
        return (res.rowcount or 0) > 0

    # -------------------------------------------------------------------------
    # 2. Findings & Deduplication
    # -------------------------------------------------------------------------

    async def is_duplicate(self, user_id: int, automation_type: str, dedup_key: str) -> bool:
        """Check whether a finding with the specified dedup_key already exists for this user."""
        stmt = select(AutomationFindingModel.id).where(
            AutomationFindingModel.user_id == user_id,
            AutomationFindingModel.automation_type == automation_type,
            AutomationFindingModel.dedup_key == dedup_key,
        )
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none() is not None

    async def get_existing_dedup_keys(
        self, user_id: int, automation_type: str, dedup_keys: list[str]
    ) -> set[str]:
        """Return the subset of dedup_keys that already exist in the database for this user."""
        if not dedup_keys:
            return set()
        stmt = select(AutomationFindingModel.dedup_key).where(
            AutomationFindingModel.user_id == user_id,
            AutomationFindingModel.automation_type == automation_type,
            AutomationFindingModel.dedup_key.in_(dedup_keys),
        )
        res = await self.session.execute(stmt)
        return set(res.scalars().all())

    async def save_finding(
        self,
        user_id: int,
        automation_type: str,
        dedup_key: str,
        title: str,
        data: dict[str, Any],
        source_url: str | None = None,
        qualification_score: float = 0.0,
        status: str = AutomationFindingStatus.QUALIFIED.value,
        generated_draft: dict[str, Any] | None = None,
        follow_up_at: datetime | None = None,
        automation_config_id: str | None = None,
        workflow_id: str | None = None,
        run_id: str | None = None,
    ) -> AutomationFindingModel:
        """Save a new finding or update an existing finding if matching the dedup_key."""
        stmt = select(AutomationFindingModel).where(
            AutomationFindingModel.user_id == user_id,
            AutomationFindingModel.automation_type == automation_type,
            AutomationFindingModel.dedup_key == dedup_key,
        )
        res = await self.session.execute(stmt)
        existing = res.scalar_one_or_none()

        now = datetime.now(timezone.utc)
        if existing:
            existing.title = title
            existing.source_url = source_url or existing.source_url
            existing.qualification_score = qualification_score
            existing.status = status
            existing.data = data
            if generated_draft is not None:
                existing.generated_draft = generated_draft
            if follow_up_at is not None:
                existing.follow_up_at = follow_up_at
            if workflow_id:
                existing.workflow_id = workflow_id
            if run_id:
                existing.run_id = run_id
            existing.updated_at = now
            await self.session.flush()
            return existing

        finding = AutomationFindingModel(
            user_id=user_id,
            automation_type=automation_type,
            automation_config_id=automation_config_id,
            dedup_key=dedup_key,
            title=title,
            source_url=source_url,
            qualification_score=qualification_score,
            status=status,
            data=data,
            generated_draft=generated_draft,
            follow_up_at=follow_up_at,
            workflow_id=workflow_id,
            run_id=run_id,
        )
        self.session.add(finding)
        await self.session.flush()
        return finding

    async def list_findings(
        self,
        user_id: int,
        automation_type: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AutomationFindingModel]:
        """List findings strictly isolated to the requesting user."""
        stmt = select(AutomationFindingModel).where(AutomationFindingModel.user_id == user_id)
        if automation_type:
            stmt = stmt.where(AutomationFindingModel.automation_type == automation_type)
        if status:
            stmt = stmt.where(AutomationFindingModel.status == status)
        stmt = stmt.order_by(AutomationFindingModel.created_at.desc()).limit(limit).offset(offset)
        res = await self.session.execute(stmt)
        return list(res.scalars().all())

    async def get_finding(self, finding_id: str, user_id: int) -> AutomationFindingModel | None:
        """Fetch a single finding by id, enforcing user isolation."""
        stmt = select(AutomationFindingModel).where(
            AutomationFindingModel.id == finding_id,
            AutomationFindingModel.user_id == user_id,
        )
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def update_finding_status(
        self,
        finding_id: str,
        user_id: int,
        status: str,
        follow_up_at: datetime | None = None,
    ) -> bool:
        """Update finding status and optional follow-up timestamp."""
        values: dict[str, Any] = {
            "status": status,
            "updated_at": datetime.now(timezone.utc),
        }
        if follow_up_at is not None:
            values["follow_up_at"] = follow_up_at

        stmt = (
            update(AutomationFindingModel)
            .where(
                AutomationFindingModel.id == finding_id,
                AutomationFindingModel.user_id == user_id,
            )
            .values(**values)
        )
        res = await self.session.execute(stmt)
        await self.session.flush()
        return (res.rowcount or 0) > 0

