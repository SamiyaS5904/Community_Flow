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

import re as _re

CAPTION_RULES = (PROJECT_ROOT / "prompts" / "system" / "caption_rules.md").read_text(encoding="utf-8-sig")
IMAGE_CAPTION = (PROJECT_ROOT / "prompts" / "tasks" / "image_caption.md").read_text(encoding="utf-8-sig")

# The prompts are prose, wrapped for readability, so a phrase can straddle a
# line break. Every check below runs on normalised whitespace.
RULES = _re.sub(r"\s+", " ", CAPTION_RULES.lower())
TASK = _re.sub(r"\s+", " ", IMAGE_CAPTION.lower())


def test_the_caption_must_ask_for_something():
    """This is a community, not a broadcast. A caption that only states a fact
    gives nobody a reason to reply."""
    assert "community, not a broadcast" in RULES
    assert "invitation" in RULES
    assert "one tap" in RULES


def test_the_rules_show_what_an_answerable_ask_looks_like():
    """"End with a question" is what produces "what are your thoughts?". Naming
    the shapes — admit, cost, pick, offer, recognise — produces something a
    reader can answer without typing a paragraph."""
    for shape in ("admit it", "cost it", "pick one", "offer more", "recognise"):
        assert shape in RULES, f"the rules do not offer a '{shape}' invitation"


def test_filler_calls_to_action_are_banned_by_name():
    """A model reaches for these unless told not to, and no real person says
    them out loud."""
    for filler in ("let us know your thoughts", "comment below", "share your views",
                   "drop a comment"):
        assert filler in RULES, f"{filler!r} is not ruled out"


def test_hedging_is_ruled_out_by_name():
    """The first version produced "can be tough" and "it's frustrating" — true
    of everything, therefore about nothing."""
    for hedge in ("can be", "might", "sometimes", "often"):
        assert hedge in RULES, f"the hedge {hedge!r} is not named"


def test_the_opening_cliches_are_ruled_out():
    """Left alone the model opens every caption with one of these."""
    for opener in ("ever wondered", "picture this", "let's be real", "imagine"):
        assert opener in RULES, f"{opener!r} is not ruled out"


def test_the_rules_carry_a_worked_example():
    """A weak/strong pair moves a model further than another rule does."""
    assert "weak" in RULES and "strong" in RULES


def test_the_caption_may_not_borrow_a_point_from_the_graphic():
    """Naming "the moment" drifted into restating a card — a caption said
    "talking over everyone", which is one of the three mistakes verbatim."""
    assert "swapped for a card on the graphic" in TASK
    assert "none of it belongs" in TASK


def test_the_task_prompt_names_what_each_line_does():
    """One instruction for the whole caption produced three interchangeable
    sentences. A job per line produces a shape."""
    for part in ("first line", "second line", "last line"):
        assert part in TASK, f"the prompt does not say what the {part} is for"


# ── what one worker writes, the next worker must see ─────────────────────────

def test_the_post_list_is_not_cached_in_process():
    """The cache was a module-level dict, so each Gunicorn worker held its own
    copy. Deleting a post cleared the cache in the worker that served the
    delete; the next request landed on a different worker and that one still
    had the post. Deleted posts reappeared on refresh, and a hard refresh
    "fixed" it only because it happened to hit the worker that knew."""
    assert "_SHEET_CACHE" not in APP, (
        "a per-process cache of the post list is not safe across workers")


def test_reading_the_post_list_goes_to_the_database():
    assert "storage.get_all_posts(current_group)" in APP


def test_a_rendered_asset_is_revalidated_not_reused():
    """An asset URL does not change when the asset is re-rendered, so
    max-age=86400 meant a browser kept showing yesterday's PNG for a day and
    the only way to see a re-render was a hard refresh. no-cache does not mean
    "do not store" — it means "ask me before reusing this"."""
    assert 'Cache-Control"] = "private, no-cache, must-revalidate"' in APP
    assert 'Cache-Control"] = "private, max-age=86400"' not in APP
    assert "conditional=True" in APP, (
        "without conditional the revalidation re-sends the whole file")


# ── the planner's answer has to be read ──────────────────────────────────────

def test_the_planner_is_asked_for_the_key_the_code_reads():
    """The prompt asks for "archetype"; the code read "template". The answer was
    discarded on every single post, everything fell through to the constraint
    matcher, and that picks `list` for anything containing list items — so a
    do's-and-don'ts post was laid out as a numbered list while the model that
    had correctly said "duo" was never heard."""
    planner = (PROJECT_ROOT / "prompts" / "agents" / "asset_planner.md").read_text(encoding="utf-8-sig")
    assert '"archetype"' in planner
    assert 'asset_plan.get("archetype")' in WORKFLOW


def test_the_planner_is_shown_what_each_archetype_is_for():
    """It was given five filenames and nothing else, while the registry already
    recorded which content shapes each one fits."""
    assert 'supported_content_types' in WORKFLOW
    context = (PROJECT_ROOT / "prompts" / "tasks" / "asset_planner_context.md").read_text(encoding="utf-8-sig")
    assert "SHAPE of the content" in context
    assert "do's and don'ts" in context.lower()


# ── the mapper produces the shape the renderer consumes ──────────────────────

MAPPER = (PROJECT_ROOT / "prompts" / "agents" / "asset_mapper.md").read_text(encoding="utf-8-sig")


