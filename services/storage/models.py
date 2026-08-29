"""
services/storage/models.py
==========================
The schema. Four tables replacing one flat table of 22 TEXT columns.

The single most important change is that `Post.group_id` is a real column with
an index. Previously the tenant was a display string in a `tab_name` column and
every background thread re-derived it from a Flask session it did not have —
which is why scheduled publishes went to the wrong community's chat. A publish
now reads its tenant off the row it is publishing.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# text-embedding-3-small
EMBEDDING_DIM = 1536


class Base(DeclarativeBase):
    pass


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class PostState:
    """The post lifecycle.

    The ordering matters: a post can only be approved from NEEDS_REVIEW, which
    it can only reach once every declared asset exists. That is what makes
    "approve raced its own render" unrepresentable rather than merely unlikely.
    """

    DRAFT = "draft"                    # content written, no assets requested yet
    RENDERING = "rendering"            # declared assets are being produced
    ASSET_FAILED = "asset_failed"      # an asset did not render — visible, never silent
    NEEDS_REVIEW = "needs_review"      # content and all assets ready; the approval gate
    APPROVED = "approved"              # carries scheduled_for; the reconciler's queue
    PUBLISHING = "publishing"          # claimed by the reconciler; guards double-send
    PUBLISHED = "published"            # carries telegram_message_id; never re-sent
    PUBLISH_FAILED = "publish_failed"  # carries the real Telegram error
    REJECTED = "rejected"              # operator declined it; never publishes

    ALL = (DRAFT, RENDERING, ASSET_FAILED, NEEDS_REVIEW, APPROVED,
           PUBLISHING, PUBLISHED, PUBLISH_FAILED, REJECTED)
    #: States an operator is expected to act on.
    OPEN = (DRAFT, RENDERING, ASSET_FAILED, NEEDS_REVIEW)
    #: States that mean the post is done, one way or the other.
    TERMINAL = (PUBLISHED, PUBLISH_FAILED, REJECTED)


class TopicStatus:
    CANDIDATE = "candidate"    # admitted to the pool, not yet planned
    SCHEDULED = "scheduled"    # assigned to a slot in a cycle plan
    USED = "used"              # published; counts against future dedup
    RETIRED = "retired"        # expired or rejected by an operator

    ALL = (CANDIDATE, SCHEDULED, USED, RETIRED)
    #: Statuses a new candidate is compared against for duplication.
    DEDUP_AGAINST = (SCHEDULED, USED)


class TopicSource:
    SEED = "seed"              # imported once from the group's existing plan
    DISCOVERY = "discovery"    # found by the scheduled web search
    MANUAL = "manual"          # typed in by an operator


class Post(Base):
    __tablename__ = "posts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    group_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now())

    # What it is
    content_type: Mapped[str] = mapped_column(String(32), default="message")
    category: Mapped[str] = mapped_column(String(64), default="")
    topic: Mapped[str] = mapped_column(Text, default="")
    title: Mapped[str] = mapped_column(Text, default="")
    content: Mapped[str] = mapped_column(Text, default="")
    search_used: Mapped[bool] = mapped_column(Boolean, default=False)

    # Where it is in its life
    state: Mapped[str] = mapped_column(
        String(32), default=PostState.DRAFT, nullable=False, index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Publishing
    scheduled_for: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True)
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    telegram_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Assets. asset_document is the canonical mapper output and the source of
    # truth: the rendered files below are a cache, and a missing one is
    # re-rendered rather than lost, which is what lets container storage stay
    # ephemeral.
    asset_document: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    wants_image: Mapped[bool] = mapped_column(Boolean, default=False)
    wants_pdf: Mapped[bool] = mapped_column(Boolean, default=False)
    image_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    pdf_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    template_used: Mapped[str | None] = mapped_column(String(128), nullable=True)
    asset_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    caption_strategy: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Provenance
    cycle_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    topic_id: Mapped[str | None] = mapped_column(
        ForeignKey("topics.id", ondelete="SET NULL"), nullable=True)

    # Telemetry
    generation_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    render_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)

    topic_row: Mapped["Topic | None"] = relationship(back_populates="posts")

    __table_args__ = (
        # The reconciler's query: due, approved posts for every tenant at once.
        Index("ix_posts_state_scheduled", "state", "scheduled_for"),
        Index("ix_posts_group_state", "group_id", "state"),
    )

    def __repr__(self) -> str:
        return f"<Post {self.id[:8]} {self.group_id} {self.state}>"

    @property
    def assets_ready(self) -> bool:
        """True when every asset this post declared actually exists."""
        if self.wants_image and not self.image_path:
            return False
        if self.wants_pdf and not self.pdf_path:
            return False
        return True


class Topic(Base):
    __tablename__ = "topics"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    group_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    title: Mapped[str] = mapped_column(Text, nullable=False)
    angle: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(64), default="")
    content_type: Mapped[str] = mapped_column(String(32), default="message")

    source: Mapped[str] = mapped_column(String(16), default=TopicSource.MANUAL)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(
        String(16), default=TopicStatus.CANDIDATE, nullable=False, index=True)

    # Cosine distance against this is the dedup gate. Nullable so a topic can be
    # stored before the embedding call succeeds.
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(EMBEDDING_DIM), nullable=True)
    #: Set when a topic was admitted despite being near an existing one.
    similar_to: Mapped[str | None] = mapped_column(Text, nullable=True)
    similarity: Mapped[float | None] = mapped_column(Float, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, server_default=func.now())
    #: Discovered topics go stale — hiring news is worthless in three months.
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    posts: Mapped[list[Post]] = relationship(back_populates="topic_row")

    __table_args__ = (
        Index("ix_topics_group_status", "group_id", "status"),
    )

    def __repr__(self) -> str:
        return f"<Topic {self.id[:8]} {self.group_id} {self.status} {self.title[:40]!r}>"


class Cycle(Base):
    __tablename__ = "cycles"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    group_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    cycle_number: Mapped[int] = mapped_column(Integer, nullable=False)

    starts_on: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_on: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    #: The Planner's output: the cycle skeleton with pool topics assigned to slots.
    plan: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, server_default=func.now())

    __table_args__ = (
        Index("ix_cycles_group_number", "group_id", "cycle_number"),
    )

    def __repr__(self) -> str:
        return f"<Cycle {self.id} {self.group_id} #{self.cycle_number}>"


class GroupState(Base):
    """Runtime state for a tenant. Its *configuration* stays in config.yaml —
    this is only what the engine learns while running."""

    __tablename__ = "group_state"

    group_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_discovery_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    topics_added_this_week: Mapped[int] = mapped_column(Integer, default=0)
    last_cycle_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now())

    def __repr__(self) -> str:
        return f"<GroupState {self.group_id}>"
