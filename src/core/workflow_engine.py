"""Lightweight persistent workflow engine executing multi-step agent tools with checkpoints and recovery in PostgreSQL."""

import asyncio
from datetime import datetime, timezone
import logging
import time
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.database.repositories.workflow_repo import WorkflowRepository
from src.tools.executor import ToolExecutor
from src.tools.schemas import ToolResult

logger = logging.getLogger(__name__)


def _get_nested_context(context: dict[str, Any], key_path: str) -> Any:
    """Resolve dot-separated key path in context dictionary e.g. 'step1.output'."""
    parts = key_path.split(".")
    curr: Any = context
    for p in parts:
        if isinstance(curr, dict) and p in curr:
            curr = curr[p]
        else:
            return None
    return curr


def _interpolate_arguments(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Substitute placeholders like '{context.step1.sum}' with values from context."""
    resolved: dict[str, Any] = {}
    for k, v in args.items():
        if isinstance(v, str) and v.startswith("{") and v.endswith("}"):
            var_path = v[1:-1].strip()
            lookup_path = var_path[8:] if var_path.startswith("context.") else var_path
            val = _get_nested_context(context, lookup_path)
            resolved[k] = val if val is not None else v
        elif isinstance(v, dict):
            resolved[k] = _interpolate_arguments(v, context)
        else:
            resolved[k] = v
    return resolved


class WorkflowEngine:
    """Lightweight persistent workflow foundation backed by PostgreSQL."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tool_executor: ToolExecutor,
    ) -> None:
        self.session_factory = session_factory
        self.tool_executor = tool_executor

    async def recover_on_startup(self) -> int:
        """Call on system boot to recover any interrupted workflows safely to 'paused'."""
        async with self.session_factory() as session:
            repo = WorkflowRepository(session)
            return await repo.recover_interrupted_workflows()

    async def create_workflow(
        self,
        user_id: int,
        name: str,
        steps: list[dict[str, Any]],
        context_data: dict[str, Any] | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        """Create and persist a new workflow."""
        if not steps:
            raise ValueError("A workflow requires at least one step in 'steps'.")
        async with self.session_factory() as session:
            repo = WorkflowRepository(session)
            wf = await repo.create_workflow(user_id, name, steps, context_data, description)
            return {
                "workflow_id": wf.id,
                "name": wf.name,
                "status": wf.status,
                "total_steps": wf.total_steps,
                "created_at": wf.created_at.isoformat(),
            }

    async def get_status(self, workflow_id: str, user_id: int) -> dict[str, Any]:
        """Retrieve current persistent workflow status, step progress, and history."""
        async with self.session_factory() as session:
            repo = WorkflowRepository(session)
            wf = await repo.get_workflow(workflow_id, user_id)
            if not wf:
                raise ValueError(f"Workflow '{workflow_id}' not found for user {user_id}.")
            return {
                "workflow_id": wf.id,
                "name": wf.name,
                "status": wf.status,
                "current_step_index": wf.current_step_index,
                "total_steps": wf.total_steps,
                "context_data": wf.context_data,
                "execution_history": wf.execution_history,
                "error_message": wf.error_message,
                "created_at": wf.created_at.isoformat(),
                "completed_at": wf.completed_at.isoformat() if wf.completed_at else None,
            }

    async def list_workflows(self, user_id: int, status: str | None = None) -> list[dict[str, Any]]:
        """List workflows owned by user."""
        async with self.session_factory() as session:
            repo = WorkflowRepository(session)
            wfs = await repo.list_workflows(user_id, status)
            return [
                {
                    "workflow_id": w.id,
                    "name": w.name,
                    "status": w.status,
                    "current_step_index": w.current_step_index,
                    "total_steps": w.total_steps,
                    "created_at": w.created_at.isoformat(),
                }
                for w in wfs
            ]

    async def pause_workflow(self, workflow_id: str, user_id: int) -> bool:
        """Pause a running or pending workflow."""
        async with self.session_factory() as session:
            repo = WorkflowRepository(session)
            wf = await repo.get_workflow(workflow_id, user_id)
            if not wf:
                raise ValueError(f"Workflow '{workflow_id}' not found.")
            if wf.status in ("completed", "failed", "cancelled"):
                raise ValueError(f"Cannot pause workflow in terminal state '{wf.status}'.")
            return await repo.update_status(workflow_id, "paused")

    async def cancel_workflow(self, workflow_id: str, user_id: int) -> bool:
        """Cancel a workflow execution."""
        async with self.session_factory() as session:
            repo = WorkflowRepository(session)
            wf = await repo.get_workflow(workflow_id, user_id)
            if not wf:
                raise ValueError(f"Workflow '{workflow_id}' not found.")
            if wf.status in ("completed", "cancelled"):
                raise ValueError(f"Workflow is already in terminal state '{wf.status}'.")
            return await repo.update_status(workflow_id, "cancelled")

    async def execute_workflow(self, workflow_id: str, user_id: int) -> dict[str, Any]:
        """Execute a workflow step-by-step from its current checkpoint until completed, paused, or blocked."""
        async with self.session_factory() as session:
            repo = WorkflowRepository(session)
            wf = await repo.get_workflow(workflow_id, user_id)
            if not wf:
                raise ValueError(f"Workflow '{workflow_id}' not found for user {user_id}.")

            if wf.status not in ("pending", "paused", "running"):
                raise ValueError(f"Cannot execute workflow in state '{wf.status}'.")

            await repo.update_status(workflow_id, "running")

        # Reload state in memory
        current_index = wf.current_step_index
        total_steps = wf.total_steps
        steps = list(wf.steps)
        context = dict(wf.context_data)

        while current_index < total_steps:
            # Check for external pause/cancellation before each step
            async with self.session_factory() as session:
                repo = WorkflowRepository(session)
                live_wf = await repo.get_workflow(workflow_id)
                if live_wf and live_wf.status in ("paused", "cancelled"):
                    logger.info("Workflow %s execution interrupted by status change to %s", workflow_id, live_wf.status)
                    return {
                        "workflow_id": workflow_id,
                        "status": live_wf.status,
                        "current_step_index": current_index,
                        "message": f"Execution halted: workflow is {live_wf.status}.",
                    }

            step = steps[current_index]
            step_name = step.get("name", f"step_{current_index + 1}")
            tool_name = step.get("tool_name")
            raw_args = step.get("arguments", {})
            max_retries = step.get("max_retries", 1)

            if not tool_name:
                err_msg = f"Step '{step_name}' is missing 'tool_name'."
                async with self.session_factory() as session:
                    repo = WorkflowRepository(session)
                    await repo.update_status(workflow_id, "failed", error_message=err_msg)
                return {"workflow_id": workflow_id, "status": "failed", "error": err_msg}

            resolved_args = _interpolate_arguments(raw_args, context)
            attempt = 0
            success = False
            last_error = None
            step_start = time.perf_counter()

            while attempt <= max_retries and not success:
                attempt += 1
                try:
                    tool_result: ToolResult = await self.tool_executor.execute_tool(
                        tool_name=tool_name,
                        arguments=resolved_args,
                        user_id=user_id,
                    )
                    if tool_result.success:
                        success = True
                        step_output = tool_result.output
                    else:
                        last_error = tool_result.error or "Tool execution reported failure."
                        if "approval" in last_error.lower() or "permission" in last_error.lower():
                            # Needs user approval
                            async with self.session_factory() as session:
                                repo = WorkflowRepository(session)
                                history_rec = {
                                    "step_index": current_index,
                                    "step_name": step_name,
                                    "tool_name": tool_name,
                                    "status": "waiting_for_approval",
                                    "attempt": attempt,
                                    "error": last_error,
                                    "timestamp": datetime.now(timezone.utc).isoformat(),
                                }
                                await repo.save_step_checkpoint(
                                    workflow_id, current_index, context, history_rec, next_status="waiting_for_approval"
                                )
                            return {
                                "workflow_id": workflow_id,
                                "status": "waiting_for_approval",
                                "current_step_index": current_index,
                                "step_name": step_name,
                                "tool_name": tool_name,
                                "message": last_error,
                            }
                except Exception as exc:
                    last_error = str(exc)

                if not success and attempt <= max_retries:
                    await asyncio.sleep(0.5)

            elapsed = round(time.perf_counter() - step_start, 3)

            if not success:
                # Step failed after retries
                err_msg = f"Step '{step_name}' ({tool_name}) failed after {attempt} attempt(s): {last_error}"
                history_rec = {
                    "step_index": current_index,
                    "step_name": step_name,
                    "tool_name": tool_name,
                    "status": "failed",
                    "attempts": attempt,
                    "error": last_error,
                    "elapsed_seconds": elapsed,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                async with self.session_factory() as session:
                    repo = WorkflowRepository(session)
                    await repo.save_step_checkpoint(
                        workflow_id, current_index, context, history_rec, next_status="failed"
                    )
                    await repo.update_status(workflow_id, "failed", error_message=err_msg)
                return {
                    "workflow_id": workflow_id,
                    "status": "failed",
                    "current_step_index": current_index,
                    "failed_step": step_name,
                    "error": err_msg,
                }

            # Step succeeded: update context and advance checkpoint
            context[step_name] = step_output
            current_index += 1
            is_last = current_index >= total_steps
            next_status = "completed" if is_last else "running"

            history_rec = {
                "step_index": current_index - 1,
                "step_name": step_name,
                "tool_name": tool_name,
                "status": "completed",
                "attempts": attempt,
                "output_preview": str(step_output)[:500] if step_output else None,
                "elapsed_seconds": elapsed,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

            async with self.session_factory() as session:
                repo = WorkflowRepository(session)
                await repo.save_step_checkpoint(
                    workflow_id, current_index, context, history_rec, next_status=next_status
                )

        return {
            "workflow_id": workflow_id,
            "status": "completed",
            "total_steps": total_steps,
            "current_step_index": current_index,
            "context_data": context,
            "message": "Workflow completed successfully.",
        }