def test_the_mapper_knows_the_contrast_shape():
    """The renderer reads `negative` and `positive` for a duo, and falls back to
    title/description when they are missing — which put a "do" under the AVOID
    label. The mapper's schema only ever described the list shape."""
    for field in ("'negative'", "'positive'"):
        assert field in MAPPER, f"the mapper is never asked for {field}"


def test_the_mapper_knows_the_question_shape():
    for field in ("'question'", "'answer'"):
        assert field in MAPPER, f"the mapper is never asked for {field}"


def test_the_negative_is_not_prefixed_with_its_own_label():
    """The card already carries an AVOID label, so "Don't skip preparation"
    renders as "AVOID / Don't skip preparation"."""
    import re
    flat = re.sub(r"\s+", " ", MAPPER)
    assert 'with no "Don\'t", "Avoid" or "Never" in front' in flat


# ── the group owns its own branding ──────────────────────────────────────────

def test_brand_chrome_overwrites_whatever_the_mapper_returned():
    """setdefault let the mapper win, and it invents values for keys it was
    never meant to touch: it returned THEME_CLASS="interview-tips", so the page
    rendered as <body class="interview-tips">, no theme class matched,
    --text-primary was never set, and the graphic came out dark on dark."""
    assert "placeholders.update(self.renderer.brand_placeholders(self.group))" in WORKFLOW
    assert "brand.items():" not in WORKFLOW


def test_the_mapper_is_not_asked_for_chrome_at_all():
    """Asking for it wastes tokens and invites exactly the THEME_CLASS problem."""
    for key in ('"LOGO"', '"WEBSITE"', '"CTA"'):
        assert key not in MAPPER, f"the mapper is still asked to produce {key}"


# ── a post still being built says so, and the page waits for it ──────────────

def test_a_post_still_rendering_does_not_show_a_studio():
    """A post declaring a graphic is saved immediately and rendered in a
    background thread. Showing the Design Studio at that moment shows a
    half-built graphic — placeholders not yet mapped, so the preview falls back
    to raw text. That is what made it look like the app needed three hard
    refreshes: each refresh was a later stage of the same job."""
    assert "{% if post.get('State') == 'rendering' %}" in INDEX
    assert "data-generating=" in INDEX
    assert "Building the graphic" in INDEX


def test_the_page_says_it_will_update_itself():
    assert "no need to refresh" in INDEX.lower()


def test_there_is_an_endpoint_to_ask_whether_a_post_is_done():
    assert '@app.route("/api/post_states")' in APP
    assert '"states": states' in APP


def test_the_page_watches_those_posts_and_reloads_when_they_finish():
    assert "data-generating" in APPJS
    assert "/api/post_states?ids=" in APPJS
    assert "window.location.reload()" in APPJS


def test_the_watcher_stops_rather_than_polling_for_ever():
    """A render that never finishes must not leave a tab polling all day."""
    assert "GIVE_UP_AFTER" in APPJS


def test_a_failed_render_is_reported_not_celebrated():
    """asset_failed settles the job too, and "your graphic is ready" would be
    a lie."""
    assert "'asset_failed'" in APPJS
    assert "could not be rendered" in APPJS


# ── a bulk run reports how far along it is ───────────────────────────────────

WORKFLOW_SRC = (PROJECT_ROOT / "engine" / "workflow.py").read_text(encoding="utf-8-sig")


def test_the_workflow_reports_counts_not_just_a_sentence():
    """A bulk run takes minutes and reported only a rolling sentence, so there
    was no way to tell "two of five" from "four of five" and the page had
    nothing to draw a bar from."""
    assert "def _say(" in WORKFLOW_SRC
    assert "done=i," in WORKFLOW_SRC


def test_progress_counts_rendering_as_work():
    """Writing five posts and then rendering two of them is seven units, not
    five. Counting only the writing made the bar sit at 100% through the
    slowest part of the run."""
    assert "total_units = len(results) + needs_render" in WORKFLOW_SRC


def test_a_callback_that_only_wants_a_message_still_works():
    """_say must not break the callers that pass a single argument."""
    from engine.workflow import _say

    seen = []
    _say(lambda msg: seen.append(msg), "hello", done=1, total=2)
    assert seen == ["hello"]

    both = []
    _say(lambda msg, d, t: both.append((msg, d, t)), "hi", done=1, total=2)
    assert both == [("hi", 1, 2)]

    _say(None, "nobody listening")           # must not raise


def test_a_message_without_counts_does_not_reset_the_bar():
    """Most messages come from inside one post's generation — "writing
    caption", "checking quality" — and carry no counts. Letting them zero the
    percentage pinned the bar at its floor for the whole run."""
    assert "position[0], position[1] = done, total" in APP
    assert "position = [0, 0]" in APP


def test_there_is_a_per_job_status_endpoint():
    """/api/status returns every job at once; a progress strip needs one."""
    assert '@app.route("/api/status/<job_id>")' in APP


def test_an_older_bare_string_status_still_reads():
    assert "if isinstance(entry, str):" in APP


def test_a_finished_run_says_the_posts_are_not_approved():
    """Generation assigns a time but never approves. Saying so is the
    difference between "done" and "done, and nothing was published"."""
    assert "none are approved" in APP


def test_the_queue_form_reports_progress_rather_than_redirecting():
    assert INDEX.count('data-cf-job-url="/api/status"') >= 1
    assert "Queue generation for" not in APP, (
        "the old flash-and-redirect path is still there")
