"""Safe executor for invoking tools with parameter handling, timeouts, and permissions."""

import asyncio
from inspect import iscoroutinefunction
import json
import logging
import re
import time
from typing import Any

from src.tools.errors import (
    ToolArgumentError,
    ToolExecutionError,
    ToolNotFoundError,
    ToolPermissionError,
    ToolTimeoutError,
)
from src.tools.permissions import PermissionPolicy, ToolPermission
from src.tools.registry import ToolRegistry
from src.tools.schemas import ToolResult, validate_arguments

logger = logging.getLogger(__name__)

# Keys that may contain sensitive credentials to be masked in logs
_SENSITIVE_KEY_PATTERN = re.compile(
    r"(token|key|secret|password|auth|credential|api[_-]?key|access[_-]?token)",
    re.IGNORECASE,
)


def mask_sensitive_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy of arguments with sensitive values masked."""
    masked: dict[str, Any] = {}
    for k, v in arguments.items():
        if _SENSITIVE_KEY_PATTERN.search(k):
            masked[k] = "[REDACTED]"
        elif isinstance(v, dict):
            masked[k] = mask_sensitive_arguments(v)
        else:
            masked[k] = v
    return masked


class ToolExecutor:
    """Executes tools safely with timeouts, schema validation, permissions, and logging."""

    def __init__(
        self,
        registry: ToolRegistry,
        permission_policy: PermissionPolicy | None = None,
        default_timeout: float = 30.0,
        max_output_chars: int = 25000,
        execution_tracker: Any = None,
    ) -> None:
        self.registry = registry
        self.permission_policy = permission_policy or PermissionPolicy()
        self.default_timeout = default_timeout
        self.max_output_chars = max_output_chars
        self.execution_tracker = execution_tracker
        self._result_cache: dict[str, ToolResult] = {}

    def clear_cache(self) -> None:
        """Clear cached tool execution results."""
        self._result_cache.clear()

    def _store_and_return(self, result: ToolResult) -> ToolResult:
        """Store tool result in memoization cache and return it."""
        if result.call_id:
            self._result_cache[result.call_id] = result
            if len(self._result_cache) > 250:
                self._result_cache.pop(next(iter(self._result_cache)))
        return result

    async def execute_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        call_id: str | None = None,
        user_id: int = 101,
        agent_type: str | None = None,
        execution_record: Any = None,
    ) -> ToolResult:
        """Execute a tool returning a structured ToolResult model."""
        actual_call_id = call_id or f"call_{int(time.time()*1000)}"

        # If this call_id has already been executed, return cached result to prevent duplicate execution
        if call_id and call_id in self._result_cache:
            logger.info("ToolExecutor: returning cached result for existing call_id '%s'", call_id)
            return self._result_cache[call_id]

        start_time = time.perf_counter()

        masked_args = mask_sensitive_arguments(arguments)
        logger.info(
            "ToolExecutor: executing tool '%s' [call_id=%s] for user_id=%s with args=%s",
            tool_name,
            actual_call_id,
            user_id,
            masked_args,
        )

        # 1. Unknown tool rejection
        definition = self.registry.get_definition(tool_name)
        func = self.registry.get_callable(tool_name)
        if not definition or func is None:
            err_msg = f"Tool '{tool_name}' not found."
            logger.warning("ToolExecutor: %s", err_msg)
            duration = time.perf_counter() - start_time
            await self._record_observability(
                execution_record, actual_call_id, tool_name, "error", duration, err_msg, masked_args, 0
            )
            return self._store_and_return(ToolResult(
                call_id=actual_call_id,
                tool_name=tool_name,
                success=False,
                error=err_msg,
                execution_time_seconds=round(duration, 4),
            ))

        # 2. Permission and policy verification
        is_allowed, denial_reason = self.permission_policy.check_permission(
            user_id=user_id,
            tool_name=tool_name,
            required_permission=definition.permission,
            requires_approval=definition.requires_approval,
            agent_type=agent_type,
            allowed_agents=definition.allowed_agents,
        )
        if not is_allowed:
            err_msg = denial_reason or "Permission denied."
            logger.warning("ToolExecutor: permission check failed for tool '%s': %s", tool_name, err_msg)
            duration = time.perf_counter() - start_time
            await self._record_observability(
                execution_record, actual_call_id, tool_name, "blocked", duration, err_msg, masked_args, 0
            )
            return self._store_and_return(ToolResult(
                call_id=actual_call_id,
                tool_name=tool_name,
                success=False,
                error=err_msg,
                execution_time_seconds=round(duration, 4),
            ))

        # 3. Strict argument validation against schema
        is_valid, validation_error = validate_arguments(definition.parameters_schema, arguments)
        if not is_valid:
            err_msg = f"Invalid arguments for tool '{tool_name}': {validation_error}"
            logger.warning("ToolExecutor: %s", err_msg)
            duration = time.perf_counter() - start_time
            await self._record_observability(
                execution_record, actual_call_id, tool_name, "error", duration, err_msg, masked_args, 0
            )
            return self._store_and_return(ToolResult(
                call_id=actual_call_id,
                tool_name=tool_name,
                success=False,
                error=err_msg,
                execution_time_seconds=round(duration, 4),
            ))

        # 4. Safe invocation with timeout
        timeout = definition.timeout_seconds if definition.timeout_seconds > 0 else self.default_timeout
        try:
            call_kwargs = dict(arguments)
            import inspect
            try:
                sig = inspect.signature(func)
                if "user_id" in sig.parameters and "user_id" not in call_kwargs:
                    call_kwargs["user_id"] = user_id
            except (ValueError, TypeError):
                pass

            if iscoroutinefunction(func):
                raw_result = await asyncio.wait_for(
                    func(**call_kwargs),
                    timeout=timeout,
                )
            else:
                raw_result = await asyncio.wait_for(
                    asyncio.to_thread(func, **call_kwargs),
                    timeout=timeout,
                )

            duration = time.perf_counter() - start_time

            # 5. Output size limiting and truncation
            processed_result, is_truncated = self._truncate_output(raw_result)
            result_size = len(str(processed_result))

            logger.info(
                "ToolExecutor: tool '%s' succeeded in %.3fs (size=%d bytes, truncated=%s)",
                tool_name,
                duration,
                result_size,
                is_truncated,
            )

            await self._record_observability(
                execution_record, actual_call_id, tool_name, "success", duration, None, masked_args, result_size
            )

            return self._store_and_return(ToolResult(
                call_id=actual_call_id,
                tool_name=tool_name,
                success=True,
                output=processed_result,
                execution_time_seconds=round(duration, 4),
                is_truncated=is_truncated,
            ))

        except asyncio.TimeoutError:
            duration = time.perf_counter() - start_time
            err_msg = f"Tool '{tool_name}' timed out after {timeout:.1f}s."
            logger.warning("ToolExecutor: %s", err_msg)
            await self._record_observability(
                execution_record, actual_call_id, tool_name, "timeout", duration, err_msg, masked_args, 0
            )
            return self._store_and_return(ToolResult(
                call_id=actual_call_id,
                tool_name=tool_name,
                success=False,
                error=err_msg,
                execution_time_seconds=round(duration, 4),
            ))
        except Exception as exc:
            duration = time.perf_counter() - start_time
            err_msg = f"Tool '{tool_name}' raised execution error: {exc}"
            logger.error("ToolExecutor: %s", err_msg, exc_info=True)
            await self._record_observability(
                execution_record, actual_call_id, tool_name, "error", duration, err_msg, masked_args, 0
            )
            return self._store_and_return(ToolResult(
                call_id=actual_call_id,
                tool_name=tool_name,
                success=False,
                error=err_msg,
                execution_time_seconds=round(duration, 4),
            ))

    async def execute(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Backward-compatible invocation method returning a dict with 'success', 'result', or 'error'."""
        result = await self.execute_tool(tool_name=tool_name, arguments=arguments)
        if result.success:
            return {"success": True, "result": result.output}
        return {"success": False, "error": result.error}

    def _truncate_output(self, raw_result: Any) -> tuple[Any, bool]:
        """Truncate excessive output to preserve LLM token context."""
        if raw_result is None:
            return None, False

        if isinstance(raw_result, str):
            if len(raw_result) > self.max_output_chars:
                truncated = raw_result[: self.max_output_chars] + "\n... [Truncated: output exceeded size limit]"
                return truncated, True
            return raw_result, False

        if isinstance(raw_result, (dict, list)):
            try:
                serialized = json.dumps(raw_result)
                if len(serialized) > self.max_output_chars:
                    truncated = serialized[: self.max_output_chars] + "... [Truncated: output exceeded size limit]"
                    return truncated, True
            except (TypeError, ValueError):
                pass

        return raw_result, False

    async def _record_observability(
        self,
        record: Any,
        call_id: str,
        name: str,
        status: str,
        duration: float,
        error: str | None,
        masked_args: dict[str, Any],
        size_bytes: int,
    ) -> None:
        """Record tool execution telemetry into ExecutionTracker if available."""
        if not self.execution_tracker:
            return
        try:
            summary = json.dumps(masked_args)
            if len(summary) > 100:
                summary = summary[:97] + "..."
            if hasattr(self.execution_tracker, "record_tool_execution"):
                await self.execution_tracker.record_tool_execution(
                    record=record,
                    tool_call_id=call_id,
                    tool_name=name,
                    status=status,
                    duration_seconds=duration,
                    error_message=error,
                    arguments_summary=summary,
                    result_size_bytes=size_bytes,
                )
        except Exception as exc:
            logger.debug("Failed recording tool telemetry: %s", exc)
