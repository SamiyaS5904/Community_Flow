"""
tests/test_storage.py
=====================
Storage-layer tests. These need a reachable Postgres (DATABASE_URL) and are
skipped when there isn't one, so the rest of the suite still runs offline.

They cover the three properties the old flat table could not give us:
tenant isolation, an approval gate that cannot race a render, and a claim that
cannot double-send.
"""
from __future__ import annotations

import math
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("FLASK_SECRET_KEY", "test-secret-not-used-for-anything")

from services.storage import repositories as repo          # noqa: E402
from services.storage.db import DatabaseNotConfigured, normalise_url  # noqa: E402
from services.storage.models import (                       # noqa: E402
    EMBEDDING_DIM, PostState, TopicSource, TopicStatus,
)


def _database_available() -> bool:
    """Neon suspends an idle endpoint, so the first connection can time out
    while it wakes. Retry before deciding the database is unreachable —
    otherwise a cold start silently skips every test in this file."""
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
    not _database_available(),
    reason="no reachable DATABASE_URL — storage tests skipped",
)


# ── url normalisation (no database needed) ───────────────────────────────────

def test_normalise_url_selects_psycopg3():
    assert normalise_url("postgresql://u:p@h/db").startswith("postgresql+psycopg://")
    assert normalise_url("postgres://u:p@h/db").startswith("postgresql+psycopg://")


def test_normalise_url_drops_the_pooled_endpoint():
    """PgBouncer transaction pooling does not hold session advisory locks."""
    out = normalise_url("postgresql://u:p@ep-x-pooler.region.neon.tech/db")
    assert "-pooler." not in out
    assert "ep-x.region.neon.tech" in out


def test_missing_url_names_the_fix():
    from services.storage import db

    saved = os.environ.pop("DATABASE_URL", None)
    try:
        from engine.config import config
        original = config.DATABASE_URL
        config.DATABASE_URL = ""
        with pytest.raises(DatabaseNotConfigured, match="DATABASE_URL"):
            db.database_url()
        config.DATABASE_URL = original
    finally:
        if saved is not None:
            os.environ["DATABASE_URL"] = saved


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def tenants():
    """Two throwaway tenant ids, cleaned up afterwards."""
    suffix = uuid.uuid4().hex[:8]
    a, b = f"test_a_{suffix}", f"test_b_{suffix}"
    yield a, b
    from services.storage.db import session_scope
    with session_scope() as s:
        for gid in (a, b):
            for post in repo.list_posts(s, gid):
                repo.delete_post(s, post.id)
            for topic in s.query(type(repo).__module__ and __import__(
                    "services.storage.models", fromlist=["Topic"]).Topic).filter_by(
                    group_id=gid).all():
                s.delete(topic)


def _vector(seed: float) -> list[float]:
    """A deterministic unit vector; nearby seeds give near-parallel vectors."""
    raw = [math.sin(seed + i * 0.01) for i in range(EMBEDDING_DIM)]
    norm = math.sqrt(sum(x * x for x in raw))
    return [x / norm for x in raw]


# ── posts ────────────────────────────────────────────────────────────────────

@needs_db
def test_posts_are_isolated_by_tenant(tenants):
    from services.storage.db import session_scope
    a, b = tenants
    with session_scope() as s:
        repo.create_post(s, a, topic="A topic")
        repo.create_post(s, b, topic="B topic")
    with session_scope() as s:
        assert [p.topic for p in repo.list_posts(s, a)] == ["A topic"]
        assert [p.topic for p in repo.list_posts(s, b)] == ["B topic"]


@needs_db
def test_a_post_carries_its_own_tenant(tenants):
    """Fetching by id alone must still tell you where to publish it."""
    from services.storage.db import session_scope
    a, _ = tenants
    with session_scope() as s:
        post_id = repo.create_post(s, a, topic="T").id
    with session_scope() as s:
        assert repo.get_post(s, post_id).group_id == a


@needs_db
def test_due_for_publish_excludes_posts_whose_assets_are_missing(tenants):
    from services.storage.db import session_scope
    a, _ = tenants
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    with session_scope() as s:
        ready = repo.create_post(s, a, topic="ready", state=PostState.APPROVED,
                                 scheduled_for=past, wants_image=True,
                                 image_path="/tmp/x.png")
        pending = repo.create_post(s, a, topic="no asset yet", state=PostState.APPROVED,
                                   scheduled_for=past, wants_image=True)
        ready_id, pending_id = ready.id, pending.id
    with session_scope() as s:
        due = {p.id for p in repo.due_for_publish(s)}
    assert ready_id in due
    assert pending_id not in due, "a post with a declared but missing asset must not publish"


