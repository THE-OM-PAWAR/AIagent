"""Centralized system prompts and persona templates."""

DEFAULT_SYSTEM_INSTRUCTION = (
    "You are a helpful, respectful, and concise personal AI assistant."
)


def get_default_system_instruction() -> str:
    """Return the base default system prompt."""
    return DEFAULT_SYSTEM_INSTRUCTION

