"""Automations module implementing personal recurring automation systems."""

from src.automations.manager import AutomationManager
from src.automations.schemas import (
    AutomationScheduleConfig,
    ClientAcquisitionConfig,
    ContentEngineConfig,
    JobHunterConfig,
    NetworkingHunterConfig,
    NotificationPreferences,
)
from src.automations.templates import (
    AUTOMATION_TEMPLATES,
    BaseAutomationTemplate,
    ClientAcquisitionTemplate,
    ContentEngineTemplate,
    JobHunterTemplate,
    NetworkingHunterTemplate,
    get_template,
)
from src.automations.tools import get_automation_tools
from src.database.models.automation import AutomationFindingStatus, AutomationType

__all__ = [
    "AutomationType",
    "AutomationFindingStatus",
    "AutomationManager",
    "BaseAutomationTemplate",
    "JobHunterTemplate",
    "ClientAcquisitionTemplate",
    "ContentEngineTemplate",
    "NetworkingHunterTemplate",
    "AUTOMATION_TEMPLATES",
    "get_template",
    "get_automation_tools",
    "JobHunterConfig",
    "ClientAcquisitionConfig",
    "ContentEngineConfig",
    "NetworkingHunterConfig",
    "AutomationScheduleConfig",
    "NotificationPreferences",
]

