"""Exception hierarchy for the tool calling and execution engine."""


class ToolError(Exception):
    """Base exception for all tool-related errors."""

    def __init__(self, message: str, tool_name: str | None = None) -> None:
        super().__init__(message)
        self.tool_name = tool_name
        self.message = message


class ToolNotFoundError(ToolError):
    """Raised when an invoked tool is not registered."""
    pass


class ToolArgumentError(ToolError):
    """Raised when tool arguments fail schema validation."""
    pass


class ToolPermissionError(ToolError):
    """Raised when tool execution violates permission or approval policies."""
    pass


class ToolTimeoutError(ToolError):
    """Raised when a tool execution exceeds its allowed timeout."""
    pass


class ToolExecutionError(ToolError):
    """Raised when an unhandled exception occurs during tool execution."""
    pass


class ToolResultSizeLimitError(ToolError):
    """Raised when a tool's output exceeds permissible size limits."""
    pass


class ToolLoopLimitError(ToolError):
    """Raised when the multi-turn tool calling loop exceeds maximum iterations."""
    pass


class ToolDuplicateCallError(ToolError):
    """Raised when the exact same tool call is repeated in an infinite loop."""
    pass

