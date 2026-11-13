"""Workflow Manager and validation engine enforcing structured workflow rules, acyclic dependencies, and tool access."""

import logging
import re
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.database.repositories.workflow_repo import WorkflowRepository
from src.tools.permissions import PermissionPolicy
from src.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

STEP_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_\-]+$")

VALID_CONDITION_OPERATORS = {
    "eq", "==", "neq", "!=", "gt", ">", "gte", ">=", "lt", "<", "lte", "<=",
    "in", "not_in", "exists", "not_exists", "truthy", "falsy", "contains"
}


class WorkflowValidationError(ValueError):
    """Raised when a workflow definition fails structural, schema, or permission validation."""
    pass


class WorkflowManager:
    """Manages workflow definitions, structural validation, DAG dependency checking, and persistence."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tool_registry: ToolRegistry | None = None,
        permission_policy: PermissionPolicy | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.tool_registry = tool_registry
        self.permission_policy = permission_policy

    def validate_workflow_definition(
        self,
        name: str,
        steps: list[dict[str, Any]],
        user_id: int | None = None,
        context_data: dict[str, Any] | None = None,
    ) -> None:
        """Validate workflow definition against naming, schema, tool registry, and acyclic DAG constraints.

        Raises:
            WorkflowValidationError: If any validation rule is violated.
        """
        if not name or not isinstance(name, str) or not name.strip():
            raise WorkflowValidationError("Workflow name must be a non-empty string.")
        if len(name) > 255:
            raise WorkflowValidationError("Workflow name must not exceed 255 characters.")

        if not steps or not isinstance(steps, list):
            raise WorkflowValidationError("Workflow must contain a non-empty list of 'steps'.")

        seen_step_names: set[str] = set()
        step_names_ordered: list[str] = []

        for idx, step in enumerate(steps):
            if not isinstance(step, dict):
                raise WorkflowValidationError(f"Step at index {idx} must be a dictionary.")

            step_name = step.get("name")
            if not step_name or not isinstance(step_name, str):
                raise WorkflowValidationError(f"Step at index {idx} is missing a valid 'name'.")
            step_name = step_name.strip()

            if not STEP_NAME_PATTERN.match(step_name):
                raise WorkflowValidationError(
                    f"Step name '{step_name}' is invalid. Must contain only alphanumeric characters, underscores, or hyphens."
                )

            if step_name in seen_step_names:
                raise WorkflowValidationError(f"Duplicate step name '{step_name}' detected. Step names must be unique.")
            seen_step_names.add(step_name)
            step_names_ordered.append(step_name)

            # Tool existence and permission verification
            tool_name = step.get("tool_name")
            if not tool_name or not isinstance(tool_name, str):
                raise WorkflowValidationError(f"Step '{step_name}' is missing a valid 'tool_name'.")

            if self.tool_registry is not None:
                tool_def = self.tool_registry.get_definition(tool_name)
                if not tool_def:
                    raise WorkflowValidationError(
                        f"Step '{step_name}' references unknown tool '{tool_name}'. Tool is not registered."
                    )

                # Tool permission policy check if user_id provided
                if self.permission_policy is not None and user_id is not None:
                    # At definition time, verify user meets base permission rank; execution-time approval checks occur on run
                    allowed, reason = self.permission_policy.check_permission(
                        user_id=user_id,
                        tool_name=tool_def.name,
                        required_permission=tool_def.permission,
                        requires_approval=False,
                    )
                    if not allowed and "approval" not in (reason or "").lower():
                        raise WorkflowValidationError(
                            f"Step '{step_name}' requires tool '{tool_name}' which user {user_id} lacks permission to execute: {reason}"
                        )

            # Arguments / input template
            args = step.get("arguments") if "arguments" in step else step.get("input_template", {})
            if args is not None and not isinstance(args, dict):
                raise WorkflowValidationError(f"Step '{step_name}' arguments must be a dictionary.")

            # Dependencies verification
            deps = step.get("dependencies", [])
            if not isinstance(deps, list):
                raise WorkflowValidationError(f"Step '{step_name}' dependencies must be a list of step names.")

            for dep in deps:
                if not isinstance(dep, str):
                    raise WorkflowValidationError(f"Step '{step_name}' dependency '{dep}' must be a string.")
                if dep == step_name:
                    raise WorkflowValidationError(f"Step '{step_name}' cannot depend on itself.")
                # Verify dependency refers to a preceding step in sequence
                if dep not in seen_step_names:
                    raise WorkflowValidationError(
                        f"Step '{step_name}' depends on '{dep}', which does not precede it. Forward references and cycles are forbidden."
                    )

            # Condition verification
            condition = step.get("condition")
            if condition is not None:
                if not isinstance(condition, dict):
                    raise WorkflowValidationError(f"Step '{step_name}' condition must be a dictionary.")
                op = str(condition.get("operator", "")).lower().strip()
                if op not in VALID_CONDITION_OPERATORS:
                    raise WorkflowValidationError(
                        f"Step '{step_name}' condition operator '{op}' is unsupported. Valid operators: {sorted(VALID_CONDITION_OPERATORS)}"
                    )
                field = condition.get("field")
                if not field or not isinstance(field, str):
                    raise WorkflowValidationError(f"Step '{step_name}' condition is missing a valid 'field' string.")

            # Bounds verification
            retries = step.get("max_retries", 1)
            if not isinstance(retries, int) or retries < 0 or retries > 10:
                raise WorkflowValidationError(f"Step '{step_name}' max_retries must be an integer between 0 and 10.")

            timeout = step.get("timeout_seconds", 60)
            if not isinstance(timeout, (int, float)) or timeout <= 0 or timeout > 300:
                raise WorkflowValidationError(f"Step '{step_name}' timeout_seconds must be between 1 and 300.")

        if context_data is not None and not isinstance(context_data, dict):
            raise WorkflowValidationError("Initial 'context_data' must be a dictionary.")

    async def create_workflow(
        self,
        user_id: int,
        name: str,
        steps: list[dict[str, Any]],
        context_data: dict[str, Any] | None = None,
        description: str | None = None,
        is_one_time: bool = True,
    ) -> dict[str, Any]:
        """Validate and persist a workflow definition and its normalized steps."""
        self.validate_workflow_definition(
            name=name,
            steps=steps,
            user_id=user_id,
            context_data=context_data,
        )

        async with self.session_factory() as session:
            repo = WorkflowRepository(session)
            wf = await repo.create_workflow(
                user_id=user_id,
                name=name,
                steps=steps,
                context_data=context_data,
                description=description,
                is_one_time=is_one_time,
            )
            return {
                "workflow_id": wf.id,
                "name": wf.name,
                "description": wf.description,
                "status": wf.status,
                "total_steps": wf.total_steps,
                "is_one_time": wf.is_one_time,
                "created_at": wf.created_at.isoformat(),
            }

    async def get_workflow(self, workflow_id: str, user_id: int | None = None) -> dict[str, Any] | None:
        """Retrieve workflow definition with its steps and recent runs."""
        async with self.session_factory() as session:
            repo = WorkflowRepository(session)
            wf = await repo.get_workflow(workflow_id, user_id)
            if not wf:
                return None
            return {
                "workflow_id": wf.id,
                "user_id": wf.user_id,
                "name": wf.name,
                "description": wf.description,
                "status": wf.status,
                "is_one_time": wf.is_one_time,
                "current_step_index": wf.current_step_index,
                "total_steps": wf.total_steps,
                "steps": [
                    {
                        "step_order": s.step_order,
                        "name": s.name,
                        "tool_name": s.tool_name,
                        "arguments": s.input_template,
                        "dependencies": s.dependencies,
                        "condition": s.condition,
                        "status": s.status,
                        "max_retries": s.max_retries,
                        "timeout_seconds": s.timeout_seconds,
                    }
                    for s in (wf.step_definitions or [])
                ] or wf.steps,
                "context_data": wf.context_data,
                "execution_history": wf.execution_history,
                "checkpoint_data": wf.checkpoint_data,
                "error_message": wf.error_message,
                "created_at": wf.created_at.isoformat(),
                "completed_at": wf.completed_at.isoformat() if wf.completed_at else None,
            }

    async def list_workflows(
        self, user_id: int, status: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        """List workflows for a user."""
        async with self.session_factory() as session:
            repo = WorkflowRepository(session)
            wfs = await repo.list_workflows(user_id=user_id, status=status, limit=limit)
            return [
                {
                    "workflow_id": w.id,
                    "name": w.name,
                    "description": w.description,
                    "status": w.status,
                    "is_one_time": w.is_one_time,
                    "current_step_index": w.current_step_index,
                    "total_steps": w.total_steps,
                    "created_at": w.created_at.isoformat(),
                    "completed_at": w.completed_at.isoformat() if w.completed_at else None,
                }
                for w in wfs
            ]
