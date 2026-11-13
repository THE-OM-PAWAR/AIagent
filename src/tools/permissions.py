"""Permission levels and policy management for tools."""

from enum import Enum
import logging
from typing import Any
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class ToolPermission(str, Enum):
    """Permission levels required to invoke a tool."""
    READ_ONLY = "read_only"
    WRITE = "write"
    SENSITIVE = "sensitive"
    ADMIN = "admin"

    @property
    def rank(self) -> int:
        """Numeric rank for hierarchical permission checks."""
        _ranks = {
            ToolPermission.READ_ONLY: 1,
            ToolPermission.WRITE: 2,
            ToolPermission.SENSITIVE: 3,
            ToolPermission.ADMIN: 4,
        }
        return _ranks[self]

    def satisfies(self, required: "ToolPermission") -> bool:
        """Return True if this permission level can perform an action requiring `required`."""
        return self.rank >= required.rank


class PermissionPolicy(BaseModel):
    """Enforces access controls, agent whitelisting, and approval requirements."""

    default_user_permission: ToolPermission = ToolPermission.WRITE
    user_permissions: dict[int, ToolPermission] = Field(default_factory=dict)
    requires_approval_tools: set[str] = Field(default_factory=set)

    def get_user_permission(self, user_id: int) -> ToolPermission:
        """Get the granted permission level for a user ID."""
        return self.user_permissions.get(user_id, self.default_user_permission)

    def check_permission(
        self,
        user_id: int,
        tool_name: str,
        required_permission: ToolPermission,
        requires_approval: bool = False,
        agent_type: str | None = None,
        allowed_agents: list[str] | None = None,
        approval_granted: bool = False,
    ) -> tuple[bool, str | None]:
        """Verify whether an invocation is allowed under current policy.

        Returns:
            Tuple of (is_allowed, denial_reason).
        """
        # 1. Agent whitelisting check
        if allowed_agents and agent_type:
            allowed_lower = [a.lower() for a in allowed_agents]
            if agent_type.lower() not in allowed_lower:
                return (
                    False,
                    f"Tool '{tool_name}' is not accessible by agent '{agent_type}'. Allowed agents: {allowed_agents}",
                )

        # 2. User hierarchical permission check
        user_perm = self.get_user_permission(user_id)
        if not user_perm.satisfies(required_permission):
            return (
                False,
                f"User {user_id} with permission '{user_perm.value}' cannot execute tool '{tool_name}' requiring '{required_permission.value}'.",
            )

        # 3. Explicit approval check
        if (requires_approval or tool_name in self.requires_approval_tools) and not approval_granted:
            return (
                False,
                f"Tool '{tool_name}' requires human-in-the-loop approval before execution.",
            )

        return True, None

