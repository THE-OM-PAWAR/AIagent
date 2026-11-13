"""Asynchronous application entry point for the Telegram AI Assistant."""

import utils.session
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
    workflow_scheduler = None
    if getattr(config, "database_url", None):
        try:
            from src.core.workflow_engine import WorkflowEngine
            from src.core.workflow_manager import WorkflowManager
            from src.core.workflow_planner import WorkflowPlanner
            from src.core.workflow_scheduler import WorkflowScheduler
            from src.database.models import Base
            from src.database.session import get_sessionmaker
            from src.tools.executor import ToolExecutor

            engine = get_engine(config.database_url)
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

            session_factory = get_sessionmaker(config.database_url)
            wf_executor = ToolExecutor(orchestrator.tool_registry)
            wf_manager = WorkflowManager(
                session_factory=session_factory,
                tool_registry=orchestrator.tool_registry,
                permission_policy=orchestrator.permission_policy,
            )
            workflow_engine = WorkflowEngine(
                session_factory=session_factory,
                tool_executor=wf_executor,
                workflow_manager=wf_manager,
            )
            wf_planner = WorkflowPlanner(
                workflow_manager=wf_manager,
                workflow_engine=workflow_engine,
                llm_manager=orchestrator.llm_manager,
                gemini_client=gemini_client,
            )
            workflow_engine.workflow_planner = wf_planner
            orchestrator.workflow_engine = workflow_engine

            workflow_scheduler = WorkflowScheduler(
                session_factory=session_factory,
                workflow_engine=workflow_engine,
                poll_interval_seconds=float(getattr(config, "scheduler_poll_interval", 5.0)),
            )
            workflow_engine.workflow_scheduler = workflow_scheduler
            workflow_scheduler.start()

            recovered = await workflow_engine.recover_on_startup()
            if recovered > 0:
                logger.info("Recovered %d interrupted workflows on startup.", recovered)
        except Exception as exc:
            logger.warning("WorkflowEngine initialization failed (%s). Continuing without persistent workflows.", exc)
            workflow_engine = None
            workflow_scheduler = None

    # 5c. Initialize Integration Framework & Connectors
    integration_registry = None
    try:
        from src.integrations.connectors.web_search import WebSearchConnector
        from src.integrations.registry import IntegrationRegistry
        from src.integrations.security.crypto import CredentialEncryptor

        enc_key = config.integration_encryption_key or config.telegram_bot_token
        encryptor = CredentialEncryptor(enc_key)
        integration_registry = IntegrationRegistry(encryptor=encryptor)

        # Register Web Search Connector
        sys_search_key = config.tavily_api_key or config.brave_api_key
        search_conn = WebSearchConnector(
            default_provider=config.search_provider,
            system_api_key=sys_search_key,
        )
        integration_registry.register_connector(search_conn)

        # Register Google Workspace, Places, and Calendly Connectors
        from src.integrations.connectors.calendar import GoogleCalendarConnector
        from src.integrations.connectors.calendly import CalendlyConnector, CalendlyOAuthClient
        from src.integrations.connectors.drive import GoogleDriveConnector
        from src.integrations.connectors.gmail import GmailConnector
        from src.integrations.connectors.google import GoogleOAuthClient, GoogleWorkspaceConnector
        from src.integrations.connectors.maps import GooglePlacesConnector

        google_oauth_client = GoogleOAuthClient(
            client_id=config.google_client_id,
            client_secret=config.google_client_secret,
            redirect_uri=config.google_redirect_uri,
        )
        calendly_oauth_client = CalendlyOAuthClient(
            client_id=config.calendly_client_id,
            client_secret=config.calendly_client_secret,
            redirect_uri=config.calendly_redirect_uri,
        )

        integration_registry.register_connector(GoogleWorkspaceConnector(oauth_client=google_oauth_client, database_url=config.database_url))
        integration_registry.register_connector(GmailConnector(database_url=config.database_url))
        integration_registry.register_connector(GoogleCalendarConnector(database_url=config.database_url))
        integration_registry.register_connector(GoogleDriveConnector(database_url=config.database_url))
        integration_registry.register_connector(GooglePlacesConnector(system_api_key=config.google_maps_api_key, database_url=config.database_url))
        integration_registry.register_connector(CalendlyConnector(oauth_client=calendly_oauth_client, system_api_key=config.calendly_api_key, database_url=config.database_url))

        logger.info("Integration framework initialized with Web Search, Google Workspace, Places, and Calendly connectors.")
    except Exception as exc:
        logger.warning("Integration framework initialization skipped (%s). Continuing in standard mode.", exc)
        integration_registry = None

    # Register Phase 2 & 3 built-in tools and external integration tools into registry
    orchestrator.tool_registry.register_builtin_tools(
        memory_manager=memory_manager,
        llm_manager=orchestrator.llm_manager,
        workflow_engine=workflow_engine,
        integration_registry=integration_registry,
        database_url=config.database_url,
        system_search_key=config.tavily_api_key or config.brave_api_key,
        google_maps_key=config.google_maps_api_key,
        calendly_token=config.calendly_api_key,
    )

    # 6. Build Telegram bot application with orchestrator injection
    bot_app = create_bot_app(
        config,
        gemini_client,
        orchestrator=orchestrator,
        integration_registry=integration_registry,
        workflow_engine=workflow_engine,
        workflow_scheduler=workflow_scheduler,
    )

    if workflow_scheduler is not None:
        async def _tg_scheduler_notify(user_id: int, message_html: str) -> None:
            try:
                from telegram.constants import ParseMode
                await bot_app.bot.send_message(
                    chat_id=user_id,
                    text=message_html,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            except Exception as notify_err:
                logger.warning("Could not dispatch scheduled Telegram alert to user %s: %s", user_id, notify_err)

        workflow_scheduler.notification_handler = _tg_scheduler_notify

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
        if workflow_scheduler and workflow_scheduler.is_running:
            try:
                await workflow_scheduler.stop()
            except Exception as exc:
                logger.error("Error stopping WorkflowScheduler: %s", exc)
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
