"""
tests/test_topic_pool.py
========================
The dedup gate and the editorial guardrails.

The guardrail tests run offline. The semantic tests need Postgres and use a
fake embedder, so they neither call OpenAI nor depend on a particular model's
notion of similarity — they check that the *thresholds* behave, which is the
part the code owns.
"""
from __future__ import annotations

import hashlib
import math
import sys
import uuid
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from engine.planning.topic_pool import (   # noqa: E402
    DEFAULT_DUPLICATE_THRESHOLD, DEFAULT_SIMILAR_THRESHOLD,
    Guardrails, TopicPool, Verdict,
)
from services.embedding_service import EmbeddingError  # noqa: E402
from services.storage.models import EMBEDDING_DIM, TopicSource, TopicStatus  # noqa: E402


def _database_available() -> bool:
    import time
    from services.storage.db import healthcheck
    for attempt in range(3):
        try:
            if healthcheck()[0]:
                return True
        except Exception:
            pass
        if attempt < 2:
            time.sleep(2)
    return False


needs_db = pytest.mark.skipif(
    not _database_available(), reason="no reachable DATABASE_URL")


# ── guardrails (offline) ─────────────────────────────────────────────────────

def test_guardrails_read_from_strategy():
    rails = Guardrails.from_strategy({"editorial_guardrails": {
        "never_cover": ["guaranteed placement"],
        "banned_phrases": ["get rich"],
        "max_new_per_week": 3,
        "duplicate_threshold": 0.9,
    }})
    assert rails.never_cover == ["guaranteed placement"]
    assert rails.max_new_per_week == 3
    assert rails.duplicate_threshold == 0.9


def test_guardrails_default_when_strategy_is_silent():
    rails = Guardrails.from_strategy({})
    assert rails.duplicate_threshold == DEFAULT_DUPLICATE_THRESHOLD
    assert rails.similar_threshold == DEFAULT_SIMILAR_THRESHOLD
    assert rails.max_new_per_week > 0


@pytest.mark.parametrize("title,expected", [
    ("Guaranteed placement in 30 days", "never_cover"),
    ("GUARANTEED PLACEMENT for everyone", "never_cover"),   # case-insensitive
    ("How to get rich quick", "banned_phrase"),
    ("How to structure a STAR answer", None),
    ("   ", "empty"),
])
def test_guardrail_violations(title, expected):
    rails = Guardrails(
        never_cover=["guaranteed placement"], banned_phrases=["get rich"])
    result = rails.violation(title)
    if expected is None:
        assert result is None
    else:
        assert result is not None and expected in result


# ── the gate (needs Postgres, fake embedder) ─────────────────────────────────

class FakeEmbedder:
    """Deterministic embeddings: equal text is identical, and `near` produces a
    vector a controllable angle away, so thresholds can be tested exactly."""

    enabled = True

    def __init__(self, angles: dict[str, float] | None = None):
        self.angles = angles or {}
        self.calls = 0

    def _unit(self, seed: float) -> list[float]:
        raw = [math.sin(seed + i * 0.017) for i in range(EMBEDDING_DIM)]
        norm = math.sqrt(sum(x * x for x in raw))
        return [x / norm for x in raw]

    def embed(self, text: str) -> list[float]:
        self.calls += 1
        # Seeds must not collide: two distinct titles landing on the same seed
        # produce identical vectors, which the batch dedup then rejects as
        # duplicates and the test reads as a cap failure.
        default = float(int(hashlib.sha256(text.encode()).hexdigest()[:8], 16) % 100_000)
        return self._unit(self.angles.get(text, default))

    def embed_many(self, texts):
        return [self.embed(t) for t in texts]


class BrokenEmbedder:
    enabled = True

    def embed(self, text):
        raise EmbeddingError("embedding provider is down")

    def embed_many(self, texts):
        raise EmbeddingError("embedding provider is down")


@pytest.fixture
def group():
    from engine.group_config import list_available_groups, load_group_config
    g = load_group_config(list_available_groups()[0])
    g.id = f"test_pool_{uuid.uuid4().hex[:8]}"     # isolate from real data
    yield g
    from services.storage.db import session_scope
    from services.storage.models import Topic
    with session_scope() as s:
        for t in s.query(Topic).filter_by(group_id=g.id).all():
            s.delete(t)


@needs_db
def test_a_clean_topic_is_admitted(group):
    pool = TopicPool(group, FakeEmbedder(), Guardrails())
    decision = pool.admit("Reading habits that lift VARC accuracy")
    assert decision.verdict == Verdict.ADMITTED
    assert decision.topic_id


@needs_db
def test_a_near_identical_topic_is_rejected(group):
    """The whole point: same meaning, different words, still caught."""
    embedder = FakeEmbedder({"original": 1.0, "restated": 1.0})
    pool = TopicPool(group, embedder, Guardrails())

    first = pool.admit("original")
    _promote(group.id, first.topic_id, TopicStatus.USED)

    second = pool.admit("restated")
    assert second.verdict == Verdict.REJECTED_DUPLICATE
    assert second.similarity is not None and second.similarity > 0.99


@needs_db
def test_an_unrelated_topic_passes_the_gate(group):
    embedder = FakeEmbedder({"first": 1.0, "different": 500.0})
    pool = TopicPool(group, embedder, Guardrails())

    first = pool.admit("first")
    _promote(group.id, first.topic_id, TopicStatus.USED)

    assert pool.admit("different").admitted


