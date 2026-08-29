"""
tests/test_prompts.py
=====================
Guards the rule that makes the prompt library worth having: no prompt text may
be authored inside dashboard/, engine/ or services/.

Without this test the 53 inline prompt sites grow back one hotfix at a time.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Directories that orchestrate but must never author prompt text.
CODE_DIRS = ["dashboard", "engine", "services", "agents"]

# engine/prompts.py documents the file format, so its docstrings legitimately
# contain prompt-shaped examples.
EXEMPT = {"engine/prompts.py"}

# Phrases that only appear in text written to be read by a model.
PROMPT_MARKERS = [
    "you must", "return only", "do not wrap", "output only",
    "format as a", "your task is", "critical format", "output format",
    "never write", "do not include any prose", "you are writing on behalf of",
    "do not hallucinate", "absolute rules",
]


def _string_literals(path: Path):
    """Yield every string constant in a Python file, with its line number."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    except SyntaxError as exc:
        pytest.fail(f"{path} does not parse: {exc}")

    # Module/class/function docstrings describe code, not prompts.
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in docstrings:
            yield node.lineno, node.value


def test_no_prompt_text_authored_in_code():
    """Prompt text belongs in prompts/, loaded by name — never inline."""
    offenders: list[str] = []

    for directory in CODE_DIRS:
        for path in (PROJECT_ROOT / directory).rglob("*.py"):
            rel = path.relative_to(PROJECT_ROOT).as_posix()
            if rel in EXEMPT:
                continue
            for lineno, text in _string_literals(path):
                if len(text) < 40:
                    continue
                lowered = text.lower()
                hits = [m for m in PROMPT_MARKERS if m in lowered]
                if hits:
                    offenders.append(f"{rel}:{lineno} contains {hits[0]!r}")

    assert not offenders, (
        "Prompt text found in code. Move it to prompts/ and load it with "
        "engine.prompts.render():\n  " + "\n  ".join(offenders)
    )


def test_every_prompt_parses():
    from engine.prompts import available, check_all

    assert available(), "no prompts discovered under prompts/"
    assert check_all() == [], "prompt files failed to parse"


def test_agent_prompts_declare_required_front_matter():
    from engine.prompts import meta

    for name in ("planner", "research", "writer", "qa",
                 "pdf_writer", "asset_planner", "asset_mapper"):
        front = meta(f"agents/{name}")
        for key in ("role", "goal", "expected_output", "agent_type"):
            assert front.get(key), f"agents/{name}.md front matter missing '{key}'"


def test_missing_parameter_raises_instead_of_leaking_a_brace():
    """A literal '{topic}' reaching the model is worse than a loud failure."""
    from engine.prompts import PromptError, render

    with pytest.raises(PromptError) as excinfo:
        render("tasks/topic_invention")          # both params omitted
    message = str(excinfo.value)
    assert "category" in message and "recent_topics" in message


def test_unknown_prompt_names_the_file():
    from engine.prompts import PromptError, render

    with pytest.raises(PromptError, match="not found"):
        render("tasks/does_not_exist")


def test_rendered_prompts_have_no_unfilled_placeholders():
    """Every task prompt renders cleanly when given its declared parameters."""
    from engine.prompts import available, render

    # Parameter names are discoverable from the template itself.
    placeholder = re.compile(r"(?<!\{)\{([a-z_][a-z0-9_]*)\}(?!\})")

    for name in available():
        body = (PROJECT_ROOT / "prompts" / f"{name}.md").read_text(encoding="utf-8-sig")
        params = {key: f"<{key}>" for key in set(placeholder.findall(body))}
        text = render(name, **params)
        leftover = placeholder.findall(text)
        assert not leftover, f"prompt '{name}' left {leftover} unfilled"


def test_json_shaped_prompts_escape_their_braces():
    """A JSON example with single braces would be eaten by str.format."""
    from engine.prompts import render

    text = render(
        "agents/asset_mapper",
        logo_light="logo.png", website="example.com", cta_default="Join",
    )
    assert '"number": "01"' in text, "JSON example was consumed by formatting"
    assert '"items": [' in text


def test_mapper_covers_every_content_key_the_templates_need():
    """The mapper schema must name every placeholder a template demands real
    text for. It did not name QUOTE/SUBTEXT/TAGLINE, so the motivation
    templates routinely rendered as blank cards."""
    import os

    from engine.prompts import render
    from engine.workflow import PlatformWorkflow

    wf = PlatformWorkflow.__new__(PlatformWorkflow)
    wf.config = type("C", (), {"PROJECT_ROOT": str(PROJECT_ROOT)})()

    schema = render(
        "agents/asset_mapper",
        logo_light="logo.png", website="example.com", cta_default="Join",
    )

    # Only the registry's archetypes are filled from mapper output. pdf.html is
    # a document skeleton whose pages services/pdf_document.py composes.
    import json as _json
    templates_dir = PROJECT_ROOT / "design_templates"
    registry = _json.loads((templates_dir / "registry.json").read_text(encoding="utf-8"))

    for entry in registry:
        if not entry.get("enabled", True):
            continue
        for key in wf._content_keys(wf._get_template_placeholders(entry["file"])):
            assert f'"{key}"' in schema, (
                f"{entry['id']} needs {key}, but the Asset Mapper schema never "
                f"asks the model to produce it"
            )
