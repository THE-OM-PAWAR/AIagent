"""Persistent workflow execution engine with recovery, exponential backoff retries, lease concurrency, and human-in-the-loop approvals."""

import asyncio
from datetime import datetime, timezone
import logging
import random
import re
import time
from typing import Any
import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.approval_manager import ApprovalManager, ApprovalNotFoundError
from src.core.workflow_manager import WorkflowManager, WorkflowValidationError
from src.database.models.workflow import (
    ApprovalStatus,
    WorkflowState,
    validate_workflow_state_transition,
)
from src.database.repositories.workflow_repo import (
    WorkflowConcurrencyError,
    WorkflowRepository,
)
from src.tools.executor import ToolExecutor
from src.tools.schemas import ToolResult

logger = logging.getLogger(__name__)

_INTERPOLATION_PATTERN = re.compile(r"\{([^}]+)\}")


def _get_nested_context(context: dict[str, Any], key_path: str) -> Any:
    """Resolve dot-separated key path in context dictionary e.g. 'step1.output.id' or 'context.step1.sum'."""
    clean_path = key_path.strip()
    if clean_path.startswith("context."):
        clean_path = clean_path[8:]
    elif clean_path.startswith("steps."):
        clean_path = clean_path[6:]

    parts = clean_path.split(".")
    curr: Any = context
    for p in parts:
        if isinstance(curr, dict) and p in curr:
            curr = curr[p]
        elif isinstance(curr, list) and p.isdigit():
            idx = int(p)
            if 0 <= idx < len(curr):
                curr = curr[idx]
            else:
                return None
        elif hasattr(curr, p):
            curr = getattr(curr, p)
        else:
            return None
    return curr


def _interpolate_value(val: Any, context: dict[str, Any]) -> Any:
    """Resolve placeholder variables within a value, preserving native types when standalone."""
    if isinstance(val, str):
        # Exact standalone match e.g. "{step1.output}" -> preserve int, dict, list type
        if val.startswith("{") and val.endswith("}") and val.count("{") == 1 and val.count("}") == 1:
            var_path = val[1:-1].strip()
            resolved = _get_nested_context(context, var_path)
            return resolved if resolved is not None else val

        # Embedded template string e.g. "Result is: {step1.sum} items"
        def _repl(match: re.Match) -> str:
            path = match.group(1).strip()
            res = _get_nested_context(context, path)
            return str(res) if res is not None else match.group(0)

        return _INTERPOLATION_PATTERN.sub(_repl, val)

    elif isinstance(val, dict):
        return {k: _interpolate_value(v, context) for k, v in val.items()}
    elif isinstance(val, list):
        return [_interpolate_value(item, context) for item in val]
    return val


