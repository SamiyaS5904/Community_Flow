"""
services/storage/post_store.py
==============================
The bridge between the Postgres models and the display-key dictionaries the
dashboard templates already speak.

Jinja renders `post.get('Approval Status')` in roughly 1,500 lines of template,
so changing that vocabulary is a UI-layer job, not a storage one. This adapter
keeps the old shape while the rows underneath become real, typed, tenant-owning
Postgres records — and it is the single place the two vocabularies meet.

It also translates between the old two-column status pair
(`Approval Status` + `Publish Status`) and the single `Post.state` lifecycle.
The old pair could express nonsense — approved-and-failed, pending-and-published
— which is exactly how posts ended up stuck. One state column cannot.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Iterable

from services.storage.db import session_scope
from services.storage.models import Post, PostState
from services.storage import repositories as repo

log = logging.getLogger(__name__)

_TZ = timezone.utc

# Display key -> model attribute, for fields that map straight across.
_FIELD_MAP: dict[str, str] = {
    "Post ID": "id",
    "Group": "group_id",
    "Content Type": "content_type",
    "Topic": "topic",
    "Title": "title",
    "Generated Content": "content",
    "Telegram Message ID": "telegram_message_id",
    "Error": "error",
    "PDF Path": "pdf_path",
    "Image Path": "image_path",
    "Template Used": "template_used",
    "Asset Type": "asset_type",
    "Caption Strategy": "caption_strategy",
}

# state -> (Approval Status, Publish Status), for templates that still branch on the pair.
_STATE_TO_PAIR: dict[str, tuple[str, str]] = {
    PostState.DRAFT:          ("pending",  "unpublished"),
    PostState.RENDERING:      ("pending",  "unpublished"),
    PostState.ASSET_FAILED:   ("pending",  "unpublished"),
    PostState.NEEDS_REVIEW:   ("pending",  "unpublished"),
    PostState.APPROVED:       ("approved", "unpublished"),
    PostState.PUBLISHING:     ("approved", "unpublished"),
    PostState.PUBLISHED:      ("approved", "published"),
    PostState.PUBLISH_FAILED: ("approved", "failed"),
    PostState.REJECTED:       ("rejected", "unpublished"),
}


def _fmt_dt(value: datetime | None, pattern: str) -> str:
    return value.strftime(pattern) if value else ""


@lru_cache(maxsize=64)
def _group_tz(group_id: str):
    """The group's own timezone, for rendering stored UTC back to its clock.

    Cached because to_display runs once per post on every dashboard render, and
    the answer only changes when the group's config file does.
    """
    try:
        from engine.group_config import load_group_config
        return load_group_config(group_id).tz
    except Exception:
        log.warning("No timezone for group %r; displaying times in UTC.", group_id)
        return timezone.utc


def _fmt_local(value: datetime | None, pattern: str, group_id: str) -> str:
    """Format a stored instant on the group's own clock.

    Times are stored in UTC, which is correct, but they were being formatted
    straight from that value with no conversion — while the approve route
    converts the operator's local time *into* UTC on the way in. The asymmetry
    is what made a post scheduled for midnight IST come back displaying 18:30.

    It also compounded, because `Scheduled Time` is fed to a `datetime-local`
    input: the browser shows the raw UTC as though it were local, and
    re-submitting that form converted it to UTC a second time, shifting the
    post by another 5½ hours on every edit.
    """
    if not value:
        return ""
    # A naive value out of the database is UTC; saying so explicitly is what
    # makes astimezone correct rather than a no-op against the server's clock.
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(_group_tz(group_id)).strftime(pattern)


def to_display(post: Post) -> dict[str, Any]:
    """Render a Post as the dictionary the templates expect."""
    approval, publish = _STATE_TO_PAIR.get(post.state, ("pending", "unpublished"))
    return {
        "Post ID": post.id,
        "Date": _fmt_local(post.created_at, "%Y-%m-%d", post.group_id),
        "Time": _fmt_local(post.created_at, "%H:%M", post.group_id),
        "Group": post.group_id,
        "Content Type": post.content_type or "",
        "Topic": post.topic or "",
        "Title": post.title or "",
        "Generated Content": post.content or "",
        "Search Used": "Yes" if post.search_used else "No",
        "Approval Status": approval,
        "Publish Status": publish,
        "Telegram Message ID": post.telegram_message_id or "",
        "Telegram Link": "",
        "Error": post.error or "",
        "PDF Path": post.pdf_path or ("pending" if post.wants_pdf else "N/A"),
        "Image Path": post.image_path or ("pending" if post.wants_image else "N/A"),
        "Scheduled Time": _fmt_local(post.scheduled_for, "%Y-%m-%dT%H:%M", post.group_id),
        "Template Used": post.template_used or "N/A",
        "Asset Type": post.asset_type or "N/A",
        "Generation Time": f"{post.generation_seconds:.2f}s" if post.generation_seconds else "N/A",
        "Rendering Time": f"{post.render_seconds:.2f}s" if post.render_seconds else "N/A",
        "Caption Strategy": post.caption_strategy or "N/A",
        # New fields, available to templates as they are updated.
        "State": post.state,
        "Assets Ready": post.assets_ready,
    }


def _apply_updates(post: Post, updates: dict[str, Any]) -> None:
    """Write display-key updates onto a Post, translating status pairs."""
    approval = updates.get("Approval Status")
    publish = updates.get("Publish Status")

    for key, value in updates.items():
        if key in ("Approval Status", "Publish Status"):
            continue
        attr = _FIELD_MAP.get(key)
        if attr and attr != "id":
            setattr(post, attr, value)
        elif key == "Scheduled Time" and value:
            # An aware datetime is the only form that carries its own zone, so
            # it is passed straight through. A bare string has no zone, and the
            # only safe reading of one here is UTC — which is exactly how every
            # scheduled post ended up 5½ hours late: the approve route handed
            # over the raw form value, which is the operator's local time, and
            # this parsed it as UTC. Callers must convert before they get here.
            if isinstance(value, datetime):
                post.scheduled_for = (value if value.tzinfo
                                      else value.replace(tzinfo=_TZ))
            else:
                try:
                    naive = datetime.strptime(str(value), "%Y-%m-%dT%H:%M")
                    post.scheduled_for = naive.replace(tzinfo=_TZ)
                except ValueError:
                    log.warning("Unparseable Scheduled Time %r on post %s", value, post.id)

    # Status pair -> state. Publish status wins, since it is the later fact.
    if publish == "published":
        post.state = PostState.PUBLISHED
        post.published_at = post.published_at or datetime.now(_TZ)
    elif publish == "failed":
        post.state = PostState.PUBLISH_FAILED
    elif approval == "approved":
        if post.state not in (PostState.PUBLISHED, PostState.PUBLISHING):
            post.state = PostState.APPROVED
    elif approval == "rejected":
        post.state = PostState.REJECTED
    elif approval in ("pending", "missed"):
        # "missed" was the old marker for a publish window that slipped. There
        # is no such state now: the reconciler simply picks it up late, and a
        # long-overdue post shows as a backlog rather than being quietly parked.
        if post.state in PostState.TERMINAL:
            log.warning("Refusing to move post %s out of %s", post.id, post.state)
        else:
            post.state = PostState.NEEDS_REVIEW

    post.updated_at = datetime.now(_TZ)


class PostStore:
    """Postgres-backed replacement for LocalDBService.

    Method names and argument order match the old service so the 28 existing
    call sites keep working; the second argument is now a `group_id` rather
    than a spreadsheet tab name.
    """

    # ── reads ────────────────────────────────────────────────────────────────

    def get_all_posts(self, group_id: str) -> list[dict[str, Any]]:
        with session_scope() as session:
            return [to_display(p) for p in repo.list_posts(session, group_id)]

    def get_post_by_id(self, post_id: str, group_id: str | None = None) -> dict[str, Any]:
        """Fetch one post. group_id is accepted for call-site compatibility but
        is not needed — the row knows which tenant it belongs to."""
        with session_scope() as session:
            post = repo.get_post(session, post_id)
            return to_display(post) if post else {}

    # ── writes ───────────────────────────────────────────────────────────────

    def create(self, group_id: str, **fields: Any) -> str:
        """Create a post from model field names. Returns its id."""
        with session_scope() as session:
            return repo.create_post(session, group_id, **fields).id

    def create_many(self, group_id: str, rows: Iterable[dict[str, Any]]) -> list[str]:
        with session_scope() as session:
            return [repo.create_post(session, group_id, **row).id for row in rows]

    def update_post(
        self, post_id: str, updates: dict[str, Any], group_id: str | None = None,
        row_cache: dict | None = None,
    ) -> bool:
        """Apply display-key updates. Raises if the write fails.

        The old implementation returned False on failure and nearly every
        caller ignored it, so a post could be sent to Telegram and its status
        update silently lost. Failures now propagate.
        """
        with session_scope() as session:
            post = repo.get_post(session, post_id)
            if post is None:
                log.warning("update_post: %s not found", post_id)
                return False
            _apply_updates(post, updates)
            return True

    def set_state(self, post_id: str, state: str, error: str | None = None) -> bool:
        with session_scope() as session:
            return repo.set_state(session, post_id, state, error) is not None

    def delete_post(self, post_id: str, group_id: str | None = None) -> bool:
        with session_scope() as session:
            return repo.delete_post(session, post_id)

    # ── helpers the new code paths use directly ──────────────────────────────

    def recent_topics(self, group_id: str, limit: int = 30) -> list[str]:
        with session_scope() as session:
            return repo.recent_topics(session, group_id, limit)

    def scheduled_slots(self, group_id: str) -> set[str]:
        with session_scope() as session:
            return repo.scheduled_slots(session, group_id)

    def counts(self, group_id: str) -> dict[str, int]:
        with session_scope() as session:
            return repo.count_by_state(session, group_id)
