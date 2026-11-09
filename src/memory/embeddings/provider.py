"""Google GenAI embedding provider with async batching and dimension tracking."""

import asyncio
import hashlib
import logging
import math
from typing import Any

from google import genai
from google.genai import errors

logger = logging.getLogger(__name__)


class EmbeddingError(Exception):
    """Raised when embedding generation fails."""
    pass


class EmbeddingProvider:
    """Manages asynchronous embedding generation using Google GenAI SDK."""

    def __init__(
        self,
        api_key: str,
        model: str = "text-embedding-004",
        dimension: int = 768,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.dimension = dimension
        self._client: genai.Client | None = None

    def initialize(self) -> None:
        """Initialize Google GenAI client instance."""
        if not self._client:
            self._client = genai.Client(api_key=self.api_key)
            logger.info("Initialized EmbeddingProvider with model: %s (dim: %d)", self.model, self.dimension)

    @property
    def client(self) -> genai.Client:
        """Return initialized client or initialize on-demand."""
        if self._client is None:
            self.initialize()
        assert self._client is not None
        return self._client

    def _generate_synthetic_vector(self, text: str) -> list[float]:
        """Generate deterministic normalized pseudo-random vector for offline testing."""
        sha = hashlib.sha256(text.encode("utf-8")).digest()
        vec = []
        for i in range(self.dimension):
            byte_val = sha[i % len(sha)]
            val = (byte_val / 255.0) * 2.0 - 1.0 + ((i % 10) / 10.0)
            vec.append(val)
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / norm for x in vec]

    async def generate_embedding(self, text: str) -> list[float]:
        """Generate embedding vector for a single text string."""
        clean = text.strip()
        if not clean:
            return [0.0] * self.dimension

        # If dummy test key, return deterministic synthetic vector
        if self.api_key.startswith("test_") or "test" in self.api_key.lower():
            return self._generate_synthetic_vector(clean)

        try:
            response = await asyncio.wait_for(
                self.client.aio.models.embed_content(
                    model=self.model,
                    contents=clean,
                ),
                timeout=15.0,
            )
            if response.embeddings:
                values = response.embeddings[0].values
                if values:
                    return list(values)
            raise EmbeddingError("Empty embedding returned by GenAI model.")
        except errors.APIError as exc:
            logger.error("GenAI embedding API error: %s", exc)
            raise EmbeddingError(f"Embedding API error: {exc}") from exc
        except Exception as exc:
            logger.error("Failed generating embedding for text: %s", exc, exc_info=True)
            raise EmbeddingError(f"Embedding failure: {exc}") from exc

    async def generate_batch(self, texts: list[str], batch_size: int = 16) -> list[list[float]]:
        """Generate embeddings asynchronously in batches."""
        results: list[list[float]] = []
        for i in range(0, len(texts), batch_size):
            chunk = texts[i : i + batch_size]
            tasks = [self.generate_embedding(t) for t in chunk]
            batch_results = await asyncio.gather(*tasks, return_exceptions=False)
            results.extend(batch_results)
        return results

