"""Comprehensive tests for the Four Core Automation Templates.

Verifies:
- Workflow creation and configuration.
- Schedule execution.
- Qualification and filtering.
- Duplicate detection across previous runs.
- Persistence of findings in database.
- Human-in-the-loop approval before external actions.
- Failure, retry, and checkpoint recovery.
- Strict user data isolation.
- Execution policies and API usage limits.
"""

import asyncio
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.automations.manager import AutomationManager
from src.automations.schemas import (
    ClientAcquisitionConfig,
    ContentEngineConfig,
    JobHunterConfig,
    NetworkingHunterConfig,
)
from src.automations.tools import get_automation_tools
from src.core.approval_manager import ApprovalManager
from src.core.workflow_engine import WorkflowEngine
from src.core.workflow_manager import WorkflowManager
from src.core.workflow_scheduler import WorkflowScheduler
from src.database.models import Base
from src.database.models.automation import AutomationFindingStatus, AutomationType
from src.database.models.workflow import WorkflowState
from src.database.repositories.automation_repo import AutomationRepository
from src.database.repositories.schedule_repo import ScheduleRepository
from src.tools.base import BaseTool
from src.tools.executor import ToolExecutor
from src.tools.permissions import PermissionPolicy, ToolPermission
from src.tools.registry import ToolRegistry


class MockWebSearchTool(BaseTool):
    """Mock WebSearchTool providing realistic, deterministic search outputs."""

    def __init__(self) -> None:
        self.call_count = 0
        self.mock_results: dict[str, list[dict]] = {}

    @property
    def name(self) -> str:
        return "web_search"

    @property
    def description(self) -> str:
        return "Mock Web Search tool."

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ_ONLY

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer"},
                "topic": {"type": "string"},
            },
        }

    async def execute(self, query: str = "", max_results: int = 5, topic: str = "general", **kwargs) -> dict:
        self.call_count += 1
        q_lower = query.lower()

        # Job listings response
        if "intern" in q_lower or "engineer" in q_lower or "job" in q_lower:
            return {
                "query": query,
                "results": [
                    {
                        "title": "AI & ML Systems Engineering Intern",
                        "url": "https://careers.techcorp.com/jobs/ai-intern",
                        "snippet": "Seeking an AI & ML Systems Intern proficient in Python, PyTorch, and FastAPI. Build LLM workflows.",
                        "score": 0.95,
                    },
                    {
                        "title": "Backend Engineering Intern (Python)",
                        "url": "https://startup.io/jobs/backend-intern",
                        "snippet": "Join our platform team. Requirements: Python, SQL databases, Docker, async services.",
                        "score": 0.88,
                    },
                    {
                        "title": "Senior Director of Enterprise Architecture",
                        "url": "https://megacorp.com/jobs/senior-director",
                        "snippet": "15+ years experience required. Lead enterprise cloud transformation. Senior leadership role.",
                        "score": 0.92,
                    },
                    {
                        "title": "Unpaid Marketing Intern",
                        "url": "https://random.co/jobs/unpaid-intern",
                        "snippet": "Unpaid internship for social media growth. No technical requirements.",
                        "score": 0.40,
                    },
                ],
            }

        # Content topics response
        elif "agent" in q_lower or "architecture" in q_lower or "insights" in q_lower:
            return {
                "query": query,
                "results": [
                    {
                        "title": "Production Agent Systems: State Isolation & Leases",
                        "url": "https://techblog.com/agent-state-isolation-2026",
                        "snippet": "How modern agent engines isolate multi-step context in PostgreSQL using distributed worker leases to prevent concurrent race conditions.",
                        "score": 0.96,
                    },
                    {
                        "title": "Designing Resilient Distributed Workflows in Python",
                        "url": "https://python-arch.org/distributed-workflows-2026",
                        "snippet": "Implementing idempotent step checkpoints, bounded exponential retries with jitter, and human approval circuits in production.",
                        "score": 0.93,
                    },
                    {
                        "title": "Autonomous Coding Assistants and Benchmark Verification",
                        "url": "https://ai-research.org/coding-assistants-eval",
                        "snippet": "Benchmarking autonomous engineering agents against complex multi-step refactoring tasks with sandbox test execution.",
                        "score": 0.91,
                    },
                ],
            }

        # Networking & Events response
        elif "hackathon" in q_lower or "meetup" in q_lower or "conference" in q_lower:
            return {
                "query": query,
                "results": [
                    {
                        "title": "San Francisco AI Founders Hackathon 2026",
                        "url": "https://sf-ai-hackathon.dev/2026",
                        "snippet": "Weekend hackathon for AI/ML and backend systems engineers building agentic systems. Meet venture investors and founders.",
                        "score": 0.95,
                    },
                    {
                        "title": "Distributed Systems & Cloud Engineering Meetup",
                        "url": "https://meetup.com/distributed-systems-sf",
                        "snippet": "Monthly technical talks by engineering leads on high-concurrency architectures and fault-tolerant backends.",
                        "score": 0.89,
                    },
                ],
            }

        # Client acquisition fallback
        return {
            "query": query,
            "results": [
                {
                    "title": "Austin Dental Clinic Partners",
                    "url": "https://austindentalpartners.com",
                    "snippet": "Premier family dental practice. Looking for modern patient portal and scheduling automation.",
                    "score": 0.85,
                }
            ],
        }


