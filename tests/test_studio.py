"""
tests/test_studio.py
====================
The Design Studio's contract with the renderer.

The studio's whole value is that what you see is what gets exported. Three
separate bugs broke that, each silently: the style controls wrote a token onto
the wrong element, the live preview turned an image source into markup, and the
fit cascade only ran on export. These pin all three.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from services.render_service import FIT_JS, RenderService  # noqa: E402


# ── style overrides ──────────────────────────────────────────────────────────

def test_overrides_target_body_as_well_as_root():
    """The theme classes declare these same tokens on <body>. A custom property
    is inherited from the nearest ancestor that declares it, so a value set only
    on <html> is shadowed for everything inside the page — and !important does
    not help, because the conflict is between two elements rather than within
    one. Every colour and font control was inert until this changed."""
    css = RenderService.override_css({"accentColor": "#3B82F6"})
    assert ":root, body" in css, "an override scoped to :root alone never reaches the page"


def test_every_control_maps_to_a_token_the_templates_actually_use():
    """The controls previously wrote rules for .motivation-text and
    .editorial-title — classes belonging to templates deleted two commits
    earlier. Overriding tokens instead is what makes one set of controls work
    across every archetype."""
    stylesheet = (PROJECT_ROOT / "design_templates" / "styles.css").read_text(encoding="utf-8")
    for control, (token, _unit) in RenderService.OVERRIDE_TOKENS.items():
        assert f"{token}:" in stylesheet, (
            f"the '{control}' control writes {token}, which styles.css never defines"
        )


def test_size_controls_carry_their_unit():
    css = RenderService.override_css({"titleSize": 72})
    assert "--text-display: 72px" in css, "a unitless length is ignored by the browser"


def test_font_controls_resolve_to_a_self_hosted_stack():
    """A free-text font name would render as a silent fallback: renders must not
    touch the network, so only the families this project ships can be used."""
    css = RenderService.override_css({"headingFont": "editorial"})
    assert "Fraunces" in css
    for stack in RenderService.FONT_CHOICES.values():
        family = stack.split(",")[0].strip("'\" ")
        matches = list((PROJECT_ROOT / "design_templates" / "fonts").glob(f"{family.replace(' ', '')}*"))
        assert matches, f"{family} is offered as a choice but is not self-hosted"


def test_no_overrides_emits_no_style_block():
    assert RenderService.override_css({}) == ""
    assert RenderService.override_css(None) == ""


def test_camel_and_snake_case_both_work():
    """The browser sends camelCase; a stored asset document has snake_case."""
    assert "--accent" in RenderService.override_css({"accent_color": "#fff"})
    assert "--accent" in RenderService.override_css({"accentColor": "#fff"})


# ── the live preview ─────────────────────────────────────────────────────────

@pytest.fixture
def renderer(tmp_path):
    return RenderService(base_output_dir=str(tmp_path),
                         templates_dir=str(PROJECT_ROOT / "design_templates"))


def test_an_attribute_placeholder_is_not_wrapped_in_markup(renderer):
    """The preview wraps values in a <span> so the editor can address them,
    which turned src="{{LOGO}}" into src="<span …>logo.png</span>" — the logo
    was a broken image in every preview."""
    html = renderer.build_html(
        "archetypes/list.html",
        {"LOGO": "logo_light.png", "TITLE": "A title", "items": []},
        is_live_preview=True,
    )
    assert 'src="logo_light.png"' in html
    assert 'src="<span' not in html


def test_text_placeholders_do_get_a_handle(renderer):
    html = renderer.build_html(
        "archetypes/list.html", {"TITLE": "A title", "items": []}, is_live_preview=True)
    assert '<span data-field="TITLE">A title</span>' in html


def test_markup_placeholders_get_a_handle_that_is_not_a_layout_box(renderer):
    """Per-item edits go through ITEMS_HTML, so it needs an element to target —
    but a plain span between the flex container and its cards would become a
    layout box and collapse the gaps."""
    html = renderer.build_html(
        "archetypes/list.html",
        {"TITLE": "t", "items": [{"number": "01", "title": "a", "description": "b"}]},
        is_live_preview=True,
    )
    assert 'data-field="ITEMS_HTML"' in html
    assert "display: contents" in html


def test_a_static_render_has_no_editor_scaffolding(renderer):
    html = renderer.build_html(
        "archetypes/list.html", {"TITLE": "A title", "items": []}, is_live_preview=False)
    assert "data-field" not in html
    assert "UPDATE_FIELD" not in html


# ── the fit cascade ──────────────────────────────────────────────────────────

def test_the_fit_cascade_is_shared_by_the_preview_and_the_exporter():
    """It used to live inside the render call only, so the studio showed the
    raw un-fitted layout — the last card sitting on the footer — while the
    exported file was fine."""
    source = (PROJECT_ROOT / "services" / "render_service.py").read_text(encoding="utf-8-sig")
    # One definition, used in both paths.
    assert source.count("function cfFit()") == 1
    assert "return cfFit(); }" in source, "the exporter does not run the shared cascade"
    assert "FIT_JS}</script>" in source, "the live preview does not include it"


def test_the_cascade_gives_up_as_little_as_possible():
    """Spacing, then type size, then scale. Going straight to the type floor
    for a 27px overflow shrinks the text AND leaves the page half empty."""
    assert FIT_JS.index("is-dense") < FIT_JS.index("is-compact") < FIT_JS.index("scale(")


def test_the_cascade_measures_content_not_page():
    """.content is a `flex: 1` item, so its children spill out of it rather
    than stretching .page — which kept reporting scrollHeight 1350 while the
    last card was already on top of the footer."""
    assert "content.scrollHeight > content.clientHeight" in FIT_JS


def test_the_scale_floor_is_a_floor_not_a_fixed_value():
    """A fixed 0.92 silently failed to close larger overflows."""
    assert "have / need" in FIT_JS and "Math.max(0.75" in FIT_JS


def test_the_preview_fits_after_load_not_before():
    """DOMContentLoaded is too early: nothing has been laid out with the real
    faces yet, so fonts.ready resolves against an empty pending set, the
    fallback metrics are narrower, the page 'fits', and no tier is applied —
    then the real faces load and it overflows with nothing to catch it."""
    source = (PROJECT_ROOT / "services" / "render_service.py").read_text(encoding="utf-8-sig")
    assert "window.addEventListener('load', cfFitWhenReady)" in source
    assert "'loadingdone', cfFitWhenReady" in source
    assert "DOMContentLoaded', cfFitWhenReady" not in source


# ── the editor's markup must match the renderer's ────────────────────────────

def test_the_studio_builds_the_same_card_markup_as_the_renderer():
    """Any difference between them shows up as a preview that does not look
    like the exported file, which is the one thing this panel exists to
    prevent."""
    studio = (PROJECT_ROOT / "dashboard" / "static" / "js" / "studio.js").read_text(encoding="utf-8-sig")
    renderer = (PROJECT_ROOT / "services" / "render_service.py").read_text(encoding="utf-8-sig")

    for fragment in ('card point-card', 'point-number', 'card-body', 'card-text',
                     'card-example', 'example-label', 'card tip-card card-row'):
        assert fragment in studio, f"the studio never emits {fragment!r}"
        assert fragment in renderer, f"the renderer never emits {fragment!r}"


def test_the_studio_renders_the_tip_because_the_renderer_puts_it_in_items():
    """TIP is appended to ITEMS_HTML rather than being a placeholder of its
    own, so rebuilding the items without it made the Pro Tip card vanish from
    the preview while the export still had it."""
    studio = (PROJECT_ROOT / "dashboard" / "static" / "js" / "studio.js").read_text(encoding="utf-8-sig")
    assert "Pro Tip" in studio
    assert 'data-field="TIP"' in studio


# ── state, shown once and shown honestly ─────────────────────────────────────

def test_the_history_table_reads_the_state_column():
    """It used to reconstruct a status from the Approval Status / Publish
    Status pair through nine if-branches, which could not express asset_failed,
    publishing or rejected at all — so those three showed as "Unknown" on the
    one screen you go to when you want to know what went wrong."""
    index = (PROJECT_ROOT / "dashboard" / "templates" / "index.html").read_text(encoding="utf-8-sig")
    assert "state_chip(post.get('State'))" in index
    assert "badge bg-success\">Published" not in index


def test_every_post_state_has_a_chip_and_a_word_for_it():
    """A state with no entry falls through to the raw enum name, which is not
    something to show an operator."""
    from services.storage.models import PostState

    chip = (PROJECT_ROOT / "dashboard" / "templates" / "partials" / "chip.html").read_text(encoding="utf-8-sig")
    css = (PROJECT_ROOT / "dashboard" / "static" / "css" / "app.css").read_text(encoding="utf-8-sig")

    for state in PostState.ALL:
        assert f"'{state}'" in chip, f"{state} has no label in the chip macro"
        assert f".chip-{state}" in css, f"{state} has no colour in app.css"


def test_a_failed_asset_is_visible_where_the_operator_approves():
    """A post whose render failed showed nothing at all on its card: not the
    asset, not a warning. The operator approved it and it sat in the queue
    forever, because the reconciler will not publish a post whose declared
    asset does not exist."""
    index = (PROJECT_ROOT / "dashboard" / "templates" / "index.html").read_text(encoding="utf-8-sig")
    assert index.count("post.get('State') == 'asset_failed'") >= 2, (
        "both the PDF and the image branch need a failure state"
    )
    assert "Try again" in index


# ── D4: a group's visual identity comes from its config ──────────────────────

def test_a_groups_palette_reaches_the_render():
    """Every group declared primary_color and a theme in config.yaml, and none
    of it reached a render: the templates read --accent and --bg from the shared
    stylesheet and nothing wrote a group's values over them. Two communities
    with different palettes in config produced identical graphics."""
    from engine.group_config import list_available_groups, load_group_config

    for gid in list_available_groups():
        group = load_group_config(gid)
        tokens = RenderService.brand_tokens(group)
        assert tokens.get("accentColor") == group.primary_color, (
            f"{gid}'s primary_color never reaches the canvas"
        )


def test_two_groups_do_not_render_the_same_graphic():
    """The point of the token system: shared archetypes, per-tenant identity,
    with no template copied to get it."""
    from engine.group_config import list_available_groups, load_group_config

    palettes = [
        RenderService.override_css(RenderService.brand_tokens(load_group_config(gid)))
        for gid in list_available_groups()
    ]
    assert len(set(palettes)) == len(palettes), (
        "two communities produce identical styling — give them different brand values"
    )


def test_a_post_override_wins_over_the_group_palette():
    """Both write the same tokens. A post-level accent has to shadow the
    community's, or the studio's colour picker would do nothing on a group that
    declares one."""
    import inspect
    source = inspect.getsource(RenderService.build_html)
    brand = source.index("brand_tokens(group)")
    post = source.index("visual_overrides or placeholders")
    assert brand < post, "the group palette must be spread first so the post's wins"


def test_a_light_theme_keeps_its_background():
    """secondary_color is the surface a dark theme sits on. Applying it to a
    light template is how you get white text on white."""
    light = type("G", (), {"primary_color": "#111111", "secondary_color": "#222222",
                           "theme": "light", "fonts": {}})()
    assert "bgColor" not in RenderService.brand_tokens(light)


def test_a_group_font_choice_must_be_one_this_project_ships():
    from engine.group_config import list_available_groups, load_group_config

    for gid in list_available_groups():
        for role, choice in (load_group_config(gid).fonts or {}).items():
            assert choice in RenderService.FONT_CHOICES, (
                f"{gid} asks for the {role} font {choice!r}, which is not self-hosted"
            )
