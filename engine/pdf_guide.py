"""
engine/pdf_guide.py
===================
Writes a multi-page guide in two passes.

One call for a whole guide does not work. Asked for four sections of three
paragraphs each, gpt-4o-mini returns valid JSON containing four sections of
about thirty words — consistently, across retries, however the prompt is
worded and however large the token budget. It will not write long prose inside
a JSON field when it is also tracking a dozen other keys.

Splitting the job fixes it. The Writer produces an outline: title, subtitle,
cover points, introduction, section headings with a one-line intent each, and
a checklist. Then each section is expanded by its own call, which has one job
and does it properly.

Cost is four or five small calls instead of one — a rounding error on
gpt-4o-mini, and the difference between a guide worth downloading and four
half-empty pages.
"""
from __future__ import annotations

import json
import logging
from typing import Callable

from engine.group_config import GroupConfig
from engine.prompts import render as render_prompt

log = logging.getLogger(__name__)

#: Sections are expanded one at a time; more than this is a very long read.
MAX_SECTIONS = 6


class GuideError(RuntimeError):
    """The outline could not be produced at all."""


def _parse_json(raw: str) -> dict:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[-1] if "\n" in text else text
    return json.loads(text)


def write_guide(
    group: GroupConfig,
    topic: str,
    outline_agent: dict,
    call_json: Callable[[dict, str], str],
    call_expand: Callable[[str], str],
    progress: Callable[[str], None] | None = None,
) -> dict:
    """Produce a complete guide document.

    Args:
        outline_agent: the pdf_writer agent dict.
        call_json:  fn(agent, prompt) -> raw JSON text, for the outline pass.
        call_expand: fn(prompt) -> raw JSON text, for one section.
        progress:   optional status callback.

    Returns the document PdfDocument consumes.
    """
    def say(message: str) -> None:
        if progress:
            progress(message)

    say("Planning the guide…")
    raw = call_json(outline_agent, f"Topic: {topic}")
    if raw.startswith("[ERROR]"):
        raise GuideError(f"Outline pass failed: {raw}")
    try:
        doc = _parse_json(raw)
    except json.JSONDecodeError as exc:
        raise GuideError(f"Outline was not valid JSON: {exc}") from exc

    sections = [s for s in (doc.get("SECTIONS") or []) if isinstance(s, dict)][:MAX_SECTIONS]
    if not sections:
        raise GuideError("Outline contained no sections.")

    headings = [str(s.get("heading", "")).strip() for s in sections]
    expanded = []

    for index, section in enumerate(sections):
        heading = headings[index]
        say(f"Writing section {index + 1}/{len(sections)}: {heading[:32]}…")

        siblings = "; ".join(h for i, h in enumerate(headings) if i != index) or "none"
        prompt = render_prompt(
            "tasks/pdf_section_expand",
            group_name=group.name,
            audience=group.audience_description,
            guide_title=str(doc.get("TITLE", topic)),
            heading=heading,
            intent=str(section.get("intent", "")).strip() or heading,
            siblings=siblings,
        )

        body = example = ""
        try:
            part = _parse_json(call_expand(prompt))
            body = str(part.get("body", "")).strip()
            example = str(part.get("example", "")).strip()
        except Exception as exc:
            # One failed section must not lose the guide; it keeps whatever the
            # outline said and the page renders shorter.
            log.warning("Section %r could not be expanded: %s", heading, exc)

        expanded.append({
            "heading": heading,
            "body": body or str(section.get("intent", "")),
            "example": example,
        })

    doc["SECTIONS"] = expanded
    words = sum(len(s["body"].split()) for s in expanded)
    log.info("Guide %r: %d sections, %d words of body", doc.get("TITLE"), len(expanded), words)
    return doc
