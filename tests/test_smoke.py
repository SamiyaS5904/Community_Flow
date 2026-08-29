"""
tests/test_smoke.py
===================
The safety net for restructuring work: fast, offline, no live API keys.

These tests do not check that the pipeline produces *good* content — they check
that the application still assembles. That is exactly what is at risk while
files are being moved between packages, prompts are being pulled out of code,
and dashboard/app.py is being split up.

Run:  pytest -q
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# The dashboard refuses to import without a secret key, and engine.config reads
# .env at import time. Set placeholders before anything imports them so the
# suite runs on a machine with no .env at all.
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret-not-used-for-anything")
os.environ.setdefault("DASHBOARD_PASSWORD", "test-password")


# ── configuration and tenants ────────────────────────────────────────────────

def test_group_configs_load():
    """Every groups/<id>/config.yaml parses into a usable GroupConfig."""
    from engine.group_config import list_available_groups, load_group_config

    groups = list_available_groups()
    assert groups, "no groups discovered under groups/"

    for gid in groups:
        g = load_group_config(gid)
        assert g.id == gid
        assert g.name, f"{gid} has no name"
        assert g.word_count_min < g.word_count_max, f"{gid} word counts are inverted"


def test_group_ids_are_unique():
    """The group id is the tenant discriminator on every row. Two groups
    sharing one would silently merge their posts."""
    from engine.group_config import list_available_groups

    ids = list_available_groups()
    assert len(ids) == len(set(ids)), f"duplicate group ids: {ids}"


# ── agents ───────────────────────────────────────────────────────────────────

AGENT_NAMES = [
    "planner_agent", "research_agent", "writer_agent", "qa_agent",
    "pdf_writer_agent", "asset_planner_agent", "asset_mapper_agent",
]


@pytest.mark.parametrize("agent_name", AGENT_NAMES)
def test_agent_definition_is_complete(agent_name):
    """Every agent builds for every tenant and declares the keys callers read."""
    import agents.definitions as defs
    from engine.group_config import list_available_groups, load_group_config

    factory = getattr(defs, agent_name)
    for gid in list_available_groups():
        agent = factory(load_group_config(gid))
        for key in ("role", "goal", "instructions", "expected_output", "agent_type"):
            assert agent.get(key), f"{agent_name} for {gid} is missing '{key}'"


def test_prompt_builder_tiers():
    """Structural agents must not receive the brand-voice block."""
    from engine.group_config import list_available_groups, load_group_config
    from engine.prompt_builder import PromptBuilder

    group = load_group_config(list_available_groups()[0])

    writer = PromptBuilder.build_system_prompt(group=group, agent_type="writer")
    planner = PromptBuilder.build_system_prompt(group=group, agent_type="planner")

    assert group.audience_description in writer
    assert group.audience_description not in planner
    assert len(planner) < len(writer)


# ── design templates ─────────────────────────────────────────────────────────

def test_registry_templates_exist():
    """Every enabled registry entry points at a file the renderer can read."""
    import json

    templates_dir = PROJECT_ROOT / "design_templates"
    registry = json.loads((templates_dir / "registry.json").read_text(encoding="utf-8"))

    for entry in registry:
        if not entry.get("enabled", True):
            continue
        assert (templates_dir / entry["file"]).exists(), \
            f"registry entry '{entry['id']}' points at missing {entry['file']}"


def test_renders_do_not_reach_the_network():
    """A render that waits on a remote asset is slow and fails offline."""
    templates_dir = PROJECT_ROOT / "design_templates"
    offenders = []
    for path in list(templates_dir.rglob("*.html")) + list(templates_dir.rglob("*.css")):
        text = path.read_text(encoding="utf-8", errors="replace")
        # Strip HTML comments: the card library is reference material, not markup.
        import re
        text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
        if "http://" in text or "https://" in text:
            offenders.append(path.name)
    assert not offenders, f"remote references in render assets: {offenders}"


def test_self_hosted_fonts_present():
    fonts_dir = PROJECT_ROOT / "design_templates" / "fonts"
    assert (PROJECT_ROOT / "design_templates" / "fonts.css").exists()
    assert list(fonts_dir.glob("*.woff2")), "no self-hosted font files"


# ── renderer (no browser launched) ───────────────────────────────────────────

def test_build_html_substitutes_every_placeholder():
    """An unresolved {{KEY}} in the output is a visible defect in the asset."""
    import re

    from services.render_service import RenderService

    templates_dir = PROJECT_ROOT / "design_templates"
    renderer = RenderService(
        base_output_dir=str(PROJECT_ROOT / "generated" / "test"),
        templates_dir=str(templates_dir),
    )
    placeholders = {
        "CATEGORY": "TEST", "HOOK": "HOOK", "TITLE": "Title", "SUBTITLE": "Sub",
        "QUOTE": "Quote", "SUBTEXT": "Subtext", "TAGLINE": "Tag",
        "WEBSITE": "example.com", "CTA": "Join", "PAGE": "1", "SOURCE": "",
        "items": [{"number": "01", "title": "One", "description": "Desc",
                   "example": "An example."}],
    }
    html = renderer.build_html("archetypes/list.html", placeholders)
    leftover = re.findall(r"\{\{([A-Z0-9_]+)\}\}", re.sub(r"<!--.*?-->", "", html, flags=re.DOTALL))
    assert not leftover, f"unsubstituted placeholders: {leftover}"


def test_item_example_is_rendered():
    """The example line is the whole point of the per-item schema addition."""
    from services.render_service import RenderService

    renderer = RenderService(
        base_output_dir=str(PROJECT_ROOT / "generated" / "test"),
        templates_dir=str(PROJECT_ROOT / "design_templates"),
    )
    html = renderer.build_html("archetypes/list.html", {
        "items": [{"number": "01", "title": "T", "description": "D",
                   "example": "UNIQUE-EXAMPLE-STRING"}],
    })
    assert "UNIQUE-EXAMPLE-STRING" in html
    assert "card-example" in html


def test_render_cache_key_tracks_the_stylesheet():
    """Editing styles.css must invalidate cached renders, not serve stale ones."""
    from services.render_service import RenderService

    renderer = RenderService(
        base_output_dir=str(PROJECT_ROOT / "generated" / "test"),
        templates_dir=str(PROJECT_ROOT / "design_templates"),
    )
    digest = renderer._stylesheet_digest()
    assert "styles.css:" in digest and "fonts.css:" in digest


# ── dashboard ────────────────────────────────────────────────────────────────

def test_dashboard_imports_and_routes_resolve():
    """Catches broken imports and url_for targets after a file move."""
    from dashboard.app import app

    rules = {r.endpoint for r in app.url_map.iter_rules()}
    for endpoint in ("index", "login", "logout", "approve", "reject",
                     "delete_post", "serve_output", "create_manual"):
        assert endpoint in rules, f"route '{endpoint}' disappeared"


def test_login_is_required_for_the_dashboard():
    from dashboard.app import app

    app.config["TESTING"] = True
    with app.test_client() as client:
        response = client.get("/")
        assert response.status_code in (301, 302)
        assert "/login" in response.headers.get("Location", "")


def test_templates_render_without_a_session():
    """The login page must not depend on request-scoped tenant state."""
    from dashboard.app import app

    app.config["TESTING"] = True
    with app.test_client() as client:
        assert client.get("/login").status_code == 200


# ── P0 regression guards ─────────────────────────────────────────────────────

def test_get_workflow_refuses_to_guess_a_tenant():
    """Outside a request there is no session; defaulting sends posts to the
    wrong community's chat, so it must raise instead."""
    import pytest as _pytest
    from dashboard.app import get_workflow

    with _pytest.raises(RuntimeError, match="group_id"):
        get_workflow()


