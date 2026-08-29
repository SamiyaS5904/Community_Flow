"""
services/embedding_service.py
=============================
Turns topic text into vectors, so "have we said this before?" can be answered
by meaning rather than by string match.

This is what makes the dedup gate worth having: "How to write a resume" and
"Resume writing tips that actually work" share almost no words but say the same
thing, and the old approach — pasting recent topic strings into a prompt and
asking the model not to repeat them — could never catch that. (It also never
ran on the live path, where the list was passed but never used.)

Uses the same OPENAI_API_KEY as text generation. text-embedding-3-small costs
about $0.02 per million tokens, so a topic title is a rounding error.
"""
from __future__ import annotations

import logging
import time

import openai

log = logging.getLogger(__name__)

MODEL = "text-embedding-3-small"
DIMENSIONS = 1536


from exceptions import EmbeddingError  # re-exported: callers import it from here


class EmbeddingService:
    MAX_RETRIES = 3
    RETRY_DELAY = 2

    def __init__(self, api_key: str, model: str = MODEL, timeout: float = 30.0):
        self.model = model
        self._enabled = bool(api_key)
        self.client = openai.OpenAI(api_key=api_key, timeout=timeout) if api_key else None

    @property
    def enabled(self) -> bool:
        return self._enabled

    def embed(self, text: str) -> list[float]:
        """Embed one string. Raises EmbeddingError rather than returning None,
        so a caller cannot mistake a failure for "no near duplicate"."""
        return self.embed_many([text])[0]

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch in one call — the API charges per token, not per request."""
        if not texts:
            return []
        if not self._enabled:
            raise EmbeddingError(
                "OPENAI_API_KEY is not set, so topics cannot be embedded and "
                "deduplication would silently pass everything."
            )

        cleaned = [(t or "").strip().replace("\n", " ") for t in texts]
        last_error: Exception | None = None

        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                response = self.client.embeddings.create(model=self.model, input=cleaned)
                return [item.embedding for item in response.data]
            except openai.RateLimitError as exc:
                last_error = exc
                log.warning("Embedding rate limited (attempt %d), backing off", attempt)
                time.sleep(self.RETRY_DELAY * attempt)
            except Exception as exc:
                last_error = exc
                log.warning("Embedding attempt %d failed: %s", attempt, exc)
                time.sleep(self.RETRY_DELAY)

        raise EmbeddingError(
            f"Could not embed {len(texts)} text(s) after {self.MAX_RETRIES} attempts: {last_error}"
        )
