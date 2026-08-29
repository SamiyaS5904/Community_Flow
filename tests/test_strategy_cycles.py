"""
tests/test_strategy_cycles.py
=============================
Cycle arithmetic and cycle planning.

The property under test is the one the old design could not have: the same
plan day, reached in a different cycle, must not produce the same content.
"""
from __future__ import annotations

import json
import sys
import uuid
from datetime import date, timedelta
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from engine.planning.strategy import Strategy, StrategyNotFound  # noqa: E402


def _strategy(duration=30, cycle_length=15, anchor="2026-07-01", slots_per_day=3):
    return Strategy(
        group_id="test_group",
        duration_days=duration,
        cycle_length=cycle_length,
        anchor=date.fromisoformat(anchor),
        days=[
            {
                "day": d,
                "theme": f"theme {d}",
                "slots": [
                    {"time": f"{8 + i * 4:02d}:00", "content_type": "message", "cta": False}
                    for i in range(slots_per_day)
                ],
            }
            for d in range(1, duration + 1)
        ],
        guardrails={},
        source_path=Path("strategy.json"),
    )


# ── cycle arithmetic (offline) ───────────────────────────────────────────────

def test_the_anchor_is_cycle_zero_day_one():
    s = _strategy()
    p = s.position(date(2026, 7, 1))
    assert (p.cycle_number, p.day_in_cycle, p.plan_day) == (0, 1, 1)


def test_a_thirty_day_plan_is_read_as_two_fifteen_day_cycles():
    s = _strategy()
    first = s.position(date(2026, 7, 1))
    second = s.position(date(2026, 7, 16))
    assert first.cycle_number == 0 and first.plan_day == 1
    assert second.cycle_number == 1 and second.plan_day == 16


def test_the_plan_wraps_but_the_cycle_number_does_not():
    """This is the fix for the old repeat: the plan day comes round again, so
    the rhythm repeats — but the cycle is new, so the topics are not."""
    s = _strategy()
    first = s.position(date(2026, 7, 1))
    third = s.position(date(2026, 7, 31))     # 30 days later

    assert first.plan_day == third.plan_day == 1, "the rhythm should repeat"
    assert third.cycle_number > first.cycle_number, "the cycle must not repeat"
    assert first.cycle_id != third.cycle_id


def test_cycle_numbers_increase_forever():
    s = _strategy()
    seen = [s.position(date(2026, 7, 1) + timedelta(days=15 * i)).cycle_number
            for i in range(8)]
    assert seen == list(range(8))


def test_a_week_may_span_a_cycle_boundary():
    """Cycles are contiguous halves of one calendar walk, not sealed campaigns.
    Sealing them would break the date-to-day mapping the dedup key relies on."""
    s = _strategy()
    week = [s.position(date(2026, 7, 12) + timedelta(days=i)) for i in range(7)]
    cycles = {p.cycle_number for p in week}
    assert len(cycles) == 2, "a week crossing day 15 should resolve two cycles"
    # and the calendar stays continuous across the boundary
    assert [p.plan_day for p in week] == [12, 13, 14, 15, 16, 17, 18]


def test_each_group_can_sit_on_its_own_cycle_phase():
    early = _strategy(anchor="2026-07-01")
    late = _strategy(anchor="2026-07-08")
    day = date(2026, 7, 20)
    assert early.position(day).day_in_cycle != late.position(day).day_in_cycle


def test_cycle_dates_cover_exactly_the_cycle_length():
    s = _strategy(cycle_length=15)
    dates = s.cycle_dates(2)
    assert len(dates) == 15
    assert dates[0] == date(2026, 7, 1) + timedelta(days=30)


# ── slots ────────────────────────────────────────────────────────────────────

def test_slots_carry_their_provenance():
    s = _strategy()
    slots = s.slots_for_date(date(2026, 7, 3))
    assert slots, "expected slots for that day"
    for slot in slots:
        assert slot["date"] == "2026-07-03"
        assert slot["plan_day"] == 3
        assert slot["cycle_id"].endswith("-c0")
        assert slot["theme"] == "theme 3"


