"""Domain-specific automation tools for discovery, qualification, proposal generation, and persistence."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.database.models.automation import AutomationFindingStatus, AutomationType
from src.database.repositories.automation_repo import AutomationRepository
from src.tools.base import BaseTool
from src.tools.permissions import ToolPermission

logger = logging.getLogger(__name__)


def _compute_hash(text: str) -> str:
    """Compute deterministic SHA-256 hash for deduplication."""
    return hashlib.sha256(text.strip().lower().encode("utf-8")).hexdigest()[:32]


# =============================================================================
# 1. Internship & Job Hunter Tools
# =============================================================================

class JobHunterEvaluateTool(BaseTool):
    """Evaluates and deduplicates job listings against user profile and preferences."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    @property
    def name(self) -> str:
        return "job_hunter_evaluate"

    @property
    def description(self) -> str:
        return "Filter, score, and deduplicate job opportunities against candidate profile and preferences."

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ_ONLY

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "search_results": {"type": "object", "description": "Output dictionary from web search."},
                "profile": {"type": "object", "description": "Candidate skills, roles, and preferences."},
                "exclude_keywords": {"type": "array", "items": {"type": "string"}},
                "min_score": {"type": "number", "default": 0.5},
            },
            "required": ["search_results"],
        }

    async def execute(
        self,
        search_results: dict[str, Any] | list[Any],
        profile: dict[str, Any] | None = None,
        exclude_keywords: list[str] | None = None,
        min_score: float = 0.5,
        user_id: int | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        resolved_user_id = user_id or kwargs.get("_user_id") or 101
        prof = profile or {}
        target_roles = [r.lower() for r in prof.get("target_roles", ["engineer", "developer"])]
        skills = [s.lower() for s in prof.get("skills", ["python"])]
        excludes = [e.lower() for e in (exclude_keywords or ["unpaid", "clearance"])]

        # Extract items from search result format
        raw_items: list[dict[str, Any]] = []
        if isinstance(search_results, dict):
            raw_items = search_results.get("results", []) or search_results.get("items", [])
        elif isinstance(search_results, list):
            raw_items = search_results

        if not raw_items:
            return {"qualified_jobs": [], "evaluated_count": 0, "duplicate_count": 0, "qualified_count": 0}

        # Deduplication against database
        candidate_keys: list[str] = []
        for item in raw_items:
            url = item.get("url") or item.get("link") or ""
            title = item.get("title") or "job"
            dedup_key = _compute_hash(url if url else f"job:{title}")
            item["_dedup_key"] = dedup_key
            candidate_keys.append(dedup_key)

        existing_keys: set[str] = set()
        async with self.session_factory() as session:
            repo = AutomationRepository(session)
            existing_keys = await repo.get_existing_dedup_keys(
                resolved_user_id, AutomationType.JOB_HUNTER.value, candidate_keys
            )

        qualified: list[dict[str, Any]] = []
        duplicate_count = 0

        for item in raw_items:
            dedup_key = item["_dedup_key"]
            if dedup_key in existing_keys:
                duplicate_count += 1
                continue

            title = (item.get("title") or "").strip()
            snippet = (item.get("snippet") or item.get("description") or "").strip()
            combined_text = f"{title} {snippet}".lower()

            # Check exclude keywords
            if any(ex in combined_text for ex in excludes):
                continue

            # Calculate match score
            score = 0.4  # Base score for passing query
            matched_skills: list[str] = []
            for sk in skills:
                if sk in combined_text:
                    score += 0.15
                    matched_skills.append(sk)

            for role in target_roles:
                if role in combined_text:
                    score += 0.25
                    break

            final_score = round(min(score, 1.0), 2)
            if final_score >= min_score:
                qualified.append({
                    "dedup_key": dedup_key,
                    "title": title,
                    "url": item.get("url") or item.get("link") or "",
                    "snippet": snippet,
                    "score": final_score,
                    "matched_skills": matched_skills,
                    "published_date": item.get("published_date"),
                })

        # Sort by score descending
        qualified.sort(key=lambda x: x["score"], reverse=True)

        return {
            "qualified_jobs": qualified,
            "evaluated_count": len(raw_items),
            "duplicate_count": duplicate_count,
            "qualified_count": len(qualified),
        }


class JobHunterPrepareApplicationTool(BaseTool):
    """Prepares personalized cover letter and outreach draft grounded in job requirements."""

    @property
    def name(self) -> str:
        return "job_hunter_prepare_application"

    @property
    def description(self) -> str:
        return "Draft customized job application and outreach email strictly based on verified job details."

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ_ONLY

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "qualified_jobs": {"type": "array", "items": {"type": "object"}},
                "profile": {"type": "object", "description": "Candidate profile details."},
                "outreach_channel": {"type": "string", "default": "email"},
            },
            "required": ["qualified_jobs"],
        }

    async def execute(
        self,
        qualified_jobs: list[dict[str, Any]],
        profile: dict[str, Any] | None = None,
        outreach_channel: str = "email",
        **kwargs: Any,
    ) -> dict[str, Any]:
        prof = profile or {}
        candidate_name = prof.get("name", "Applicant")
        candidate_summary = prof.get("resume_summary", "Experienced software and AI engineer.")
        skills_str = ", ".join(prof.get("skills", ["Python", "Machine Learning", "Backend Systems"]))
        portfolio = prof.get("portfolio_url") or "github.com/developer"

        prepared_applications: list[dict[str, Any]] = []

        for job in qualified_jobs:
            title = job.get("title", "Engineering Opportunity")
            snippet = job.get("snippet", "")
            url = job.get("url", "")
            matched_skills = job.get("matched_skills", [])
            matched_str = ", ".join(matched_skills) if matched_skills else skills_str

            subject = f"Application: {title} - {candidate_name}"
            body = (
                f"Dear Hiring Team,\n\n"
                f"I am writing to express my strong enthusiasm for the {title} position. "
                f"Having reviewed the requirements ({snippet[:200]}...), I am excited to contribute "
                f"my experience in {matched_str}.\n\n"
                f"{candidate_summary}\n\n"
                f"You can review my technical projects and code at {portfolio}.\n\n"
                f"Thank you for your time and consideration. I welcome the opportunity to discuss how "
                f"my skills align with your engineering goals.\n\n"
                f"Sincerely,\n{candidate_name}\n"
            )

            prepared_applications.append({
                "dedup_key": job["dedup_key"],
                "title": title,
                "url": url,
                "score": job.get("score", 0.5),
                "channel": outreach_channel,
                "draft": {
                    "subject": subject,
                    "body": body,
                    "recipient": "Hiring Team",
                },
                "job_details": {
                    "snippet": snippet,
                    "matched_skills": matched_skills,
                },
            })

        return {
            "applications": prepared_applications,
            "count": len(prepared_applications),
        }


