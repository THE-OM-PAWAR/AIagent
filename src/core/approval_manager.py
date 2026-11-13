"""Approval Manager for managing human-in-the-loop approvals, expirations, and resolution audit records."""

from datetime import datetime, timedelta, timezone
import logging
from typing import Any
import uuid

from sqlalchemy import desc, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.database.models.workflow import ApprovalStatus, WorkflowApprovalModel

logger = logging.getLogger(__name__)


def _as_utc(dt: datetime) -> datetime:
    """Ensure datetime is timezone-aware UTC for robust comparison across engines."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class ApprovalError(Exception):
    """Base exception for approval lifecycle failures."""
    pass


class ApprovalNotFoundError(ApprovalError):
    """Raised when an approval request cannot be located."""
    pass


class ApprovalExpiredError(ApprovalError):
    """Raised when attempting to resolve an expired approval request."""
    pass


class ApprovalUnauthorizedError(ApprovalError):
    """Raised when an unauthorized user attempts to resolve an approval request."""
    pass


class ApprovalInvalidStateError(ApprovalError):
    """Raised when attempting to resolve a non-pending approval request."""
    pass


class ApprovalManager:
    """Manages creation, inspection, authorization, and resolution of sensitive workflow approval requests."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        default_ttl_hours: int = 24,
    ) -> None:
        self.session_factory = session_factory
        self.default_ttl_hours = default_ttl_hours

    async def create_approval_request(
        self,
        workflow_id: str,
        run_id: str,
        step_name: str,
        tool_name: str,
        action_summary: str,
        action_arguments: dict[str, Any],
        requested_by_user_id: int,
        ttl_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Persist a new pending approval request in PostgreSQL."""
        ttl = ttl_seconds if ttl_seconds is not None else int(self.default_ttl_hours * 3600)
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl)
        approval_id = str(uuid.uuid4())

        async with self.session_factory() as session:
            approval = WorkflowApprovalModel(
                id=approval_id,
                workflow_id=workflow_id,
                run_id=run_id,
                step_name=step_name,
                tool_name=tool_name,
                action_summary=action_summary,
                action_arguments=action_arguments or {},
                requested_by_user_id=requested_by_user_id,
                status=ApprovalStatus.PENDING.value,
                expires_at=expires_at,
            )
            session.add(approval)
            await session.commit()
            await session.refresh(approval)

            logger.info(
                "Approval request created: id=%s for workflow=%s, run=%s, tool=%s, user=%d, expires=%s",
                approval_id,
                workflow_id,
                run_id,
                tool_name,
                requested_by_user_id,
                expires_at.isoformat(),
            )
            return {
                "approval_id": approval.id,
                "workflow_id": approval.workflow_id,
                "run_id": approval.run_id,
                "step_name": approval.step_name,
                "tool_name": approval.tool_name,
                "action_summary": approval.action_summary,
                "action_arguments": approval.action_arguments,
                "status": approval.status,
                "expires_at": approval.expires_at.isoformat(),
            }

    async def get_approval(self, approval_id: str) -> dict[str, Any] | None:
        """Retrieve an approval record by ID with lazy expiration check."""
        async with self.session_factory() as session:
            stmt = select(WorkflowApprovalModel).where(WorkflowApprovalModel.id == approval_id)
            res = await session.execute(stmt)
            appr = res.scalars().first()
            if not appr:
                return None

            # Check if pending request has expired
            now = datetime.now(timezone.utc)
            if appr.status == ApprovalStatus.PENDING.value and _as_utc(appr.expires_at) < _as_utc(now):
                appr.status = ApprovalStatus.EXPIRED.value
                await session.commit()
                await session.refresh(appr)

            return {
                "approval_id": appr.id,
                "workflow_id": appr.workflow_id,
                "run_id": appr.run_id,
                "step_name": appr.step_name,
                "tool_name": appr.tool_name,
                "action_summary": appr.action_summary,
                "action_arguments": appr.action_arguments,
                "requested_by_user_id": appr.requested_by_user_id,
                "status": appr.status,
                "resolved_by_user_id": appr.resolved_by_user_id,
                "resolution_notes": appr.resolution_notes,
                "expires_at": appr.expires_at.isoformat(),
                "created_at": appr.created_at.isoformat(),
            }

    async def list_pending_approvals(self, user_id: int) -> list[dict[str, Any]]:
        """List active pending approvals for a user, filtering out and updating expired ones."""
        await self.expire_stale_approvals()

        async with self.session_factory() as session:
            now = datetime.now(timezone.utc)
            stmt = (
                select(WorkflowApprovalModel)
                .where(WorkflowApprovalModel.requested_by_user_id == user_id)
                .where(WorkflowApprovalModel.status == ApprovalStatus.PENDING.value)
                .where(WorkflowApprovalModel.expires_at > now)
                .order_by(desc(WorkflowApprovalModel.created_at))
            )
            res = await session.execute(stmt)
            items = list(res.scalars().all())
            return [
                {
                    "approval_id": a.id,
                    "workflow_id": a.workflow_id,
                    "run_id": a.run_id,
                    "step_name": a.step_name,
                    "tool_name": a.tool_name,
                    "action_summary": a.action_summary,
                    "action_arguments": a.action_arguments,
                    "status": a.status,
                    "expires_at": a.expires_at.isoformat(),
                    "created_at": a.created_at.isoformat(),
                }
                for a in items
            ]

    async def approve_request(
        self,
        approval_id: str,
        user_id: int,
        resolution_notes: str | None = None,
        is_admin: bool = False,
    ) -> dict[str, Any]:
        """Approve a pending approval request after validating identity and expiration."""
        async with self.session_factory() as session:
            stmt = select(WorkflowApprovalModel).where(WorkflowApprovalModel.id == approval_id)
            res = await session.execute(stmt)
            appr = res.scalars().first()
            if not appr:
                raise ApprovalNotFoundError(f"Approval request '{approval_id}' not found.")

            now = datetime.now(timezone.utc)
            if _as_utc(appr.expires_at) < _as_utc(now):
                appr.status = ApprovalStatus.EXPIRED.value
                await session.commit()
                raise ApprovalExpiredError(f"Approval request '{approval_id}' expired at {appr.expires_at.isoformat()}.")

            if appr.status != ApprovalStatus.PENDING.value:
                raise ApprovalInvalidStateError(
                    f"Approval request '{approval_id}' is already resolved in state '{appr.status}'."
                )

            # Security: User must own the request or be authorized administrator
            if appr.requested_by_user_id != user_id and not is_admin:
                raise ApprovalUnauthorizedError(
                    f"User {user_id} is not authorized to resolve approval request '{approval_id}'."
                )

            appr.status = ApprovalStatus.APPROVED.value
            appr.resolved_by_user_id = user_id
            appr.resolution_notes = resolution_notes or "Approved by user"
            await session.commit()
            await session.refresh(appr)

            logger.info("Approval '%s' APPROVED by user %d for tool '%s'", approval_id, user_id, appr.tool_name)
            return {
                "approval_id": appr.id,
                "workflow_id": appr.workflow_id,
                "run_id": appr.run_id,
                "step_name": appr.step_name,
                "tool_name": appr.tool_name,
                "action_arguments": appr.action_arguments,
                "status": appr.status,
                "resolved_by_user_id": appr.resolved_by_user_id,
                "resolution_notes": appr.resolution_notes,
            }

    async def reject_request(
        self,
        approval_id: str,
        user_id: int,
        resolution_notes: str | None = None,
        is_admin: bool = False,
    ) -> dict[str, Any]:
        """Reject a pending approval request."""
        async with self.session_factory() as session:
            stmt = select(WorkflowApprovalModel).where(WorkflowApprovalModel.id == approval_id)
            res = await session.execute(stmt)
            appr = res.scalars().first()
            if not appr:
                raise ApprovalNotFoundError(f"Approval request '{approval_id}' not found.")

            now = datetime.now(timezone.utc)
            if _as_utc(appr.expires_at) < _as_utc(now):
                appr.status = ApprovalStatus.EXPIRED.value
                await session.commit()
                raise ApprovalExpiredError(f"Approval request '{approval_id}' has already expired.")

            if appr.status != ApprovalStatus.PENDING.value:
                raise ApprovalInvalidStateError(
                    f"Approval request '{approval_id}' cannot be rejected in state '{appr.status}'."
                )

            if appr.requested_by_user_id != user_id and not is_admin:
                raise ApprovalUnauthorizedError(
                    f"User {user_id} is not authorized to reject approval request '{approval_id}'."
                )

            appr.status = ApprovalStatus.REJECTED.value
            appr.resolved_by_user_id = user_id
            appr.resolution_notes = resolution_notes or "Rejected by user"
            await session.commit()
            await session.refresh(appr)

            logger.info("Approval '%s' REJECTED by user %d for tool '%s'", approval_id, user_id, appr.tool_name)
            return {
                "approval_id": appr.id,
                "workflow_id": appr.workflow_id,
                "run_id": appr.run_id,
                "step_name": appr.step_name,
                "tool_name": appr.tool_name,
                "status": appr.status,
                "resolved_by_user_id": appr.resolved_by_user_id,
                "resolution_notes": appr.resolution_notes,
            }

    async def expire_stale_approvals(self) -> int:
        """Batch transition any overdue pending approvals to expired."""
        async with self.session_factory() as session:
            now = datetime.now(timezone.utc)
            stmt = (
                update(WorkflowApprovalModel)
                .where(WorkflowApprovalModel.status == ApprovalStatus.PENDING.value)
                .where(WorkflowApprovalModel.expires_at <= now)
                .values(status=ApprovalStatus.EXPIRED.value)
            )
            res = await session.execute(stmt)
            await session.commit()
            if res.rowcount > 0:
                logger.info("Expired %d stale pending workflow approval requests.", res.rowcount)
            return res.rowcount
