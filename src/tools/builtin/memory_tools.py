"""Production-grade memory operations tools connecting directly to the MemoryManager RAG engine."""

import logging
from typing import Any

from src.memory.manager import MemoryManager
from src.memory.schemas.memory import FactType, MemoryCandidate, MemoryCategory, MemoryUpdate
from src.tools.base import BaseTool
from src.tools.permissions import ToolPermission

logger = logging.getLogger(__name__)


class MemoryOperationsTool(BaseTool):
    """Tool exposing search, save, update, and archive memory operations to the agent."""

    def __init__(self, memory_manager: MemoryManager | None = None) -> None:
        self.memory_manager = memory_manager

    @property
    def name(self) -> str:
        return "memory_operations"

    @property
    def description(self) -> str:
        return (
            "Access long-term persistent memory: search relevant user facts and project knowledge (RAG), "
            "save new verified personal facts, update existing memories, or archive outdated memories. "
            "Protects against silent overwrites and runs automated conflict detection."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "Memory operation to execute.",
                    "enum": ["search_memory", "save_memory", "update_memory", "archive_memory"],
                },
                "query": {
                    "type": "string",
                    "description": "Semantic search query for 'search_memory'.",
                },
                "content": {
                    "type": "string",
                    "description": "Memory content string for 'save_memory' or 'update_memory'.",
                },
                "category": {
                    "type": "string",
                    "description": "Memory category for classification.",
                    "enum": ["semantic", "episodic", "project", "task", "working"],
                },
                "memory_id": {
                    "type": "string",
                    "description": "UUID identifier of the memory to update or archive.",
                },
                "importance": {
                    "type": "number",
                    "description": "Importance score between 0.0 and 1.0 (default 0.6).",
                },
                "project_association": {
                    "type": "string",
                    "description": "Optional project tag or code associating this memory.",
                },
                "top_k": {
                    "type": "integer",
                    "description": "Maximum number of search results to retrieve (default 5).",
                },
            },
            "required": ["action"],
        }

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.WRITE

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        if not self.memory_manager:
            return {
                "action": kwargs.get("action"),
                "success": False,
                "error": "MemoryManager is not configured or unavailable in current environment.",
            }

        action = kwargs.get("action", "search_memory")
        # ToolExecutor passes user_id in kwargs or context
        user_id = kwargs.get("user_id", 101)

        if action == "search_memory":
            query = kwargs.get("query")
            if not query:
                raise ValueError("The 'query' parameter is required for action='search_memory'.")

            cat_str = kwargs.get("category")
            categories = [MemoryCategory(cat_str)] if cat_str else None
            top_k = kwargs.get("top_k", 5)

            results = await self.memory_manager.retrieve(
                user_id=user_id,
                query=query,
                categories=categories,
                top_k=top_k,
            )

            formatted_results = []
            for r in results:
                formatted_results.append({
                    "memory_id": r.memory_id,
                    "content": r.content,
                    "category": r.category.value,
                    "importance": r.importance,
                    "score": round(r.fused_score, 4),
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                })

            return {
                "action": "search_memory",
                "query": query,
                "results_count": len(formatted_results),
                "memories": formatted_results,
            }

        elif action == "save_memory":
            content = kwargs.get("content")
            if not content:
                raise ValueError("The 'content' parameter is required for action='save_memory'.")

            cat_str = kwargs.get("category", "semantic")
            try:
                category = MemoryCategory(cat_str)
            except ValueError:
                category = MemoryCategory.SEMANTIC

            importance = kwargs.get("importance", 0.6)
            project_assoc = kwargs.get("project_association")

            candidate = MemoryCandidate(
                category=category,
                content=content.strip(),
                fact_type=FactType.EXPLICIT_FACT,
                importance=min(max(importance, 0.0), 1.0),
                confidence=1.0,
                project_association=project_assoc,
            )

            record = await self.memory_manager.remember(
                user_id=user_id,
                candidate=candidate,
                source_reference={"origin": "tool_memory_operations"},
            )

            if record:
                return {
                    "action": "save_memory",
                    "success": True,
                    "memory_id": record.id,
                    "content": record.content,
                    "category": record.category.value,
                    "status": "Saved successfully in PostgreSQL and synchronized to Qdrant.",
                }
            else:
                return {
                    "action": "save_memory",
                    "success": False,
                    "status": "Conflict or deduplication detected; established memory preserved without overwrite.",
                }

        elif action == "update_memory":
            memory_id = kwargs.get("memory_id")
            content = kwargs.get("content")
            if not memory_id:
                raise ValueError("The 'memory_id' parameter is required for action='update_memory'.")
            if not content:
                raise ValueError("The 'content' parameter is required for action='update_memory'.")

            importance = kwargs.get("importance")
            update_data = MemoryUpdate(
                content=content.strip(),
                importance=importance,
            )

            updated = await self.memory_manager.update_memory(
                user_id=user_id,
                memory_id=memory_id,
                update_data=update_data,
            )

            if updated:
                return {
                    "action": "update_memory",
                    "success": True,
                    "memory_id": updated.id,
                    "new_content": updated.content,
                    "status": "Memory updated and vector refreshed.",
                }
            else:
                return {
                    "action": "update_memory",
                    "success": False,
                    "error": f"Memory with id '{memory_id}' not found for user {user_id}.",
                }

        elif action == "archive_memory":
            memory_id = kwargs.get("memory_id")
            if not memory_id:
                raise ValueError("The 'memory_id' parameter is required for action='archive_memory'.")

            success = await self.memory_manager.archive_memory(
                user_id=user_id,
                memory_id=memory_id,
            )

            return {
                "action": "archive_memory",
                "success": success,
                "memory_id": memory_id,
                "status": "Archived" if success else f"Memory '{memory_id}' not found.",
            }

        raise ValueError(f"Unknown memory action '{action}'.")

