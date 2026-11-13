"""Pydantic validation schemas for core automation configurations."""

from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, Field


class ScheduleTriggerConfig(BaseModel):
    """Trigger configuration parameters for recurring schedules."""

    interval_seconds: int | None = None
    time: str | None = None  # e.g. "09:00"
    day_of_week: str | None = None  # e.g. "mon", "fri"
    cron_expression: str | None = None


class AutomationScheduleConfig(BaseModel):
    """Schedule and execution policy settings for an automation."""

    trigger_type: Literal["one_time", "interval", "daily", "weekly", "cron"] = "daily"
    trigger_config: dict[str, Any] = Field(default_factory=lambda: {"time": "09:00"})
    timezone: str = "UTC"
    catch_up_policy: Literal["run_once", "skip_all", "run_all"] = "run_once"
    allow_overlap: bool = False
    max_concurrency: int = 1
    max_consecutive_failures: int | None = 3
    run_timeout_seconds: int | None = 180
    api_call_budget: int | None = 15
    max_runs: int | None = None


class NotificationPreferences(BaseModel):
    """User alert preferences for automation events."""

    notify_on_run_complete: bool = True
    notify_on_run_failure: bool = True
    notify_on_approval_needed: bool = True
    notify_on_new_findings: bool = True


# -----------------------------------------------------------------------------
# 1. Internship & Job Hunter Configuration
# -----------------------------------------------------------------------------

class UserJobProfile(BaseModel):
    """Candidate profile and qualifications for job matching."""

    name: str = "Engineer"
    target_roles: list[str] = Field(default_factory=lambda: ["AI Engineer", "ML Engineer", "Backend Engineer"])
    skills: list[str] = Field(default_factory=lambda: ["Python", "FastAPI", "PyTorch", "SQL", "LLM", "Docker"])
    experience_level: str = "Intern / Junior"
    preferred_locations: list[str] = Field(default_factory=lambda: ["Remote", "Hybrid"])
    min_salary: int | None = None
    portfolio_url: str | None = None
    resume_summary: str = (
        "Backend & AI engineer with experience building scalable distributed systems, "
        "production LLM workflows, and REST/gRPC microservices."
    )
    contact_email: str | None = None


class JobHunterConfig(BaseModel):
    """Configuration for Internship & Job Hunter Automation."""

    search_queries: list[str] = Field(
        default_factory=lambda: [
            "AI engineer internship remote 2026",
            "backend engineering intern python 2026",
            "machine learning engineer junior remote",
        ]
    )
    user_profile: UserJobProfile = Field(default_factory=UserJobProfile)
    exclude_keywords: list[str] = Field(
        default_factory=lambda: ["senior", "lead", "staff", "principal", "director", "unpaid", "clearance"]
    )
    min_qualification_score: float = 0.6
    max_results_per_run: int = 5
    outreach_channel: Literal["email", "cover_letter", "linkedin_dm"] = "email"
    approval_required: bool = True
    schedule: AutomationScheduleConfig = Field(
        default_factory=lambda: AutomationScheduleConfig(
            trigger_type="daily",
            trigger_config={"time": "09:00"},
            timezone="UTC",
            api_call_budget=12,
        )
    )
    notification_preferences: NotificationPreferences = Field(default_factory=NotificationPreferences)


# -----------------------------------------------------------------------------
# 2. Client Acquisition Engine Configuration
# -----------------------------------------------------------------------------

class ClientLeadCriteria(BaseModel):
    """Criteria for filtering and scoring business leads."""

    category: str = "software development"
    location: str = "Austin, TX"
    min_rating: float = 3.5
    require_contact_info: bool = True
    target_digital_needs: list[str] = Field(
        default_factory=lambda: [
            "custom web applications",
            "process automation",
            "AI assistant integration",
            "CRM & database modernization",
            "booking system",
        ]
    )


