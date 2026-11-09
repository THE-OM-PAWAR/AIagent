"""Agent Router for intent classification and deterministic role dispatching."""

import logging
import re
from typing import Any
from pydantic import BaseModel, Field

from src.agents.base import AgentType

logger = logging.getLogger(__name__)

# Deterministic regex patterns for intent routing
_ANALYST_PATTERNS = re.compile(
    r"\b(analy(?:ze|sis|tics)|compar(?:e|ison|ative)|versus|\bvs\b|trade[- ]?off|evaluat(?:e|ion)|"
    r"benchmark|pros and cons|feasibility|audit|risk assessment|architectur(?:e|al) review|"
    r"deep dive|strategic review|technical evaluation)\b",
    re.IGNORECASE,
)

_EXECUTOR_PATTERNS = re.compile(
    r"\b(plan(?:ning)?|roadmap|action(?:able)? plan|steps? to|break down|implementation steps?|"
    r"milestones?|\btodo\b|task list|blocker|track(?:ing)? progress|how to implement|how to build|"
    r"next steps?|work breakdown|execution plan|checklist)\b",
    re.IGNORECASE,
)

_GENERAL_GREETINGS = re.compile(
    r"^(hi|hello|hey|greetings|good morning|good evening|who are you|what can you do|help me)\b",
    re.IGNORECASE,
)


class AgentRoutingDecision(BaseModel):
    """Routing metadata explaining agent selection."""
    selected_agent: AgentType
    confidence: float = 1.0
    intent: str = "general"
    reasoning: str = ""
    is_ambiguous: bool = False


class AgentRouter:
    """Lightweight intent classification and agent selection router."""

    def __init__(self, default_agent: AgentType = AgentType.GENERAL) -> None:
        self.default_agent = default_agent

    def route_request(self, user_text: str, context_metadata: dict[str, Any] | None = None) -> AgentRoutingDecision:
        """Analyze query and select the most appropriate specialized agent."""
        text = user_text.strip()
        lower_text = text.lower()

        # 1. Quick greeting / small talk check -> General
        if _GENERAL_GREETINGS.search(lower_text) and len(text.split()) <= 8:
            return AgentRoutingDecision(
                selected_agent=AgentType.GENERAL,
                confidence=0.98,
                intent="greeting",
                reasoning="Matched casual greeting or brief general query.",
            )

        # 2. Check Analyst matches
        analyst_matches = len(_ANALYST_PATTERNS.findall(lower_text))

        # 3. Check Executor matches
        executor_matches = len(_EXECUTOR_PATTERNS.findall(lower_text))

        # 4. Deterministic decision resolution
        if analyst_matches > 0 and analyst_matches > executor_matches:
            return AgentRoutingDecision(
                selected_agent=AgentType.ANALYST,
                confidence=0.90,
                intent="complex_analysis",
                reasoning=f"Matched {analyst_matches} analytical keyword indicator(s).",
            )

        if executor_matches > 0 and executor_matches > analyst_matches:
            return AgentRoutingDecision(
                selected_agent=AgentType.EXECUTOR,
                confidence=0.90,
                intent="task_planning",
                reasoning=f"Matched {executor_matches} execution/planning keyword indicator(s).",
            )

        # 5. Tie or multiple matches
        if analyst_matches > 0 and executor_matches > 0:
            # If both match, look at dominant verbs
            if any(w in lower_text for w in ["plan", "step", "todo", "roadmap", "execute"]):
                return AgentRoutingDecision(
                    selected_agent=AgentType.EXECUTOR,
                    confidence=0.75,
                    intent="actionable_planning",
                    reasoning="Query contains both analysis and task planning; prioritized execution breakdown.",
                    is_ambiguous=True,
                )
            return AgentRoutingDecision(
                selected_agent=AgentType.ANALYST,
                confidence=0.75,
                intent="complex_analysis",
                reasoning="Query contains both analysis and task planning; prioritized technical evaluation.",
                is_ambiguous=True,
            )

        # 6. Default fallback to General Assistant
        return AgentRoutingDecision(
            selected_agent=self.default_agent,
            confidence=0.85,
            intent="general_assistance",
            reasoning="Default fallback to General Assistant for standard conversation.",
        )

