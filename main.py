"""Asynchronous application entry point for the Telegram AI Assistant."""

import asyncio
import signal
import sys

from src.config import Config, ConfigError
from src.core.orchestrator import AgentOrchestrator
from src.database.session import close_engine, get_engine
from src.intelligence.providers.gemini import GeminiClient
from src.interfaces.telegram import create_bot_app, start_bot, stop_bot
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

    # 3. Initialize core services (PostgreSQL & Gemini)
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

    # Verify PostgreSQL (Neon) connection with retry backoff for containerized environments
    max_retries = 5
    for attempt in range(1, max_retries + 1):
        try:
            from sqlalchemy import text
            engine = get_engine(config.database_url)
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            logger.info("Connected to PostgreSQL successfully.")
            break
        except Exception as exc:
            if attempt == max_retries:
                logger.error(
                    "Failed connecting to PostgreSQL after %d attempts: %s",
                    max_retries,
                    exc,
                )
                sys.exit(1)
            logger.warning(
                "PostgreSQL connection attempt %d/%d failed (%s). Retrying in 2 seconds...",
                attempt,
                max_retries,
                exc,
            )
            await asyncio.sleep(2)

    # 4. Initialize Phase 1 Memory Manager (if available)
    memory_manager = None
    if getattr(config, "database_url", None):
        try:
            from src.memory.embeddings.provider import EmbeddingProvider
            from src.memory.manager import MemoryManager
            from src.memory.storage.qdrant import QdrantStorage

            embedding_provider = EmbeddingProvider(
                api_key=config.gemini_api_key,
                model_name=config.embedding_model,
                dimension=config.embedding_dimension,
            )
            qdrant_storage = QdrantStorage(
                url=config.qdrant_url,
                api_key=config.qdrant_api_key,
                collection_name=config.qdrant_collection_name,
                vector_dimension=config.embedding_dimension,
            )
            memory_manager = MemoryManager(
                database_url=config.database_url,
                qdrant_storage=qdrant_storage,
                embedding_provider=embedding_provider,
                default_top_k=config.memory_top_k,
                default_similarity_threshold=config.memory_similarity_threshold,
            )
            await memory_manager.initialize()
            logger.info("MemoryManager initialized successfully for RAG.")
        except Exception as exc:
            logger.warning("MemoryManager initialization skipped (%s). Continuing in standard mode.", exc)
            memory_manager = None

    # 5. Initialize Agent Orchestration Layer
    orchestrator = AgentOrchestrator(
        gemini_client=gemini_client,
        database_url=config.database_url,
        memory_manager=memory_manager,
        context_limit=config.conversation_history_limit,
        system_prompt=config.gemini_system_instruction,
        model_name=config.gemini_model,
    )

    # 5b. Initialize Workflow Engine & Persistent State
    workflow_engine = None
    if getattr(config, "database_url", None):
        try:
            from src.core.workflow_engine import WorkflowEngine
            from src.database.models import Base
            from src.database.session import get_sessionmaker
            from src.tools.executor import ToolExecutor

            engine = get_engine(config.database_url)
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

            session_factory = get_sessionmaker(config.database_url)
            wf_executor = ToolExecutor(orchestrator.tool_registry)
            workflow_engine = WorkflowEngine(session_factory=session_factory, tool_executor=wf_executor)
            recovered = await workflow_engine.recover_on_startup()
            if recovered > 0:
                logger.info("Recovered %d interrupted workflows on startup.", recovered)
        except Exception as exc:
            logger.warning("WorkflowEngine initialization failed (%s). Continuing without persistent workflows.", exc)
            workflow_engine = None

    # Register Phase 2 & 3 built-in tools into orchestrator tool registry
    orchestrator.tool_registry.register_builtin_tools(
        memory_manager=memory_manager,
        llm_manager=orchestrator.llm_manager,
        workflow_engine=workflow_engine,
    )

    # 6. Build Telegram bot application with orchestrator injection
    bot_app = create_bot_app(config, gemini_client, orchestrator=orchestrator)

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
            await close_engine()
        except Exception as exc:
            logger.error("Error closing PostgreSQL engine: %s", exc)
        logger.info("Application shutdown sequence completed.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
