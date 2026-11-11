"""Tool for initiating, inspecting, pausing, resuming, and managing persistent workflows."""

import logging
from typing import Any

from src.core.workflow_engine import WorkflowEngine
from src.tools.base import BaseTool
from src.tools.permissions import ToolPermission

logger = logging.getLogger(__name__)


class WorkflowControlTool(BaseTool):
    """Tool exposing persistent multi-step workflow control and state inspection to the AI agent."""

    def __init__(self, workflow_engine: WorkflowEngine) -> None:
        self.workflow_engine = workflow_engine

    @property
    def name(self) -> str:
        return "workflow_control"

    @property
    def description(self) -> str:
        return (
            "Create, start, inspect, pause, resume, and cancel persistent multi-step agent workflows. "
            "Maintains execution checkpoints in PostgreSQL, tracks execution history, and recovers safely "
            "after application restarts without duplicate step execution."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "Workflow action to perform.",
                    "enum": [
                        "create_workflow",
                        "start_workflow",
                        "get_status",
                        "pause_workflow",
                        "resume_workflow",
                        "cancel_workflow",
                        "list_workflows",
                    ],
                },
                "workflow_id": {
                    "type": "string",
                    "description": "UUID of the target workflow for status, pause, resume, cancel, or start.",
                },
                "name": {
                    "type": "string",
                    "description": "Descriptive workflow name for action='create_workflow'.",
                },
                "description": {
                    "type": "string",
                    "description": "Optional workflow description.",
                },
                "steps": {
                    "type": "array",
                    "description": "Ordered list of step definitions for action='create_workflow'.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "Step identifier."},
                            "tool_name": {"type": "string", "description": "Registered tool name to invoke."},
                            "arguments": {"type": "object", "description": "Tool parameters schema with optional {context.key} substitutions."},
                            "max_retries": {"type": "integer", "description": "Maximum retry attempts upon failure."},
                        },
                        "required": ["name", "tool_name", "arguments"],
                    },
                },
                "context_data": {
                    "type": "object",
                    "description": "Initial key-value context variables for action='create_workflow'.",
                },
                "user_id": {
                    "type": "integer",
                    "description": "Telegram user ID owning the workflow.",
                },
                "status_filter": {
                    "type": "string",
                    "description": "Optional status filter for action='list_workflows'.",
                    "enum": ["pending", "running", "waiting_for_approval", "paused", "completed", "failed", "cancelled"],
                },
            },
            "required": ["action"],
        }

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.WRITE

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        action = kwargs.get("action", "get_status")
        user_id = kwargs.get("user_id", 0)

        if action == "create_workflow":
            name = kwargs.get("name")
            if not name:
                raise ValueError("The 'name' parameter is required for action='create_workflow'.")
            steps = kwargs.get("steps") or []
            if not steps:
                raise ValueError("A workflow must have at least one step in 'steps'.")
            ctx = kwargs.get("context_data") or {}
            desc = kwargs.get("description")

            created = await self.workflow_engine.create_workflow(
                user_id=user_id,
                name=name,
                steps=steps,
                context_data=ctx,
                description=desc,
            )
            return {
                "action": "create_workflow",
                "success": True,
                "workflow": created,
                "message": f"Workflow '{name}' created with {len(steps)} steps. Ready to start.",
            }

        elif action == "start_workflow":
            wf_id = kwargs.get("workflow_id")
            if not wf_id:
                raise ValueError("The 'workflow_id' parameter is required for action='start_workflow'.")
            res = await self.workflow_engine.execute_workflow(workflow_id=wf_id, user_id=user_id)
            return {
                "action": "start_workflow",
                "result": res,
            }

        elif action == "get_status":
            wf_id = kwargs.get("workflow_id")
            if not wf_id:
                raise ValueError("The 'workflow_id' parameter is required for action='get_status'.")
            status = await self.workflow_engine.get_status(workflow_id=wf_id, user_id=user_id)
            return {
                "action": "get_status",
                "workflow": status,
            }

        elif action == "pause_workflow":
            wf_id = kwargs.get("workflow_id")
            if not wf_id:
                raise ValueError("The 'workflow_id' parameter is required for action='pause_workflow'.")
            success = await self.workflow_engine.pause_workflow(workflow_id=wf_id, user_id=user_id)
            return {
                "action": "pause_workflow",
                "workflow_id": wf_id,
                "paused": success,
            }

        elif action == "resume_workflow":
            wf_id = kwargs.get("workflow_id")
            if not wf_id:
                raise ValueError("The 'workflow_id' parameter is required for action='resume_workflow'.")
            res = await self.workflow_engine.execute_workflow(workflow_id=wf_id, user_id=user_id)
            return {
                "action": "resume_workflow",
                "result": res,
            }

        elif action == "cancel_workflow":
            wf_id = kwargs.get("workflow_id")
            if not wf_id:
                raise ValueError("The 'workflow_id' parameter is required for action='cancel_workflow'.")
            success = await self.workflow_engine.cancel_workflow(workflow_id=wf_id, user_id=user_id)
            return {
                "action": "cancel_workflow",
                "workflow_id": wf_id,
                "cancelled": success,
            }

        elif action == "list_workflows":
            s_filter = kwargs.get("status_filter")
            wfs = await self.workflow_engine.list_workflows(user_id=user_id, status=s_filter)
            return {
                "action": "list_workflows",
                "count": len(wfs),
                "workflows": wfs,
            }

        raise ValueError(f"Unknown action '{action}' for workflow_control.")