def _interpolate_arguments(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Substitute placeholders in tool arguments schema using current execution context."""
    return _interpolate_value(args, context)


def evaluate_condition(condition: dict[str, Any] | None, context: dict[str, Any]) -> bool:
    """Evaluate a step's conditional rule against current execution context."""
    if not condition:
        return True

    field_path = condition.get("field", "")
    op = str(condition.get("operator", "truthy")).lower().strip()
    target_value = condition.get("value")

    actual_value = _get_nested_context(context, field_path)

    try:
        if op in ("exists",):
            return actual_value is not None
        elif op in ("not_exists",):
            return actual_value is None
        elif op in ("truthy",):
            return bool(actual_value)
        elif op in ("falsy",):
            return not bool(actual_value)
        elif op in ("eq", "=="):
            return actual_value == target_value
        elif op in ("neq", "!="):
            return actual_value != target_value
        elif op in ("gt", ">"):
            return actual_value > target_value
        elif op in ("gte", ">="):
            return actual_value >= target_value
        elif op in ("lt", "<"):
            return actual_value < target_value
        elif op in ("lte", "<="):
            return actual_value <= target_value
        elif op in ("in",):
            return actual_value in target_value if target_value is not None else False
        elif op in ("not_in",):
            return actual_value not in target_value if target_value is not None else True
        elif op in ("contains",):
            return target_value in actual_value if actual_value is not None else False
    except Exception as exc:
        logger.warning(
            "Condition evaluation failed for field='%s' op='%s' (actual=%s, target=%s): %s",
            field_path,
            op,
            actual_value,
            target_value,
            exc,
        )
        return False

    return False


def is_retryable_error(error: Exception | str) -> bool:
    """Classify whether a step error is transient and eligible for retry."""
    err_str = str(error).lower()

    # Explicitly non-retryable errors
    non_retryable_tokens = [
        "approval",
        "permission",
        "unauthorized",
        "forbidden",
        "validation",
        "invalid argument",
        "not found",
        "syntax error",
        "division by zero",
        "cannot depend on itself",
        "circular",
    ]
    if any(tok in err_str for tok in non_retryable_tokens):
        return False

    # Explicitly retryable errors
    retryable_tokens = [
        "timeout",
        "timed out",
        "connection",
        "rate limit",
        "quota",
        "500",
        "502",
        "503",
        "504",
        "temporary",
        "network",
    ]
    if any(tok in err_str for tok in retryable_tokens):
        return True

    return True


def compute_backoff_delay(
    attempt: int,
    initial_delay: float = 0.5,
    backoff_factor: float = 2.0,
    max_delay: float = 8.0,
    jitter: float = 0.2,
) -> float:
    """Calculate exponential backoff delay with randomized jitter."""
    base = min(max_delay, initial_delay * (backoff_factor ** max(0, attempt - 1)))
    jitter_val = random.uniform(0, base * jitter) if jitter > 0 else 0.0
    return round(base + jitter_val, 3)


class WorkflowEngine:
    """Production persistent workflow execution engine backed by PostgreSQL with leases, retries, and approvals."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tool_executor: ToolExecutor,
        workflow_manager: WorkflowManager | None = None,
        approval_manager: ApprovalManager | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.tool_executor = tool_executor
        self.workflow_manager = workflow_manager or WorkflowManager(
            session_factory=session_factory,
            tool_registry=getattr(tool_executor, "registry", None),
        )
        self.approval_manager = approval_manager or ApprovalManager(session_factory=session_factory)

    async def recover_on_startup(self) -> int:
        """Recover any interrupted running workflows and runs safely to 'paused' on system boot."""
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
        is_one_time: bool = True,
    ) -> dict[str, Any]:
        """Validate and create a persistent workflow definition."""
        return await self.workflow_manager.create_workflow(
            user_id=user_id,
            name=name,
            steps=steps,
            context_data=context_data,
            description=description,
            is_one_time=is_one_time,
        )

    async def get_status(self, workflow_id: str, user_id: int) -> dict[str, Any]:
        """Retrieve current persistent workflow status, step progress, context, and history."""
        status = await self.workflow_manager.get_workflow(workflow_id, user_id)
        if not status:
            raise ValueError(f"Workflow '{workflow_id}' not found for user {user_id}.")
        return status

    async def list_workflows(self, user_id: int, status: str | None = None) -> list[dict[str, Any]]:
        """List workflows owned by user."""
        return await self.workflow_manager.list_workflows(user_id=user_id, status=status)

    async def pause_workflow(self, workflow_id: str, user_id: int) -> bool:
        """Pause a running, pending, or waiting workflow at the next safe boundary."""
        async with self.session_factory() as session:
            repo = WorkflowRepository(session)
            wf = await repo.get_workflow(workflow_id, user_id)
            if not wf:
                raise ValueError(f"Workflow '{workflow_id}' not found.")
            if wf.status in (WorkflowState.COMPLETED.value, WorkflowState.FAILED.value, WorkflowState.CANCELLED.value):
                raise ValueError(f"Cannot pause workflow in terminal state '{wf.status}'.")
            return await repo.update_status(workflow_id, WorkflowState.PAUSED.value)

    async def cancel_workflow(self, workflow_id: str, user_id: int) -> bool:
        """Cancel a workflow execution."""
        async with self.session_factory() as session:
            repo = WorkflowRepository(session)
            wf = await repo.get_workflow(workflow_id, user_id)
            if not wf:
                raise ValueError(f"Workflow '{workflow_id}' not found.")
            if wf.status in (WorkflowState.COMPLETED.value, WorkflowState.CANCELLED.value):
                raise ValueError(f"Workflow is already in terminal state '{wf.status}'.")
            return await repo.update_status(workflow_id, WorkflowState.CANCELLED.value)

    async def resume_approved_workflow(
        self,
        approval_id: str,
        user_id: int,
    ) -> dict[str, Any]:
        """Resume execution of a workflow paused for human approval after valid approval grant."""
        appr = await self.approval_manager.get_approval(approval_id)
        if not appr:
            raise ApprovalNotFoundError(f"Approval request '{approval_id}' not found.")
        if appr["status"] != ApprovalStatus.APPROVED.value:
            raise ValueError(
                f"Cannot resume workflow for approval '{approval_id}': status is '{appr['status']}', expected 'approved'."
            )

        logger.info(
            "Resuming workflow %s (run %s) following approved request %s for step %s",
            appr["workflow_id"],
            appr["run_id"],
            approval_id,
            appr["step_name"],
        )

        return await self.execute_workflow(
            workflow_id=appr["workflow_id"],
            user_id=user_id,
            run_id_override=appr["run_id"],
            approved_step=appr["step_name"],
            trigger_type="approval_resumed",
        )

    async def execute_workflow(
        self,
        workflow_id: str,
        user_id: int,
        trigger_type: str = "manual",
        initial_context_override: dict[str, Any] | None = None,
        run_id_override: str | None = None,
        approved_step: str | None = None,
        idempotency_key: str | None = None,
        schedule_id: str | None = None,
    ) -> dict[str, Any]:
        """Execute or resume workflow with lease concurrency, idempotency, bounded retries, and checkpoints."""
        worker_token = f"worker_{uuid.uuid4().hex[:12]}"

        async with self.session_factory() as session:
            repo = WorkflowRepository(session)
            wf = await repo.get_workflow(workflow_id, user_id)
            if not wf:
                raise ValueError(f"Workflow '{workflow_id}' not found for user {user_id}.")

            if wf.is_one_time and wf.status in (WorkflowState.COMPLETED.value, WorkflowState.CANCELLED.value):
                raise ValueError(f"Cannot execute workflow in terminal state '{wf.status}'.")
            elif not wf.is_one_time and wf.status == WorkflowState.CANCELLED.value:
                raise ValueError(f"Cannot execute recurring workflow in terminal state '{wf.status}'.")

            # 1. Resolve or create execution run instance
            if run_id_override:
                run = await repo.get_run(run_id_override, user_id)
                if not run:
                    raise ValueError(f"Specified run '{run_id_override}' not found.")
                run_id = run.id
                context = dict(run.context_data)
            else:
                initial_ctx = dict(wf.context_data)
                if initial_context_override:
                    initial_ctx.update(initial_context_override)

                resolved_schedule_id = schedule_id or (
                    initial_context_override.get("schedule_id") if initial_context_override else None
                )

                run = await repo.create_run(
                    workflow_id=workflow_id,
                    user_id=user_id,
                    trigger_type=trigger_type,
                    initial_context=initial_ctx,
                    idempotency_key=idempotency_key,
                    schedule_id=resolved_schedule_id,
                )
                run_id = run.id
                context = dict(initial_ctx)

            # 2. Acquire exclusive execution lease to prevent duplicate concurrent runs
            acquired = await repo.acquire_execution_lease(run_id=run_id, worker_token=worker_token, lease_seconds=60)
            if not acquired:
                raise WorkflowConcurrencyError(
                    f"Workflow run '{run_id}' is locked by another active worker lease. Concurrency conflict avoided."
                )

            # Transition status to running
            await repo.update_run_status(run_id, WorkflowState.RUNNING.value)

        try:
            # 3. Identify previously completed steps to guarantee idempotency on recovery
            current_index = run.current_step_index if (run_id_override or not wf.is_one_time) else wf.current_step_index
            total_steps = wf.total_steps
            steps = list(wf.steps)
            completed_step_names: set[str] = set()
            skipped_step_names: set[str] = set()

            # Inspect run audit logs for completed steps (isolate to this run if recurring)
            history_sources = (run.step_runs or []) if not wf.is_one_time else (run.step_runs or []) + (wf.execution_history or [])
            for item in history_sources:
                st_name = item.get("step_name")
                if st_name:
                    if item.get("status") == "completed":
                        completed_step_names.add(st_name)
                    elif item.get("status") == "skipped":
                        skipped_step_names.add(st_name)

            # Execution budget tracking
            api_call_budget = (
                initial_context_override.get("api_call_budget")
                if initial_context_override
                else None
            )
            tool_calls_count = 0

            while current_index < total_steps:
                # Renew execution lease at step boundary
                async with self.session_factory() as session:
                    repo = WorkflowRepository(session)
                    await repo.renew_execution_lease(run_id=run_id, worker_token=worker_token, lease_seconds=60)
                    live_wf = await repo.get_workflow(workflow_id)
                    if live_wf and live_wf.status in (WorkflowState.PAUSED.value, WorkflowState.CANCELLED.value):
                        logger.info("Workflow %s halted at boundary: status is %s", workflow_id, live_wf.status)
                        await repo.update_run_status(run_id, live_wf.status)
                        return {
                            "workflow_id": workflow_id,
                            "run_id": run_id,
                            "status": live_wf.status,
                            "current_step_index": current_index,
                            "message": f"Execution halted: workflow is {live_wf.status}.",
                        }

                step = steps[current_index]
                step_name = step.get("name", f"step_{current_index + 1}")
                tool_name = step.get("tool_name")
                raw_args = step.get("arguments") if "arguments" in step else step.get("input_template", {})
                max_retries = step.get("max_retries", 1)
                timeout_sec = step.get("timeout_seconds", 60)
                condition = step.get("condition")
                dependencies = step.get("dependencies", [])

                # Idempotency safeguard: Never blindly repeat completed steps upon resume
                if step_name in completed_step_names:
                    logger.info("Step '%s' previously completed. Reusing checkpoint output without re-executing.", step_name)
                    current_index += 1
                    continue

                if not tool_name:
                    err_msg = f"Step '{step_name}' is missing 'tool_name'."
                    async with self.session_factory() as session:
                        repo = WorkflowRepository(session)
                        await repo.update_run_status(run_id, WorkflowState.FAILED.value, error_message=err_msg, completed=True)
                    return {"workflow_id": workflow_id, "run_id": run_id, "status": "failed", "error": err_msg}

                # Check dependencies
                unresolved = [dep for dep in dependencies if dep not in completed_step_names and dep not in skipped_step_names]
                if unresolved:
                    err_msg = f"Step '{step_name}' cannot execute: prerequisite dependencies {unresolved} have not completed."
                    async with self.session_factory() as session:
                        repo = WorkflowRepository(session)
                        await repo.update_run_status(run_id, WorkflowState.FAILED.value, error_message=err_msg, completed=True)
                    return {"workflow_id": workflow_id, "run_id": run_id, "status": "failed", "error": err_msg}

                # If prerequisite dependency was skipped, skip this step as well
                if any(dep in skipped_step_names for dep in dependencies):
                    logger.info("Step '%s' skipped because prerequisite dependency was skipped.", step_name)
                    skipped_step_names.add(step_name)
                    history_rec = {
                        "step_index": current_index,
                        "step_name": step_name,
                        "tool_name": tool_name,
                        "status": "skipped",
                        "reason": "Prerequisite dependency was skipped.",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                    current_index += 1
                    is_last = current_index >= total_steps
                    next_status = WorkflowState.COMPLETED.value if is_last else WorkflowState.RUNNING.value
                    async with self.session_factory() as session:
                        repo = WorkflowRepository(session)
                        await repo.save_step_checkpoint(
                            workflow_id, current_index, context, history_rec, next_status=next_status, run_id=run_id
                        )
                    continue

                # Check condition
                if condition and not evaluate_condition(condition, context):
                    logger.info("Step '%s' condition evaluated to False. Skipping step.", step_name)
                    skipped_step_names.add(step_name)
                    history_rec = {
                        "step_index": current_index,
                        "step_name": step_name,
                        "tool_name": tool_name,
                        "status": "skipped",
                        "reason": f"Condition evaluated to False: {condition}",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                    current_index += 1
                    is_last = current_index >= total_steps
                    next_status = WorkflowState.COMPLETED.value if is_last else WorkflowState.RUNNING.value
                    async with self.session_factory() as session:
                        repo = WorkflowRepository(session)
                        await repo.save_step_checkpoint(
                            workflow_id, current_index, context, history_rec, next_status=next_status, run_id=run_id
                        )
                    continue

                # Interpolate inputs
                resolved_args = _interpolate_arguments(raw_args or {}, context)
                attempt = 0
                success = False
                last_error = None
                step_start = time.perf_counter()
                approval_granted = approved_step == step_name

                # Bounded retry loop with exponential backoff & jitter
                while attempt <= max_retries and not success:
                    if api_call_budget is not None and tool_calls_count >= api_call_budget:
                        last_error = f"Execution aborted: API call budget of {api_call_budget} reached for this run."
                        logger.warning(last_error)
                        break

                    attempt += 1
                    tool_calls_count += 1
                    try:
                        step_call_id = f"step_{run_id[:8]}_{step_name}_att{attempt}{'_appr' if approval_granted else ''}"
                        tool_result: ToolResult = await asyncio.wait_for(
                            self.tool_executor.execute_tool(
                                tool_name=tool_name,
                                arguments=resolved_args,
                                user_id=user_id,
                                call_id=step_call_id,
                                approval_granted=approval_granted,
                            ),
                            timeout=float(timeout_sec),
                        )

                        if tool_result.success:
                            success = True
                            step_output = tool_result.output
                        else:
                            last_error = tool_result.error or "Tool execution failed."

                            # Check if human approval is required
                            if ("approval" in last_error.lower() or "permission" in last_error.lower()) and not approval_granted:
                                logger.info("Step '%s' requires approval. Creating approval request...", step_name)
                                appr_info = await self.approval_manager.create_approval_request(
                                    workflow_id=workflow_id,
                                    run_id=run_id,
                                    step_name=step_name,
                                    tool_name=tool_name,
                                    action_summary=f"Workflow step '{step_name}' needs authorization to execute tool '{tool_name}'.",
                                    action_arguments=resolved_args,
                                    requested_by_user_id=user_id,
                                )

                                history_rec = {
                                    "step_index": current_index,
                                    "step_name": step_name,
                                    "tool_name": tool_name,
                                    "status": WorkflowState.WAITING_FOR_APPROVAL.value,
                                    "approval_id": appr_info["approval_id"],
                                    "attempt": attempt,
                                    "error": last_error,
                                    "timestamp": datetime.now(timezone.utc).isoformat(),
                                }

                                async with self.session_factory() as session:
                                    repo = WorkflowRepository(session)
                                    await repo.save_step_checkpoint(
                                        workflow_id,
                                        current_index,
                                        context,
                                        history_rec,
                                        next_status=WorkflowState.WAITING_FOR_APPROVAL.value,
                                        run_id=run_id,
                                    )
                                    await repo.update_run_status(run_id, WorkflowState.WAITING_FOR_APPROVAL.value)

                                return {
                                    "workflow_id": workflow_id,
                                    "run_id": run_id,
                                    "status": WorkflowState.WAITING_FOR_APPROVAL.value,
                                    "current_step_index": current_index,
                                    "step_name": step_name,
                                    "tool_name": tool_name,
                                    "approval_id": appr_info["approval_id"],
                                    "message": last_error,
                                }

                    except asyncio.TimeoutError:
                        last_error = f"Step '{step_name}' timed out after {timeout_sec}s."
                    except Exception as exc:
                        last_error = str(exc)

                    # Determine retry eligibility
                    if not success:
                        retryable = is_retryable_error(last_error or "")
                        if not retryable or attempt > max_retries:
                            logger.warning(
                                "Step '%s' failed (attempt %d/%d). Retryable: %s. Aborting retry loop: %s",
                                step_name,
                                attempt,
                                max_retries + 1,
                                retryable,
                                last_error,
                            )
                            break

                        delay = compute_backoff_delay(attempt=attempt)
                        logger.info("Step '%s' failed (attempt %d/%d). Retrying in %.2fs: %s", step_name, attempt, max_retries + 1, delay, last_error)
                        await asyncio.sleep(delay)

                elapsed = round(time.perf_counter() - step_start, 3)

                if not success:
                    err_msg = f"Step '{step_name}' ({tool_name}) failed after {attempt} attempt(s): {last_error}"
                    history_rec = {
                        "step_index": current_index,
                        "step_name": step_name,
                        "tool_name": tool_name,
                        "status": WorkflowState.FAILED.value,
                        "attempts": attempt,
                        "error": last_error,
                        "elapsed_seconds": elapsed,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                    async with self.session_factory() as session:
                        repo = WorkflowRepository(session)
                        await repo.save_step_checkpoint(
                            workflow_id, current_index, context, history_rec, next_status=WorkflowState.FAILED.value, run_id=run_id
                        )
                        await repo.update_run_status(run_id, WorkflowState.FAILED.value, error_message=err_msg, completed=True)
                    return {
                        "workflow_id": workflow_id,
                        "run_id": run_id,
                        "status": WorkflowState.FAILED.value,
                        "current_step_index": current_index,
                        "failed_step": step_name,
                        "error": err_msg,
                    }

                # Step completed successfully: advance checkpoint
                context[step_name] = step_output
                context[f"{step_name}.output"] = step_output
                completed_step_names.add(step_name)
                current_index += 1
                is_last = current_index >= total_steps
                next_status = WorkflowState.COMPLETED.value if is_last else WorkflowState.RUNNING.value

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
                        workflow_id, current_index, context, history_rec, next_status=next_status, run_id=run_id
                    )
                    if is_last:
                        await repo.update_run_status(run_id, WorkflowState.COMPLETED.value, completed=True)

            return {
                "workflow_id": workflow_id,
                "run_id": run_id,
                "status": WorkflowState.COMPLETED.value,
                "total_steps": total_steps,
                "current_step_index": current_index,
                "context_data": context,
                "message": "Workflow completed successfully.",
            }

        finally:
            # Always cleanly release exclusive worker lease
            try:
                async with self.session_factory() as session:
                    repo = WorkflowRepository(session)
                    await repo.release_execution_lease(run_id=run_id, worker_token=worker_token)
            except Exception as release_err:
                logger.warning("Failed releasing lease for run %s: %s", run_id, release_err)
