"""
engine/prompts.py
=================
The one way to get prompt text into the application.

Every prompt lives as a Markdown file under prompts/. Python parameterises and
assembles them; it never authors them. Before this existed there were roughly
53 prompt sites across four files — 27 inline in dashboard/app.py and 12 in
engine/workflow.py — so changing the writing voice meant grepping the codebase.

File format: optional YAML front matter for structured fields, then the body.

    ---
    role: Quality Assurance Editor
    agent_type: qa
    ---
    Check the provided draft. Reject and rewrite it if it:
    - Uses any of: {avoid_phrases}

Placeholders use single braces and are filled by keyword argument. A missing
one raises rather than rendering "{avoid_phrases}" into a live prompt.

Usage:
    from engine.prompts import render, load_agent

    text  = render("tasks/poll_format")
    agent = load_agent("writer", group_name="CAT Prep", tone="calm")
"""
from __future__ import annotations

import re
import string
import threading
from pathlib import Path
from typing import Any

import yaml

from engine.config import config

PROMPTS_DIR = Path(config.PROJECT_ROOT) / "prompts"

_cache: dict[str, tuple[dict[str, Any], str]] = {}
_cache_lock = threading.Lock()

_FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


class PromptError(Exception):
    """A prompt file is missing, malformed, or was rendered with bad params."""


class _StrictFormatter(string.Formatter):
    """Formatter that reports every missing key at once, by name."""

    def __init__(self) -> None:
        super().__init__()
        self.missing: list[str] = []

    def get_value(self, key, args, kwargs):
        if isinstance(key, str):
            if key not in kwargs:
                self.missing.append(key)
                return ""
            return kwargs[key]
        return super().get_value(key, args, kwargs)


def _read(name: str) -> tuple[dict[str, Any], str]:
    """Return (front_matter, body) for a prompt name like 'agents/writer'."""
    with _cache_lock:
        cached = _cache.get(name)
    if cached is not None:
        return cached

    path = PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        raise PromptError(
            f"Prompt '{name}' not found at {path}. "
            "Every prompt lives under prompts/ — see prompts/README.md."
        )

    raw = path.read_text(encoding="utf-8")
    meta: dict[str, Any] = {}
    match = _FRONT_MATTER.match(raw)
    if match:
        try:
            meta = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError as exc:
            raise PromptError(f"Prompt '{name}' has invalid front matter: {exc}") from exc
        raw = raw[match.end():]

    body = raw.strip()
    with _cache_lock:
        _cache[name] = (meta, body)
    return meta, body


def render(name: str, **params: Any) -> str:
    """Load a prompt and fill its placeholders.

    Raises PromptError naming every placeholder the caller failed to supply,
    rather than letting a literal "{topic}" reach the model.
    """
    _, body = _read(name)
    formatter = _StrictFormatter()
    text = formatter.vformat(body, (), params)
    if formatter.missing:
        raise PromptError(
            f"Prompt '{name}' is missing parameter(s): "
            f"{', '.join(sorted(set(formatter.missing)))}"
        )
    return text


def meta(name: str) -> dict[str, Any]:
    """Front-matter fields for a prompt, without rendering the body."""
    return dict(_read(name)[0])


def load_agent(name: str, **params: Any) -> dict:
    """Build a pipeline agent dict from prompts/agents/<name>.md.

    Front matter supplies role, goal, expected_output and agent_type; the body
    becomes `instructions`. This is the shape PlatformWorkflow and
    OpenAIService already expect.
    """
    front, _ = _read(f"agents/{name}")
    missing = [k for k in ("role", "goal", "expected_output", "agent_type")
               if not front.get(k)]
    if missing:
        raise PromptError(
            f"Agent prompt 'agents/{name}' front matter is missing: {', '.join(missing)}"
        )

    formatter = _StrictFormatter()
    agent = {
        "role": formatter.vformat(str(front["role"]), (), params),
        "goal": formatter.vformat(str(front["goal"]), (), params),
        "expected_output": formatter.vformat(str(front["expected_output"]), (), params),
        "agent_type": str(front["agent_type"]),
        "instructions": render(f"agents/{name}", **params),
    }
    if formatter.missing:
        raise PromptError(
            f"Agent prompt 'agents/{name}' is missing parameter(s): "
            f"{', '.join(sorted(set(formatter.missing)))}"
        )
    return agent


def available() -> list[str]:
    """Every prompt name discoverable under prompts/, sorted."""
    if not PROMPTS_DIR.exists():
        return []
    return sorted(
        str(p.relative_to(PROMPTS_DIR).with_suffix("")).replace("\\", "/")
        for p in PROMPTS_DIR.rglob("*.md")
        if p.name != "README.md"
    )


def clear_cache() -> None:
    """Drop parsed prompts. Call after editing a prompt file at runtime."""
    with _cache_lock:
        _cache.clear()


def check_all() -> list[str]:
    """Parse every prompt file and return a list of problems.

    Called by engine.health at startup so a malformed prompt is caught on boot
    rather than three LLM calls into a batch run.
    """
    problems: list[str] = []
    for name in available():
        try:
            _read(name)
        except PromptError as exc:
            problems.append(str(exc))
    return problems