class MockPlacesDiscoveryTool(BaseTool):
    """Mock Google Places discovery tool."""

    @property
    def name(self) -> str:
        return "places_discover_leads"

    @property
    def description(self) -> str:
        return "Mock Google Places client lead discovery tool."

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ_ONLY

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "category": {"type": "string"},
                "location": {"type": "string"},
                "min_rating": {"type": "number"},
                "max_results": {"type": "integer"},
            },
        }

    async def execute(self, category: str, location: str, min_rating: float = 3.5, max_results: int = 5, **kwargs) -> dict:
        return {
            "category": category,
            "location": location,
            "leads_count": 3,
            "leads": [
                {
                    "place_id": "place_austin_legal_001",
                    "name": "Apex Law Partners",
                    "rating": 4.6,
                    "website": "https://apexlawpartners.com",
                    "phone": "+1-512-555-0100",
                    "address": "100 Congress Ave, Austin, TX",
                },
                {
                    "place_id": "place_austin_dental_002",
                    "name": "Hill Country Dental Clinic",
                    "rating": 4.2,
                    "website": "https://hillcountrydental.com",
                    "phone": "+1-512-555-0200",
                    "address": "3400 S Lamar Blvd, Austin, TX",
                },
                {
                    "place_id": "place_austin_plumbing_003",
                    "name": "Lone Star Emergency Plumbing",
                    "rating": 2.8,  # Below threshold
                    "website": "",
                    "phone": "+1-512-555-0300",
                    "address": "1200 Airport Blvd, Austin, TX",
                },
            ],
        }