def test_queue_results_carry_their_asset_flags():
    """generate_queue reads these off the result, not the raw slot.

    Slots in a strategy file only carry content_type; the *_required flags are
    derived by enrich_slot inside generate_single_content. Reading them back
    off the slot is why queue generation produced no images at all."""
    import inspect
    from engine import workflow as wf_mod

    source = inspect.getsource(wf_mod.PlatformWorkflow.generate_queue)
    assert 'r["wants_pdf"]' in source and 'r["wants_image"]' in source
    assert 'slot.get("pdf_required"' not in source, \
        "generate_queue is reading asset flags off the raw slot again"


def test_publish_resolves_the_chat_from_the_post():
    import inspect
    from engine import workflow as wf_mod

    source = inspect.getsource(wf_mod.PlatformWorkflow.publish_post)
    assert "_chat_id_for" in source
    assert "self.config.TELEGRAM_CHAT_ID" not in source, \
        "publish_post is using the ambient chat id again"


def test_save_group_reads_the_audience_it_interpolates():
    """The generated config.yaml interpolates {audience}; not reading it from
    the form raised NameError on every submission."""
    import inspect
    from dashboard import app as app_mod

    source = inspect.getsource(app_mod.save_group)
    assert 'request.form.get("audience"' in source


def test_health_does_not_block_startup_on_optional_services():
    """Sheets is legacy and Serper is optional; treating them as fatal is why
    `python run.py` refused to boot."""
    import inspect
    from engine import health

    source = inspect.getsource(health.run_health_checks)
    assert 'issues.append("Google Sheets' not in source
    assert "warnings" in source


def test_post_state_machine_gates_approval_on_assets():
    from services.storage.models import Post, PostState

    assert PostState.NEEDS_REVIEW in PostState.OPEN
    waiting = Post(group_id="g", wants_image=True, image_path=None)
    assert not waiting.assets_ready
    ready = Post(group_id="g", wants_image=True, image_path="/tmp/a.png")
    assert ready.assets_ready
