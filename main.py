"""Asynchronous application entry point for the Telegram AI Assistant."""

import asyncio
import signal
import sys

from src.bot import create_bot_app, start_bot, stop_bot
from src.config import Config, ConfigError
from src.database import DatabaseClient
from src.gemini import GeminiClient
from src.logger import get_logger, setup_logging

logger = get_logger("app")


async def main() -> None:
    """Asynchronous entry point managing application lifecycle."""
    # 1. Load and validate environment configuration
    try:
        config = Config.load()
    except ConfigError as err:
        print(f"\n[CONFIG ERROR] {err}\n", file=sys.stderr)
        sys.exit(1)

    # 2. Configure structured logging
    setup_logging(config.log_level)
    logger.info("Starting Telegram AI Assistant backend...")
    logger.info(
        "Configuration: environment=%s, log_level=%s, model=%s, context_limit=%d",
        config.environment,
        config.log_level,
        config.gemini_model,
        config.conversation_history_limit,
    )

    # 3. Initialize core services (Database & Gemini)
    db_client = DatabaseClient(uri=config.mongodb_uri, db_name=config.mongodb_db_name)
    gemini_client = GeminiClient(
        api_key=config.gemini_api_key,
        model=config.gemini_model,
        system_instruction=config.gemini_system_instruction,
        timeout=config.gemini_timeout_seconds,
    )

    try:
        gemini_client.initialize()
    except Exception as exc:
        logger.error("Failed initializing Gemini service: %s", exc, exc_info=True)
        sys.exit(1)

    try:
        await db_client.connect()
    except Exception as exc:
        logger.error(
            "Failed connecting to MongoDB at %s: %s",
            db_client.masked_uri,
            exc,
        )
        sys.exit(1)

    # 4. Build Telegram bot application with Gemini and DB injection
    bot_app = create_bot_app(config, gemini_client, db_client)

    # 5. Setup graceful shutdown handling
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def handle_stop_signal() -> None:
        logger.info("Shutdown signal received. Initiating graceful shutdown...")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, handle_stop_signal)
        except (NotImplementedError, AttributeError):
            # Fallback for environments lacking loop.add_signal_handler (e.g. Windows)
            pass

    # 6. Startup Telegram Bot with long polling
    try:
        await start_bot(bot_app)
        logger.info("Application is running and polling for updates. Press Ctrl+C to terminate.")
        await stop_event.wait()
    except (asyncio.CancelledError, KeyboardInterrupt):
        logger.info("Interrupt received during execution.")
    except Exception as exc:
        logger.error("Runtime error encountered: %s", exc, exc_info=True)
    finally:
        # 7. Clean shutdown sequence
        logger.info("Starting application shutdown sequence...")
        try:
            await stop_bot(bot_app)
        except Exception as exc:
            logger.error("Error during bot shutdown: %s", exc)
        try:
            await db_client.close()
        except Exception as exc:
            logger.error("Error closing MongoDB client: %s", exc)
        logger.info("Application shutdown sequence completed.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
