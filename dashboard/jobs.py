"""
dashboard/jobs.py
=================
The publish reconciler, and the background jobs around it.

Publishing used to work by creating one APScheduler date-job per post, keyed by
post id, in an in-memory jobstore, closing over the asset paths as they looked
at approval time. That single decision produced four separate defects:

  - the job ran with no Flask session, so it resolved the tenant to the default
    group and delivered every community's posts to the same chat;
  - it captured asset paths before the render finished, so an immediate publish
    sent a post whose image did not exist yet;
  - the jobstore was in memory and the recovery path was single-tenant, so a
    restart dropped every other group's approved posts;
  - reject and delete each had to remember to cancel the orphaned job.

The reconciler holds no state. Every 30 seconds it asks the database a
question — "which posts are approved, due, have their assets, and have not been
published?" — and acts on the answer. Tenancy comes from the row. Asset
readiness is part of the query. A restart loses nothing because there was
nothing to lose. Rejecting a post simply stops it matching.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from services.storage.db import advisory_lock, session_scope
from services.storage import repositories as repo
from services.storage.models import PostState

log = logging.getLogger(__name__)

#: How often to look for due posts. The publish-latency target is 60s, so this
#: leaves room for the publish itself.
RECONCILE_SECONDS = 30

#: Postgres advisory lock id. Gunicorn runs three workers and each would
#: otherwise run its own reconciler, sending every post three times.
RECONCILER_LOCK = 0x43464C57  # "CFLW"

#: A post claimed for publishing but never finished — the process died
#: mid-send — is returned to the queue after this long.
STUCK_MINUTES = 15


class PublishError(RuntimeError):
    """Preflight failed; the post must not be sent."""


def preflight(post: dict, chat_id: str) -> None:
    """Refuse to publish something that cannot succeed.

    Cheaper than a Telegram round-trip and produces a much better error than
    "Bad Request: wrong file identifier".
    """
    if not chat_id:
        raise PublishError(
            f"No Telegram chat configured for group '{post.get('Group')}'."
        )
    if not (post.get("Generated Content") or "").strip():
        raise PublishError("The post has no content.")

    content_type = (post.get("Content Type") or "").lower()
    if content_type == "pdf" and post.get("PDF Path") in (None, "", "N/A", "pending", "Failed"):
        raise PublishError("This is a PDF post but no PDF has been rendered.")
    if content_type == "image" and post.get("Image Path") in (None, "", "N/A", "pending", "Failed"):
        raise PublishError("This is an image post but no image has been rendered.")


def publish_due_posts(get_workflow, now: datetime | None = None) -> dict:
    """One reconciler pass. Returns a summary for logging and the dashboard.

    Args:
        get_workflow: fn(group_id) -> PlatformWorkflow. Injected so this module
            does not import the Flask app and can be tested directly.
    """
    moment = now or datetime.now(timezone.utc)
    summary = {"claimed": 0, "published": 0, "failed": 0, "released": 0, "skipped": 0}

    with advisory_lock(RECONCILER_LOCK) as holding:
        if not holding:
            # Another worker owns this tick. Not an error.
            return {**summary, "skipped_lock": True}

        with session_scope() as session:
            summary["released"] = repo.release_stuck_publishing(session, STUCK_MINUTES)

        with session_scope() as session:
            due = [(p.id, p.group_id) for p in repo.due_for_publish(session, moment)]

        for post_id, group_id in due:
            # Claim first: a conditional UPDATE, so two overlapping ticks
            # cannot both take the same post.
            with session_scope() as session:
                if repo.claim_for_publish(session, post_id) is None:
                    summary["skipped"] += 1
                    continue
            summary["claimed"] += 1

            try:
                workflow = get_workflow(group_id)
                post = workflow.storage.get_post_by_id(post_id)
                if not post:
                    raise PublishError("The post disappeared between claim and publish.")

                preflight(post, workflow._chat_id_for(group_id))

                ok = workflow.publish_post(
                    post_id,
                    post.get("Generated Content", ""),
                    post.get("PDF Path"),
                    post.get("Image Path"),
                )
                if ok:
                    summary["published"] += 1
                    log.info("Published %s to %s", post_id[:8], group_id)
                else:
                    summary["failed"] += 1
            except Exception as exc:
                summary["failed"] += 1
                log.exception("Publishing %s failed", post_id)
                with session_scope() as session:
                    repo.mark_publish_failed(session, post_id, str(exc)[:1000])

    if summary["claimed"] or summary["released"]:
        log.info("Reconciler: %s", summary)
    return summary


def overdue_backlog(group_id: str, minutes: int = 10) -> list[dict]:
    """Approved posts whose time has passed and which are still not out.

    Surfaced in the dashboard rather than stamped "missed" and forgotten, which
    is what the old scheduler did to anything more than five minutes late.
    """
    from datetime import timedelta

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    with session_scope() as session:
        posts = repo.list_posts(session, group_id, states=[PostState.APPROVED])
        return [
            {
                "id": p.id,
                "topic": p.topic,
                "scheduled_for": p.scheduled_for,
                "assets_ready": p.assets_ready,
                "minutes_late": int((datetime.now(timezone.utc) - p.scheduled_for).total_seconds() // 60),
            }
            for p in posts
            if p.scheduled_for and p.scheduled_for < cutoff
        ]


def register(scheduler, get_workflow) -> None:
    """Attach the reconciler to a running APScheduler."""
    scheduler.add_job(
        func=lambda: publish_due_posts(get_workflow),
        trigger="interval",
        seconds=RECONCILE_SECONDS,
        id="publish-reconciler",
        replace_existing=True,
        max_instances=1,          # a slow tick must not overlap the next
        coalesce=True,            # missed ticks collapse into one
        misfire_grace_time=RECONCILE_SECONDS * 4,
    )
    log.info("Publish reconciler registered (every %ds).", RECONCILE_SECONDS)
