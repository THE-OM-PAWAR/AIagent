"""Command Line Interface (CLI) runner for interactive terminal testing and interaction."""

import asyncio
import sys
from typing import Any

from src.core.orchestrator import AgentOrchestrator


async def run_cli_session(orchestrator: AgentOrchestrator, user_id: int = 999999) -> None:
    """Start an interactive CLI session with the assistant.

    Args:
        orchestrator: Configured AgentOrchestrator.
        user_id: Simulated user ID for CLI interaction.
    """
    print("\n--- AIagent Interactive CLI ---")
    print("Type your message and press Enter. Type 'exit' or 'quit' to end.\n")

    conversation_id = f"cli_{user_id}"

    while True:
        try:
            prompt = await asyncio.to_thread(input, "You: ")
            prompt_clean = prompt.strip()
            if not prompt_clean:
                continue
            if prompt_clean.lower() in {"exit", "quit"}:
                print("Ending CLI session. Goodbye!")
                break

            response = await orchestrator.handle_user_message(
                user_id=user_id,
                message_text=prompt_clean,
                conversation_id=conversation_id,
            )
            print(f"\nAIagent: {response}\n")
        except (KeyboardInterrupt, EOFError):
            print("\nExiting CLI.")
            break
        except Exception as exc:
            print(f"\n[Error] {exc}\n", file=sys.stderr)

