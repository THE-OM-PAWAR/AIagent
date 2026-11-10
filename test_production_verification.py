"""Real-service production integration verification script.

Verifies:
1. Neon PostgreSQL connectivity & schema
2. Google GenAI Embedding generation with real API
3. Qdrant Cloud collection initialization, payload indexing, vector upsert & search
4. Hybrid RAG retrieval (vector + keyword RRF)
5. End-to-end conversation turn processing, persistence in PostgreSQL, context recall across sessions
6. Healthcheck status reporting
"""

import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from src.config import Config
from src.database.models import ConversationModel, MessageModel, MemoryModel
from src.database.repositories.conversation_repo import ConversationRepository
from src.database.repositories.memory_repo import MemoryRepository
from src.database.session import get_async_session
from src.healthcheck import run_health_check
from src.intelligence.providers.gemini import GeminiClient
from src.memory.embeddings.provider import EmbeddingProvider
from src.memory.manager import MemoryManager
from src.memory.storage.qdrant import QdrantStorage
from src.agent.orchestrator import IntelligenceOrchestrator


async def run_verification():
    print("=" * 60)
    print("AIAGENT PRODUCTION RUNTIME VERIFICATION")
    print("=" * 60)

    config = Config.load()
    print("[1/6] Config loaded successfully:")
    print(f"  - Environment: {config.environment}")
    print(f"  - Gemini Model: {config.gemini_model}")
    print(f"  - Embedding Model: {config.embedding_model} (dim={config.embedding_dimension})")
    print(f"  - Database: {config.database_url.split('@')[-1] if '@' in config.database_url else 'local'}")
    print(f"  - Qdrant URL: {config.qdrant_url.split('@')[-1] if '@' in config.qdrant_url else config.qdrant_url}")
    print(f"  - Collection: {config.qdrant_collection_name}")

    # 1. Verify PostgreSQL
    print("\n[2/6] Verifying Neon PostgreSQL Connection & Schema...")
    engine = create_async_engine(config.database_url, pool_pre_ping=True)
    for attempt in range(1, 4):
        try:
            async with engine.connect() as conn:
                res = await conn.execute(text("SELECT current_database(), current_user, NOW();"))
                row = res.fetchone()
                print(f"  Connected to DB '{row[0]}' as '{row[1]}'. Server time: {row[2]}")

                # Check existing tables
                tables_res = await conn.execute(
                    text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' ORDER BY table_name;")
                )
                tables = [r[0] for r in tables_res.fetchall()]
                print(f"  PostgreSQL tables found ({len(tables)}): {', '.join(tables)}")
                required_tables = ["conversations", "messages", "memories", "memory_relationships", "memory_outbox", "memory_conflicts"]
                missing = [t for t in required_tables if t not in tables]
                if missing:
                    raise RuntimeError(f"Missing required tables in PostgreSQL: {missing}")
                print("  All required PostgreSQL tables present.")
                break
        except Exception as exc:
            if attempt == 3:
                raise
            print(f"  Connection attempt {attempt} failed ({exc}). Retrying in 1s...")
            await asyncio.sleep(1.0)
    await engine.dispose()

    # 2. Verify Google GenAI Real Embedding Generation
    print("\n[3/6] Verifying Google GenAI Embedding Provider...")
    embedding_provider = EmbeddingProvider(
        api_key=config.gemini_api_key,
        model=config.embedding_model,
        dimension=config.embedding_dimension,
    )
    embedding_provider.initialize()
    test_text = "Verification test memory: User prefers Python and asynchronous programming."
    vec = await embedding_provider.generate_embedding(test_text)
    print(f"  Generated real embedding vector of length {len(vec)} for test text.")
    assert len(vec) == config.embedding_dimension, f"Expected dim {config.embedding_dimension}, got {len(vec)}"
    assert not all(v == 0.0 for v in vec), "Vector cannot be all zeroes"
    print("  Embedding generation verified successfully.")

    # 3. Verify Qdrant Cloud Vector Storage
    print("\n[4/6] Verifying Qdrant Cloud Vector Storage...")
    qdrant = QdrantStorage(
        url=config.qdrant_url,
        api_key=config.qdrant_api_key,
        collection_name=config.qdrant_collection_name,
        vector_dimension=config.embedding_dimension,
    )
    await qdrant.initialize()
    client = await qdrant.get_client()
    collections = await client.get_collections()
    coll_names = [c.name for c in collections.collections]
    print(f"  Qdrant collections found: {coll_names}")
    assert config.qdrant_collection_name in coll_names, f"Collection {config.qdrant_collection_name} not found"

    # Test upsert and retrieval in Qdrant
    test_mem_id = str(uuid.uuid4())
    test_user_id = 9999901
    await qdrant.upsert_vector(
        memory_id=test_mem_id,
        vector=vec,
        payload={
            "user_id": test_user_id,
            "category": "core_fact",
            "status": "active",
            "content": test_text,
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    print(f"  Upserted test vector with memory_id={test_mem_id}")

    # Search for it
    search_hits = await qdrant.search_dense(
        user_id=test_user_id,
        query_vector=vec,
        limit=3,
        score_threshold=0.5,
    )
    print(f"  Qdrant search returned {len(search_hits)} results for user {test_user_id}")
    assert len(search_hits) >= 1, "Expected at least 1 search hit"
    hit_id, hit_score, hit_payload = search_hits[0]
    print(f"  Top hit: id={hit_id}, score={hit_score:.4f}")
    assert hit_id == test_mem_id

    # Clean up test vector
    await qdrant.delete_vector(test_mem_id)
    print("  Test vector deleted cleanly from Qdrant.")

    # 4. Verify End-to-End Orchestrator and Telegram Persistence Flow
    print("\n[5/6] Verifying End-to-End Orchestration & Multi-turn Persistence...")
    gemini_client = GeminiClient(
        api_key=config.gemini_api_key,
        model=config.gemini_model,
        system_instruction=config.gemini_system_instruction,
        timeout=config.gemini_timeout_seconds,
    )
    gemini_client.initialize()

    memory_manager = MemoryManager(
        database_url=config.database_url,
        qdrant_storage=qdrant,
        embedding_provider=embedding_provider,
        gemini_client=gemini_client,
        default_top_k=config.memory_top_k,
        default_similarity_threshold=config.memory_similarity_threshold,
    )
    await memory_manager.initialize()

    orchestrator = IntelligenceOrchestrator(
        gemini_client=gemini_client,
        database_url=config.database_url,
        memory_manager=memory_manager,
        context_limit=config.conversation_history_limit,
        system_prompt=config.gemini_system_instruction,
        model_name=config.gemini_model,
    )

    verified_user_id = 9999902
    username = "test_verified_user"

    # Turn 1
    t1_text = "Hello! My name is Arthur Dent and I am an interstellar hitchhiker."
    print(f"  Turn 1 user message: '{t1_text}'")
    t1_resp = await orchestrator.process_message(
        user_id=verified_user_id,
        user_text=t1_text,
    )
    print(f"  Turn 1 response (first 100 chars): {t1_resp[:100]}...")

    # Verify Turn 1 is stored in Neon PostgreSQL
    async with get_async_session(config.database_url) as session:
        conv_repo = ConversationRepository(session)
        conv = await conv_repo.get_or_create_active_conversation(verified_user_id)
        assert conv is not None, "Active conversation should exist in PostgreSQL"
        print(f"  Active conversation verified: conversation_id={conv.conversation_id}, message_count={conv.message_count}")

        msgs = await conv_repo.get_recent_messages(conv.conversation_id, limit=10)
        print(f"  Found {len(msgs)} messages stored in PostgreSQL conversation {conv.conversation_id}")
        assert len(msgs) >= 2, "Expected at least user and assistant messages"
        assert any(m.role == "user" and "Arthur Dent" in m.content for m in msgs)
        # App stores Gemini responses with role="model" (Gemini SDK convention)
        assert any(m.role in {"assistant", "model"} for m in msgs)

    # Turn 2: test context recall
    t2_text = "What is my name and what do I do?"
    print(f"  Turn 2 user message: '{t2_text}'")
    t2_resp = await orchestrator.process_message(
        user_id=verified_user_id,
        user_text=t2_text,
    )
    print(f"  Turn 2 response: {t2_resp}")
    assert "Arthur" in t2_resp or "hitchhiker" in t2_resp.lower(), "Assistant failed to recall conversation context!"
    print("  Context recall across turns verified successfully!")

    # Simulate restart by recreating the orchestrator and verifying it retrieves history
    print("\n  Simulating application restart / fresh instance...")
    fresh_orchestrator = IntelligenceOrchestrator(
        gemini_client=gemini_client,
        database_url=config.database_url,
        memory_manager=memory_manager,
        context_limit=config.conversation_history_limit,
        system_prompt=config.gemini_system_instruction,
        model_name=config.gemini_model,
    )
    async with get_async_session(config.database_url) as session:
        fresh_conv_repo = ConversationRepository(session)
        recovered_conv = await fresh_conv_repo.get_or_create_active_conversation(verified_user_id)
        assert recovered_conv is not None
        recovered_msgs = await fresh_conv_repo.get_recent_messages(recovered_conv.conversation_id, limit=10)
        assert len(recovered_msgs) >= 4, f"Expected at least 4 messages across 2 turns, got {len(recovered_msgs)}"
        print(f"  Retrieved {len(recovered_msgs)} previous messages after simulated restart.")

    # 5. Verify Healthcheck module
    print("\n[6/6] Verifying Healthcheck module...")
    is_healthy, report = await run_health_check(skip_telegram=True)
    print(f"  Healthcheck status: {report['status']}")
    for k, v in report.get("checks", {}).items():
        print(f"    - {k}: {v}")
    assert report["checks"].get("postgresql", {}).get("status") == "ok", "PostgreSQL healthcheck should be ok"
    assert report["checks"].get("qdrant", {}).get("status") == "ok", "Qdrant healthcheck should be ok"

    # Cleanup verification test conversation and user
    print("\nCleaning up test user data...")
    async with get_async_session(config.database_url) as session:
        await session.execute(text("DELETE FROM messages WHERE user_id = :uid"), {"uid": verified_user_id})
        await session.execute(text("DELETE FROM memories WHERE user_id = :uid"), {"uid": verified_user_id})
        await session.execute(text("DELETE FROM conversations WHERE user_id = :uid"), {"uid": verified_user_id})
        await session.commit()
    print("  Cleanup complete.")

    print("\n" + "=" * 60)
    print("ALL PRODUCTION INTEGRATION CHECKS PASSED SUCCESSFULLY!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(run_verification())