@needs_db
def test_future_posts_are_not_due(tenants):
    from services.storage.db import session_scope
    a, _ = tenants
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    with session_scope() as s:
        pid = repo.create_post(s, a, state=PostState.APPROVED, scheduled_for=future).id
    with session_scope() as s:
        assert pid not in {p.id for p in repo.due_for_publish(s)}


@needs_db
def test_claim_is_atomic_so_a_post_cannot_double_send(tenants):
    from services.storage.db import session_scope
    a, _ = tenants
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    with session_scope() as s:
        pid = repo.create_post(s, a, state=PostState.APPROVED, scheduled_for=past).id
    with session_scope() as s:
        assert repo.claim_for_publish(s, pid) is not None
    with session_scope() as s:
        assert repo.claim_for_publish(s, pid) is None


@needs_db
def test_stuck_publishing_posts_are_released(tenants):
    """A process killed mid-publish must not strand the post forever."""
    from services.storage.db import session_scope
    a, _ = tenants
    with session_scope() as s:
        pid = repo.create_post(s, a, state=PostState.PUBLISHING).id
        post = repo.get_post(s, pid)
        post.updated_at = datetime.now(timezone.utc) - timedelta(hours=1)
    with session_scope() as s:
        assert repo.release_stuck_publishing(s, older_than_minutes=15) >= 1
    with session_scope() as s:
        assert repo.get_post(s, pid).state == PostState.APPROVED


# ── topics and the dedup gate ────────────────────────────────────────────────

@needs_db
def test_nearest_topic_finds_the_near_duplicate(tenants):
    from services.storage.db import session_scope
    a, _ = tenants
    with session_scope() as s:
        repo.add_topic(s, a, "How to write a resume", status=TopicStatus.USED,
                       source=TopicSource.SEED, embedding=_vector(1.0))
        repo.add_topic(s, a, "Mock test strategy", status=TopicStatus.USED,
                       source=TopicSource.SEED, embedding=_vector(50.0))
    with session_scope() as s:
        match = repo.nearest_topic(s, a, _vector(1.0005))
        assert match is not None
        topic, similarity = match
        assert topic.title == "How to write a resume"
        assert similarity > 0.9, f"near-identical vector scored only {similarity:.3f}"


@needs_db
def test_dedup_ignores_unplanned_candidates(tenants):
    """A topic sitting in the pool is not yet a repetition."""
    from services.storage.db import session_scope
    a, _ = tenants
    with session_scope() as s:
        repo.add_topic(s, a, "Only a candidate", status=TopicStatus.CANDIDATE,
                       embedding=_vector(2.0))
    with session_scope() as s:
        assert repo.nearest_topic(s, a, _vector(2.0)) is None


@needs_db
def test_dedup_does_not_cross_tenants(tenants):
    from services.storage.db import session_scope
    a, b = tenants
    with session_scope() as s:
        repo.add_topic(s, b, "B's topic", status=TopicStatus.USED, embedding=_vector(3.0))
    with session_scope() as s:
        assert repo.nearest_topic(s, a, _vector(3.0)) is None


@needs_db
def test_expired_topics_retire(tenants):
    from services.storage.db import session_scope
    a, _ = tenants
    with session_scope() as s:
        repo.add_topic(s, a, "Stale hiring news", source=TopicSource.DISCOVERY,
                       expires_at=datetime.now(timezone.utc) - timedelta(days=1))
        repo.add_topic(s, a, "Evergreen advice", source=TopicSource.SEED)
    with session_scope() as s:
        assert repo.retire_expired_topics(s, a) == 1
    with session_scope() as s:
        titles = [t.title for t in repo.available_topics(s, a)]
        assert titles == ["Evergreen advice"]


@needs_db
def test_publishing_marks_its_topic_used(tenants):
    from services.storage.db import session_scope
    a, _ = tenants
    with session_scope() as s:
        topic = repo.add_topic(s, a, "Scheduled topic", status=TopicStatus.SCHEDULED,
                               embedding=_vector(4.0))
        pid = repo.create_post(s, a, state=PostState.PUBLISHING, topic_id=topic.id).id
        tid = topic.id
    with session_scope() as s:
        repo.mark_published(s, pid, "999")
    with session_scope() as s:
        from services.storage.models import Topic
        assert s.get(Topic, tid).status == TopicStatus.USED