class ClientAcquisitionConfig(BaseModel):
    """Configuration for Client Acquisition Engine."""

    discovery_source: Literal["places", "web_search"] = "places"
    criteria: ClientLeadCriteria = Field(default_factory=ClientLeadCriteria)
    service_offerings: list[str] = Field(
        default_factory=lambda: [
            "Full-Stack Web & Mobile App Development",
            "Custom AI Automation & Agent Systems",
            "API & Third-party Integrations",
            "Cloud Infrastructure & Backend Modernization",
        ]
    )
    portfolio_summary: str = (
        "Experienced software consultancy delivering robust, automated web and backend systems."
    )
    sender_name: str = "Om Patel"
    contact_email: str | None = None
    max_leads_per_run: int = 5
    approval_required: bool = True
    schedule: AutomationScheduleConfig = Field(
        default_factory=lambda: AutomationScheduleConfig(
            trigger_type="weekly",
            trigger_config={"days_of_week": ["mon"], "time": "10:00"},
            timezone="UTC",
            api_call_budget=15,
        )
    )
    notification_preferences: NotificationPreferences = Field(default_factory=NotificationPreferences)


# -----------------------------------------------------------------------------
# 3. Personal Brand & Content Engine Configuration
# -----------------------------------------------------------------------------

class ContentEngineConfig(BaseModel):
    """Configuration for Personal Brand & Content Engine."""

    research_topics: list[str] = Field(
        default_factory=lambda: [
            "AI agent systems production architecture",
            "Python async backend performance best practices",
            "LLM evaluation techniques in production",
            "Autonomous developer tooling 2026",
        ]
    )
    content_pillars: list[str] = Field(
        default_factory=lambda: [
            "AI Architecture & System Design",
            "Backend Engineering & Reliability",
            "Startup Engineering & Builder Insights",
        ]
    )
    target_audience: str = "Software Engineers, Tech Leads, Startup Founders"
    tone_of_voice: Literal["technical_authoritative", "practical_guide", "founder_insights"] = "technical_authoritative"
    posts_per_week: int = 5
    avoid_unsupported_claims: bool = True
    require_citations: bool = True
    approval_required: bool = True
    schedule: AutomationScheduleConfig = Field(
        default_factory=lambda: AutomationScheduleConfig(
            trigger_type="weekly",
            trigger_config={"days_of_week": ["sun"], "time": "18:00"},
            timezone="UTC",
            api_call_budget=10,
        )
    )
    notification_preferences: NotificationPreferences = Field(default_factory=NotificationPreferences)


# -----------------------------------------------------------------------------
# 4. Networking & Opportunity Hunter Configuration
# -----------------------------------------------------------------------------

class NetworkingPreferences(BaseModel):
    """User targeting preferences for events and professional connections."""

    target_interests: list[str] = Field(
        default_factory=lambda: ["AI/ML", "Startups", "Distributed Systems", "Venture Capital"]
    )
    target_roles: list[str] = Field(
        default_factory=lambda: ["Founder", "Co-Founder", "CTO", "Lead Engineer", "Investor", "Organizer"]
    )
    preferred_locations: list[str] = Field(default_factory=lambda: ["Remote", "San Francisco", "New York"])
    user_background: str = (
        "AI & Backend Software Engineer passionate about autonomous agents, high-concurrency systems, "
        "and early-stage technology ventures."
    )
    networking_goal: str = (
        "Connect with technical founders and builders to exchange engineering insights and collaborate."
    )


class NetworkingHunterConfig(BaseModel):
    """Configuration for Networking & Opportunity Hunter Automation."""

    event_search_queries: list[str] = Field(
        default_factory=lambda: [
            "tech startup hackathons founder meetups 2026",
            "AI developer conference demo day 2026",
            "engineering mixer networking founders",
        ]
    )
    preferences: NetworkingPreferences = Field(default_factory=NetworkingPreferences)
    max_events_per_run: int = 4
    approval_required: bool = True
    schedule: AutomationScheduleConfig = Field(
        default_factory=lambda: AutomationScheduleConfig(
            trigger_type="weekly",
            trigger_config={"days_of_week": ["thu"], "time": "14:00"},
            timezone="UTC",
            api_call_budget=12,
        )
    )
    notification_preferences: NotificationPreferences = Field(default_factory=NotificationPreferences)
