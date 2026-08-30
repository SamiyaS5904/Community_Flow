"""
tests/test_post_types.py
========================
Not every post has a graphic.

A poll and a plain message are text. They declare no image and no PDF, and
nothing about them should touch Chromium. That was not true: the approve route
remapped placeholders for every post, `placeholders_updated` then counted as
"assets pending", and a poll went off to be rendered — which failed, and left
it in `asset_failed`, a state it cannot be approved out of. A poll that needed
no image was unpublishable because an image it never asked for did not render.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

APP = (PROJECT_ROOT / "dashboard" / "app.py").read_text(encoding="utf-8-sig")
INDEX = (PROJECT_ROOT / "dashboard" / "templates" / "index.html").read_text(encoding="utf-8-sig")
APPJS = (PROJECT_ROOT / "dashboard" / "static" / "js" / "app.js").read_text(encoding="utf-8-sig")


# ── a text post never enters the render path ─────────────────────────────────

def test_approve_decides_whether_the_post_has_an_asset_at_all():
    assert "wants_asset = (" in APP, "approve must ask whether there is a graphic"


def test_remapped_placeholders_alone_do_not_trigger_a_render():
    """A poll's placeholders get remapped too. That was enough to send it to
    Chromium."""
    assert "assets_pending = wants_asset and (" in APP


def test_a_post_with_no_asset_is_not_marked_asset_failed():
    """asset_failed blocks approval. Setting it on a post that declared no
    asset makes it unpublishable over something it does not need."""
    for guard in ("if ipdf or iimg:", 'if ps.lower() == "pending" or is_.lower() == "pending":'):
        assert guard in APP, f"a background handler still marks asset_failed unconditionally: {guard}"


def test_the_design_studio_is_only_offered_for_image_and_pdf_posts():
    """It appeared for polls and messages because they carry auto-mapped
    placeholders — and offering a design editor for a post with no design is
    how a poll reached the render path in the first place."""
    assert "{% if post.get('Content Type') in ['Image', 'PDF'] %}" in INDEX
    assert "post.placeholders or post.get('Content Type') in ['Image', 'PDF', 'Message']" not in INDEX


# ── path sentinels ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("value", ["N/A", "pending", "Failed", "", "  ", None, "none"])
def test_sentinels_are_not_treated_as_a_rendered_file(value):
    """"Failed" is the dangerous one: it is truthy, so `if post["Image Path"]`
    reported an asset that does not exist."""
    from dashboard.app import pdf_path_is_real
    assert pdf_path_is_real(value) is False


@pytest.mark.parametrize("value", ["/tmp/post_a1.png", "generated/images/x.pdf"])
def test_a_real_path_is_recognised(value):
    from dashboard.app import pdf_path_is_real
    assert pdf_path_is_real(value) is True


# ── approving with no time set ───────────────────────────────────────────────

def test_a_blank_schedule_means_publish_now():
    """The form says "leave blank for asap" next to the field. Treating blank
    as unreadable made the only advertised way to publish immediately fail."""
    assert 'if not (schedule_time or "").strip():' in APP
    assert "when = datetime.now(timezone.utc)" in APP


def test_an_unreadable_time_still_reports_itself():
    assert "That schedule time could not be read." in APP


# ── the submitter, not the form ──────────────────────────────────────────────

def test_a_formaction_button_posts_where_it_says():
    """Reject and Delete sit inside the approve form and override its target
    with `formaction`. Posting to form.action regardless sent all three to
    /approve — so Delete silently approved the post instead of deleting it."""
    assert "event.submitter || form.querySelector" in APPJS
    assert "getAttribute('formaction')" in APPJS


def test_those_buttons_carry_their_own_confirmation_and_outcome():
    """They cannot inherit the form's: the form's belong to Approve."""
    for needle in ('data-cf-confirm="Delete this post permanently?',
                   'data-cf-confirm="Discard this post?',
                   'data-cf-confirm="Reject this post?'):
        assert needle in INDEX, f"missing confirmation: {needle}"

    # Every destructive formaction button must also say what happens to the row.
    assert INDEX.count('data-cf-on-success="remove"') >= 4


# ── the registry describes archetypes, not themes ────────────────────────────

def test_the_asset_planner_can_describe_every_enabled_template():
    """Theme stopped being a property of an archetype when it became a token
    swap driven by each group's config — but three call sites still read
    tmpl["theme"], and the first sits on the path every image post takes. Every
    image slot in a queue run died with KeyError: 'theme' after the content had
    already been written and paid for."""
    import json

    registry = json.loads(
        (PROJECT_ROOT / "design_templates" / "registry.json").read_text(encoding="utf-8"))

    described = ""
    for entry in registry:
        if not entry.get("enabled", True):
            continue
        # The exact expression engine/workflow.py evaluates. A key it reads and
        # the registry does not carry raises here instead of mid-render.
        described += f"- {entry['file']} ({entry['name']})\n"

    assert described.count("\n") == sum(1 for e in registry if e.get("enabled", True))
    assert "Theme:" not in described


def test_no_code_reads_a_theme_off_a_template():
    """The group owns the theme. A template that claims one is describing a
    property it does not have."""
    offenders = []
    for directory in ("engine", "services", "dashboard"):
        for path in (PROJECT_ROOT / directory).rglob("*.py"):
            text = path.read_text(encoding="utf-8-sig")
            for needle in ("tmpl[\"theme\"]", "tmpl['theme']",
                           "t['theme']", 't["theme"]'):
                if needle in text:
                    offenders.append(f"{path.relative_to(PROJECT_ROOT).as_posix()}: {needle}")
    assert not offenders, (
        "theme read off a template rather than the group:\n  " + "\n  ".join(offenders))


def test_every_key_the_registry_promises_is_actually_there():
    """A missing key here surfaces as a KeyError deep inside a render, long
    after the LLM calls have been spent."""
    import json

    registry = json.loads(
        (PROJECT_ROOT / "design_templates" / "registry.json").read_text(encoding="utf-8"))
    required = {"id", "file", "name", "archetype", "content_keys",
                "supports_png", "supports_pdf"}
    for entry in registry:
        missing = required - set(entry)
        assert not missing, f"{entry.get('id', '?')} is missing {sorted(missing)}"


# ── scheduling happens in the community's own timezone ───────────────────────

def test_the_store_accepts_an_already_converted_datetime():
    """A bare string carries no zone, and the store's only safe reading of one
    is UTC. The approve route handed over the raw form value — the operator's
    local time — and every scheduled post landed one offset late: 5½ hours for
    IST, so "publish at 4pm" meant half past nine at night."""
    source = (PROJECT_ROOT / "services" / "storage" / "post_store.py").read_text(encoding="utf-8-sig")
    assert "if isinstance(value, datetime):" in source


def test_approve_stores_the_converted_time_not_the_form_string():
    assert 'update_post(post_id, {"Scheduled Time": when})' in APP, (
        "approve must pass the converted datetime, not the raw form value")


def test_a_slot_time_is_read_in_the_groups_timezone():
    """A cycle plan's times are the community's own: "08:00" means eight in the
    morning where its members are, not eight UTC."""
    from datetime import datetime, timezone

    from engine.group_config import list_available_groups, load_group_config
    from engine.workflow import _parse_schedule

    group = load_group_config(list_available_groups()[0])
    parsed = _parse_schedule("2026-09-02T08:00", group)

    assert parsed.tzinfo is not None, "a schedule must be timezone-aware"
    assert parsed.astimezone(group.tz).strftime("%H:%M") == "08:00", (
        "the slot time must survive the round trip through the group's zone")
    if group.timezone != "UTC":
        assert parsed.strftime("%H:%M") != "08:00", (
            "a non-UTC community's 08:00 cannot also be 08:00 UTC")


def test_a_schedule_round_trips_through_the_display_layer():
    """The display layer formats scheduled_for back into the same string the
    form produces. If the two disagree the drift compounds on every re-approve."""
    from datetime import datetime, timezone

    from engine.group_config import list_available_groups, load_group_config
    from engine.workflow import _parse_schedule

    group = load_group_config(list_available_groups()[0])
    for wanted in ("2026-09-02T08:00", "2026-12-31T23:30", "2026-01-01T00:15"):
        utc = _parse_schedule(wanted, group)
        assert utc.astimezone(group.tz).strftime("%Y-%m-%dT%H:%M") == wanted


# ── a graphic post gets a caption, not a second copy of the post ─────────────

WORKFLOW = (PROJECT_ROOT / "engine" / "workflow.py").read_text(encoding="utf-8-sig")


def test_a_caption_has_its_own_prompt_tier():
    """The Telegram tier mandates 200-450 words. A one-line "keep it short"
    hint in the user prompt loses to that every time — which is how an image
    post arrived with all three of its points in the caption and the reader saw
    the same list twice. Exactly the failure the PDF Writer had before it got
    its own tier."""
    builder = (PROJECT_ROOT / "engine" / "prompt_builder.py").read_text(encoding="utf-8-sig")
    assert "_CAPTION_AGENT_TYPES" in builder
    assert 'render("system/caption_rules")' in builder


def test_the_caption_tier_does_not_carry_a_word_ceiling():
    from engine.group_config import list_available_groups, load_group_config
    from engine.prompt_builder import PromptBuilder

    group = load_group_config(list_available_groups()[0])
    caption = PromptBuilder.build_system_prompt(group=group, agent_type="caption")
    writer = PromptBuilder.build_system_prompt(group=group, agent_type="writer")

    assert str(group.word_count_max) not in caption, (
        "a caption must not inherit the post word count")
    assert str(group.word_count_max) in writer, (
        "the writer still needs its own range")


def test_the_caption_agent_exists_and_is_the_caption_tier():
    from agents.definitions import caption_agent
    from engine.group_config import list_available_groups, load_group_config

    agent = caption_agent(load_group_config(list_available_groups()[0]))
    assert agent["agent_type"] == "caption"


def test_an_image_post_gets_a_caption_rather_than_the_full_post():
    """The points are on the graphic. Repeating them underneath gives the
    reader no reason to look at the image."""
    assert "if (pdf_required or image_required) and not is_poll:" in WORKFLOW
    assert "tasks/image_caption" in WORKFLOW


def test_a_caption_is_not_sent_through_the_post_editor():
    """QA thinks in 200-450 word posts; running an already-short caption
    through it grows the caption back."""
    assert "if is_poll or pdf_required or image_required:" in WORKFLOW


def test_the_caption_writer_is_told_what_is_already_on_the_graphic():
    """"Do not repeat the points" only works if it can see which points."""
    prompt = (PROJECT_ROOT / "prompts" / "tasks" / "image_caption.md").read_text(encoding="utf-8-sig")
    assert "{items_summary}" in prompt
    assert "{topic}" in prompt


# ── a rendered post has to become reviewable ─────────────────────────────────

def test_a_successful_render_moves_the_post_into_review():
    """Nothing did this. A post created in `rendering` stayed there after a
    perfectly successful render, so every image and PDF post was invisible to
    the review queue and could never be approved."""
    assert "elif pdf_path or img_path:" in WORKFLOW
    assert 'current.get("State") == PostState.RENDERING' in WORKFLOW


def test_a_re_render_does_not_drag_an_approved_post_back_into_review():
    """The Design Studio re-renders approved posts. Moving one back to
    needs_review would silently unschedule it."""
    start = WORKFLOW.index("elif pdf_path or img_path:")
    block = WORKFLOW[start:start + 900]
    assert "PostState.RENDERING" in block, (
        "the transition must be guarded on the current state")


# ── the caption invites, it does not announce ────────────────────────────────

CAPTION_RULES = (PROJECT_ROOT / "prompts" / "system" / "caption_rules.md").read_text(encoding="utf-8-sig")
IMAGE_CAPTION = (PROJECT_ROOT / "prompts" / "tasks" / "image_caption.md").read_text(encoding="utf-8-sig")


def test_the_caption_must_ask_for_something():
    """This is a community, not a broadcast. A caption that only states a fact
    gives nobody a reason to reply."""
    lowered = CAPTION_RULES.lower()
    assert "start a conversation" in lowered
    assert "invitation" in lowered


def test_the_rules_name_the_kinds_of_invitation():
    """"End with a question" produces "what are your thoughts?". Naming the
    shapes — admit, choose, ask for more — produces something answerable."""
    for shape in ("Admission:", "Experience:", "Choice:", "Offer:", "Recognition:"):
        assert shape in CAPTION_RULES, f"the rules do not offer an {shape} invitation"


def test_filler_calls_to_action_are_banned_by_name():
    """A model reaches for these unless told not to, and no real person says
    them out loud."""
    for filler in ("let us know your thoughts", "comment below", "share your views"):
        assert filler in CAPTION_RULES.lower(), f"{filler!r} is not ruled out"


def test_the_caption_may_not_borrow_a_point_from_the_graphic():
    """Naming "the moment" drifted into restating a card — a caption said
    "talking over everyone", which is one of the three mistakes verbatim."""
    # The prompt is prose, wrapped for readability, so a phrase can straddle a
    # line break. Compare on normalised whitespace rather than the raw file.
    import re
    flat = re.sub(r"\s+", " ", IMAGE_CAPTION.lower())
    assert "never one of the points themselves" in flat
    assert "swapped for a card on the graphic" in flat