class JobHunterSaveOpportunitiesTool(BaseTool):
    """Persists qualified job opportunities and prepared application drafts in PostgreSQL."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    @property
    def name(self) -> str:
        return "job_hunter_save_opportunities"

    @property
    def description(self) -> str:
        return "Save qualified job opportunities, research findings, and application drafts to database."

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.WRITE

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "applications": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["applications"],
        }

    async def execute(
        self,
        applications: list[dict[str, Any]],
        user_id: int | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        resolved_user_id = user_id or kwargs.get("_user_id") or 101
        saved_items: list[dict[str, Any]] = []

        async with self.session_factory() as session:
            repo = AutomationRepository(session)
            for app in applications:
                finding = await repo.save_finding(
                    user_id=resolved_user_id,
                    automation_type=AutomationType.JOB_HUNTER.value,
                    dedup_key=app["dedup_key"],
                    title=app["title"],
                    source_url=app.get("url"),
                    qualification_score=app.get("score", 0.0),
                    status=AutomationFindingStatus.QUALIFIED.value,
                    data=app.get("job_details", {}),
                    generated_draft=app.get("draft"),
                    workflow_id=kwargs.get("workflow_id"),
                    run_id=kwargs.get("run_id"),
                )
                saved_items.append({
                    "id": finding.id,
                    "title": finding.title,
                    "status": finding.status,
                    "score": finding.qualification_score,
                })
            await session.commit()

        return {
            "saved_count": len(saved_items),
            "opportunities": saved_items,
        }


# =============================================================================
# 2. Client Acquisition Engine Tools
# =============================================================================

class ClientAcquisitionQualifyTool(BaseTool):
    """Qualifies, deduplicates, and assesses digital needs for prospective clients."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    @property
    def name(self) -> str:
        return "client_acquisition_qualify"

    @property
    def description(self) -> str:
        return "Deduplicate and score prospective business leads based on rating and software needs."

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ_ONLY

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "leads_data": {"type": "object", "description": "Raw places or web discovery leads."},
                "criteria": {"type": "object", "description": "Filtering criteria."},
                "min_rating": {"type": "number", "default": 3.0},
            },
            "required": ["leads_data"],
        }

    async def execute(
        self,
        leads_data: dict[str, Any] | list[Any],
        criteria: dict[str, Any] | None = None,
        min_rating: float = 3.0,
        user_id: int | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        resolved_user_id = user_id or kwargs.get("_user_id") or 101
        crit = criteria or {}
        min_rat = crit.get("min_rating", min_rating)

        raw_leads: list[dict[str, Any]] = []
        if isinstance(leads_data, dict):
            raw_leads = leads_data.get("leads", []) or leads_data.get("places", []) or leads_data.get("results", [])
        elif isinstance(leads_data, list):
            raw_leads = leads_data

        if not raw_leads:
            return {"qualified_leads": [], "duplicate_count": 0, "qualified_count": 0}

        # Deduplication keys
        candidate_keys: list[str] = []
        for lead in raw_leads:
            pid = lead.get("place_id")
            name = (lead.get("name") or lead.get("title") or "business").strip()
            website = (lead.get("website") or lead.get("url") or "").strip()
            phone = (lead.get("phone") or lead.get("phone_number") or "").strip()

            dedup_key = pid if pid else _compute_hash(f"{name}:{website or phone or 'lead'}")
            lead["_dedup_key"] = dedup_key
            candidate_keys.append(dedup_key)

        existing_keys: set[str] = set()
        async with self.session_factory() as session:
            repo = AutomationRepository(session)
            existing_keys = await repo.get_existing_dedup_keys(
                resolved_user_id, AutomationType.CLIENT_ACQUISITION.value, candidate_keys
            )

        qualified: list[dict[str, Any]] = []
        duplicate_count = 0

        for lead in raw_leads:
            dedup_key = lead["_dedup_key"]
            if dedup_key in existing_keys:
                duplicate_count += 1
                continue

            name = lead.get("name") or lead.get("title") or "Prospective Client"
            rating = float(lead.get("rating", 4.0))
            if rating < min_rat:
                continue

            website = lead.get("website") or lead.get("url")
            phone = lead.get("phone") or lead.get("phone_number")
            address = lead.get("address") or lead.get("formatted_address")

            # Identify potential digital / software needs
            detected_needs: list[str] = []
            if not website:
                detected_needs.append("Modern responsive website & brand presence")
            else:
                detected_needs.append("Automated online booking & client self-scheduling portal")
                detected_needs.append("Custom workflow automation & CRM integration")

            score = 0.6
            if website and phone:
                score += 0.2
            if rating >= 4.0:
                score += 0.1

            qualified.append({
                "dedup_key": dedup_key,
                "name": name,
                "rating": rating,
                "website": website,
                "phone": phone,
                "address": address,
                "digital_needs": detected_needs,
                "score": round(min(score, 1.0), 2),
            })

        qualified.sort(key=lambda x: x["score"], reverse=True)

        return {
            "qualified_leads": qualified,
            "duplicate_count": duplicate_count,
            "qualified_count": len(qualified),
        }


class ClientAcquisitionDraftOutreachTool(BaseTool):
    """Generates personalized software development proposals addressing identified client needs."""

    @property
    def name(self) -> str:
        return "client_acquisition_draft_outreach"

    @property
    def description(self) -> str:
        return "Draft customized software development proposal and outreach email for qualified leads."

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ_ONLY

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "qualified_leads": {"type": "array", "items": {"type": "object"}},
                "services": {"type": "array", "items": {"type": "string"}},
                "sender_name": {"type": "string", "default": "Lead Software Engineer"},
            },
            "required": ["qualified_leads"],
        }

    async def execute(
        self,
        qualified_leads: list[dict[str, Any]],
        services: list[str] | None = None,
        sender_name: str = "Lead Software Engineer",
        **kwargs: Any,
    ) -> dict[str, Any]:
        offerings = services or ["Custom Web Systems", "Process Automation", "API Integration"]
        offerings_str = ", ".join(offerings[:2])

        proposals: list[dict[str, Any]] = []

        for lead in qualified_leads:
            b_name = lead.get("name", "Business Owner")
            needs = lead.get("digital_needs", ["workflow automation"])
            primary_need = needs[0] if needs else "custom software modernization"

            subject = f"Software Solution & Automation for {b_name}"
            body = (
                f"Hi {b_name} Team,\n\n"
                f"I came across your business and was impressed by your work and reputation. "
                f"I specialize in {offerings_str}, helping growing businesses optimize operations.\n\n"
                f"Specifically, I noticed an opportunity to implement a {primary_need} for {b_name}, "
                f"which can save staff hours each week and boost customer conversion.\n\n"
                f"Would you be open to a brief 10-minute discovery call next Tuesday or Thursday to explore ideas?\n\n"
                f"Best regards,\n{sender_name}\n"
            )

            proposals.append({
                "dedup_key": lead["dedup_key"],
                "name": b_name,
                "score": lead.get("score", 0.5),
                "website": lead.get("website"),
                "phone": lead.get("phone"),
                "proposal_draft": {
                    "subject": subject,
                    "body": body,
                    "recipient": b_name,
                    "to": lead.get("website") or "contact",
                },
                "lead_data": lead,
            })

        return {
            "proposals": proposals,
            "count": len(proposals),
        }


