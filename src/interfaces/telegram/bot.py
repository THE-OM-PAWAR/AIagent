"""Telegram bot application setup and lifecycle management."""

from typing import Any
import logging
import time
from telegram.ext import Application, ApplicationBuilder
from telegram.request import HTTPXRequest

from src.config import Config
from src.core.orchestrator import AgentOrchestrator
from src.intelligence.providers.gemini import GeminiClient
from src.interfaces.telegram.handlers import register_handlers

logger = logging.getLogger(__name__)


def create_bot_app(
    config: Config,
    gemini_client: GeminiClient,
    db_client: Any = None,
    orchestrator: AgentOrchestrator | None = None,
    integration_registry: Any = None,
) -> Application:
    """Build and configure the Telegram bot Application with timeouts and dependencies.

    Args:
        config: Application configuration instance.
        gemini_client: Initialized GeminiClient instance.
        db_client: Optional legacy db_client instance.
        orchestrator: Optional AgentOrchestrator instance.
        integration_registry: Optional IntegrationRegistry instance.

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

    if orchestrator is None:
        orchestrator = AgentOrchestrator(
            gemini_client=gemini_client,
            database_url=config.database_url,
            db_client=db_client,
            context_limit=config.conversation_history_limit,
        )

    app = ApplicationBuilder().token(config.telegram_bot_token).request(request).build()
    app.bot_data["config"] = config
    app.bot_data["gemini_client"] = gemini_client
    app.bot_data["orchestrator"] = orchestrator
    if integration_registry is not None:
        app.bot_data["integration_registry"] = integration_registry
    app.bot_data["start_time"] = time.time()
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
