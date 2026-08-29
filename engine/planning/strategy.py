"""
engine/planning/strategy.py
===========================
Reads a group's content strategy and does the cycle arithmetic.

A strategy is the durable part of a content plan: the posting rhythm, the
content-type mix, and each day's theme. It deliberately holds no topic strings.
Those live in the topic pool, because a plan with topics baked in can only
repeat — the old file had 155 of them and the day number wrapped, so day 31
republished day 1 word for word.

Cycles
------
The plan is consumed as fixed-length editorial cycles (15 days by default).
Cycles are contiguous halves of one continuous calendar walk, not sealed
campaigns: a week spanning the boundary simply crosses it and resolves two
cycle plans. That keeps the day-number-to-date mapping intact, which the
scheduling dedup key depends on.

The anchor date is per-group config, not a module constant — two tenants
should not be forced onto the same cycle phase.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from engine.config import config
from engine.group_config import GroupConfig

log = logging.getLogger(__name__)

DEFAULT_CYCLE_LENGTH = 15
DEFAULT_ANCHOR = date(2026, 7, 1)


class StrategyNotFound(FileNotFoundError):
    """No strategy.json (or legacy blueprint.json) for this group."""


@dataclass(frozen=True)
class CyclePosition:
    """Where a date falls in the editorial calendar."""

    cycle_number: int      # increments forever from the anchor: 0, 1, 2, …
    day_in_cycle: int      # 1..cycle_length
    plan_day: int          # 1..duration_days — which day of the plan to use
    cycle_id: str
    starts_on: date
    ends_on: date

    def __str__(self) -> str:
        return (f"cycle {self.cycle_number} day {self.day_in_cycle}"
                f" (plan day {self.plan_day})")


@dataclass
class Strategy:
    """One group's posting rhythm."""

    group_id: str
    duration_days: int
    cycle_length: int
    anchor: date
    days: list[dict]
    guardrails: dict
    source_path: Path
    #: True when loaded from a legacy blueprint.json that still carries topics.
    legacy: bool = False

    # ── loading ──────────────────────────────────────────────────────────────

    @classmethod
    def load(cls, group: GroupConfig) -> "Strategy":
        group_dir = config.GROUPS_DIR / group.id
        strategy_path = group_dir / "strategy.json"
        legacy_path = group_dir / "blueprint.json"

        if strategy_path.exists():
            path, legacy = strategy_path, False
        elif legacy_path.exists():
            # Still readable so an un-migrated group keeps working.
            path, legacy = legacy_path, True
            log.info("Group %s is still on blueprint.json; run "
                     "scripts/maintenance/migrate_strategy.py to convert it.",
                     group.id)
        else:
            raise StrategyNotFound(
                f"No strategy for group '{group.id}'. Expected "
                f"{strategy_path} (or the legacy {legacy_path})."
            )

        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            group_id=group.id,
            duration_days=int(data.get("duration_days", 30)),
            cycle_length=int(data.get("cycle_length", DEFAULT_CYCLE_LENGTH)),
            anchor=_parse_anchor(data.get("cycle_anchor")) or DEFAULT_ANCHOR,
            days=data.get("days", []),
            guardrails=data.get("editorial_guardrails", {}) or {},
            source_path=path,
            legacy=legacy,
        )

    @classmethod
    def exists_for(cls, group: GroupConfig) -> bool:
        group_dir = config.GROUPS_DIR / group.id
        return (group_dir / "strategy.json").exists() or (group_dir / "blueprint.json").exists()

    # ── cycle arithmetic ─────────────────────────────────────────────────────

    def position(self, when: date | datetime) -> CyclePosition:
        """Where a calendar date sits in the editorial cycle."""
        day = when.date() if isinstance(when, datetime) else when
        delta = (day - self.anchor).days

        cycle_number = delta // self.cycle_length
        day_in_cycle = (delta % self.cycle_length) + 1

        # Which day of the plan this is. Cycles walk the plan in order and wrap
        # at its end, so a 30-day plan read in 15-day cycles alternates between
        # days 1-15 and 16-30.
        plan_day = (delta % self.duration_days) + 1

        starts_on = self.anchor + timedelta(days=cycle_number * self.cycle_length)
        return CyclePosition(
            cycle_number=cycle_number,
            day_in_cycle=day_in_cycle,
            plan_day=plan_day,
            cycle_id=f"{self.group_id}-c{cycle_number}",
            starts_on=starts_on,
            ends_on=starts_on + timedelta(days=self.cycle_length - 1),
        )

    def cycle_dates(self, cycle_number: int) -> list[date]:
        start = self.anchor + timedelta(days=cycle_number * self.cycle_length)
        return [start + timedelta(days=i) for i in range(self.cycle_length)]

    # ── slots ────────────────────────────────────────────────────────────────

    def slots_for_plan_day(self, plan_day: int) -> list[dict]:
        for entry in self.days:
            if int(entry.get("day", 0)) == plan_day:
                return list(entry.get("slots", []))
        return []

    def theme_for_plan_day(self, plan_day: int) -> str:
        for entry in self.days:
            if int(entry.get("day", 0)) == plan_day:
                return entry.get("theme", "")
        return ""

    def slots_for_date(self, when: date | datetime) -> list[dict]:
        """The skeleton slots for one calendar date, each stamped with where it
        came from so a generated post can be traced back to the plan."""
        position = self.position(when)
        day = when.date() if isinstance(when, datetime) else when
        slots = []
        for slot in self.slots_for_plan_day(position.plan_day):
            enriched = dict(slot)
            enriched["date"] = day.strftime("%Y-%m-%d")
            enriched["plan_day"] = position.plan_day
            enriched["cycle_id"] = position.cycle_id
            enriched["theme"] = self.theme_for_plan_day(position.plan_day)
            slots.append(enriched)
        return slots

    def slot_count_per_day(self) -> int:
        counts = [len(d.get("slots", [])) for d in self.days if d.get("slots")]
        return max(counts) if counts else 0

    # ── seed topics ──────────────────────────────────────────────────────────

    def seed_topics(self) -> list[dict]:
        """Topic strings still embedded in the plan, for a one-time import.

        A migrated strategy.json returns nothing here — which is the point.
        """
        seeds = []
        for entry in self.days:
            for slot in entry.get("slots", []):
                title = (slot.get("topic") or "").strip()
                if title:
                    seeds.append({
                        "title": title,
                        "content_type": slot.get("content_type", "message"),
                        "category": slot.get("category", ""),
                        "angle": entry.get("theme", ""),
                    })
        return seeds


def _parse_anchor(value) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except ValueError:
        log.warning("Unparseable cycle_anchor %r; using the default.", value)
        return None
