"""
engine/planning/cycle.py
========================
Builds a cycle plan: one editorial fortnight, with pool topics assigned to the
slots the strategy declares.

This is where the Planner earns its place. For a group with a structured plan
it was never called at all — day-to-day generation read topic strings straight
out of the file — so nothing in the system ever looked at a stretch of calendar
as a whole. Planning per cycle instead of per day is what allows category
rotation, spacing related topics apart, and refreshing stale ones, none of
which a single day can see.

The plan is persisted so an operator can review and edit a fortnight before a
word is generated, and so generation is reproducible: the same cycle plan
always produces the same assignment of topics to slots.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timezone
from typing import Callable

from engine.group_config import GroupConfig
from engine.planning.strategy import CyclePosition, Strategy
from engine.planning.topic_pool import TopicPool
from engine.prompts import render as render_prompt
from services.storage.db import session_scope
from services.storage import repositories as repo
from services.storage.models import Topic, TopicStatus

log = logging.getLogger(__name__)


class CyclePlanner:
    """Assigns pool topics to the slots of one cycle."""

    def __init__(
        self,
        group: GroupConfig,
        strategy: Strategy,
        pool: TopicPool,
        plan_with_model: Callable[[str], str] | None = None,
    ):
        """
        Args:
            plan_with_model: optional callable taking a prompt and returning
                the Planner's JSON. Without it, assignment falls back to a
                deterministic rotation — which is worse editorially but never
                leaves a cycle unplanned because an API call failed.
        """
        self.group = group
        self.strategy = strategy
        self.pool = pool
        self.plan_with_model = plan_with_model

    # ── building ─────────────────────────────────────────────────────────────

    def build(self, when: date | datetime | None = None, force: bool = False) -> dict:
        """Build (or fetch) the cycle plan covering `when`."""
        moment = when or datetime.now(timezone.utc).date()
        position = self.strategy.position(moment)

        if not force:
            existing = self.load(position.cycle_id)
            if existing:
                log.info("Cycle %s already planned; reusing it.", position.cycle_id)
                return existing

        skeleton = self._skeleton(position)
        slots_needing_topics = [s for s in skeleton if not s.get("topic")]
        candidates = self.pool.available(limit=max(40, len(slots_needing_topics) * 4))

        if not candidates:
            log.warning(
                "Cycle %s: the topic pool is empty, so slots will generate their "
                "own topics. Run discovery or seed the pool.", position.cycle_id
            )

        assignments = self._assign(skeleton, candidates)
        plan = {
            "cycle_id": position.cycle_id,
            "group_id": self.group.id,
            "cycle_number": position.cycle_number,
            "starts_on": position.starts_on.isoformat(),
            "ends_on": position.ends_on.isoformat(),
            "cycle_length": self.strategy.cycle_length,
            "built_at": datetime.now(timezone.utc).isoformat(),
            "slots": assignments,
            "topics_available": len(candidates),
            "slots_planned": sum(1 for s in assignments if s.get("topic")),
        }
        self.save(plan, position)
        self._reserve(assignments)
        return plan

    def _skeleton(self, position: CyclePosition) -> list[dict]:
        """Every slot in the cycle, in calendar order."""
        slots = []
        for day in self.strategy.cycle_dates(position.cycle_number):
            for index, slot in enumerate(self.strategy.slots_for_date(day)):
                slots.append({
                    "date": day.isoformat(),
                    "time": slot.get("time", "12:00"),
                    "content_type": slot.get("content_type", "message"),
                    "cta": bool(slot.get("cta", False)),
                    "theme": slot.get("theme", ""),
                    "plan_day": slot.get("plan_day"),
                    "slot_index": index,
                    "topic": slot.get("topic", ""),      # empty in a migrated strategy
                    "topic_id": None,
                })
        return slots

    # ── assignment ───────────────────────────────────────────────────────────

    def _assign(self, skeleton: list[dict], candidates: list[dict]) -> list[dict]:
        if not candidates:
            return skeleton
        if self.plan_with_model:
            try:
                return self._assign_with_model(skeleton, candidates)
            except Exception as exc:
                log.warning("Planner call failed (%s); falling back to rotation.", exc)
        return self._assign_by_rotation(skeleton, candidates)

    def _assign_by_rotation(self, skeleton: list[dict], candidates: list[dict]) -> list[dict]:
        """Deterministic fallback: match content type where possible, else take
        the next unused topic. Never assigns the same topic twice."""
        by_type: dict[str, list[dict]] = {}
        for candidate in candidates:
            by_type.setdefault(candidate.get("content_type", "message"), []).append(candidate)
        used: set[str] = set()

        for slot in skeleton:
            if slot.get("topic"):
                continue
            wanted = slot["content_type"]
            pick = next(
                (c for c in by_type.get(wanted, []) if c["id"] not in used),
                next((c for c in candidates if c["id"] not in used), None),
            )
            if pick is None:
                continue        # pool exhausted; the slot invents its own topic
            used.add(pick["id"])
            slot["topic"] = pick["title"]
            slot["topic_id"] = pick["id"]
            slot["instruction"] = pick.get("angle", "")
            slot["source_url"] = pick.get("source_url")
        return skeleton

    def _assign_with_model(self, skeleton: list[dict], candidates: list[dict]) -> list[dict]:
        prompt = render_prompt(
            "tasks/cycle_plan",
            group_name=self.group.name,
            audience=self.group.audience_description,
            cycle_length=self.strategy.cycle_length,
            slots=json.dumps(
                [
                    {"n": i, "date": s["date"], "time": s["time"],
                     "content_type": s["content_type"], "theme": s["theme"]}
                    for i, s in enumerate(skeleton)
                ],
                indent=1,
            ),
            topics=json.dumps(
                [{"id": c["id"], "title": c["title"], "content_type": c["content_type"],
                  "category": c["category"]} for c in candidates],
                indent=1,
            ),
        )
        raw = self.plan_with_model(prompt)
        mapping = _parse_assignment(raw)
        if not mapping:
            raise ValueError("Planner returned no usable assignment")

        by_id = {c["id"]: c for c in candidates}
        used: set[str] = set()
        for slot_index, topic_id in mapping.items():
            if slot_index >= len(skeleton) or topic_id in used:
                continue
            candidate = by_id.get(topic_id)
            if candidate is None:
                continue
            used.add(topic_id)
            slot = skeleton[slot_index]
            slot["topic"] = candidate["title"]
            slot["topic_id"] = candidate["id"]
            slot["instruction"] = candidate.get("angle", "")
            slot["source_url"] = candidate.get("source_url")

        # Anything the model skipped still gets a topic.
        return self._assign_by_rotation(skeleton, [c for c in candidates if c["id"] not in used])

    def _reserve(self, assignments: list[dict]) -> None:
        """Mark assigned topics SCHEDULED so dedup counts them from now on."""
        topic_ids = [s["topic_id"] for s in assignments if s.get("topic_id")]
        if not topic_ids:
            return
        with session_scope() as session:
            for topic_id in topic_ids:
                topic = session.get(Topic, topic_id)
                if topic and topic.status == TopicStatus.CANDIDATE:
                    topic.status = TopicStatus.SCHEDULED

    # ── persistence ──────────────────────────────────────────────────────────

    def save(self, plan: dict, position: CyclePosition) -> None:
        with session_scope() as session:
            repo.upsert_cycle(
                session,
                cycle_id=position.cycle_id,
                group_id=self.group.id,
                cycle_number=position.cycle_number,
                starts_on=datetime.combine(position.starts_on, time.min, tzinfo=timezone.utc),
                ends_on=datetime.combine(position.ends_on, time.max, tzinfo=timezone.utc),
                plan=plan,
            )

    def load(self, cycle_id: str) -> dict | None:
        with session_scope() as session:
            cycle = repo.get_cycle(session, cycle_id)
            return dict(cycle.plan) if cycle and cycle.plan else None

    def slots_for_date(self, when: date | datetime) -> list[dict]:
        """The planned slots for one date, building the cycle if needed."""
        day = when.date() if isinstance(when, datetime) else when
        plan = self.build(day)
        return [s for s in plan.get("slots", []) if s.get("date") == day.isoformat()]


def _parse_assignment(raw: str) -> dict[int, str]:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[-1] if "\n" in text else text
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        log.error("Cycle plan was not valid JSON: %.200s", text)
        return {}

    if isinstance(data, dict):
        data = data.get("assignments") or next(
            (v for v in data.values() if isinstance(v, list)), []
        )
    mapping: dict[int, str] = {}
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            mapping[int(item["slot"])] = str(item["topic_id"])
        except (KeyError, TypeError, ValueError):
            continue
    return mapping