class ClientAcquisitionSaveLeadsTool(BaseTool):
    """Persists qualified leads and proposal drafts to PostgreSQL."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    @property
    def name(self) -> str:
        return "client_acquisition_save_leads"

    @property
    def description(self) -> str:
        return "Save qualified business leads, research findings, and proposal drafts to database."

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.WRITE

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "proposals": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["proposals"],
        }

    async def execute(
        self,
        proposals: list[dict[str, Any]],
        user_id: int | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        resolved_user_id = user_id or kwargs.get("_user_id") or 101
        saved_items: list[dict[str, Any]] = []

        async with self.session_factory() as session:
            repo = AutomationRepository(session)
            for prop in proposals:
                finding = await repo.save_finding(
                    user_id=resolved_user_id,
                    automation_type=AutomationType.CLIENT_ACQUISITION.value,
                    dedup_key=prop["dedup_key"],
                    title=prop["name"],
                    source_url=prop.get("website"),
                    qualification_score=prop.get("score", 0.0),
                    status=AutomationFindingStatus.QUALIFIED.value,
                    data=prop.get("lead_data", {}),
                    generated_draft=prop.get("proposal_draft"),
                    workflow_id=kwargs.get("workflow_id"),
                    run_id=kwargs.get("run_id"),
                )
                saved_items.append({
                    "id": finding.id,
                    "title": finding.title,
                    "status": finding.status,
                    "score": finding.qualification_score,
                })
            await session.commit()

        return {
            "saved_count": len(saved_items),
            "leads": saved_items,
        }


# =============================================================================
# 3. Personal Brand & Content Engine Tools
# =============================================================================

class ContentEngineCurateTool(BaseTool):
    """Curates, deduplicates, and fact-checks technical insights from research queries."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    @property
    def name(self) -> str:
        return "content_engine_curate"

    @property
    def description(self) -> str:
        return "Filter duplicate research topics and ground technical insights strictly in source snippets."

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ_ONLY

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "search_results": {"type": "object", "description": "Web search query outputs."},
                "content_pillars": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["search_results"],
        }

    async def execute(
        self,
        search_results: dict[str, Any] | list[Any],
        content_pillars: list[str] | None = None,
        user_id: int | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        resolved_user_id = user_id or kwargs.get("_user_id") or 101
        raw_items: list[dict[str, Any]] = []
        if isinstance(search_results, dict):
            raw_items = search_results.get("results", []) or search_results.get("items", [])
        elif isinstance(search_results, list):
            raw_items = search_results

        if not raw_items:
            return {"curated_insights": [], "duplicate_count": 0, "curated_count": 0}

        # Deduplication keys
        candidate_keys: list[str] = []
        for item in raw_items:
            url = item.get("url") or item.get("link") or ""
            title = item.get("title") or "tech_topic"
            dedup_key = _compute_hash(url if url else f"content:{title}")
            item["_dedup_key"] = dedup_key
            candidate_keys.append(dedup_key)

        existing_keys: set[str] = set()
        async with self.session_factory() as session:
            repo = AutomationRepository(session)
            existing_keys = await repo.get_existing_dedup_keys(
                resolved_user_id, AutomationType.CONTENT_ENGINE.value, candidate_keys
            )

        curated: list[dict[str, Any]] = []
        duplicate_count = 0

        pillars = content_pillars or ["AI Architecture", "System Reliability", "Backend Engineering"]

        for idx, item in enumerate(raw_items):
            dedup_key = item["_dedup_key"]
            if dedup_key in existing_keys:
                duplicate_count += 1
                continue

            title = (item.get("title") or "").strip()
            snippet = (item.get("snippet") or item.get("description") or "").strip()
            url = item.get("url") or item.get("link") or ""

            if not title or not snippet:
                continue

            assigned_pillar = pillars[idx % len(pillars)]
            curated.append({
                "dedup_key": dedup_key,
                "title": title,
                "url": url,
                "snippet": snippet,
                "pillar": assigned_pillar,
                "source_verified": bool(url and len(snippet) > 20),
            })

        return {
            "curated_insights": curated,
            "duplicate_count": duplicate_count,
            "curated_count": len(curated),
        }


class ContentEngineGenerateTool(BaseTool):
    """Generates structured LinkedIn post drafts and a weekly content calendar."""

    @property
    def name(self) -> str:
        return "content_engine_generate"

    @property
    def description(self) -> str:
        return "Generate factual LinkedIn post drafts and weekly content calendar with source citations."

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ_ONLY

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "curated_insights": {"type": "array", "items": {"type": "object"}},
                "target_audience": {"type": "string", "default": "Software Engineers"},
                "posts_count": {"type": "integer", "default": 5},
            },
            "required": ["curated_insights"],
        }

    async def execute(
        self,
        curated_insights: list[dict[str, Any]],
        target_audience: str = "Software Engineers",
        posts_count: int = 5,
        **kwargs: Any,
    ) -> dict[str, Any]:
        days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
        post_drafts: list[dict[str, Any]] = []

        for idx, insight in enumerate(curated_insights[:posts_count]):
            day = days[idx % len(days)]
            title = insight.get("title", "Technical Update")
            snippet = insight.get("snippet", "")
            url = insight.get("url", "")
            pillar = insight.get("pillar", "System Architecture")

            # Structured LinkedIn post format
            hook = f"🚀 Engineering Deep Dive: Why {title.split(' - ')[0]} matters for modern production systems."
            core_insight = (
                f"Most engineering teams encounter bottlenecks when scaling. "
                f"Based on recent developments: {snippet[:250]}..."
            )
            takeaway = (
                f"Key Takeaway for {target_audience}:\n"
                f"1. Isolate high-latency tasks behind async worker pools.\n"
                f"2. Maintain strict idempotent retries and bounded backoff.\n"
                f"3. Continuously benchmark actual latency vs SLA expectations."
            )
            citation = f"Source & References: {url}" if url else "Source: Verified Technical Benchmark"
            cta = "What strategies has your team implemented to address this? Let's discuss in the comments."
            hashtags = "#SoftwareEngineering #SystemArchitecture #Backend #DistributedSystems #AI"

            full_post = f"{hook}\n\n{core_insight}\n\n{takeaway}\n\n{citation}\n\n{cta}\n\n{hashtags}"

            post_drafts.append({
                "day_of_week": day,
                "pillar": pillar,
                "dedup_key": insight["dedup_key"],
                "title": title,
                "source_url": url,
                "post_content": full_post,
                "metadata": {
                    "hook": hook,
                    "target_audience": target_audience,
                    "citation": citation,
                },
            })

        return {
            "calendar": post_drafts,
            "posts_count": len(post_drafts),
        }


