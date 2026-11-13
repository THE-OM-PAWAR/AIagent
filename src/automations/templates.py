"""Reusable automation templates defining multi-step workflow graphs for recurring execution."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from src.automations.schemas import (
    ClientAcquisitionConfig,
    ContentEngineConfig,
    JobHunterConfig,
    NetworkingHunterConfig,
)
from src.database.models.automation import AutomationType


class BaseAutomationTemplate(ABC):
    """Abstract base class for reusable automation templates."""

    @property
    @abstractmethod
    def automation_type(self) -> AutomationType:
        """Unique automation identifier enum."""
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable automation title."""
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        """Explanation of the recurring automation goal."""
        pass

    @abstractmethod
    def get_default_config(self) -> dict[str, Any]:
        """Return validated default configuration schema dictionary."""
        pass

    @abstractmethod
    def build_workflow_definition(
        self,
        user_id: int,
        config: dict[str, Any],
    ) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
        """Construct the workflow name, executable steps DAG, and initial context data.

        Returns:
            Tuple of (workflow_name, steps_list, initial_context_dict).
        """
        pass


# =============================================================================
# 1. Internship & Job Hunter Template
# =============================================================================

class JobHunterTemplate(BaseAutomationTemplate):
    """Recurring automation to discover, evaluate, deduplicate, and draft job applications."""

    @property
    def automation_type(self) -> AutomationType:
        return AutomationType.JOB_HUNTER

    @property
    def name(self) -> str:
        return "Internship & Job Hunter"

    @property
    def description(self) -> str:
        return (
            "Monitors AI, ML, and backend engineering listings, qualifies opportunities against user profile, "
            "deduplicates previous discoveries, drafts customized applications, and requires approval before dispatch."
        )

    def get_default_config(self) -> dict[str, Any]:
        return JobHunterConfig().model_dump()

    def build_workflow_definition(
        self,
        user_id: int,
        config: dict[str, Any],
    ) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
        validated = JobHunterConfig(**config)
        search_query = validated.search_queries[0] if validated.search_queries else "AI engineer internship remote 2026"

        workflow_name = f"Job Hunter: {validated.user_profile.name} ({validated.user_profile.experience_level})"

        initial_context = {
            "search_query": search_query,
            "user_profile": validated.user_profile.model_dump(),
            "exclude_keywords": validated.exclude_keywords,
            "min_qualification_score": validated.min_qualification_score,
            "outreach_channel": validated.outreach_channel,
            "max_results": validated.max_results_per_run,
        }

        steps = [
            {
                "name": "job_search",
                "tool_name": "web_search",
                "arguments": {
                    "query": "{context.search_query}",
                    "max_results": "{context.max_results}",
                    "topic": "general",
                },
                "dependencies": [],
                "max_retries": 1,
                "timeout_seconds": 30,
            },
            {
                "name": "evaluate_jobs",
                "tool_name": "job_hunter_evaluate",
                "arguments": {
                    "search_results": "{job_search.output}",
                    "profile": "{context.user_profile}",
                    "exclude_keywords": "{context.exclude_keywords}",
                    "min_score": "{context.min_qualification_score}",
                },
                "dependencies": ["job_search"],
                "max_retries": 1,
                "timeout_seconds": 30,
            },
            {
                "name": "prepare_applications",
                "tool_name": "job_hunter_prepare_application",
                "arguments": {
                    "qualified_jobs": "{evaluate_jobs.output.qualified_jobs}",
                    "profile": "{context.user_profile}",
                    "outreach_channel": "{context.outreach_channel}",
                },
                "dependencies": ["evaluate_jobs"],
                "condition": {"field": "evaluate_jobs.output.qualified_count", "operator": "gt", "value": 0},
                "max_retries": 1,
                "timeout_seconds": 30,
            },
            {
                "name": "save_opportunities",
                "tool_name": "job_hunter_save_opportunities",
                "arguments": {
                    "applications": "{prepare_applications.output.applications}",
                },
                "dependencies": ["prepare_applications"],
                "condition": {"field": "evaluate_jobs.output.qualified_count", "operator": "gt", "value": 0},
                "max_retries": 1,
                "timeout_seconds": 30,
            },
        ]

        return workflow_name, steps, initial_context


# =============================================================================
# 2. Client Acquisition Engine Template
# =============================================================================

