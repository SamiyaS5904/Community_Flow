"""
dashboard/routes/cycles.py
==========================
The Cycle Plan screen.

A cycle plan is the answer to "what is this community publishing for the next
fifteen days, and where did each of those topics come from" — and it was
invisible. It was built as a side effect of generating a day's content, written
to a JSON blob in Postgres, and never shown. The two questions an operator
actually has about the calendar are which slots are still empty and what got
assigned where, and neither had an answer short of reading the database.

Building a cycle calls the Planner, so it runs in a worker and the page polls
it, like every other long job here.
"""
from __future__ import annotations

import logging
import threading
import uuid
from datetime import date, datetime, timezone

from flask import Blueprint, jsonify, request, session

log = logging.getLogger(__name__)

bp = Blueprint("cycles", __name__, url_prefix="/cycles")

JOBS: dict[str, dict] = {}


def register(app, get_workflow, login_required):
    @bp.get("/")
    @login_required
    def current():
        """The cycle covering today, plus where today sits inside it."""
        group_id = session.get("active_group")
        try:
            wf = get_workflow(group_id)
            planner = wf.cycle_planner()
            if planner is None:
                # A markdown-only group has no structured strategy; its Planner
                # agent reads the plan directly and there is no cycle to show.
                return jsonify({
                    "ok": True, "has_strategy": False,
                    "message": "This community plans from a markdown blueprint, "
                               "so it has no cycle plan.",
                })

            when = _requested_date()
            position = planner.strategy.position(when)
            plan = planner.load(position.cycle_id)

            return jsonify({
                "ok": True,
                "has_strategy": True,
                "position": {
                    "cycle_number": position.cycle_number,
                    "day_in_cycle": position.day_in_cycle,
                    "plan_day": position.plan_day,
                    "cycle_id": position.cycle_id,
                    "starts_on": position.starts_on.isoformat(),
                    "ends_on": position.ends_on.isoformat(),
                    "cycle_length": planner.strategy.cycle_length,
                },
                "today": when.isoformat(),
                "plan": plan,
                "pool_available": len(wf.topic_pool().available(limit=500)),
            })
        except Exception as exc:
            log.exception("Could not read the cycle plan for %s", group_id)
            return jsonify({"ok": False, "error": str(exc)}), 500

    @bp.post("/build")
    @login_required
    def build():
        """Plan the cycle. `force` replans one that already exists."""
        group_id = session.get("active_group")
        force = request.form.get("force") == "true"
        when = _requested_date()
        job_id = uuid.uuid4().hex[:12]
        JOBS[job_id] = {"status": "running", "message": "Reading the strategy…"}

        def _run():
            try:
                planner = get_workflow(group_id).cycle_planner()
                if planner is None:
                    JOBS[job_id] = {"status": "error",
                                    "error": "This community has no strategy.json to plan from."}
                    return
                JOBS[job_id]["message"] = "Assigning topics to slots…"
                plan = planner.build(when=when, force=force)

                planned = plan.get("slots_planned", 0)
                total = len(plan.get("slots", []))
                # An unplanned slot is not a failure — it generates its own
                # topic. But it is the number worth reporting, because it is
                # what running discovery would fix.
                JOBS[job_id] = {
                    "status": "done",
                    "message": f"Planned {planned} of {total} slots.",
                    "detail": ("" if planned == total else
                               f"{total - planned} slots had no pool topic to draw from."),
                    "cycle_id": plan.get("cycle_id"),
                }
            except Exception as exc:
                log.exception("Building a cycle for %s failed", group_id)
                JOBS[job_id] = {"status": "error", "error": str(exc)[:400]}

        threading.Thread(target=_run, daemon=True).start()
        return jsonify({"ok": True, "message": "Planning the cycle.",
                        "detail": "This takes about half a minute.", "job_id": job_id})

    @bp.get("/build/status/<job_id>")
    @login_required
    def build_status(job_id):
        return jsonify(JOBS.get(job_id, {"status": "error", "error": "That job is not known."}))

    @bp.get("/download")
    @login_required
    def download():
        """The cycle plan as a CSV — one row per slot.

        CSV rather than the stored JSON: the plan is read by people, not by
        another program, and "what are we publishing next fortnight" is a
        question answered in a spreadsheet.
        """
        import csv
        import io

        from flask import Response

        group_id = session.get("active_group")
        try:
            planner = get_workflow(group_id).cycle_planner()
            if planner is None:
                return jsonify({"ok": False,
                                "message": "This community plans from a markdown "
                                           "blueprint, so it has no cycle plan."}), 404
            position = planner.strategy.position(_requested_date())
            plan = planner.load(position.cycle_id)
            if not plan:
                return jsonify({"ok": False,
                                "message": "This cycle has not been planned yet.",
                                "detail": "Plan it first, then download."}), 404
        except Exception as exc:
            log.exception("Could not export the cycle plan for %s", group_id)
            return jsonify({"ok": False, "error": str(exc)}), 500

        buffer = io.StringIO()
        writer = csv.writer(buffer)
        # These are the keys a planned slot actually carries. Writing columns
        # the slots do not have produced a spreadsheet of empty cells.
        writer.writerow(["Date", "Time", "Type", "Theme", "Topic", "Source"])
        for slot in plan.get("slots", []):
            writer.writerow([
                slot.get("date", ""),
                slot.get("time", ""),
                slot.get("content_type", ""),
                slot.get("theme", ""),
                slot.get("topic", "") or "— not assigned; this slot will invent its own",
                slot.get("source_url") or "",
            ])

        # cycle_id already starts with the group id.
        filename = f"{position.cycle_id}.csv"
        return Response(
            buffer.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    app.register_blueprint(bp)
    return bp


def _requested_date() -> date:
    """The date the screen is asking about; today unless one was given."""
    raw = (request.values.get("date") or "").strip()
    if raw:
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            log.warning("Ignoring unparseable date %r", raw)
    return datetime.now(timezone.utc).date()
