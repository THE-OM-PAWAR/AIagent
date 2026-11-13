"""Tool for initiating, planning, inspecting, pausing, resuming, and managing persistent workflows."""

import logging
from typing import Any

from src.core.workflow_engine import WorkflowEngine
from src.tools.base import BaseTool
from src.tools.permissions import ToolPermission

logger = logging.getLogger(__name__)


class WorkflowControlTool(BaseTool):
    """Tool exposing persistent multi-step workflow control and state inspection to the AI agent."""

    def __init__(
        self,
        workflow_engine: WorkflowEngine,
        workflow_planner: Any = None,
        workflow_scheduler: Any = None,
    ) -> None:
        self.workflow_engine = workflow_engine
        self.workflow_planner = workflow_planner or getattr(workflow_engine, "workflow_planner", None)
        self.workflow_scheduler = workflow_scheduler or getattr(workflow_engine, "workflow_scheduler", None)

    @property
    def name(self) -> str:
        return "workflow_control"

    @property
    def description(self) -> str:
        return (
            "Create, start, inspect, pause, resume, cancel, or plan persistent multi-step agent workflows. "
            "Allows decomposing high-level goals into sequential and conditional tool calls with checkpoints in PostgreSQL, "
            "tracking execution history, and recovering safely without duplicate step execution."
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
                        "plan_workflow",
                        "plan_and_execute_workflow",
                        "get_status",
                        "pause_workflow",
                        "resume_workflow",
                        "cancel_workflow",
                        "list_workflows",
                        "list_approvals",
                        "approve_request",
                        "reject_request",
                        "schedule_workflow",
                        "list_schedules",
                        "pause_schedule",
                        "resume_schedule",
                        "cancel_schedule",
                    ],
                },
                "workflow_id": {
                    "type": "string",
                    "description": "UUID of the target workflow for status, pause, resume, cancel, or start.",
                },
                "schedule_id": {
                    "type": "string",
                    "description": "UUID of the schedule for pause_schedule, resume_schedule, or cancel_schedule.",
                },
                "trigger_type": {
                    "type": "string",
                    "description": "Type of trigger for schedule_workflow: one_time, interval, daily, weekly, cron.",
                    "enum": ["one_time", "interval", "daily", "weekly", "cron"],
                },
                "trigger_config": {
                    "type": "object",
                    "description": "Configuration dict for the trigger (e.g. {'interval_seconds': 3600}, {'time': '09:00'}, {'cron_expression': '0 9 * * 1-5'}).",
                },
                "timezone": {
                    "type": "string",
                    "description": "Target timezone for schedules (e.g. 'UTC', 'America/New_York', 'Asia/Kolkata'). Defaults to 'UTC'.",
                },
                "catch_up_policy": {
                    "type": "string",
                    "description": "Catch-up policy if runs were missed during downtime: run_once, skip_all, run_all.",
                    "enum": ["run_once", "skip_all", "run_all"],
                },
                "max_runs": {
                    "type": "integer",
                    "description": "Maximum total runs before schedule automatically completes.",
                },
                "approval_id": {
                    "type": "string",
                    "description": "UUID of the approval request for approve_request or reject_request.",
                },
                "notes": {
                    "type": "string",
                    "description": "Optional notes or reason for approval or rejection.",
                },
                "name": {
                    "type": "string",
                    "description": "Descriptive workflow name for action='create_workflow'.",
                },
                "description": {
                    "type": "string",
                    "description": "Optional workflow description.",
                },
                "request": {
                    "type": "string",
                    "description": "Natural language request or goal for action='plan_workflow' or 'plan_and_execute_workflow'.",
                },
                "is_one_time": {
                    "type": "boolean",
                    "description": "Whether the workflow is one-time (default true).",
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
                            "dependencies": {"type": "array", "items": {"type": "string"}, "description": "Prerequisite step names."},
                            "condition": {"type": "object", "description": "Conditional rule: {field, operator, value}."},
                            "max_retries": {"type": "integer", "description": "Maximum retry attempts upon failure."},
                            "timeout_seconds": {"type": "integer", "description": "Execution timeout in seconds."},
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
            is_one_time = kwargs.get("is_one_time", True)

            created = await self.workflow_engine.create_workflow(
                user_id=user_id,
                name=name,
                steps=steps,
                context_data=ctx,
                description=desc,
                is_one_time=is_one_time,
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

        elif action == "plan_workflow":
            req_text = kwargs.get("request") or kwargs.get("name")
            if not req_text:
                raise ValueError("The 'request' parameter is required for action='plan_workflow'.")
            if not self.workflow_planner:
                raise RuntimeError("Workflow planner is not configured for automatic workflow generation.")
            planned = await self.workflow_planner.plan_workflow(user_id=user_id, natural_language_request=req_text)
            return {
                "action": "plan_workflow",
                "success": True,
                "workflow": planned,
                "message": f"Successfully planned workflow '{planned['name']}' from natural language request.",
            }

        elif action == "plan_and_execute_workflow":
            req_text = kwargs.get("request") or kwargs.get("name")
            if not req_text:
                raise ValueError("The 'request' parameter is required for action='plan_and_execute_workflow'.")
            if not self.workflow_planner:
                raise RuntimeError("Workflow planner is not configured for automatic workflow execution.")
            result = await self.workflow_planner.plan_and_execute(user_id=user_id, natural_language_request=req_text)
            return {
                "action": "plan_and_execute_workflow",
                "success": True,
                "result": result,
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

        elif action == "list_approvals":
            approvals = await self.workflow_engine.approval_manager.list_pending_approvals(user_id=user_id)
            return {
                "action": "list_approvals",
                "count": len(approvals),
                "approvals": approvals,
            }

        elif action == "approve_request":
            appr_id = kwargs.get("approval_id")
            if not appr_id:
                raise ValueError("The 'approval_id' parameter is required for action='approve_request'.")
            notes = kwargs.get("notes")
            approved = await self.workflow_engine.approval_manager.approve_request(
                approval_id=appr_id,
                user_id=user_id,
                resolution_notes=notes,
            )
            # Resume workflow execution following approval
            resume_result = await self.workflow_engine.resume_approved_workflow(
                approval_id=appr_id,
                user_id=user_id,
            )
            return {
                "action": "approve_request",
                "approval": approved,
                "execution_result": resume_result,
            }

        elif action == "reject_request":
            appr_id = kwargs.get("approval_id")
            if not appr_id:
                raise ValueError("The 'approval_id' parameter is required for action='reject_request'.")
            notes = kwargs.get("notes")
            rejected = await self.workflow_engine.approval_manager.reject_request(
                approval_id=appr_id,
                user_id=user_id,
                resolution_notes=notes,
            )
            return {
                "action": "reject_request",
                "approval": rejected,
                "message": f"Approval request '{appr_id}' rejected successfully.",
            }

        elif action == "schedule_workflow":
            if not self.workflow_scheduler:
                raise ValueError("WorkflowScheduler is not configured.")
            wf_id = kwargs.get("workflow_id")
            if not wf_id:
                raise ValueError("The 'workflow_id' parameter is required for action='schedule_workflow'.")
            t_type = kwargs.get("trigger_type")
            if not t_type:
                raise ValueError("The 'trigger_type' parameter is required for action='schedule_workflow'.")
            t_cfg = kwargs.get("trigger_config") or {}
            sch_name = kwargs.get("name") or f"Schedule for {wf_id[:8]}"
            tz_str = kwargs.get("timezone") or "UTC"
            desc = kwargs.get("description")
            c_policy = kwargs.get("catch_up_policy") or "run_once"
            m_runs = kwargs.get("max_runs")
            a_overlap = kwargs.get("allow_overlap", False)
            m_conc = kwargs.get("max_concurrency", 1)
            m_fails = kwargs.get("max_consecutive_failures")
            r_timeout = kwargs.get("run_timeout_seconds")
            b_budget = kwargs.get("api_call_budget")
            e_policy = kwargs.get("execution_policy") or {}

            sch = await self.workflow_scheduler.schedule_workflow(
                workflow_id=wf_id,
                user_id=user_id,
                name=sch_name,
                trigger_type=t_type,
                trigger_config=t_cfg,
                timezone_str=tz_str,
                description=desc,
                catch_up_policy=c_policy,
                max_runs=m_runs,
                allow_overlap=a_overlap,
                max_concurrency=m_conc,
                max_consecutive_failures=m_fails,
                run_timeout_seconds=r_timeout,
                api_call_budget=b_budget,
                execution_policy=e_policy,
            )
            return {
                "action": "schedule_workflow",
                "schedule_id": sch.id,
                "workflow_id": sch.workflow_id,
                "name": sch.name,
                "trigger_type": sch.trigger_type,
                "timezone": sch.timezone,
                "status": sch.status,
                "next_run_at": sch.next_run_at.isoformat() if sch.next_run_at else None,
            }

        elif action == "list_schedules":
            if not self.workflow_scheduler:
                raise ValueError("WorkflowScheduler is not configured.")
            s_filter = kwargs.get("status_filter")
            schedules = await self.workflow_scheduler.list_schedules(user_id=user_id, status=s_filter)
            return {
                "action": "list_schedules",
                "count": len(schedules),
                "schedules": [
                    {
                        "id": s.id,
                        "workflow_id": s.workflow_id,
                        "name": s.name,
                        "trigger_type": s.trigger_type,
                        "timezone": s.timezone,
                        "status": s.status,
                        "next_run_at": s.next_run_at.isoformat() if s.next_run_at else None,
                        "last_run_at": s.last_run_at.isoformat() if s.last_run_at else None,
                        "total_runs_count": s.total_runs_count,
                    }
                    for s in schedules
                ],
            }

        elif action == "pause_schedule":
            if not self.workflow_scheduler:
                raise ValueError("WorkflowScheduler is not configured.")
            sch_id = kwargs.get("schedule_id")
            if not sch_id:
                raise ValueError("The 'schedule_id' parameter is required for action='pause_schedule'.")
            success = await self.workflow_scheduler.pause_schedule(sch_id, user_id=user_id)
            return {
                "action": "pause_schedule",
                "schedule_id": sch_id,
                "paused": success,
            }

        elif action == "resume_schedule":
            if not self.workflow_scheduler:
                raise ValueError("WorkflowScheduler is not configured.")
            sch_id = kwargs.get("schedule_id")
            if not sch_id:
                raise ValueError("The 'schedule_id' parameter is required for action='resume_schedule'.")
            success = await self.workflow_scheduler.resume_schedule(sch_id, user_id=user_id)
            return {
                "action": "resume_schedule",
                "schedule_id": sch_id,
                "resumed": success,
            }

        elif action == "cancel_schedule":
            if not self.workflow_scheduler:
                raise ValueError("WorkflowScheduler is not configured.")
            sch_id = kwargs.get("schedule_id")
            if not sch_id:
                raise ValueError("The 'schedule_id' parameter is required for action='cancel_schedule'.")
            success = await self.workflow_scheduler.cancel_schedule(sch_id, user_id=user_id)
            return {
                "action": "cancel_schedule",
                "schedule_id": sch_id,
                "cancelled": success,
            }

        raise ValueError(f"Unknown action '{action}' for workflow_control.")