class ClientAcquisitionTemplate(BaseAutomationTemplate):
    """Recurring automation to discover prospective software clients, evaluate needs, and draft proposals."""

    @property
    def automation_type(self) -> AutomationType:
        return AutomationType.CLIENT_ACQUISITION

    @property
    def name(self) -> str:
        return "Client Acquisition Engine"

    @property
    def description(self) -> str:
        return (
            "Discovers prospective businesses via Google Places or web search, identifies digital & software needs, "
            "deduplicates previous leads, prepares customized proposals, and saves research for follow-ups."
        )

    def get_default_config(self) -> dict[str, Any]:
        return ClientAcquisitionConfig().model_dump()

    def build_workflow_definition(
        self,
        user_id: int,
        config: dict[str, Any],
    ) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
        validated = ClientAcquisitionConfig(**config)

        workflow_name = f"Client Acquisition: {validated.criteria.category} in {validated.criteria.location}"

        initial_context = {
            "criteria": validated.criteria.model_dump(),
            "service_offerings": validated.service_offerings,
            "sender_name": validated.sender_name,
            "portfolio_summary": validated.portfolio_summary,
            "max_leads": validated.max_leads_per_run,
        }

        # Select discovery tool based on source preference
        if validated.discovery_source == "places":
            discovery_step = {
                "name": "discover_leads",
                "tool_name": "places_discover_leads",
                "arguments": {
                    "category": "{context.criteria.category}",
                    "location": "{context.criteria.location}",
                    "min_rating": "{context.criteria.min_rating}",
                    "max_results": "{context.max_leads}",
                },
                "dependencies": [],
                "max_retries": 1,
                "timeout_seconds": 30,
            }
        else:
            search_q = f"{validated.criteria.category} businesses in {validated.criteria.location}"
            initial_context["search_query"] = search_q
            discovery_step = {
                "name": "discover_leads",
                "tool_name": "web_search",
                "arguments": {
                    "query": "{context.search_query}",
                    "max_results": "{context.max_leads}",
                    "topic": "general",
                },
                "dependencies": [],
                "max_retries": 1,
                "timeout_seconds": 30,
            }

        steps = [
            discovery_step,
            {
                "name": "qualify_leads",
                "tool_name": "client_acquisition_qualify",
                "arguments": {
                    "leads_data": "{discover_leads.output}",
                    "criteria": "{context.criteria}",
                    "min_rating": "{context.criteria.min_rating}",
                },
                "dependencies": ["discover_leads"],
                "max_retries": 1,
                "timeout_seconds": 30,
            },
            {
                "name": "draft_proposals",
                "tool_name": "client_acquisition_draft_outreach",
                "arguments": {
                    "qualified_leads": "{qualify_leads.output.qualified_leads}",
                    "services": "{context.service_offerings}",
                    "sender_name": "{context.sender_name}",
                },
                "dependencies": ["qualify_leads"],
                "condition": {"field": "qualify_leads.output.qualified_count", "operator": "gt", "value": 0},
                "max_retries": 1,
                "timeout_seconds": 30,
            },
            {
                "name": "save_leads",
                "tool_name": "client_acquisition_save_leads",
                "arguments": {
                    "proposals": "{draft_proposals.output.proposals}",
                },
                "dependencies": ["draft_proposals"],
                "condition": {"field": "qualify_leads.output.qualified_count", "operator": "gt", "value": 0},
                "max_retries": 1,
                "timeout_seconds": 30,
            },
        ]

        return workflow_name, steps, initial_context


# =============================================================================
# 3. Personal Brand & Content Engine Template
# =============================================================================

class ContentEngineTemplate(BaseAutomationTemplate):
    """Recurring automation to research tech developments, curate insights, and draft weekly content calendars."""

    @property
    def automation_type(self) -> AutomationType:
        return AutomationType.CONTENT_ENGINE

    @property
    def name(self) -> str:
        return "Personal Brand & Content Engine"

    @property
    def description(self) -> str:
        return (
            "Monitors technical breakthroughs and system architectures, curates verified facts, "
            "avoids unsupported claims, prepares a weekly LinkedIn content calendar, and requires approval before publishing."
        )

    def get_default_config(self) -> dict[str, Any]:
        return ContentEngineConfig().model_dump()

    def build_workflow_definition(
        self,
        user_id: int,
        config: dict[str, Any],
    ) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
        validated = ContentEngineConfig(**config)
        topic_query = validated.research_topics[0] if validated.research_topics else "AI agent architecture 2026"

        workflow_name = f"Content Engine: {validated.content_pillars[0] if validated.content_pillars else 'Tech Insights'}"

        initial_context = {
            "topic_query": topic_query,
            "content_pillars": validated.content_pillars,
            "target_audience": validated.target_audience,
            "posts_per_week": validated.posts_per_week,
        }

        steps = [
            {
                "name": "research_topics",
                "tool_name": "web_search",
                "arguments": {
                    "query": "{context.topic_query}",
                    "max_results": 8,
                    "topic": "news",
                },
                "dependencies": [],
                "max_retries": 1,
                "timeout_seconds": 30,
            },
            {
                "name": "curate_insights",
                "tool_name": "content_engine_curate",
                "arguments": {
                    "search_results": "{research_topics.output}",
                    "content_pillars": "{context.content_pillars}",
                },
                "dependencies": ["research_topics"],
                "max_retries": 1,
                "timeout_seconds": 30,
            },
            {
                "name": "generate_calendar",
                "tool_name": "content_engine_generate",
                "arguments": {
                    "curated_insights": "{curate_insights.output.curated_insights}",
                    "target_audience": "{context.target_audience}",
                    "posts_count": "{context.posts_per_week}",
                },
                "dependencies": ["curate_insights"],
                "condition": {"field": "curate_insights.output.curated_count", "operator": "gt", "value": 0},
                "max_retries": 1,
                "timeout_seconds": 30,
            },
            {
                "name": "save_calendar",
                "tool_name": "content_engine_save_drafts",
                "arguments": {
                    "calendar": "{generate_calendar.output.calendar}",
                },
                "dependencies": ["generate_calendar"],
                "condition": {"field": "curate_insights.output.curated_count", "operator": "gt", "value": 0},
                "max_retries": 1,
                "timeout_seconds": 30,
            },
            {
                "name": "publish_approval",
                "tool_name": "content_engine_publish_approval",
                "arguments": {
                    "action": "publish_weekly_calendar",
                    "platform": "linkedin",
                },
                "dependencies": ["save_calendar"],
                "condition": {"field": "curate_insights.output.curated_count", "operator": "gt", "value": 0},
                "max_retries": 1,
                "timeout_seconds": 30,
            },
        ]

        return workflow_name, steps, initial_context


