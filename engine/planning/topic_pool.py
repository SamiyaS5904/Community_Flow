"""
engine/planning/topic_pool.py
=============================
The living topic pool and the gate that keeps it from repeating itself.

The old design could not avoid repeating: a group's plan held 155 fixed topic
strings and the day number wrapped, so day 31 republished day 1 word for word,
forever. Topics now live in a pool that is continuously replenished, and
every candidate passes two gates before it is admitted:

  1. Editorial guardrails from the group's strategy.json — never_cover,
     banned_phrases, a weekly cap. This is where editorial judgement lives,
     written once in config rather than applied by hand to each topic.
  2. Semantic deduplication against everything the group has already scheduled
     or published.

Discovery admits topics without human approval on purpose: the goal is
hands-off automation, and safety comes from these gates rather than from a
person in the loop. A human still approves every *post* before it publishes.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Sequence

from engine.group_config import GroupConfig
from services.embedding_service import EmbeddingError, EmbeddingService
from services.storage.db import session_scope
from services.storage import repositories as repo
from services.storage.models import Topic, TopicSource, TopicStatus

log = logging.getLogger(__name__)


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity. Embeddings arrive normalised, but do not assume it."""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0

#: Above this, a candidate says the same thing as something already used.
DEFAULT_DUPLICATE_THRESHOLD = 0.86
#: Between this and the duplicate threshold, admit but record what it resembles.
DEFAULT_SIMILAR_THRESHOLD = 0.78


class Verdict:
    ADMITTED = "admitted"
    ADMITTED_SIMILAR = "admitted_similar"
    REJECTED_DUPLICATE = "rejected_duplicate"
    REJECTED_GUARDRAIL = "rejected_guardrail"
    REJECTED_CAP = "rejected_cap"
    REJECTED_ERROR = "rejected_error"


@dataclass
class Decision:
    """What the gate decided about one candidate, and why.

    Rejections are returned rather than silently dropped so the dashboard can
    show which guardrail is doing the work — a pool that admits nothing and a
    pool that is simply quiet look identical otherwise.
    """

    title: str
    verdict: str
    reason: str = ""
    similar_to: str | None = None
    similarity: float | None = None
    topic_id: str | None = None

    @property
    def admitted(self) -> bool:
        return self.verdict in (Verdict.ADMITTED, Verdict.ADMITTED_SIMILAR)


@dataclass
class Guardrails:
    """Editorial rules, declared per group in strategy.json."""

    discovery_queries: list[str] = field(default_factory=list)
    must_cover: list[str] = field(default_factory=list)
    never_cover: list[str] = field(default_factory=list)
    banned_phrases: list[str] = field(default_factory=list)
    freshness_days: int = 45
    max_new_per_week: int = 20
    duplicate_threshold: float = DEFAULT_DUPLICATE_THRESHOLD
    similar_threshold: float = DEFAULT_SIMILAR_THRESHOLD

    @classmethod
    def from_strategy(cls, strategy: dict | None) -> "Guardrails":
        raw = (strategy or {}).get("editorial_guardrails", {}) or {}
        defaults = cls()
        return cls(
            discovery_queries=list(raw.get("discovery_queries", [])),
            must_cover=list(raw.get("must_cover", [])),
            never_cover=list(raw.get("never_cover", [])),
            banned_phrases=list(raw.get("banned_phrases", [])),
            freshness_days=int(raw.get("freshness_days", defaults.freshness_days)),
            max_new_per_week=int(raw.get("max_new_per_week", defaults.max_new_per_week)),
            duplicate_threshold=float(
                raw.get("duplicate_threshold", defaults.duplicate_threshold)),
            similar_threshold=float(
                raw.get("similar_threshold", defaults.similar_threshold)),
        )

    def violation(self, title: str) -> str | None:
        """The guardrail this title breaks, or None if it passes."""
        lowered = (title or "").lower()
        if not lowered.strip():
            return "empty title"
        for phrase in self.never_cover:
            if phrase.lower() in lowered:
                return f"never_cover: {phrase!r}"
        for phrase in self.banned_phrases:
            if phrase.lower() in lowered:
                return f"banned_phrase: {phrase!r}"
        return None


