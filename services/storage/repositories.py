"""
services/storage/repositories.py
================================
The query layer. All SQL lives here — routes, workflow and jobs call these
functions and never write queries of their own.

Every function that touches posts takes an explicit `group_id`. There is no
"current group" to fall back on: losing track of the tenant is what sent one
community's posts to another's chat, so the type signature makes it impossible
to forget rather than merely inadvisable.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from services.storage.models import (
    Cycle, GroupState, Post, PostState, Topic, TopicSource, TopicStatus,
)

log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── posts ────────────────────────────────────────────────────────────────────

def create_post(session: Session, group_id: str, **fields: Any) -> Post:
    post = Post(group_id=group_id, **fields)
    session.add(post)
    session.flush()
    return post


def get_post(session: Session, post_id: str) -> Post | None:
    """Fetch by id alone — the row carries its own tenant."""
    return session.get(Post, post_id)


def list_posts(
    session: Session,
    group_id: str,
    states: Sequence[str] | None = None,
    limit: int | None = None,
) -> list[Post]:
    stmt = select(Post).where(Post.group_id == group_id)
    if states:
        stmt = stmt.where(Post.state.in_(states))
    stmt = stmt.order_by(Post.created_at.desc())
    if limit:
        stmt = stmt.limit(limit)
    return list(session.scalars(stmt))


def count_by_state(session: Session, group_id: str) -> dict[str, int]:
    rows = session.execute(
        select(Post.state, func.count())
        .where(Post.group_id == group_id)
        .group_by(Post.state)
    ).all()
    return {state: n for state, n in rows}


def count_posts(session: Session, group_id: str, states: Sequence[str]) -> int:
    """How many posts of this tenant are in any of these states.

    A count, not a list. The sidebar badges render on every page, and loading
    every post to take len() of two filtered slices was the most expensive
    thing on it.
    """
    return session.execute(
        select(func.count())
        .select_from(Post)
        .where(Post.group_id == group_id, Post.state.in_(list(states)))
    ).scalar_one()


def set_state(
    session: Session, post_id: str, state: str, error: str | None = None
) -> Post | None:
    """Move a post to a new state. `error` is cleared unless one is supplied."""
    post = session.get(Post, post_id)
    if post is None:
        log.warning("set_state: post %s not found", post_id)
        return None
    post.state = state
    post.error = error
    post.updated_at = _now()
    return post


def due_for_publish(session: Session, now: datetime | None = None) -> list[Post]:
    """The reconciler's query: approved, due, assets ready, across every tenant.

    Asset readiness is a precondition here rather than a check at publish time,
    which is what removes the approve-races-its-own-render failure entirely.
    """
    moment = now or _now()
    candidates = session.scalars(
        select(Post)
        .where(Post.state == PostState.APPROVED)
        .where(Post.scheduled_for.is_not(None))
        .where(Post.scheduled_for <= moment)
        .order_by(Post.scheduled_for)
    )
    return [p for p in candidates if p.assets_ready]


def claim_for_publish(session: Session, post_id: str) -> Post | None:
    """Take a post from APPROVED to PUBLISHING, atomically.

    Returns the post only if this caller won the claim. Two reconciler ticks
    overlapping cannot both publish the same post.
    """
    result = session.execute(
        update(Post)
        .where(Post.id == post_id, Post.state == PostState.APPROVED)
        .values(state=PostState.PUBLISHING, updated_at=_now())
        .returning(Post.id)
    ).scalar()
    if result is None:
        return None
    return session.get(Post, post_id)


def mark_published(session: Session, post_id: str, message_id: str) -> Post | None:
    post = session.get(Post, post_id)
    if post is None:
        return None
    post.state = PostState.PUBLISHED
    post.telegram_message_id = str(message_id)
    post.published_at = _now()
    post.error = None
    post.updated_at = _now()
    if post.topic_id:
        topic = session.get(Topic, post.topic_id)
        if topic is not None:
            topic.status = TopicStatus.USED
            topic.used_at = _now()
    return post


def mark_publish_failed(session: Session, post_id: str, error: str) -> Post | None:
    return set_state(session, post_id, PostState.PUBLISH_FAILED, error=error[:2000])


def release_stuck_publishing(session: Session, older_than_minutes: int = 15) -> int:
    """Return posts stranded in PUBLISHING by a crash back to APPROVED.

    Without this a process killed mid-publish leaves the post claimed forever.
    """
    cutoff = _now() - timedelta(minutes=older_than_minutes)
    rows = session.execute(
        update(Post)
        .where(Post.state == PostState.PUBLISHING, Post.updated_at < cutoff)
        .values(state=PostState.APPROVED, updated_at=_now())
        .returning(Post.id)
    ).all()
    if rows:
        log.warning("Released %d post(s) stuck in PUBLISHING back to APPROVED", len(rows))
    return len(rows)


def scheduled_slots(session: Session, group_id: str) -> set[str]:
    """`YYYY-MM-DDTHH:MM` keys already taken, for batch-generation dedup."""
    rows = session.scalars(
        select(Post.scheduled_for)
        .where(Post.group_id == group_id)
        .where(Post.scheduled_for.is_not(None))
        .where(Post.state != PostState.PUBLISH_FAILED)
    )
    return {dt.strftime("%Y-%m-%dT%H:%M") for dt in rows if dt}


def recent_topics(session: Session, group_id: str, limit: int = 30) -> list[str]:
    return [
        t for t in session.scalars(
            select(Post.topic)
            .where(Post.group_id == group_id, Post.topic != "")
            .order_by(Post.created_at.desc())
            .limit(limit)
        )
    ]


def delete_post(session: Session, post_id: str) -> bool:
    post = session.get(Post, post_id)
    if post is None:
        return False
    session.delete(post)
    return True


# ── topics ───────────────────────────────────────────────────────────────────

def add_topic(
    session: Session,
    group_id: str,
    title: str,
    *,
    source: str = TopicSource.MANUAL,
    embedding: list[float] | None = None,
    **fields: Any,
) -> Topic:
    topic = Topic(
        group_id=group_id, title=title, source=source, embedding=embedding, **fields
    )
    session.add(topic)
    session.flush()
    return topic


def nearest_topic(
    session: Session, group_id: str, embedding: Sequence[float]
) -> tuple[Topic, float] | None:
    """The closest already-committed topic, and its cosine similarity.

    Compared only against SCHEDULED and USED topics: a candidate sitting in the
    pool unplanned is not yet a repetition. pgvector's `<=>` is cosine distance,
    so similarity is 1 - distance, and the index makes this a lookup rather
    than a scan over everything the group has ever posted.
    """
    distance = Topic.embedding.cosine_distance(list(embedding))
    row = session.execute(
        select(Topic, distance.label("distance"))
        .where(Topic.group_id == group_id)
        .where(Topic.status.in_(TopicStatus.DEDUP_AGAINST))
        .where(Topic.embedding.is_not(None))
        .order_by(distance)
        .limit(1)
    ).first()
    if row is None:
        return None
    topic, dist = row
    return topic, 1.0 - float(dist)


def available_topics(
    session: Session, group_id: str, limit: int = 100, category: str | None = None
) -> list[Topic]:
    """Candidates a Planner may draw from, freshest usable first."""
    stmt = (
        select(Topic)
        .where(Topic.group_id == group_id)
        .where(Topic.status == TopicStatus.CANDIDATE)
        .where((Topic.expires_at.is_(None)) | (Topic.expires_at > _now()))
    )
    if category:
        stmt = stmt.where(Topic.category == category)
    return list(session.scalars(stmt.order_by(Topic.created_at.desc()).limit(limit)))


def list_topics(
    session: Session, group_id: str, statuses: Sequence[str] | None = None,
    limit: int = 300,
) -> list[Topic]:
    """The pool as an operator sees it — every status, newest first.

    `available_topics` deliberately returns only what a Planner may draw from.
    The Topic Pool screen has to show the rest too: what is already scheduled,
    what has been used, and what was retired, because "why did nothing get
    planned this cycle" is usually answered by one of those three.
    """
    stmt = select(Topic).where(Topic.group_id == group_id)
    if statuses:
        stmt = stmt.where(Topic.status.in_(list(statuses)))
    return list(session.scalars(stmt.order_by(Topic.created_at.desc()).limit(limit)))


def set_topic_status(session: Session, topic_id: str, status: str) -> Topic | None:
    topic = session.get(Topic, topic_id)
    if topic is None:
        return None
    topic.status = status
    return topic


def retire_expired_topics(session: Session, group_id: str | None = None) -> int:
    stmt = (
        update(Topic)
        .where(Topic.status == TopicStatus.CANDIDATE)
        .where(Topic.expires_at.is_not(None), Topic.expires_at <= _now())
        .values(status=TopicStatus.RETIRED)
        .returning(Topic.id)
    )
    if group_id:
        stmt = stmt.where(Topic.group_id == group_id)
    return len(session.execute(stmt).all())


def topic_counts(session: Session, group_id: str) -> dict[str, int]:
    rows = session.execute(
        select(Topic.status, func.count())
        .where(Topic.group_id == group_id)
        .group_by(Topic.status)
    ).all()
    return {status: n for status, n in rows}


# ── cycles ───────────────────────────────────────────────────────────────────

def upsert_cycle(
    session: Session, cycle_id: str, group_id: str, cycle_number: int,
    starts_on: datetime, ends_on: datetime, plan: dict | None = None,
) -> Cycle:
    cycle = session.get(Cycle, cycle_id)
    if cycle is None:
        cycle = Cycle(id=cycle_id, group_id=group_id, cycle_number=cycle_number,
                      starts_on=starts_on, ends_on=ends_on, plan=plan)
        session.add(cycle)
    else:
        cycle.cycle_number = cycle_number
        cycle.starts_on = starts_on
        cycle.ends_on = ends_on
        if plan is not None:
            cycle.plan = plan
    session.flush()
    return cycle


def get_cycle(session: Session, cycle_id: str) -> Cycle | None:
    return session.get(Cycle, cycle_id)


def latest_cycle(session: Session, group_id: str) -> Cycle | None:
    return session.scalars(
        select(Cycle)
        .where(Cycle.group_id == group_id)
        .order_by(Cycle.cycle_number.desc())
        .limit(1)
    ).first()


# ── group state ──────────────────────────────────────────────────────────────

def group_state(session: Session, group_id: str) -> GroupState:
    state = session.get(GroupState, group_id)
    if state is None:
        state = GroupState(group_id=group_id)
        session.add(state)
        session.flush()
    return state
