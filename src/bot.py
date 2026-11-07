"""Telegram bot application setup and lifecycle management."""

import logging
from telegram.ext import Application, ApplicationBuilder
from telegram.request import HTTPXRequest

from src.config import Config
from src.database import DatabaseClient
from src.gemini import GeminiClient
from src.handlers import register_handlers

logger = logging.getLogger(__name__)


def create_bot_app(
    config: Config,
    gemini_client: GeminiClient,
    db_client: DatabaseClient,
) -> Application:
    """Build and configure the Telegram bot Application with timeouts and dependencies.

    Args:
        config: Application configuration instance.
        gemini_client: Initialized GeminiClient instance.
        db_client: Initialized DatabaseClient instance.

    Returns:
        Configured telegram.ext.Application ready for execution.
    """
    logger.info("Building Telegram bot application with custom HTTP timeouts...")

    # Configure resilient HTTP request timeouts to prevent hung long-polling connections
    request = HTTPXRequest(
        connection_pool_size=8,
        connect_timeout=10.0,
        read_timeout=30.0,
        write_timeout=30.0,
    )

    app = ApplicationBuilder().token(config.telegram_bot_token).request(request).build()
    app.bot_data["config"] = config
    app.bot_data["gemini_client"] = gemini_client
    app.bot_data["db_client"] = db_client
    register_handlers(app)
    return app


async def start_bot(app: Application) -> None:
    """Initialize and start the bot with long polling.

    Args:
        app: Configured Telegram application instance.
    """
    logger.info("Initializing Telegram bot...")
    await app.initialize()
    await app.start()
    if app.updater is not None:
        await app.updater.start_polling()
    logger.info("Telegram bot long polling started successfully.")


async def stop_bot(app: Application) -> None:
    """Stop bot polling and shutdown cleanly.

    Args:
        app: Running Telegram application instance.
    """
    logger.info("Stopping Telegram bot polling...")
    if app.updater is not None and app.updater.running:
        await app.updater.stop()
    if app.running:
        await app.stop()
    await app.shutdown()
    logger.info("Telegram bot shutdown completed.")