def test_a_migrated_strategy_holds_no_topics():
    """A strategy that still carries topic strings can only repeat."""
    assert _strategy().seed_topics() == []


def test_seed_topics_finds_strings_in_a_legacy_plan():
    legacy = _strategy()
    legacy.days[0]["slots"][0]["topic"] = "An old hardcoded topic"
    seeds = legacy.seed_topics()
    assert [s["title"] for s in seeds] == ["An old hardcoded topic"]


def test_missing_strategy_names_both_paths():
    from engine.group_config import load_group_config, list_available_groups

    group = load_group_config(list_available_groups()[0])
    group.id = f"nonexistent_{uuid.uuid4().hex[:6]}"
    with pytest.raises(StrategyNotFound, match="strategy.json"):
        Strategy.load(group)


# ── the real group's migrated strategy ───────────────────────────────────────

def test_placement_prep_strategy_is_migrated_and_guarded():
    path = PROJECT_ROOT / "groups" / "placement_prep" / "strategy.json"
    if not path.exists():
        pytest.skip("placement_prep has not been migrated")

    data = json.loads(path.read_text(encoding="utf-8"))
    topics = [slot.get("topic") for day in data["days"] for slot in day["slots"]]
    assert not any(topics), "strategy.json still contains topic strings"

    rails = data.get("editorial_guardrails", {})
    assert rails.get("never_cover"), "no never_cover guardrail configured"
    assert rails.get("discovery_queries"), "no discovery queries configured"
    assert data.get("cycle_anchor"), "cycle anchor must be config, not a constant"


# ── cycle planning ───────────────────────────────────────────────────────────

class _FakePool:
    """Enough of TopicPool for the planner, with a finite supply."""

    def __init__(self, titles, content_type="message"):
        self._topics = [
            {"id": f"t{i}", "title": t, "angle": "", "category": "",
             "content_type": content_type, "source": "seed", "source_url": None,
             "similar_to": None, "similarity": None}
            for i, t in enumerate(titles)
        ]

    def available(self, limit=100, category=None):
        return list(self._topics[:limit])


def test_rotation_never_assigns_one_topic_twice():
    from engine.planning.cycle import CyclePlanner

    s = _strategy(duration=2, cycle_length=2, slots_per_day=3)
    planner = CyclePlanner.__new__(CyclePlanner)
    planner.strategy = s
    skeleton = [
        {"date": "d", "time": "08:00", "content_type": "message", "cta": False,
         "theme": "", "plan_day": 1, "slot_index": i, "topic": "", "topic_id": None}
        for i in range(4)
    ]
    candidates = _FakePool(["A", "B", "C", "D", "E"]).available()

    assigned = CyclePlanner._assign_by_rotation(planner, skeleton, candidates)
    used = [s["topic"] for s in assigned if s["topic"]]
    assert len(used) == len(set(used)) == 4


def test_rotation_leaves_slots_empty_rather_than_reusing_topics():
    """An empty slot invents its own topic; a reused one is a visible repeat."""
    from engine.planning.cycle import CyclePlanner

    planner = CyclePlanner.__new__(CyclePlanner)
    skeleton = [
        {"date": "d", "time": "08:00", "content_type": "message", "cta": False,
         "theme": "", "plan_day": 1, "slot_index": i, "topic": "", "topic_id": None}
        for i in range(5)
    ]
    candidates = _FakePool(["only", "two"]).available()

    assigned = CyclePlanner._assign_by_rotation(planner, skeleton, candidates)
    filled = [s["topic"] for s in assigned if s["topic"]]
    assert filled == ["only", "two"]
    assert sum(1 for s in assigned if not s["topic"]) == 3


def test_planner_assignment_parser_handles_junk():
    from engine.planning.cycle import _parse_assignment

    assert _parse_assignment('[{"slot": 0, "topic_id": "t1"}]') == {0: "t1"}
    assert _parse_assignment('{"assignments": [{"slot": 2, "topic_id": "x"}]}') == {2: "x"}
    assert _parse_assignment("not json at all") == {}
    assert _parse_assignment('[{"slot": "bad"}]') == {}
