"""Schemas and argument validation utilities for tools."""

from collections.abc import Callable
import json
import logging
import math
from typing import Any
from pydantic import BaseModel, Field

from src.tools.permissions import ToolPermission

logger = logging.getLogger(__name__)


class ToolDefinition(BaseModel):
    """Metadata and schema specification for an executable tool."""
    name: str
    description: str
    parameters_schema: dict[str, Any] = Field(default_factory=dict)
    permission: ToolPermission = ToolPermission.READ_ONLY
    timeout_seconds: float = 30.0
    requires_approval: bool = False
    allowed_agents: list[str] | None = None

    def to_gemini_declaration_dict(self) -> dict[str, Any]:
        """Convert into a dictionary structure expected by Gemini FunctionDeclaration."""
        schema = self.parameters_schema or {"type": "object", "properties": {}}
        return {
            "name": self.name,
            "description": self.description,
            "parameters_json_schema": schema,
        }


class ToolCall(BaseModel):
    """Represents a tool call requested by the LLM."""
    call_id: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    """Encapsulates the output of a tool execution."""
    call_id: str
    tool_name: str
    success: bool
    output: Any = None
    error: str | None = None
    execution_time_seconds: float = 0.0
    is_truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Format the result into a clean dictionary for LLM function responses."""
        if self.success:
            res: dict[str, Any] = {"result": self.output}
            if self.is_truncated:
                res["is_truncated"] = True
            return res
        return {
            "error": self.error or "Tool execution failed.",
            "success": False,
        }


def validate_arguments(
    parameters_schema: dict[str, Any] | None,
    arguments: dict[str, Any] | None,
) -> tuple[bool, str | None]:
    """Validate invocation arguments against a JSON schema.

    Returns:
        (is_valid, error_message)
    """
    args = arguments if arguments is not None else {}
    if not isinstance(args, dict):
        return False, f"Expected arguments to be a dictionary, got {type(args).__name__}."

    if not parameters_schema:
        # No schema defined: empty dict is valid
        return True, None

    schema_type = parameters_schema.get("type", "object")
    if schema_type != "object":
        return False, f"Root schema type must be 'object', got '{schema_type}'."

    properties: dict[str, Any] = parameters_schema.get("properties", {})
    required: list[str] = parameters_schema.get("required", [])

    # Check for required fields
    for req_field in required:
        if req_field not in args:
            return False, f"Missing required argument: '{req_field}'."

    # Validate individual property types
    for key, val in args.items():
        prop_spec = properties.get(key)
        if not prop_spec:
            # Tolerant to extra arguments unless additionalProperties is explicitly False
            if parameters_schema.get("additionalProperties") is False:
                return False, f"Unexpected argument '{key}' is not allowed by schema."
            continue

        expected_type = prop_spec.get("type")
        if not expected_type:
            continue

        # Type checks
        if expected_type == "string":
            if not isinstance(val, str):
                return False, f"Argument '{key}' expected string, got {type(val).__name__}."
        elif expected_type == "integer":
            if not isinstance(val, int) or isinstance(val, bool):
                return False, f"Argument '{key}' expected integer, got {type(val).__name__}."
        elif expected_type == "number":
            if not isinstance(val, (int, float)) or isinstance(val, bool):
                return False, f"Argument '{key}' expected number, got {type(val).__name__}."
            if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
                return False, f"Argument '{key}' cannot be NaN or Infinity."
        elif expected_type == "boolean":
            if not isinstance(val, bool):
                return False, f"Argument '{key}' expected boolean, got {type(val).__name__}."
        elif expected_type == "array":
            if not isinstance(val, list):
                return False, f"Argument '{key}' expected array (list), got {type(val).__name__}."
        elif expected_type == "object":
            if not isinstance(val, dict):
                return False, f"Argument '{key}' expected object (dict), got {type(val).__name__}."

        # Enum checks
        if "enum" in prop_spec and val not in prop_spec["enum"]:
            return False, f"Argument '{key}' value '{val}' not in allowed choices: {prop_spec['enum']}."

    return True, None

