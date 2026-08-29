"""
engine/planning/discovery.py
============================
Where fresh topics come from.

Search was only ever used to *research* a topic somebody had already chosen.
Nothing in the system read the web to decide what was worth posting about, so
the topic list could only ever be as current as the day it was written by hand.

Discovery runs on a schedule, builds its queries from the group's own editorial
guardrails, and proposes topics with the source they came from. Everything it
proposes goes through the same admission gate as any other topic, so a bad
suggestion is caught by the guardrails or by semantic dedup rather than by a
person reading every one.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Callable

from engine.group_config import GroupConfig
from engine.planning.topic_pool import Guardrails, TopicPool
from engine.prompts import render as render_prompt
from services.search_service import SearchService
from services.storage.models import TopicSource

log = logging.getLogger(__name__)

#: Results to pull per query. More than this mostly adds noise.
RESULTS_PER_QUERY = 6


class DiscoveryRun:
    """One discovery pass for one group."""

    def __init__(
        self,
        group: GroupConfig,
        pool: TopicPool,
        search: SearchService,
        propose: Callable[[str], str],
        guardrails: Guardrails | None = None,
    ):
        """
        Args:
            propose: callable taking a prompt and returning the model's JSON
                     text — normally workflow._call_agent for the Planner.
        """
        self.group = group
        self.pool = pool
        self.search = search
        self.propose = propose
        self.guardrails = guardrails or pool.guardrails

    # ── queries ──────────────────────────────────────────────────────────────

    def queries(self) -> list[str]:
        """What to search for.

        Declared queries win; otherwise they are derived from the group's own
        categories so a new group discovers something sensible without anyone
        writing queries for it.
        """
        if self.guardrails.discovery_queries:
            return list(self.guardrails.discovery_queries)

        derived = [f"{cat.name} {datetime.now(timezone.utc).year}"
                   for cat in self.group.categories]
        return derived or [self.group.name]

    # ── the run ──────────────────────────────────────────────────────────────

    def run(self, max_candidates: int = 12) -> dict:
        """Search, propose, and admit. Returns a summary for the dashboard."""
        findings: list[dict] = []
        search_errors: list[str] = []

        for query in self.queries():
            try:
                for result in self.search.search(query, num_results=RESULTS_PER_QUERY):
                    findings.append({
                        "query": query,
                        "title": result.get("title", ""),
                        "snippet": result.get("snippet", ""),
                        "link": result.get("link", ""),
                    })
            except Exception as exc:
                # A dead query should not take the whole run with it.
                log.warning("Discovery search failed for %r: %s", query, exc)
                search_errors.append(f"{query}: {exc}")

        if not findings:
            return {
                "queries": self.queries(), "findings": 0, "proposed": 0,
                "admitted": 0, "decisions": [], "errors": search_errors,
                "note": "No search results; nothing proposed.",
            }

        proposals = self._propose_topics(findings, max_candidates)
        if not proposals:
            return {
                "queries": self.queries(), "findings": len(findings), "proposed": 0,
                "admitted": 0, "decisions": [], "errors": search_errors,
                "note": "Search returned results but no topics were proposed.",
            }

        decisions = self.pool.admit_many(
            [{**p, "source": TopicSource.DISCOVERY} for p in proposals]
        )
        admitted = [d for d in decisions if d.admitted]

        log.info(
            "Discovery for %s: %d findings -> %d proposed -> %d admitted",
            self.group.id, len(findings), len(proposals), len(admitted),
        )
        return {
            "queries": self.queries(),
            "findings": len(findings),
            "proposed": len(proposals),
            "admitted": len(admitted),
            # Rejections are reported, not dropped: an empty pool and a pool
            # whose guardrails are too tight look the same otherwise.
            "decisions": [
                {"title": d.title, "verdict": d.verdict, "reason": d.reason,
                 "similar_to": d.similar_to, "similarity": d.similarity}
                for d in decisions
            ],
            "errors": search_errors,
        }

    # ── model call ───────────────────────────────────────────────────────────

    def _propose_topics(self, findings: list[dict], limit: int) -> list[dict]:
        digest = "\n".join(
            f"- {f['title']}\n  {f['snippet']}\n  source: {f['link']}"
            for f in findings[:40]
        )
        prompt = render_prompt(
            "tasks/topic_discovery",
            group_name=self.group.name,
            audience=self.group.audience_description,
            categories=", ".join(c.id for c in self.group.categories) or "general",
            must_cover=", ".join(self.guardrails.must_cover) or "no specific requirements",
            never_cover=", ".join(self.guardrails.never_cover) or "nothing excluded",
            limit=limit,
            findings=digest,
        )

        try:
            raw = self.propose(prompt)
        except Exception as exc:
            log.error("Topic proposal call failed: %s", exc)
            return []

        return self._parse(raw, limit)

    @staticmethod
    def _parse(raw: str, limit: int) -> list[dict]:
        text = (raw or "").strip()
        if text.startswith("```"):
            text = text.strip("`")
            text = text.split("\n", 1)[-1] if "\n" in text else text
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            log.error("Discovery proposal was not valid JSON: %.200s", text)
            return []

        if isinstance(data, dict):
            data = data.get("topics") or next(
                (v for v in data.values() if isinstance(v, list)), []
            )
        if not isinstance(data, list):
            return []

        topics = []
        for item in data[:limit]:
            if isinstance(item, str):
                topics.append({"title": item.strip()})
            elif isinstance(item, dict) and item.get("title"):
                topics.append({
                    "title": str(item["title"]).strip(),
                    "angle": str(item.get("angle", "")).strip(),
                    "category": str(item.get("category", "")).strip(),
                    "source_url": item.get("source_url") or item.get("source"),
                })
        return topics
