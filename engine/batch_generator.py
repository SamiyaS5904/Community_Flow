"""
engine/batch_generator.py
==========================
Blueprint Batch Generation — runs one day or one week of blueprint slots
through the SAME full LLM pipeline as manual generation, producing posts
of identical quality/shape that land in the pending queue.

Design principles:
  - Calls existing PlatformWorkflow.generate_single_content() directly —
    no re-implementation of Research/Writer/QA/PDF/Asset logic here.
  - Skips slots that already have a post in the sheet for that group+date+time.
  - A single failed slot does NOT abort the batch; failure is logged and
    execution continues with the next slot.
  - Runs synchronously inside whatever thread the caller provides — the caller
    (Flask route) is responsible for spawning a background thread.

Progress tracking is done via a caller-supplied `progress_callback(done, total, msg)`
so the Flask route can update its own in-memory job store without coupling
this module to Flask.
"""
from __future__ import annotations

import traceback
from datetime import datetime, timezone
from typing import Callable, Optional

from engine.blueprint_engine import get_slots_range
from engine.group_config import GroupConfig
from engine.workflow import PlatformWorkflow


def generate_from_blueprint(
    workflow: PlatformWorkflow,
    mode: str,
    start_day: int,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
) -> dict:
    """
    Generate content for one blueprint day or one week from the given start_day.

    Args:
        workflow:          PlatformWorkflow instance for the active group.
        mode:              "day" (1 day = today's slots) or
                           "week" (7 consecutive days from start_day).
        start_day:         1-based blueprint day number to begin from.
        progress_callback: Optional fn(done: int, total: int, msg: str).
                           Called after every slot attempt (success or fail).

    Returns:
        {
            "generated":         int,   # successfully added to pending queue
            "skipped_duplicate": int,   # already existed for that date+time
            "failed": [                 # slots that errored (logged, not fatal)
                {"day": int, "date": str, "time": str, "topic": str, "error": str}
            ]
        }

    Raises:
        FileNotFoundError: re-raised when the group has no blueprint.json,
                           so the caller can show a meaningful error message.
    """
    num_days = 7 if mode == "week" else 1

    # One implementation, not two. This used to re-walk the slots and call
    # generate_single_content itself, in parallel with PlatformWorkflow.
    # generate_queue — so a fix to one silently missed the other, which is how
    # the working path here and the broken path the UI actually used drifted
    # apart. It now drives the same primitive the dashboard does.
    from engine.blueprint_engine import _load_blueprint, _date_for_day_offset

    duration, _ = _load_blueprint(workflow.group)

    generated = 0
    failed: list[dict] = []

    def _cb(done: int, total: int, msg: str) -> None:
        if progress_callback:
            progress_callback(done, total, msg)

    _cb(0, num_days, f"Generating {num_days} day(s) from day {start_day}…")

    for offset in range(num_days):
        day_number = ((start_day - 1 + offset) % duration) + 1
        date_str = _date_for_day_offset(offset).strftime("%Y-%m-%d")
        _cb(offset, num_days, f"[Day {day_number}] {date_str}")
        try:
            results = workflow.generate_queue(
                date_str,
                status_callback=lambda m: _cb(offset, num_days, f"  ↳ {m}"),
            )
            generated += len(results)
            failed.extend(getattr(workflow, "failed_slots", []))
        except Exception as exc:
            traceback.print_exc()
            failed.append({
                "day": day_number, "date": date_str, "time": "",
                "topic": "", "error": f"{type(exc).__name__}: {exc}",
            })
        _cb(offset + 1, num_days, f"[Day {day_number}] done")

    summary = {
        "generated": generated,
        "skipped_duplicate": 0,   # generate_queue skips slots already scheduled
        "failed": failed,
    }
    _cb(num_days, num_days,
        f"Batch complete. Generated={generated}, Failed={len(failed)}")
    return summary