class TopicPool:
    """Admission, lookup and lifecycle for one group's topics."""

    def __init__(self, group: GroupConfig, embedder: EmbeddingService,
                 guardrails: Guardrails | None = None):
        self.group = group
        self.embedder = embedder
        self.guardrails = guardrails or Guardrails()
        self._embedding_cache: dict[str, list[float]] = {}

    # ── admission ────────────────────────────────────────────────────────────

    def admit(
        self,
        title: str,
        *,
        source: str = TopicSource.MANUAL,
        source_url: str | None = None,
        category: str = "",
        content_type: str = "message",
        angle: str = "",
        enforce_cap: bool = True,
    ) -> Decision:
        """Run one candidate through both gates."""
        title = (title or "").strip()

        violation = self.guardrails.violation(title)
        if violation:
            log.info("Topic rejected by guardrail (%s): %r", violation, title)
            return Decision(title, Verdict.REJECTED_GUARDRAIL, reason=violation)

        if enforce_cap and source == TopicSource.DISCOVERY:
            added = self._added_this_week()
            if added >= self.guardrails.max_new_per_week:
                return Decision(
                    title, Verdict.REJECTED_CAP,
                    reason=f"weekly cap reached ({added}/{self.guardrails.max_new_per_week})",
                )

        try:
            embedding = self._embed_cached(title)
        except EmbeddingError as exc:
            # Admitting unembedded would let the next candidate duplicate it
            # undetected, so refuse and say why.
            log.error("Could not embed %r: %s", title, exc)
            return Decision(title, Verdict.REJECTED_ERROR, reason=str(exc))

        with session_scope() as session:
            match = repo.nearest_topic(session, self.group.id, embedding)
            similar_to = similarity = None
            if match:
                nearest, score = match
                if score >= self.guardrails.duplicate_threshold:
                    log.info("Topic rejected as duplicate (%.3f of %r): %r",
                             score, nearest.title, title)
                    return Decision(
                        title, Verdict.REJECTED_DUPLICATE,
                        reason=f"{score:.2f} similar to an existing topic",
                        similar_to=nearest.title, similarity=score,
                    )
                if score >= self.guardrails.similar_threshold:
                    similar_to, similarity = nearest.title, score

            expires_at = None
            if source == TopicSource.DISCOVERY and self.guardrails.freshness_days:
                expires_at = datetime.now(timezone.utc) + timedelta(
                    days=self.guardrails.freshness_days)

            topic = repo.add_topic(
                session, self.group.id, title,
                source=source, source_url=source_url, category=category,
                content_type=content_type, angle=angle, embedding=embedding,
                similar_to=similar_to, similarity=similarity, expires_at=expires_at,
            )
            topic_id = topic.id

        if similar_to:
            return Decision(title, Verdict.ADMITTED_SIMILAR,
                            reason=f"close to {similar_to!r}",
                            similar_to=similar_to, similarity=similarity,
                            topic_id=topic_id)
        return Decision(title, Verdict.ADMITTED, topic_id=topic_id)

    def admit_many(self, candidates: Sequence[dict], **kwargs) -> list[Decision]:
        """Admit a batch, deduplicating within the batch as well as against the pool.

        Stored dedup only compares against scheduled and used topics, because a
        candidate nobody has planned yet is not a repetition. That is right for
        the pool over time but wrong inside one run: a single discovery pass
        would happily admit six rephrasings of "time management", none of which
        had been used yet. So a batch also checks each candidate against the
        ones it has just admitted.
        """
        decisions: list[Decision] = []
        admitted_in_batch: list[tuple[str, list[float]]] = []

        for candidate in candidates:
            title = (candidate.get("title") or "").strip()

            twin = self._batch_duplicate(title, admitted_in_batch)
            if twin:
                other, score = twin
                log.info("Topic %r duplicates %r from the same batch (%.2f)",
                         title, other, score)
                decisions.append(Decision(
                    title, Verdict.REJECTED_DUPLICATE,
                    reason=f"{score:.2f} similar to another topic in this batch",
                    similar_to=other, similarity=score,
                ))
                continue

            decision = self.admit(
                title,
                source=candidate.get("source", kwargs.get("source", TopicSource.MANUAL)),
                source_url=candidate.get("source_url"),
                category=candidate.get("category", ""),
                content_type=candidate.get("content_type", "message"),
                angle=candidate.get("angle", ""),
            )
            decisions.append(decision)

            if decision.admitted:
                try:
                    admitted_in_batch.append((title, self._embed_cached(title)))
                except EmbeddingError:
                    pass   # already admitted; batch dedup just loses this one
            if decision.verdict == Verdict.REJECTED_CAP:
                log.info("Weekly cap hit; stopping admission for this run.")
                break
        return decisions

    def _batch_duplicate(
        self, title: str, admitted: list[tuple[str, list[float]]]
    ) -> tuple[str, float] | None:
        if not title or not admitted:
            return None
        try:
            vector = self._embed_cached(title)
        except EmbeddingError:
            return None
        best_title, best_score = None, 0.0
        for other_title, other_vector in admitted:
            score = _cosine(vector, other_vector)
            if score > best_score:
                best_title, best_score = other_title, score
        if best_title and best_score >= self.guardrails.duplicate_threshold:
            return best_title, best_score
        return None

    def _embed_cached(self, title: str) -> list[float]:
        """Embed once per title per run — admit() would otherwise re-embed it."""
        cached = self._embedding_cache.get(title)
        if cached is None:
            cached = self.embedder.embed(title)
            self._embedding_cache[title] = cached
        return cached

    # ── seeding ──────────────────────────────────────────────────────────────

    def seed_topics(self, topics: Sequence[dict]) -> list[Decision]:
        """Import a group's existing plan into the pool, once.

        Seeded topics bypass the weekly cap (it exists to throttle discovery,
        not a one-time import) and never expire.
        """
        return self.admit_many(
            [{**t, "source": TopicSource.SEED} for t in topics],
            source=TopicSource.SEED,
        )

    # ── lookup ───────────────────────────────────────────────────────────────

    def available(self, limit: int = 100, category: str | None = None) -> list[dict]:
        with session_scope() as session:
            return [
                {
                    "id": t.id, "title": t.title, "angle": t.angle,
                    "category": t.category, "content_type": t.content_type,
                    "source": t.source, "source_url": t.source_url,
                    "similar_to": t.similar_to, "similarity": t.similarity,
                }
                for t in repo.available_topics(session, self.group.id, limit, category)
            ]

    def counts(self) -> dict[str, int]:
        with session_scope() as session:
            return repo.topic_counts(session, self.group.id)

    def retire_expired(self) -> int:
        with session_scope() as session:
            return repo.retire_expired_topics(session, self.group.id)

    def _added_this_week(self) -> int:
        from sqlalchemy import func, select

        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        with session_scope() as session:
            return int(session.execute(
                select(func.count())
                .select_from(Topic)
                .where(Topic.group_id == self.group.id)
                .where(Topic.source == TopicSource.DISCOVERY)
                .where(Topic.created_at >= cutoff)
            ).scalar() or 0)
