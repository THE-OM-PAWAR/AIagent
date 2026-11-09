"""Asynchronous PostgreSQL session and connection pool management using SQLAlchemy 2.x."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
import logging
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

logger = logging.getLogger(__name__)

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine(database_url: str) -> AsyncEngine:
    """Return or initialize global async engine with resilient connection pooling."""
    global _engine, _sessionmaker
    if _engine is None:
        logger.info("Initializing SQLAlchemy async engine...")
        is_sqlite = database_url.startswith("sqlite")
        engine_kwargs: dict[str, Any] = {
            "echo": False,
            "future": True,
        }
        if not is_sqlite:
            # Neon PostgreSQL connection pool parameters
            engine_kwargs.update(
                {
                    "pool_size": 10,
                    "max_overflow": 20,
                    "pool_pre_ping": True,
                    "pool_recycle": 300,
                }
            )

        _engine = create_async_engine(database_url, **engine_kwargs)
        _sessionmaker = async_sessionmaker(
            bind=_engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
    return _engine


def get_sessionmaker(database_url: str | None = None) -> async_sessionmaker[AsyncSession]:
    """Return configured async sessionmaker."""
    global _sessionmaker
    if _sessionmaker is None:
        if database_url is None:
            raise RuntimeError("Database engine not initialized. Provide database_url first.")
        get_engine(database_url)
    assert _sessionmaker is not None
    return _sessionmaker


@asynccontextmanager
async def get_async_session(database_url: str | None = None) -> AsyncGenerator[AsyncSession, None]:
    """Yield an asynchronous database session within an explicit transaction context."""
    sm = get_sessionmaker(database_url)
    async with sm() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def close_engine() -> None:
    """Dispose active engine connection pools gracefully."""
    global _engine, _sessionmaker
    if _engine is not None:
        logger.info("Disposing SQLAlchemy async engine...")
        await _engine.dispose()
        _engine = None
        _sessionmaker = None