# =============================================================================
# 4. Networking & Opportunity Hunter Template
# =============================================================================

class NetworkingHunterTemplate(BaseAutomationTemplate):
    """Recurring automation to discover tech events, research organizers/founders, and prepare intro notes."""

    @property
    def automation_type(self) -> AutomationType:
        return AutomationType.NETWORKING_HUNTER

    @property
    def name(self) -> str:
        return "Networking & Opportunity Hunter"

    @property
    def description(self) -> str:
        return (
            "Discovers startup hackathons, founder meetups, and developer conferences, qualifies events by interest, "
            "deduplicates previous discoveries, drafts personalized connection messages, and saves interaction history."
        )

    def get_default_config(self) -> dict[str, Any]:
        return NetworkingHunterConfig().model_dump()

    def build_workflow_definition(
        self,
        user_id: int,
        config: dict[str, Any],
    ) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
        validated = NetworkingHunterConfig(**config)
        event_query = (
            validated.event_search_queries[0]
            if validated.event_search_queries
            else "tech startup hackathons founder meetups 2026"
        )

        workflow_name = f"Networking: {', '.join(validated.preferences.target_interests[:2])}"

        initial_context = {
            "event_query": event_query,
            "target_interests": validated.preferences.target_interests,
            "user_profile": validated.preferences.model_dump(),
            "max_events": validated.max_events_per_run,
        }

        steps = [
            {
                "name": "discover_events",
                "tool_name": "web_search",
                "arguments": {
                    "query": "{context.event_query}",
                    "max_results": "{context.max_events}",
                    "topic": "general",
                },
                "dependencies": [],
                "max_retries": 1,
                "timeout_seconds": 30,
            },
            {
                "name": "qualify_events",
                "tool_name": "networking_hunter_qualify",
                "arguments": {
                    "search_results": "{discover_events.output}",
                    "target_interests": "{context.target_interests}",
                },
                "dependencies": ["discover_events"],
                "max_retries": 1,
                "timeout_seconds": 30,
            },
            {
                "name": "prepare_outreach",
                "tool_name": "networking_hunter_prepare_outreach",
                "arguments": {
                    "qualified_events": "{qualify_events.output.qualified_events}",
                    "user_profile": "{context.user_profile}",
                },
                "dependencies": ["qualify_events"],
                "condition": {"field": "qualify_events.output.qualified_count", "operator": "gt", "value": 0},
                "max_retries": 1,
                "timeout_seconds": 30,
            },
            {
                "name": "save_contacts",
                "tool_name": "networking_hunter_save_contacts",
                "arguments": {
                    "networking_outreaches": "{prepare_outreach.output.networking_outreaches}",
                },
                "dependencies": ["prepare_outreach"],
                "condition": {"field": "qualify_events.output.qualified_count", "operator": "gt", "value": 0},
                "max_retries": 1,
                "timeout_seconds": 30,
            },
        ]

        return workflow_name, steps, initial_context


# Registry of templates
AUTOMATION_TEMPLATES: dict[str, BaseAutomationTemplate] = {
    AutomationType.JOB_HUNTER.value: JobHunterTemplate(),
    AutomationType.CLIENT_ACQUISITION.value: ClientAcquisitionTemplate(),
    AutomationType.CONTENT_ENGINE.value: ContentEngineTemplate(),
    AutomationType.NETWORKING_HUNTER.value: NetworkingHunterTemplate(),
}


def get_template(automation_type: str) -> BaseAutomationTemplate:
    """Retrieve template definition by automation type string."""
    tmpl = AUTOMATION_TEMPLATES.get(automation_type)
    if not tmpl:
        raise ValueError(f"Unknown automation type '{automation_type}'. Available: {list(AUTOMATION_TEMPLATES.keys())}")
    return tmpl

