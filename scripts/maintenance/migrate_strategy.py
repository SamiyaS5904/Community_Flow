"""
scripts/maintenance/migrate_strategy.py
=======================================
Convert a group's legacy blueprint.json into strategy.json, and import the
topic strings it held into the topic pool.

The two artefacts were fused: one file carried both the posting rhythm (which
rarely changes and belongs in version control) and 155 fixed topic strings
(which must change constantly or the calendar repeats). This splits them.

    python scripts/maintenance/migrate_strategy.py placement_prep --dry-run
    python scripts/maintenance/migrate_strategy.py placement_prep

The original blueprint.json is left in place; nothing is deleted.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from engine.config import config                                   # noqa: E402
from engine.group_config import list_available_groups, load_group_config  # noqa: E402
from engine.planning.strategy import DEFAULT_CYCLE_LENGTH, Strategy  # noqa: E402
from engine.planning.topic_pool import Guardrails, TopicPool, Verdict  # noqa: E402
from services.embedding_service import EmbeddingService            # noqa: E402
from services.storage.models import TopicSource                    # noqa: E402

#: Starting guardrails. Deliberately conservative — an operator tunes them in
#: strategy.json rather than in code.
STARTER_GUARDRAILS = {
    "discovery_queries": [],
    "must_cover": [],
    "never_cover": [
        "guaranteed placement",
        "guaranteed percentile",
        "assured selection",
    ],
    "banned_phrases": [],
    "freshness_days": 45,
    "max_new_per_week": 15,
    "duplicate_threshold": 0.86,
    "similar_threshold": 0.78,
}


def build_strategy(strategy: Strategy) -> dict:
    """The plan with every topic string removed."""
    days = []
    for entry in strategy.days:
        slots = []
        for slot in entry.get("slots", []):
            trimmed = {k: v for k, v in slot.items() if k != "topic"}
            trimmed.setdefault("content_type", "message")
            slots.append(trimmed)
        days.append({
            "day": entry.get("day"),
            "theme": entry.get("theme", ""),
            "slots": slots,
        })

    return {
        "_comment": (
            "Posting rhythm and editorial guardrails. No topic strings: topics "
            "come from the pool, so the calendar never replays verbatim."
        ),
        "duration_days": strategy.duration_days,
        "cycle_length": strategy.cycle_length or DEFAULT_CYCLE_LENGTH,
        "cycle_anchor": strategy.anchor.isoformat(),
        "target_audience": "",
        "editorial_guardrails": strategy.guardrails or dict(STARTER_GUARDRAILS),
        "days": days,
    }


def migrate(group_id: str, dry_run: bool) -> int:
    group = load_group_config(group_id)
    strategy = Strategy.load(group)

    # Seeds always come from the legacy file: that is where the topic strings
    # are, and a strategy.json written by an earlier run has none by design.
    # Reading them from `strategy` would make a re-run silently seed nothing.
    seeds = strategy.seed_topics()
    legacy_path = config.GROUPS_DIR / group_id / "blueprint.json"
    if not seeds and legacy_path.exists():
        legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
        seeds = [
            {
                "title": (slot.get("topic") or "").strip(),
                "content_type": slot.get("content_type", "message"),
                "category": slot.get("category", ""),
                "angle": entry.get("theme", ""),
            }
            for entry in legacy.get("days", [])
            for slot in entry.get("slots", [])
            if (slot.get("topic") or "").strip()
        ]
    print(f"\n{group_id}")
    print(f"  source          : {strategy.source_path.name}")
    print(f"  duration        : {strategy.duration_days} days, "
          f"{strategy.cycle_length}-day cycles, anchor {strategy.anchor}")
    print(f"  slots/day (max) : {strategy.slot_count_per_day()}")
    print(f"  topic strings   : {len(seeds)}")

    out_path = config.GROUPS_DIR / group_id / "strategy.json"
    document = build_strategy(strategy)

    if dry_run:
        print(f"  would write     : {out_path}")
        print(f"  would seed      : {len(seeds)} topics into the pool")
        return 0

    out_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    print(f"  wrote           : {out_path}")

    if not seeds:
        print("  nothing to seed.")
        return 0

    pool = TopicPool(
        group,
        EmbeddingService(api_key=config.OPENAI_API_KEY),
        Guardrails.from_strategy(document),
    )
    print(f"  seeding {len(seeds)} topics (this embeds each one)…")
    decisions = pool.seed_topics(seeds)

    tally: dict[str, int] = {}
    for decision in decisions:
        tally[decision.verdict] = tally.get(decision.verdict, 0) + 1
    for verdict, count in sorted(tally.items()):
        print(f"    {verdict:22s} {count}")

    duplicates = [d for d in decisions if d.verdict == Verdict.REJECTED_DUPLICATE]
    if duplicates:
        print(f"\n  {len(duplicates)} of the plan's own topics duplicate each other:")
        for d in duplicates[:8]:
            print(f"    {d.title[:56]!r}\n      ~{d.similarity:.2f} of {d.similar_to[:52]!r}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("group", nargs="?", help="group id (default: all groups)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change without writing or embedding")
    args = parser.parse_args()

    targets = [args.group] if args.group else list_available_groups()
    for group_id in targets:
        try:
            migrate(group_id, args.dry_run)
        except FileNotFoundError as exc:
            print(f"\n{group_id}\n  skipped: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
