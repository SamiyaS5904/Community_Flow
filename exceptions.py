"""
exceptions.py — Typed exception hierarchy for the entire pipeline.

Each service and pipeline stage raises its own typed exception so callers
can catch exactly what they need without bare `except Exception` blocks.
"""
from __future__ import annotations


class CarrotOwlError(Exception):
    """Base exception for all pipeline errors."""


# ── Per-stage exceptions ───────────────────────────────────────────────────────


class PlannerError(CarrotOwlError):
    """Raised when the Planner agent fails to produce a valid ContentPlan."""


class ResearchError(CarrotOwlError):
    """Raised when the Research agent fails to produce a ResearchBrief."""


class GenerationError(CarrotOwlError):
    """Raised when the Content Generator agent fails to produce a DraftContent."""


class QAError(CarrotOwlError):
    """Raised when the Quality Checker agent encounters an unexpected failure."""


class PublishError(CarrotOwlError):
    """Raised when Telegram publishing fails after all retries are exhausted."""


class SearchError(CarrotOwlError):
    """Raised when the web search tool fails."""


class DedupError(CarrotOwlError):
    """Raised on unexpected deduplication failures (not on is_duplicate=True)."""


class StateStoreError(CarrotOwlError):
    """Raised when the SQLite state store encounters a persistence failure."""


class PendingWriteError(CarrotOwlError):
    """Raised when a pending_writes.jsonl entry cannot be written — critical."""


class ConfigurationError(CarrotOwlError):
    """Raised when required config/env vars are missing or invalid."""


class RegistryError(CarrotOwlError):
    """Raised when a content type cannot be loaded from the registry."""



class TelegramError(PublishError):
    """A Telegram API call failed, carrying Telegram's own description.

    Subclasses PublishError so existing publish handling still catches it, but
    the message is the API's own words rather than "publish failed", which is
    what the dashboard shows the operator.
    """


class EmbeddingError(CarrotOwlError):
    """An embedding could not be produced, so dedup cannot be trusted."""