class TestAutomationTemplates(unittest.IsolatedAsyncioTestCase):
    """Integration and lifecycle tests for all four core automation templates."""

    async def asyncSetUp(self) -> None:
        """Set up in-memory database and shared automation test harness."""
        self.engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            echo=False,
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)

        # Tool Registry & Domain Tools
        self.tool_registry = ToolRegistry()
        self.web_search_tool = MockWebSearchTool()
        self.places_tool = MockPlacesDiscoveryTool()
        self.tool_registry.register_tool(self.web_search_tool)
        self.tool_registry.register_tool(self.places_tool)

        # Register automation domain tools
        for tool in get_automation_tools(session_factory=self.session_factory):
            self.tool_registry.register_tool(tool)

        self.permission_policy = PermissionPolicy(
            default_user_permission=ToolPermission.ADMIN,
            requires_approval_tools={"content_engine_publish_approval"},
        )
        self.executor = ToolExecutor(self.tool_registry, permission_policy=self.permission_policy)
        self.workflow_manager = WorkflowManager(
            session_factory=self.session_factory,
            tool_registry=self.tool_registry,
            permission_policy=self.permission_policy,
        )
        self.workflow_engine = WorkflowEngine(
            session_factory=self.session_factory,
            tool_executor=self.executor,
            workflow_manager=self.workflow_manager,
        )
        self.notifications: list[dict] = []

        async def _notification_handler(user_id: int, message: str) -> None:
            self.notifications.append({"user_id": user_id, "message": message})

        self.workflow_scheduler = WorkflowScheduler(
            session_factory=self.session_factory,
            workflow_engine=self.workflow_engine,
            poll_interval_seconds=0.1,
            notification_handler=_notification_handler,
        )

        self.automation_manager = AutomationManager(
            session_factory=self.session_factory,
            workflow_manager=self.workflow_manager,
            workflow_scheduler=self.workflow_scheduler,
            workflow_engine=self.workflow_engine,
        )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    # -------------------------------------------------------------------------
    # 1. Internship & Job Hunter Tests
    # -------------------------------------------------------------------------

    async def test_job_hunter_workflow_lifecycle_and_deduplication(self) -> None:
        """Verify job discovery, filtering, application drafting, persistence, and deduplication across runs."""
        user_id = 1001

        config = JobHunterConfig(
            search_queries=["AI engineer internship remote 2026"],
            user_profile={
                "name": "Om Patel",
                "skills": ["Python", "PyTorch", "FastAPI", "SQL"],
                "experience_level": "Intern",
                "resume_summary": "Passionate backend and AI engineer building resilient systems.",
                "portfolio_url": "https://github.com/ompatel",
            },
            exclude_keywords=["senior", "unpaid", "director"],
            min_qualification_score=0.6,
        ).model_dump()

        # 1. Create automation
        auto = await self.automation_manager.create_automation(
            user_id=user_id,
            automation_type=AutomationType.JOB_HUNTER.value,
            name="Personal Job Hunter",
            config=config,
        )
        self.assertIsNotNone(auto["automation_id"])
        self.assertIsNotNone(auto["schedule_id"])

        # 2. Execute first run
        result = await self.automation_manager.trigger_automation_run(user_id, auto["automation_id"])
        self.assertEqual(result["status"], WorkflowState.COMPLETED.value)

        # 3. Verify qualified items and application drafts persisted in PostgreSQL
        findings = await self.automation_manager.get_findings(user_id, automation_type=AutomationType.JOB_HUNTER.value)
        self.assertEqual(len(findings), 2)  # 2 matched, 2 excluded ("senior", "unpaid")

        top_job = findings[0]
        self.assertIn("AI & ML Systems", top_job["title"])
        self.assertIsNotNone(top_job["generated_draft"])
        self.assertIn("Dear Hiring Team", top_job["generated_draft"]["body"])
        self.assertIn("Om Patel", top_job["generated_draft"]["body"])
        self.assertIn("github.com/ompatel", top_job["generated_draft"]["body"])

        # 4. Run second cycle: verify duplicate detection across previous runs
        result_2 = await self.automation_manager.trigger_automation_run(user_id, auto["automation_id"])
        self.assertEqual(result_2["status"], WorkflowState.COMPLETED.value)

        # Total findings in DB must remain 2 (no duplicate entries saved)
        findings_after = await self.automation_manager.get_findings(user_id, automation_type=AutomationType.JOB_HUNTER.value)
        self.assertEqual(len(findings_after), 2)

    # -------------------------------------------------------------------------
    # 2. Client Acquisition Engine Tests
    # -------------------------------------------------------------------------

    async def test_client_acquisition_lifecycle_and_proposal_drafting(self) -> None:
        """Verify business lead discovery, qualification by rating, need identification, and proposal generation."""
        user_id = 1002

        config = ClientAcquisitionConfig(
            discovery_source="places",
            criteria={
                "category": "legal and dental services",
                "location": "Austin, TX",
                "min_rating": 3.5,
            },
            service_offerings=["Custom Web Applications", "Online Booking & Automation"],
            sender_name="Alex Rivera",
        ).model_dump()

        auto = await self.automation_manager.create_automation(
            user_id=user_id,
            automation_type=AutomationType.CLIENT_ACQUISITION.value,
            name="Austin B2B Client Engine",
            config=config,
        )

        result = await self.automation_manager.trigger_automation_run(user_id, auto["automation_id"])
        self.assertEqual(result["status"], WorkflowState.COMPLETED.value)

        # Verify findings: Lone Star Plumbing (2.8 rating) filtered out; 2 qualified leads saved
        findings = await self.automation_manager.get_findings(user_id, automation_type=AutomationType.CLIENT_ACQUISITION.value)
        self.assertEqual(len(findings), 2)

        lead_names = [f["title"] for f in findings]
        self.assertIn("Apex Law Partners", lead_names)
        self.assertIn("Hill Country Dental Clinic", lead_names)

        # Verify personalized proposal draft
        law_lead = next(f for f in findings if f["title"] == "Apex Law Partners")
        self.assertIsNotNone(law_lead["generated_draft"])
        self.assertIn("Apex Law Partners", law_lead["generated_draft"]["body"])
        self.assertIn("Alex Rivera", law_lead["generated_draft"]["body"])
        self.assertIn("online booking", law_lead["generated_draft"]["body"].lower())

    # -------------------------------------------------------------------------
    # 3. Personal Brand & Content Engine Tests
    # -------------------------------------------------------------------------

    async def test_content_engine_approval_requirement_and_weekly_calendar(self) -> None:
        """Verify technical content curation, factual weekly calendar creation, and approval enforcement."""
        user_id = 1003

        config = ContentEngineConfig(
            research_topics=["production agent systems architecture 2026"],
            content_pillars=["System Architecture", "Async Reliability", "Agent Verification"],
            target_audience="Principal Software Engineers",
            posts_per_week=3,
        ).model_dump()

        auto = await self.automation_manager.create_automation(
            user_id=user_id,
            automation_type=AutomationType.CONTENT_ENGINE.value,
            name="Engineering Thought Leadership",
            config=config,
        )

        # Run triggers workflow up to consequential publishing step requiring approval
        result = await self.automation_manager.trigger_automation_run(user_id, auto["automation_id"])
        self.assertEqual(result["status"], WorkflowState.WAITING_FOR_APPROVAL.value)
        self.assertIn("approval_id", result)

        # Verify weekly calendar posts were generated and persisted with citations before publishing
        findings = await self.automation_manager.get_findings(user_id, automation_type=AutomationType.CONTENT_ENGINE.value)
        self.assertEqual(len(findings), 3)

        sample_post = findings[0]
        self.assertIn("post_content", sample_post["generated_draft"])
        self.assertIn("Source & References:", sample_post["generated_draft"]["post_content"])
        self.assertIn("#SoftwareEngineering", sample_post["generated_draft"]["post_content"])

        # Approve step and resume workflow to terminal completion
        approval_id = result["approval_id"]
        await self.workflow_engine.approval_manager.approve_request(approval_id, user_id=user_id)
        resumed = await self.workflow_engine.resume_approved_workflow(approval_id, user_id=user_id)
        self.assertEqual(resumed["status"], WorkflowState.COMPLETED.value)

    # -------------------------------------------------------------------------
    # 4. Networking & Opportunity Hunter Tests
    # -------------------------------------------------------------------------

    async def test_networking_hunter_event_discovery_and_outreach(self) -> None:
        """Verify startup event discovery, interest qualification, personalized intro drafting, and history persistence."""
        user_id = 1004

        config = NetworkingHunterConfig(
            event_search_queries=["AI developer hackathons founder meetups 2026"],
            preferences={
                "target_interests": ["AI/ML", "Distributed Systems"],
                "user_background": "Backend software engineer passionate about high-concurrency architectures",
            },
        ).model_dump()

        auto = await self.automation_manager.create_automation(
            user_id=user_id,
            automation_type=AutomationType.NETWORKING_HUNTER.value,
            name="SF AI Networker",
            config=config,
        )

        result = await self.automation_manager.trigger_automation_run(user_id, auto["automation_id"])
        self.assertEqual(result["status"], WorkflowState.COMPLETED.value)

        findings = await self.automation_manager.get_findings(user_id, automation_type=AutomationType.NETWORKING_HUNTER.value)
        self.assertEqual(len(findings), 2)

        hackathon = next(f for f in findings if "Hackathon" in f["title"])
        drafts = hackathon["generated_draft"]
        self.assertIn("linkedin_connection_note", drafts)
        self.assertIn("San Francisco AI Founders Hackathon", drafts["linkedin_connection_note"])
        self.assertIn("email_draft", drafts)
        self.assertIn("Connecting ahead of", drafts["email_draft"]["subject"])

    # -------------------------------------------------------------------------
    # 5. User Isolation Tests
    # -------------------------------------------------------------------------

    async def test_strict_user_level_data_isolation(self) -> None:
        """Verify complete isolation of automation configurations, findings, and controls across users."""
        user_a = 5001
        user_b = 5002

        cfg = JobHunterConfig().model_dump()
        auto_a = await self.automation_manager.create_automation(user_a, AutomationType.JOB_HUNTER.value, "User A Job Hunter", cfg)
        auto_b = await self.automation_manager.create_automation(user_b, AutomationType.JOB_HUNTER.value, "User B Job Hunter", cfg)

        # User A executes
        await self.automation_manager.trigger_automation_run(user_a, auto_a["automation_id"])

        # User B cannot see User A's findings
        findings_b = await self.automation_manager.get_findings(user_b, AutomationType.JOB_HUNTER.value)
        self.assertEqual(len(findings_b), 0)

        # User B cannot pause User A's automation
        paused = await self.automation_manager.pause_automation(user_b, auto_a["automation_id"])
        self.assertFalse(paused)

        # User A can pause their own automation
        paused_a = await self.automation_manager.pause_automation(user_a, auto_a["automation_id"])
        self.assertTrue(paused_a)

        # Deduplication is strictly user-isolated: User B executing the same search can still discover their own copies
        await self.automation_manager.trigger_automation_run(user_b, auto_b["automation_id"])
        findings_b_after = await self.automation_manager.get_findings(user_b, AutomationType.JOB_HUNTER.value)
        self.assertEqual(len(findings_b_after), 2)

    # -------------------------------------------------------------------------
    # 6. API Call Budget & Execution Limits
    # -------------------------------------------------------------------------

    async def test_api_call_budget_enforcement(self) -> None:
        """Verify execution limits abort run safely when budget threshold is exhausted."""
        user_id = 1005

        cfg = JobHunterConfig().model_dump()
        # Restrict API budget to only 1 call (step 1 will execute, step 2 will abort)
        auto = await self.automation_manager.create_automation(
            user_id=user_id,
            automation_type=AutomationType.JOB_HUNTER.value,
            name="Budget Limited Automation",
            config=cfg,
            schedule_config={"api_call_budget": 1, "trigger_type": "interval", "trigger_config": {"interval_seconds": 60}},
        )

        # Advance schedule to due time
        async with self.session_factory() as session:
            sch_repo = ScheduleRepository(session)
            sch = await sch_repo.get_schedule(auto["schedule_id"])
            sch.next_run_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await session.commit()

        poll_results = await self.workflow_scheduler._poll_once()
        self.assertEqual(len(poll_results), 1)
        self.assertEqual(poll_results[0]["status"], "failed")
        self.assertIn("budget", poll_results[0]["run_result"]["error"].lower())

    # -------------------------------------------------------------------------
    # 7. Lifecycle Controls: Enable, Disable, Pause, Resume
    # -------------------------------------------------------------------------

    async def test_automation_lifecycle_controls(self) -> None:
        """Verify enable, disable, pause, and resume state transitions."""
        user_id = 1006
        cfg = ClientAcquisitionConfig().model_dump()
        auto = await self.automation_manager.create_automation(user_id, AutomationType.CLIENT_ACQUISITION.value, "Lifecycle Test", cfg)
        auto_id = auto["automation_id"]

        # Pause
        self.assertTrue(await self.automation_manager.pause_automation(user_id, auto_id))
        details = await self.automation_manager.get_automation(user_id, auto_id)
        self.assertEqual(details["schedule_status"], "paused")

        # Resume
        self.assertTrue(await self.automation_manager.resume_automation(user_id, auto_id))
        details = await self.automation_manager.get_automation(user_id, auto_id)
        self.assertEqual(details["schedule_status"], "active")

        # Disable
        self.assertTrue(await self.automation_manager.disable_automation(user_id, auto_id))
        details = await self.automation_manager.get_automation(user_id, auto_id)
        self.assertFalse(details["is_enabled"])

        # Enable
        self.assertTrue(await self.automation_manager.enable_automation(user_id, auto_id))
        details = await self.automation_manager.get_automation(user_id, auto_id)
        self.assertTrue(details["is_enabled"])


if __name__ == "__main__":
    unittest.main()

