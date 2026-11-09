"""Telegram bot application setup and lifecycle management (re-exports from interfaces.telegram)."""

from src.interfaces.telegram.bot import create_bot_app, start_bot, stop_bot

__all__ = ["create_bot_app", "start_bot", "stop_bot"]
