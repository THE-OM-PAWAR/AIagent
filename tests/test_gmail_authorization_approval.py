"""Comprehensive unit and integration tests for Gmail authorization, approvals, and idempotency."""

import asyncio
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.approval_manager import ApprovalManager
from src.database.models.base import Base
from src.database.models.workflow import ApprovalStatus, WorkflowApprovalModel
from src.integrations.connectors.gmail.client import GmailClient
from src.integrations.connectors.gmail.tools import (
    GmailDraftTool,
    GmailReadTool,
    GmailSearchTool,
    GmailSendTool,
)
from src.tools.base import BaseTool
from src.tools.executor import ToolExecutor
from src.tools.permissions import PermissionPolicy, ToolPermission
from src.tools.registry import ToolRegistry


class TestGmailAuthorizationAndApproval(unittest.IsolatedAsyncioTestCase):
    """Verifies Gmail authorization hierarchies, ApprovalManager integration, and idempotency."""

    async def asyncSetUp(self) -> None:
        # 1. Setup in-memory SQLite database for ApprovalManager
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)
        self.approval_manager = ApprovalManager(self.session_factory, default_ttl_hours=1)

        # 2. Setup mock Gmail client
        self.mock_client = MagicMock(spec=GmailClient)
        self.mock_client.access_token = "mock_access_token_123"
        self.mock_client.send_email = AsyncMock(return_value={
            "message_id": "msg_abc123",
            "thread_id": "thread_xyz789",
            "to": "test@example.com",
            "subject": "Greetings",
            "status": "sent",
        })
        self.mock_client.create_draft = AsyncMock(return_value={
            "draft_id": "draft_456",
            "to": "test@example.com",
            "subject": "Greetings",
            "status": "draft_created",
        })

        # 3. Setup Tool Registry and Executor
        self.registry = ToolRegistry()
        self.search_tool = GmailSearchTool(client=self.mock_client)
        self.draft_tool = GmailDraftTool(client=self.mock_client)
        self.send_tool = GmailSendTool(client=self.mock_client)

        self.registry.register_tool(self.search_tool)
        self.registry.register_tool(self.draft_tool)
        self.registry.register_tool(self.send_tool)

        self.permission_policy = PermissionPolicy(default_user_permission=ToolPermission.WRITE)
        self.executor = ToolExecutor(
            registry=self.registry,
            permission_policy=self.permission_policy,
            approval_manager=self.approval_manager,
        )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    # --------------------------------------------------------------------------
    # 1. Permission Audit & Hierarchy Checks
    # --------------------------------------------------------------------------
    def test_gmail_tool_permission_definitions(self) -> None:
        """Verify strict adherence to permission tiers without security downgrades."""
        self.assertEqual(self.search_tool.permission, ToolPermission.READ_ONLY)
        self.assertEqual(self.draft_tool.permission, ToolPermission.WRITE)
        # SENSITIVE must NEVER be downgraded to WRITE
        self.assertEqual(self.send_tool.permission, ToolPermission.SENSITIVE)
        self.assertTrue(self.send_tool.requires_approval)

    def test_write_user_denied_send_with_draft_recommendation(self) -> None:
        """Verify user with WRITE permission is denied sending email, with advice to use draft."""
        is_allowed, reason = self.permission_policy.check_permission(
            user_id=101,
            tool_name="gmail_send_email",
            required_permission=ToolPermission.SENSITIVE,
        )
        self.assertFalse(is_allowed)
        self.assertIn("cannot execute tool 'gmail_send_email' requiring 'sensitive'", reason)
        self.assertIn("gmail_create_draft", reason)

    async def test_write_user_can_safely_create_draft(self) -> None:
        """Verify user with default WRITE permission can safely execute gmail_create_draft."""
        res = await self.executor.execute_tool(
            tool_name="gmail_create_draft",
            arguments={"to": "client@example.com", "subject": "Proposal", "body": "Draft content"},
            user_id=101,
        )
        self.assertTrue(res.success)
        self.assertEqual(res.output.get("status"), "draft_created")

    # --------------------------------------------------------------------------
    # 2. Sensitive Permission & Approval Requirements
    # --------------------------------------------------------------------------
    async def test_sensitive_user_requires_approval_before_send(self) -> None:
        """User with SENSITIVE permission cannot send without explicit approval."""
        self.permission_policy.grant_user_permission(101, ToolPermission.SENSITIVE)

        # Without approval_granted, tool executor blocks execution
        res = await self.executor.execute_tool(
            tool_name="gmail_send_email",
            arguments={"to": "client@example.com", "subject": "Hello", "body": "Body", "confirmed": True},
            user_id=101,
            approval_granted=False,
        )
        self.assertFalse(res.success)
        self.assertIn("requires human-in-the-loop approval", res.error)

    async def test_unconfirmed_send_returns_pending_status_to_gemini(self) -> None:
        """Executing gmail_send_email without confirmed=True returns pending approval details and safe alternative."""
        self.permission_policy.grant_user_permission(101, ToolPermission.SENSITIVE)

        # Invocation with confirmed=False (even if approval_granted=True at executor level)
        res = await self.executor.execute_tool(
            tool_name="gmail_send_email",
            arguments={"to": "client@example.com", "subject": "Proposal", "body": "Body text", "confirmed": False},
            user_id=101,
            approval_granted=True,
        )
        self.assertTrue(res.success)
        self.assertEqual(res.output.get("status"), "pending_approval")
        self.assertEqual(res.output.get("safe_alternative"), "gmail_create_draft")

    # --------------------------------------------------------------------------
    # 3. ApprovalManager Lifecycle & Validation Checks
    # --------------------------------------------------------------------------
    async def test_valid_approval_resolves_and_executes(self) -> None:
        """End-to-end: Create approval, approve it, and execute with approval_id."""
        self.permission_policy.grant_user_permission(101, ToolPermission.SENSITIVE)

        args = {"to": "partner@example.com", "subject": "Contract", "body": "Please review", "confirmed": True}
        appr_info = await self.approval_manager.create_approval_request(
            workflow_id="wf-test-1",
            run_id="run-test-1",
            step_name="send_step",
            tool_name="gmail_send_email",
            action_summary="Send contract email",
            action_arguments=args,
            requested_by_user_id=101,
        )
        appr_id = appr_info["approval_id"]

        # Attempt execution while approval is still pending
        res_pending = await self.executor.execute_tool(
            tool_name="gmail_send_email",
            arguments=args,
            user_id=101,
            approval_id=appr_id,
        )
        self.assertFalse(res_pending.success)
        self.assertIn("is not in approved state", res_pending.error)

        # User approves the request
        await self.approval_manager.approve_request(approval_id=appr_id, user_id=101)

        # Execution with approved ID now succeeds
        res_approved = await self.executor.execute_tool(
            tool_name="gmail_send_email",
            arguments=args,
            user_id=101,
            approval_id=appr_id,
        )
        self.assertTrue(res_approved.success)
        self.assertEqual(res_approved.output.get("status"), "sent")

    async def test_approval_does_not_bypass_user_tool_authorization(self) -> None:
        """CRITICAL: User with only WRITE permission cannot execute send even with an approved request."""
        # User 202 has only WRITE permission
        self.permission_policy.grant_user_permission(202, ToolPermission.WRITE)

        args = {"to": "vip@example.com", "subject": "Notice", "body": "Details", "confirmed": True}
        appr_info = await self.approval_manager.create_approval_request(
            workflow_id="wf-test-2",
            run_id="run-test-2",
            step_name="send_step",
            tool_name="gmail_send_email",
            action_summary="Send notice",
            action_arguments=args,
            requested_by_user_id=202,
        )
        appr_id = appr_info["approval_id"]
        await self.approval_manager.approve_request(approval_id=appr_id, user_id=202)

        # Execution must still be blocked by permission hierarchy
        res = await self.executor.execute_tool(
            tool_name="gmail_send_email",
            arguments=args,
            user_id=202,
            approval_id=appr_id,
        )
        self.assertFalse(res.success)
        self.assertIn("permission 'write' cannot execute tool 'gmail_send_email' requiring 'sensitive'", res.error)

    async def test_approval_validation_ownership_and_argument_identity(self) -> None:
        """Verify ownership check and argument mismatch prevention."""
        self.permission_policy.grant_user_permission(101, ToolPermission.SENSITIVE)
        self.permission_policy.grant_user_permission(999, ToolPermission.SENSITIVE)

        original_args = {"to": "original@example.com", "subject": "Subject", "body": "Original text", "confirmed": True}
        appr_info = await self.approval_manager.create_approval_request(
            workflow_id="wf-test-3",
            run_id="run-test-3",
            step_name="step_send",
            tool_name="gmail_send_email",
            action_summary="Send to original",
            action_arguments=original_args,
            requested_by_user_id=101,
        )
        appr_id = appr_info["approval_id"]
        await self.approval_manager.approve_request(approval_id=appr_id, user_id=101)

        # 1. Ownership test: user 999 tries to execute user 101's approval
        res_wrong_user = await self.executor.execute_tool(
            tool_name="gmail_send_email",
            arguments=original_args,
            user_id=999,
            approval_id=appr_id,
        )
        self.assertFalse(res_wrong_user.success)
        self.assertIn("not authorized to execute approval request", res_wrong_user.error)

        # 2. Argument mismatch test: user 101 tries to execute with modified recipient
        tampered_args = dict(original_args)
        tampered_args["to"] = "attacker@example.com"
        res_tampered = await self.executor.execute_tool(
            tool_name="gmail_send_email",
            arguments=tampered_args,
            user_id=101,
            approval_id=appr_id,
        )
        self.assertFalse(res_tampered.success)
        self.assertIn("Argument mismatch", res_tampered.error)

    # --------------------------------------------------------------------------
    # 4. Idempotency & Duplicate Prevention
    # --------------------------------------------------------------------------
    async def test_duplicate_send_prevention_idempotency(self) -> None:
        """Verify identical email sends are deduplicated and do not call Gmail API twice."""
        self.permission_policy.grant_user_permission(101, ToolPermission.SENSITIVE)
        args = {"to": "client_dup@example.com", "subject": "Invoice", "body": "Total: $100", "confirmed": True}

        # First send
        res1 = await self.executor.execute_tool(
            tool_name="gmail_send_email",
            arguments=args,
            user_id=101,
            approval_granted=True,
        )
        self.assertTrue(res1.success)
        self.assertEqual(res1.output.get("status"), "sent")
        self.assertFalse(res1.output.get("already_sent", False))
        self.assertEqual(self.mock_client.send_email.call_count, 1)

        # Second send with identical arguments (e.g. Gemini retry or network glitch)
        res2 = await self.executor.execute_tool(
            tool_name="gmail_send_email",
            arguments=args,
            user_id=101,
            approval_granted=True,
        )
        self.assertTrue(res2.success)
        self.assertTrue(res2.output.get("already_sent"))
        # Must NOT call external send_email a second time!
        self.assertEqual(self.mock_client.send_email.call_count, 1)

    async def test_outcome_verification_fails_if_message_id_missing(self) -> None:
        """Outcome verification fails when Gmail API response omits message_id."""
        self.mock_client.send_email = AsyncMock(return_value={"status": "unknown"})
        self.permission_policy.grant_user_permission(101, ToolPermission.SENSITIVE)

        res = await self.send_tool.execute(
            to="unverified@example.com",
            subject="Test",
            body="Content",
            confirmed=True,
            idempotency_key="unique_key_unverified",
            user_id=101,
        )
        self.assertEqual(res.get("status"), "error")
        self.assertIn("outcome verification failed", res.get("error", ""))