@needs_db
def test_candidates_in_the_pool_do_not_block_new_topics(group):
    """A topic nobody has scheduled yet is not a repetition."""
    embedder = FakeEmbedder({"a": 2.0, "b": 2.0})
    pool = TopicPool(group, embedder, Guardrails())

    pool.admit("a")                       # stays a candidate
    assert pool.admit("b").admitted


@needs_db
def test_guardrail_rejection_never_calls_the_embedder(group):
    """Guardrails are free; embeddings cost money. Cheap gate first."""
    embedder = FakeEmbedder()
    pool = TopicPool(group, embedder, Guardrails(never_cover=["forbidden"]))

    decision = pool.admit("A forbidden subject")
    assert decision.verdict == Verdict.REJECTED_GUARDRAIL
    assert embedder.calls == 0


@needs_db
def test_a_failed_embedding_rejects_rather_than_admits(group):
    """Admitting unembedded would let the next duplicate slip past undetected."""
    pool = TopicPool(group, BrokenEmbedder(), Guardrails())
    decision = pool.admit("Anything at all")
    assert decision.verdict == Verdict.REJECTED_ERROR
    assert not decision.admitted


@needs_db
def test_the_weekly_cap_only_throttles_discovery(group):
    pool = TopicPool(group, FakeEmbedder(), Guardrails(max_new_per_week=2))

    verdicts = [
        pool.admit(f"discovered topic {i}", source=TopicSource.DISCOVERY).verdict
        for i in range(4)
    ]
    assert verdicts.count(Verdict.REJECTED_CAP) >= 1

    # A seeded import is not discovery and must not be throttled.
    assert pool.admit("seeded topic", source=TopicSource.SEED).admitted


@needs_db
def test_admit_many_stops_at_the_cap(group):
    pool = TopicPool(group, FakeEmbedder(), Guardrails(max_new_per_week=2))
    decisions = pool.admit_many(
        [{"title": f"topic {i}", "source": TopicSource.DISCOVERY} for i in range(6)]
    )
    assert decisions[-1].verdict == Verdict.REJECTED_CAP
    assert sum(1 for d in decisions if d.admitted) == 2, "the cap must hold"
    assert len(decisions) < 6, "admission should stop rather than run the whole batch"


@needs_db
def test_discovered_topics_expire_but_seeded_ones_do_not(group):
    pool = TopicPool(group, FakeEmbedder(), Guardrails(freshness_days=30))
    from services.storage.db import session_scope
    from services.storage.models import Topic

    discovered = pool.admit("hiring news", source=TopicSource.DISCOVERY)
    seeded = pool.admit("evergreen advice", source=TopicSource.SEED)

    with session_scope() as s:
        assert s.get(Topic, discovered.topic_id).expires_at is not None
        assert s.get(Topic, seeded.topic_id).expires_at is None


def _promote(group_id: str, topic_id: str, status: str) -> None:
    from services.storage.db import session_scope
    from services.storage.models import Topic
    with session_scope() as s:
        s.get(Topic, topic_id).status = status


@needs_db
def test_a_batch_does_not_admit_six_rephrasings_of_one_idea(group):
    """Stored dedup only compares against scheduled/used topics, so nothing in
    a single discovery run would block anything else in that run. One pass
    could admit half a dozen wordings of "time management"."""
    embedder = FakeEmbedder({
        "Time management for mocks": 1.0,
        "Managing your time during mocks": 1.0,      # same idea
        "Reading speed for VARC": 400.0,             # genuinely different
    })
    pool = TopicPool(group, embedder, Guardrails())

    decisions = pool.admit_many([
        {"title": "Time management for mocks", "source": TopicSource.DISCOVERY},
        {"title": "Managing your time during mocks", "source": TopicSource.DISCOVERY},
        {"title": "Reading speed for VARC", "source": TopicSource.DISCOVERY},
    ])

    verdicts = [d.verdict for d in decisions]
    assert verdicts[0] == Verdict.ADMITTED
    assert verdicts[1] == Verdict.REJECTED_DUPLICATE, \
        "a second wording of the same idea was admitted in the same batch"
    assert "this batch" in decisions[1].reason
    assert verdicts[2] == Verdict.ADMITTED


@needs_db
def test_titles_are_embedded_once_per_run(group):
    """admit_many checks a title against the batch and then admits it; doing
    that with two embedding calls would double the bill for no reason."""
    embedder = FakeEmbedder()
    pool = TopicPool(group, embedder, Guardrails())

    pool.admit_many([{"title": "a topic"}, {"title": "another topic"}])
    assert embedder.calls == 2, f"expected one embedding per title, got {embedder.calls}"


def test_discovery_requires_a_source_url_in_its_prompt():
    """Source attribution is the point of discovery; a null URL makes a topic
    untraceable."""
    from engine.prompts import render

    text = render(
        "tasks/topic_discovery",
        group_name="G", audience="A", categories="c", must_cover="m",
        never_cover="n", limit=5, findings="f",
    )
    assert "source_url is REQUIRED" in text
    assert "Never write null" in text


def test_discovery_parses_a_bare_json_array():
    from engine.planning.discovery import DiscoveryRun

    parsed = DiscoveryRun._parse(
        '[{"title": "One", "source_url": "https://x.test/a"}, {"title": "Two"}]', 5)
    assert [p["title"] for p in parsed] == ["One", "Two"]
    assert parsed[0]["source_url"] == "https://x.test/a"


def test_discovery_survives_a_non_json_reply():
    """A model that ignores the format must not take the run down."""
    from engine.planning.discovery import DiscoveryRun

    assert DiscoveryRun._parse("Sorry, here are some ideas:", 5) == []
    assert DiscoveryRun._parse("", 5) == []