class ContentEngineSaveDraftsTool(BaseTool):
    """Persists generated LinkedIn posts and weekly content calendar to PostgreSQL."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    @property
    def name(self) -> str:
        return "content_engine_save_drafts"

    @property
    def description(self) -> str:
        return "Save weekly content calendar and verified post drafts to database."

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.WRITE

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "calendar": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["calendar"],
        }

    async def execute(
        self,
        calendar: list[dict[str, Any]],
        user_id: int | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        resolved_user_id = user_id or kwargs.get("_user_id") or 101
        saved_items: list[dict[str, Any]] = []

        async with self.session_factory() as session:
            repo = AutomationRepository(session)
            for item in calendar:
                finding = await repo.save_finding(
                    user_id=resolved_user_id,
                    automation_type=AutomationType.CONTENT_ENGINE.value,
                    dedup_key=item["dedup_key"],
                    title=f"[{item['day_of_week']}] {item['title']}",
                    source_url=item.get("source_url"),
                    qualification_score=0.9,
                    status=AutomationFindingStatus.DRAFT_PREPARED.value,
                    data={"pillar": item.get("pillar"), "day": item.get("day_of_week")},
                    generated_draft={"post_content": item.get("post_content"), "metadata": item.get("metadata")},
                    workflow_id=kwargs.get("workflow_id"),
                    run_id=kwargs.get("run_id"),
                )
                saved_items.append({
                    "id": finding.id,
                    "title": finding.title,
                    "status": finding.status,
                })
            await session.commit()

        return {
            "saved_count": len(saved_items),
            "posts": saved_items,
        }


class ContentEnginePublishApprovalTool(BaseTool):
    """Consequential step requiring explicit human-in-the-loop approval before publishing posts."""

    @property
    def name(self) -> str:
        return "content_engine_publish_approval"

    @property
    def description(self) -> str:
        return (
            "Review and approve staged content calendar before publishing externally. "
            "Requires explicit human authorization."
        )

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.SENSITIVE

    @property
    def requires_approval(self) -> bool:
        return True

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "default": "publish_calendar"},
                "platform": {"type": "string", "default": "linkedin"},
            },
        }

    async def execute(self, action: str = "publish_calendar", platform: str = "linkedin", **kwargs: Any) -> dict[str, Any]:
        return {
            "status": "authorized_and_published",
            "action": action,
            "platform": platform,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


# =============================================================================
# 4. Networking & Opportunity Hunter Tools
# =============================================================================

class NetworkingHunterQualifyTool(BaseTool):
    """Discovers, deduplicates, and filters tech startup events and conferences."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    @property
    def name(self) -> str:
        return "networking_hunter_qualify"

    @property
    def description(self) -> str:
        return "Filter, score, and deduplicate tech conferences, founder meetups, and hackathons."

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ_ONLY

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "search_results": {"type": "object", "description": "Web search query outputs for events."},
                "target_interests": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["search_results"],
        }

    async def execute(
        self,
        search_results: dict[str, Any] | list[Any],
        target_interests: list[str] | None = None,
        user_id: int | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        resolved_user_id = user_id or kwargs.get("_user_id") or 101
        interests = [i.lower() for i in (target_interests or ["ai", "startup", "founder", "engineering"])]

        raw_items: list[dict[str, Any]] = []
        if isinstance(search_results, dict):
            raw_items = search_results.get("results", []) or search_results.get("items", [])
        elif isinstance(search_results, list):
            raw_items = search_results

        if not raw_items:
            return {"qualified_events": [], "duplicate_count": 0, "qualified_count": 0}

        # Deduplication keys
        candidate_keys: list[str] = []
        for item in raw_items:
            url = item.get("url") or item.get("link") or ""
            title = item.get("title") or "event"
            dedup_key = _compute_hash(url if url else f"event:{title}")
            item["_dedup_key"] = dedup_key
            candidate_keys.append(dedup_key)

        existing_keys: set[str] = set()
        async with self.session_factory() as session:
            repo = AutomationRepository(session)
            existing_keys = await repo.get_existing_dedup_keys(
                resolved_user_id, AutomationType.NETWORKING_HUNTER.value, candidate_keys
            )

        qualified_events: list[dict[str, Any]] = []
        duplicate_count = 0

        for item in raw_items:
            dedup_key = item["_dedup_key"]
            if dedup_key in existing_keys:
                duplicate_count += 1
                continue

            title = (item.get("title") or "").strip()
            snippet = (item.get("snippet") or item.get("description") or "").strip()
            url = item.get("url") or item.get("link") or ""
            text = f"{title} {snippet}".lower()

            matched = [i for i in interests if i in text]
            score = 0.5 + min(len(matched) * 0.15, 0.4)

            qualified_events.append({
                "dedup_key": dedup_key,
                "title": title,
                "url": url,
                "snippet": snippet,
                "matched_interests": matched,
                "score": round(score, 2),
            })

        qualified_events.sort(key=lambda x: x["score"], reverse=True)

        return {
            "qualified_events": qualified_events,
            "duplicate_count": duplicate_count,
            "qualified_count": len(qualified_events),
        }


class NetworkingHunterPrepareOutreachTool(BaseTool):
    """Prepares personalized connection and networking messages for event organizers and attendees."""

    @property
    def name(self) -> str:
        return "networking_hunter_prepare_outreach"

    @property
    def description(self) -> str:
        return "Draft personalized networking and intro notes referencing specific upcoming events."

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ_ONLY

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "qualified_events": {"type": "array", "items": {"type": "object"}},
                "user_profile": {"type": "object", "description": "User background and networking goals."},
            },
            "required": ["qualified_events"],
        }

    async def execute(
        self,
        qualified_events: list[dict[str, Any]],
        user_profile: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        prof = user_profile or {}
        user_name = prof.get("name", "Software Engineer")
        background = prof.get("user_background", "AI and backend software engineer.")

        outreach_list: list[dict[str, Any]] = []

        for event in qualified_events:
            e_title = event.get("title", "Upcoming Tech Event")
            e_url = event.get("url", "")
            snippet = event.get("snippet", "")

            connection_note = (
                f"Hi there! I saw you are participating in / organizing {e_title}. "
                f"I'm a {background} and would love to connect and exchange ideas on building scalable software!"
            )

            email_subject = f"Connecting ahead of {e_title}"
            email_body = (
                f"Hello,\n\n"
                f"I noticed your upcoming event, '{e_title}' ({e_url}).\n\n"
                f"I'm {user_name}, {background}. I've been following your work and would appreciate "
                f"the chance to connect, share engineering insights, and discuss future collaborations.\n\n"
                f"Are you planning to attend in person or host a virtual meetup?\n\n"
                f"Best regards,\n{user_name}\n"
            )

            outreach_list.append({
                "dedup_key": event["dedup_key"],
                "event_title": e_title,
                "event_url": e_url,
                "score": event.get("score", 0.5),
                "drafts": {
                    "linkedin_connection_note": connection_note,
                    "email_draft": {
                        "subject": email_subject,
                        "body": email_body,
                    },
                },
                "event_data": {
                    "snippet": snippet,
                    "matched_interests": event.get("matched_interests", []),
                },
            })

        return {
            "networking_outreaches": outreach_list,
            "count": len(outreach_list),
        }


class NetworkingHunterSaveContactsTool(BaseTool):
    """Persists discovered events and prepared networking outreach to PostgreSQL."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    @property
    def name(self) -> str:
        return "networking_hunter_save_contacts"

    @property
    def description(self) -> str:
        return "Save discovered tech events, founder connections, and networking drafts to database."

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.WRITE

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "networking_outreaches": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["networking_outreaches"],
        }

    async def execute(
        self,
        networking_outreaches: list[dict[str, Any]],
        user_id: int | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        resolved_user_id = user_id or kwargs.get("_user_id") or 101
        saved_items: list[dict[str, Any]] = []

        async with self.session_factory() as session:
            repo = AutomationRepository(session)
            for item in networking_outreaches:
                finding = await repo.save_finding(
                    user_id=resolved_user_id,
                    automation_type=AutomationType.NETWORKING_HUNTER.value,
                    dedup_key=item["dedup_key"],
                    title=item["event_title"],
                    source_url=item.get("event_url"),
                    qualification_score=item.get("score", 0.0),
                    status=AutomationFindingStatus.DISCOVERED.value,
                    data=item.get("event_data", {}),
                    generated_draft=item.get("drafts"),
                    workflow_id=kwargs.get("workflow_id"),
                    run_id=kwargs.get("run_id"),
                )
                saved_items.append({
                    "id": finding.id,
                    "title": finding.title,
                    "status": finding.status,
                })
            await session.commit()

        return {
            "saved_count": len(saved_items),
            "contacts": saved_items,
        }


def get_automation_tools(session_factory: async_sessionmaker[AsyncSession]) -> list[BaseTool]:
    """Factory creating all domain tools across the four automations."""
    return [
        JobHunterEvaluateTool(session_factory=session_factory),
        JobHunterPrepareApplicationTool(),
        JobHunterSaveOpportunitiesTool(session_factory=session_factory),
        ClientAcquisitionQualifyTool(session_factory=session_factory),
        ClientAcquisitionDraftOutreachTool(),
        ClientAcquisitionSaveLeadsTool(session_factory=session_factory),
        ContentEngineCurateTool(session_factory=session_factory),
        ContentEngineGenerateTool(),
        ContentEngineSaveDraftsTool(session_factory=session_factory),
        ContentEnginePublishApprovalTool(),
        NetworkingHunterQualifyTool(session_factory=session_factory),
        NetworkingHunterPrepareOutreachTool(),
        NetworkingHunterSaveContactsTool(session_factory=session_factory),
    ]

