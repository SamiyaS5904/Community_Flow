"""
dashboard/routes/topics.py
==========================
The Topic Pool screen.

The pool is the thing that stops the calendar repeating, and until now it had
no interface at all: topics went in from the seed import and from scheduled
discovery, and the only way to see what was in there was to query Postgres by
hand. When a cycle planned badly, "what was actually available to plan from?"
was unanswerable.

Three things happen here. You can see the pool by status; you can add a topic
by hand, which runs the same two gates discovery does rather than bypassing
them; and you can run discovery now instead of waiting for its schedule.
"""
from __future__ import annotations

import logging
import threading
import uuid

from flask import Blueprint, jsonify, request, session

from engine.planning.topic_pool import Verdict
from services.storage import repositories as repo
from services.storage.db import session_scope
from services.storage.models import TopicSource, TopicStatus

log = logging.getLogger(__name__)

bp = Blueprint("topics", __name__, url_prefix="/topics")

#: Discovery calls the web and the model, so it runs in a worker and the page
#: polls it — the same shape every other long job in this dashboard uses.
JOBS: dict[str, dict] = {}


def _humanise(verdict: str) -> str:
    return {
        Verdict.ADMITTED: "Added to the pool.",
        Verdict.ADMITTED_SIMILAR: "Added, but it is close to something already planned.",
        Verdict.REJECTED_DUPLICATE: "Too close to a topic already scheduled or used.",
        Verdict.REJECTED_GUARDRAIL: "Blocked by this community's editorial guardrails.",
        Verdict.REJECTED_CAP: "This week's limit on new topics has been reached.",
        Verdict.REJECTED_ERROR: "The topic could not be checked for duplicates.",
    }.get(verdict, verdict)


def register(app, get_workflow, login_required):
    """Attach the blueprint. `get_workflow` and `login_required` are injected so
    this module does not import dashboard.app and create a cycle."""

    def _pool():
        return get_workflow(session.get("active_group")).topic_pool()

    @bp.get("/")
    @login_required
    def listing():
        """The pool for the active group, grouped by status."""
        group_id = session.get("active_group")
        try:
            with session_scope() as s:
                rows = repo.list_topics(s, group_id)
                counts = repo.topic_counts(s, group_id)
                topics = [
                    {
                        "id": t.id,
                        "title": t.title,
                        "category": t.category,
                        "status": t.status,
                        "source": t.source,
                        "source_url": t.source_url,
                        "similar_to": t.similar_to,
                        "created_at": t.created_at.isoformat() if t.created_at else None,
                    }
                    for t in rows
                ]
        except Exception as exc:
            log.exception("Could not read the topic pool for %s", group_id)
            return jsonify({"ok": False, "error": str(exc)}), 500

        return jsonify({"ok": True, "topics": topics, "counts": counts,
                        "total": sum(counts.values())})

    @bp.post("/add")
    @login_required
    def add():
        """Add one topic by hand.

        It goes through the same dedup and guardrail gates as a discovered one.
        A hand-typed topic is not more trustworthy than a found one — it is
        just as likely to repeat something published three weeks ago, which is
        exactly what a person cannot check and the embedding can.
        """
        title = (request.form.get("title") or "").strip()
        if not title:
            return jsonify({"ok": False, "message": "Type a topic first."}), 400

        try:
            decision = _pool().admit(
                title,
                source=TopicSource.MANUAL,
                category=(request.form.get("category") or "").strip(),
                content_type=(request.form.get("content_type") or "message").strip(),
                # A person adding one topic deliberately is not the weekly
                # discovery budget this cap exists to bound.
                enforce_cap=False,
            )
        except Exception as exc:
            log.exception("Admitting a manual topic failed")
            return jsonify({"ok": False, "message": "Could not add that topic.",
                            "detail": str(exc)}), 500

        return jsonify({
            "ok": decision.admitted,
            "message": _humanise(decision.verdict),
            "detail": decision.reason or "",
            "verdict": decision.verdict,
            "topic_id": decision.topic_id,
            "similarity": round(decision.similarity, 3) if decision.similarity else None,
        }), (200 if decision.admitted else 409)

    @bp.post("/<topic_id>/retire")
    @login_required
    def retire(topic_id):
        """Take a topic out of circulation without deleting it.

        Retired rather than deleted on purpose: a used or scheduled topic is
        still what future candidates are deduplicated against, so deleting one
        would let the thing it blocked come straight back.
        """
        try:
            with session_scope() as s:
                if repo.set_topic_status(s, topic_id, TopicStatus.RETIRED) is None:
                    return jsonify({"ok": False, "message": "That topic no longer exists."}), 404
        except Exception as exc:
            log.exception("Retiring topic %s failed", topic_id)
            return jsonify({"ok": False, "message": "Could not retire that topic.",
                            "detail": str(exc)}), 500
        return jsonify({"ok": True, "message": "Retired.",
                        "detail": "It will not be planned, and still blocks duplicates."})

    @bp.post("/discover")
    @login_required
    def discover():
        """Run a discovery pass now rather than waiting for the schedule."""
        group_id = session.get("active_group")
        job_id = uuid.uuid4().hex[:12]
        JOBS[job_id] = {"status": "running", "message": "Searching the web…"}

        def _run():
            try:
                from engine.planning.discovery import DiscoveryRun

                wf = get_workflow(group_id)
                pool = wf.topic_pool()
                JOBS[job_id]["message"] = "Reading results…"
                summary = DiscoveryRun(
                    wf.group, pool, wf.search,
                    propose=lambda prompt: wf._call_agent(
                        wf.agents["planner"], prompt, use_cache=False),
                ).run()

                admitted = summary.get("admitted", 0)
                JOBS[job_id] = {
                    "status": "done",
                    "message": (f"Added {admitted} new topic{'s' if admitted != 1 else ''}."
                                if admitted else "Nothing new — everything found was a duplicate."),
                    "detail": summary.get("note") or "",
                    "summary": summary,
                }
            except Exception as exc:
                log.exception("Discovery failed for %s", group_id)
                JOBS[job_id] = {"status": "error", "error": str(exc)[:400]}

        threading.Thread(target=_run, daemon=True).start()
        return jsonify({"ok": True, "message": "Looking for new topics.",
                        "detail": "This takes about a minute.", "job_id": job_id})

    @bp.get("/discover/status/<job_id>")
    @login_required
    def discover_status(job_id):
        return jsonify(JOBS.get(job_id, {"status": "error", "error": "That job is not known."}))

    app.register_blueprint(bp)
    return bp
