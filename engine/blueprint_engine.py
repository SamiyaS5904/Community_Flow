"""
engine/blueprint_engine.py
===========================
Reads and serves slots from a group's structured blueprint.json.

No LLM calls here — this is pure JSON parsing.
The blueprint.json must live at:
    groups/<group.id>/blueprint.json   (preferred)
    blueprint.json                     (root fallback)

Blueprint JSON schema expected:
{
    "duration_days": 15,
    "days": [
        {
            "day": 1,
            "theme": "...",
            "slots": [
                {
                    "time": "08:00",
                    "category": "Motivation",
                    "topic": "...",
                    "instruction": "...",
                    "pdf_required": false,
                    "image_required": true,
                    "search_required": false,
                    "cta": false
                }
            ]
        }
    ]
}
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from engine.group_config import GroupConfig
from engine.config import config


# Anchor date used to compute the rolling day number.
# Day 1 of the cycle = 2026-07-01.
_ANCHOR_DATE = datetime(2026, 7, 1)


def _load_blueprint(group: GroupConfig) -> tuple[int, list[dict]]:
    """
    Load blueprint.json for the given group.

    Returns:
        (duration_days, days_list) tuple.

    Raises:
        FileNotFoundError: if no blueprint.json exists for this group.
    """
    # Preferred location: groups/<group.id>/blueprint.json
    group_path = config.GROUPS_DIR / group.id / "blueprint.json"
    root_path = Path(config.PROJECT_ROOT) / "blueprint.json"

    if group_path.exists():
        blueprint_path = group_path
    elif root_path.exists():
        blueprint_path = root_path
    else:
        raise FileNotFoundError(
            f"No blueprint.json found for group '{group.id}'. "
            f"Expected at: {group_path} or {root_path}. "
            "Generate one via Claude and save it there before using Blueprint Batch Generation."
        )

    with open(blueprint_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    duration = int(data.get("duration_days", 15))
    days = data.get("days", [])
    return duration, days


def _compute_day_number(target_date: datetime, duration: int) -> int:
    """
    Returns the 1-based day number within the blueprint cycle for a given date.
    Wraps around when crossing the end of the cycle.
    """
    delta = (target_date - _ANCHOR_DATE).days
    return (delta % duration) + 1


def _date_for_day_offset(offset: int) -> datetime:
    """Returns today's date + offset days (midnight, no time component)."""
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return today + timedelta(days=offset)


def _find_slots_for_day(days: list[dict], day_number: int) -> list[dict]:
    """Return the slots list for a given 1-based day number."""
    for d in days:
        if int(d.get("day", 0)) == day_number:
            return d.get("slots", [])
    return []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def enrich_slot(slot: dict) -> dict:
    """
    Checks if 'content_type' exists in the slot. If so, maps flags, category,
    and fallback values for the new blueprint format. Otherwise returns
    the slot unmodified for backward compatibility.
    """
    if "content_type" in slot:
        content_type = slot.get("content_type", "message").lower()
        CONTENT_TYPE_FLAGS = {
            "message": {"pdf_required": False, "image_required": False, "search_required": False},
            "poll":    {"pdf_required": False, "image_required": False, "search_required": False},
            "image":   {"pdf_required": False, "image_required": True,  "search_required": False},
            "link":    {"pdf_required": False, "image_required": False, "search_required": True},
            "pdf":     {"pdf_required": True,  "image_required": False, "search_required": False},
        }

        flags = CONTENT_TYPE_FLAGS.get(content_type)
        if flags is None:
            import logging
            logging.getLogger(__name__).warning(
                f"Unrecognized content_type '{content_type}' in slot. Defaulting to 'message'."
            )
            flags = CONTENT_TYPE_FLAGS["message"]

        enriched = dict(slot)
        # Sourced category from content_type if not present (with PDF uppercase, others capitalized)
        category_default = "PDF" if content_type == "pdf" else content_type.capitalize()
        enriched["category"] = slot.get("category", category_default)
        enriched["pdf_required"] = flags["pdf_required"]
        enriched["image_required"] = flags["image_required"]
        enriched["search_required"] = flags["search_required"]
        enriched["cta"] = slot.get("cta", False)
        enriched["instruction"] = slot.get("instruction", "")
        return enriched
    return slot

def get_slots_range(group: GroupConfig, start_day: int, num_days: int) -> list[dict]:
    """
    Return a flat list of slot dicts for `num_days` consecutive blueprint days
    starting at `start_day` (wrapping around the cycle).

    Each returned slot dict is enriched with fields like day, date, and mapped flags.

    Args:
        group:      The GroupConfig for the active tenant.
        start_day:  1-based blueprint day number to start from.
        num_days:   How many consecutive days to include.

    Returns:
        Flat list of slot dicts, each carrying `day` and `date`.

    Raises:
        FileNotFoundError: if no blueprint.json exists for this group.
    """
    duration, days = _load_blueprint(group)

    slots_out: list[dict] = []

    for offset in range(num_days):
        # Wrap the day number around the duration
        day_number = ((start_day - 1 + offset) % duration) + 1

        # Map day offset to a real calendar date
        # offset=0 → today, offset=1 → tomorrow, etc.
        cal_date = _date_for_day_offset(offset)
        date_str = cal_date.strftime("%Y-%m-%d")

        day_slots = _find_slots_for_day(days, day_number)
        for slot in day_slots:
            enriched = dict(slot)          # don't mutate the original
            enriched["day"] = day_number
            enriched["date"] = date_str
            enriched = enrich_slot(enriched)
            slots_out.append(enriched)

    return slots_out


def get_todays_slots(group: GroupConfig) -> list[dict]:
    """
    Convenience wrapper: returns slots for today's blueprint day.
    Delegates to get_slots_range() — no duplicated logic.
    """
    duration, _ = _load_blueprint(group)
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    today_day_number = _compute_day_number(today, duration)
    return get_slots_range(group, today_day_number, 1)


def has_blueprint(group: GroupConfig) -> bool:
    """Returns True if a blueprint.json exists for this group."""
    group_path = config.GROUPS_DIR / group.id / "blueprint.json"
    root_path = Path(config.PROJECT_ROOT) / "blueprint.json"
    return group_path.exists() or root_path.exists()
