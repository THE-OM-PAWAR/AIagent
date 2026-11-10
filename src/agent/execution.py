"""Execution tracking, concurrency locking, and duplicate protection for the Intelligence Orchestrator."""

import asyncio
from collections import deque
from datetime import datetime, timezone
import logging
import time
from typing import Any
import uuid
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class ExecutionRecord(BaseModel):
    """Detailed metadata and metrics for an individual request execution."""
    execution_id: str
    user_id: int
    conversation_id: str | None = None
    query_preview: str = ""
    start_time: float
    end_time: float | None = None
    duration_seconds: float | None = None
    status: str = "started"  # 'started', 'completed', 'degraded', 'failed'
    tokens_estimated: int = 0
    model_name: str = "gemini-3.6-flash"
    degraded_reasons: list[str] = Field(default_factory=list)
    error_message: str | None = None
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class ExecutionTracker:
    """Thread-safe in-memory ring buffer tracking recent execution metrics."""

    def __init__(self, capacity: int = 500) -> None:
        self.capacity = capacity
        self._records: deque[ExecutionRecord] = deque(maxlen=capacity)
        self._lock = asyncio.Lock()

    async def start_execution(
        self,
        user_id: int,
        query: str,
        conversation_id: str | None = None,
        model_name: str = "gemini-3.6-flash",
    ) -> ExecutionRecord:
        """Create and register a new execution record."""
        exec_id = f"exec_{uuid.uuid4().hex[:12]}"
        preview = query[:40] + ("..." if len(query) > 40 else "")
        record = ExecutionRecord(
            execution_id=exec_id,
            user_id=user_id,
            conversation_id=conversation_id,
            query_preview=preview,
            start_time=time.perf_counter(),
            model_name=model_name,
            status="started",
        )
        async with self._lock:
            self._records.append(record)
        return record

    async def complete_execution(
        self,
        record: ExecutionRecord,
        tokens_estimated: int = 0,
        degraded_reasons: list[str] | None = None,
    ) -> None:
        """Mark an execution as successfully completed (or degraded)."""
        record.end_time = time.perf_counter()
        record.duration_seconds = round(record.end_time - record.start_time, 4)
        record.tokens_estimated = tokens_estimated
        if degraded_reasons:
            record.status = "degraded"
            record.degraded_reasons = list(degraded_reasons)
        else:
            record.status = "completed"

    async def fail_execution(self, record: ExecutionRecord, error_message: str) -> None:
        """Mark an execution as failed with error details."""
        record.end_time = time.perf_counter()
        record.duration_seconds = round(record.end_time - record.start_time, 4)
        record.status = "failed"
        record.error_message = error_message

    async def get_stats(self) -> dict[str, Any]:
        """Aggregate summary metrics across recent executions."""
        async with self._lock:
            total = len(self._records)
            if total == 0:
                return {
                    "total_executions": 0,
                    "completed": 0,
                    "degraded": 0,
                    "failed": 0,
                    "avg_duration_seconds": 0.0,
                }
            completed = sum(1 for r in self._records if r.status == "completed")
            degraded = sum(1 for r in self._records if r.status == "degraded")
            failed = sum(1 for r in self._records if r.status == "failed")
            durations = [r.duration_seconds for r in self._records if r.duration_seconds is not None]
            avg_duration = sum(durations) / len(durations) if durations else 0.0

            return {
                "total_executions": total,
                "completed": completed,
                "degraded": degraded,
                "failed": failed,
                "avg_duration_seconds": round(avg_duration, 4),
            }


class ConversationLockManager:
    """Manages per-conversation concurrency locks to prevent race conditions and corrupted turns."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()

    async def acquire(self, conversation_or_user_key: str | int) -> asyncio.Lock:
        """Get or create a dedicated asyncio.Lock for a conversation or user."""
        key = str(conversation_or_user_key)
        async with self._global_lock:
            if key not in self._locks:
                self._locks[key] = asyncio.Lock()
            return self._locks[key]


class UpdateDeduplicator:
    """Bounded in-memory cache to detect and reject duplicate Telegram update IDs."""

    def __init__(self, capacity: int = 2000) -> None:
        self.capacity = capacity
        self._seen: set[int] = set()
        self._order: deque[int] = deque()
        self._lock = asyncio.Lock()

    async def is_duplicate(self, update_id: int | None) -> bool:
        """Return True if update_id has already been processed within the cache window."""
        if update_id is None:
            return False
        async with self._lock:
            if update_id in self._seen:
                return True
            self._seen.add(update_id)
            self._order.append(update_id)
            if len(self._order) > self.capacity:
                oldest = self._order.popleft()
                self._seen.discard(oldest)
            return False

