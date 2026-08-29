"""
services/render_service.py
===========================
Template-driven HTML to PNG / PDF renderer using Playwright.
"""
import hashlib
import json
import os
import re
import shutil
import uuid
from collections import OrderedDict
from typing import Dict
import logging

log = logging.getLogger(__name__)

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_INSTALLED = True
except ImportError:
    PLAYWRIGHT_INSTALLED = False
    log.warning("Playwright is not installed. Rendering will not work.")

import atexit
import queue
import threading
from concurrent.futures import Future

# Pool size. Each worker owns one Chromium, so this is also the hard ceiling on
# Chromium processes for the whole application.
_POOL_SIZE = max(1, int(os.environ.get("RENDER_POOL_SIZE", "2")))

_BROWSER_CRASH_SIGNALS = (
    "target closed",
    "browser closed",
    "connection closed",
    "browser has been closed",
    "target page, context or browser has been closed",
    "connection unexpectedly closed",
    "page has been closed",
)


class _RenderPool:
    """A fixed pool of worker threads, each owning one persistent Chromium.

    Playwright's sync API is thread-affine: a browser created on one thread
    cannot be driven from another. A thread-local browser therefore means a
    cold launch (~1.8s) and a leaked Chromium process for every calling
    thread — and this application creates a fresh thread for almost every
    render. Owning the browsers here and having callers submit work instead
    is what makes one long-lived browser per worker possible.

    The pool is lazy: no Chromium is launched until the first render.
    """

    def __init__(self, size: int):
        self._size = size
        self._jobs: "queue.Queue" = queue.Queue()
        self._workers: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._started = False
        self._live = 0
        self._live_lock = threading.Lock()

    # ── lifecycle ────────────────────────────────────────────────────────────

    def _ensure_started(self) -> None:
        if self._started:
            return
        with self._lock:
            if self._started:
                return
            if not PLAYWRIGHT_INSTALLED:
                raise RuntimeError(
                    "Playwright library is not installed. Please run: pip install playwright"
                )
            for i in range(self._size):
                t = threading.Thread(target=self._worker, name=f"render-worker-{i}", daemon=True)
                t.start()
                self._workers.append(t)
            self._started = True

    def _launch(self):
        from playwright.sync_api import sync_playwright
        try:
            pw = sync_playwright().start()
            browser = pw.chromium.launch(
                headless=True,
                args=["--disable-web-security", "--allow-file-access-from-files"],
            )
            with self._live_lock:
                self._live += 1
            return pw, browser
        except Exception as launch_err:
            msg = str(launch_err)
            if ("Executable doesn't exist" in msg
                    or "browserType.launch" in msg
                    or "look like Playwright was just installed" in msg):
                raise RuntimeError(
                    "Chromium browser is missing. Please run: playwright install chromium"
                ) from launch_err
            raise RuntimeError(f"Playwright launch failed: {msg}") from launch_err

    def _worker(self) -> None:
        pw = browser = None
        try:
            while True:
                item = self._jobs.get()
                if item is None:                      # shutdown sentinel
                    self._jobs.task_done()
                    return
                fn, future = item
                if future.set_running_or_notify_cancel():
                    try:
                        if browser is None:
                            pw, browser = self._launch()
                        future.set_result(fn(browser))
                    except Exception as exc:
                        # A crashed or externally-killed Chromium must not
                        # poison every later job on this worker — drop it so
                        # the next job relaunches.
                        if any(s in str(exc).lower() for s in _BROWSER_CRASH_SIGNALS):
                            log.warning("Chromium crashed on %s; relaunching on next job.",
                                        threading.current_thread().name)
                            pw, browser = self._discard(pw, browser)
                        future.set_exception(exc)
                self._jobs.task_done()
        finally:
            self._discard(pw, browser)

    def _discard(self, pw, browser):
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
            with self._live_lock:
                self._live = max(0, self._live - 1)
        if pw is not None:
            try:
                pw.stop()
            except Exception:
                pass
        return None, None

    # ── public API ───────────────────────────────────────────────────────────

    def submit(self, fn):
        """Run fn(browser) on a pool worker and return its result (blocking)."""
        self._ensure_started()
        future: Future = Future()
        self._jobs.put((fn, future))
        return future.result()

    def live_browsers(self) -> int:
        with self._live_lock:
            return self._live

    def shutdown(self) -> None:
        with self._lock:
            if not self._started:
                return
            for _ in self._workers:
                self._jobs.put(None)
            for t in self._workers:
                t.join(timeout=10)
            self._workers.clear()
            self._started = False


_POOL = _RenderPool(_POOL_SIZE)


#: The fit cascade, as one piece of JavaScript both the exporter and the live
#: preview run. It used to exist only inside the render call, so the Design
#: Studio showed the raw un-fitted layout — the last card sitting on the footer
#: — while the exported file was fine. A studio whose preview does not match
#: what it exports is worse than no studio.
#:
#: Each stage gives up as little as possible: spacing first, then type size,
#: then scale. Going straight to the type floor for a 27px overflow shrinks the
#: text AND leaves the page half empty, which is the worst of both outcomes.
FIT_JS = """
function cfFit() {
  const page = document.querySelector('.page');
  const content = document.querySelector('.content');
  if (!page || !content) return { overflow: false, by: 0, stage: 'none' };

  // Start from clean so re-fitting after an edit cannot compound.
  page.classList.remove('is-dense', 'is-compact');
  content.style.transform = '';
  content.style.transformOrigin = '';

  // Measure .content, not .page. .content is a `flex: 1` item, so when its
  // children exceed it they spill out of it rather than stretching the page —
  // .page keeps reporting scrollHeight 1350 and the old check saw no overflow
  // while the last card was already sitting on top of the footer.
  const over = () => content.scrollHeight > content.clientHeight + 1;
  if (!over()) return { overflow: false, by: 0, stage: 'none' };

  // The density tiers live on .page, not .content: the hero title and the page
  // padding are the two biggest space budgets on the canvas and both sit
  // outside .content, so a tier scoped to .content can only tighten cards.
  for (const stage of ['is-dense', 'is-compact']) {
    page.classList.add(stage);
    if (!over()) return { overflow: false, by: 0, stage: stage };
  }

  // Last resort: scale by exactly the ratio needed rather than a fixed 0.92,
  // which silently failed to close larger overflows. Floored at 0.75 — past
  // that the text is too small to be worth shipping.
  const need = content.scrollHeight, have = content.clientHeight;
  const scale = Math.max(0.75, have / need);
  content.style.transform = 'scale(' + scale + ')';
  content.style.transformOrigin = 'top center';

  return {
    // Still overflowing after the floor: the operator has to cut something.
    overflow: scale <= 0.75 && have / need < 0.75,
    by: need - have,
    stage: 'scaled',
    scale: scale,
  };
}
"""


#: A CTA in config is written as a sentence for the end of a Telegram post.
#: A pill on a 1080px canvas is a label. Take the first clause, cap the length,
#: and never let a paragraph into the footer.
_CTA_MAX = 26


def _cta_label(group) -> str:
    label = (getattr(group, "cta_label", "") or "").strip()
    if label:
        return label[:_CTA_MAX]
    # No label configured: derive something usable rather than dropping a whole
    # sentence into the pill, but this is a fallback, not the intended path.
    active = group.get_active_ctas()
    if not active:
        return f"Join {group.name}"[:_CTA_MAX]
    text = active[0].text.strip().splitlines()[0].strip()
    for stop in ("?", ".", "!", "—", " - "):
        if stop in text:
            text = text.split(stop)[0].strip()
            break
    return (text[:_CTA_MAX].rsplit(" ", 1)[0] + "…") if len(text) > _CTA_MAX else text


class _RenderCache:
    """Bounded LRU of render-input hash -> output path.

    The cache key is a hash of the fully-built HTML plus the export type, so it
    already accounts for the template file's contents, every placeholder value
    and any visual overrides — editing a template or a single word invalidates
    it automatically. Entries are dropped when their file disappears.
    """

    def __init__(self, capacity: int = 256):
        self._capacity = capacity
        self._entries: "OrderedDict[str, str]" = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key: str):
        with self._lock:
            path = self._entries.get(key)
            if path is None:
                self.misses += 1
                return None
            if not os.path.exists(path):
                del self._entries[key]
                self.misses += 1
                return None
            self._entries.move_to_end(key)
            self.hits += 1
            return path

    def put(self, key: str, path: str) -> None:
        with self._lock:
            self._entries[key] = path
            self._entries.move_to_end(key)
            while len(self._entries) > self._capacity:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def stats(self) -> dict:
        with self._lock:
            total = self.hits + self.misses
            return {
                "entries": len(self._entries),
                "hits": self.hits,
                "misses": self.misses,
                "hit_rate": round(self.hits / total * 100, 1) if total else 0.0,
            }


_CACHE = _RenderCache()


class RenderService:

    @classmethod
    def shutdown_shared_browser(cls):
        """Close every pooled Chromium. Registered at exit; safe to call twice."""
        _POOL.shutdown()

    @classmethod
    def live_browser_count(cls) -> int:
        """Number of Chromium instances currently held open by the pool."""
        return _POOL.live_browsers()

    @classmethod
    def cache_stats(cls) -> dict:
        """Render-cache counters, for the dashboard's diagnostics panel."""
        return _CACHE.stats()

    @classmethod
    def clear_cache(cls) -> None:
        """Drop every cached render. Call after editing a template on disk."""
        _CACHE.clear()

    def __init__(self, base_output_dir: str, templates_dir: str):
        self.base_output_dir = base_output_dir
        self.templates_dir = templates_dir
        
        # Ensure output dirs exist
        os.makedirs(os.path.join(self.base_output_dir, "images"), exist_ok=True)
        os.makedirs(os.path.join(self.base_output_dir, "pdfs"), exist_ok=True)
        
    # Stylesheets are shared by every template and are linked, not inlined, so
    # their contents never reach the built HTML. Their digest is part of the
    # render cache key. Cached on (path, mtime, size) so the common case is a
    # stat() rather than a re-read.
    _CSS_FILES = ("styles.css", "fonts.css")
    _css_digest_cache: dict = {}
    _css_digest_lock = threading.Lock()

    def _stylesheet_digest(self) -> str:
        parts = []
        for name in self._CSS_FILES:
            path = os.path.join(self.templates_dir, name)
            try:
                st = os.stat(path)
            except OSError:
                parts.append(f"{name}:missing")
                continue
            stamp = (path, st.st_mtime_ns, st.st_size)
            with self._css_digest_lock:
                cached = self._css_digest_cache.get(path)
                if cached and cached[0] == stamp:
                    parts.append(cached[1])
                    continue
            with open(path, "rb") as f:
                digest = f"{name}:{hashlib.sha256(f.read()).hexdigest()[:16]}"
            with self._css_digest_lock:
                self._css_digest_cache[path] = (stamp, digest)
            parts.append(digest)
        return "|".join(parts)

    @staticmethod
    def brand_tokens(group) -> str:
        """A <style> block carrying one tenant's identity as CSS variables.

        This is what makes a shared template look like a particular community.
        Brand name, sub-brand and logo were literals in the markup, so every
        group's assets came out branded as the first one — which made
        "add a group without touching code" true right up until the render.
        """
        if group is None:
            return ""

        website = (group.footer.split("|")[-1].strip()
                   if "|" in group.footer else group.name)
        ctas = group.get_active_ctas()
        cta = ctas[0].text.strip().splitlines()[0] if ctas else f"Join {group.name}"

        return (
            "<style id=\"brand-tokens\">\n:root{\n"
            f"  --brand-accent: {group.primary_color};\n"
            f"  --brand-accent-2: {group.accent_color};\n"
            f"  --brand-ink: {group.secondary_color};\n"
            "}\n"
            "body.theme-dark, body.theme-light, body.theme-editorial{\n"
            "  --accent: var(--brand-accent);\n"
            "}\n"
            "</style>\n"
            # Chrome text the templates read as placeholders rather than
            # hardcoding. Kept here so one function owns the whole mapping.
            "<script id=\"brand-data\" type=\"application/json\">"
            + json.dumps({
                "BRAND_NAME": group.name,
                "BRAND_SUB": group.tagline or group.description[:40],
                "WEBSITE": website,
                "CTA": cta,
                "FOOTER": group.footer,
            })
            + "</script>\n"
        )

    @staticmethod
    def brand_placeholders(group) -> dict:
        """Chrome values a template's placeholders resolve from, per tenant."""
        if group is None:
            return {}
        website = (group.footer.split("|")[-1].strip()
                   if "|" in group.footer else group.name)
        theme = getattr(group, "theme", "dark") or "dark"
        return {
            "THEME_CLASS": f"theme-{theme}",
            # A dark surface needs the light logo and vice versa.
            "LOGO": "logo_light.png" if theme == "dark" else "logo_dark.png",
            "CTA": _cta_label(group),
            "BRAND_NAME": group.name,
            "BRAND_SUB": group.tagline or "",
            "WEBSITE": website,
            "FOOTER": group.footer,
        }

    @staticmethod
    def brand_tokens(group) -> dict:
        """This tenant's visual identity, as design-token overrides.

        Every group declares primary_color, accent_color and a theme in its
        config.yaml, and none of it reached a render: the templates read
        --accent and --bg from the shared stylesheet, and nothing ever wrote a
        group's values over them. Two communities with different palettes in
        config produced identical graphics.

        That is what D4 means by "a new group is two files plus an env var,
        including its visual identity" — a group's look has to come from its
        config, not from a Python edit or a template copy.
        """
        if group is None:
            return {}

        tokens = {}
        primary = (getattr(group, "primary_color", "") or "").strip()
        if primary:
            tokens["accentColor"] = primary

        # secondary_color is the surface a dark theme sits on. A light theme
        # leaves it alone: overriding the background of a light template with
        # a dark grey is how you get white text on white.
        secondary = (getattr(group, "secondary_color", "") or "").strip()
        if secondary and (getattr(group, "theme", "dark") or "dark") == "dark":
            tokens["bgColor"] = secondary

        fonts = getattr(group, "fonts", None) or {}
        if fonts.get("heading"):
            tokens["headingFont"] = fonts["heading"]
        if fonts.get("body"):
            tokens["bodyFont"] = fonts["body"]

        return tokens

    def _read_template(self, template_name: str) -> str:
        template_path = os.path.join(self.templates_dir, template_name)
        if not os.path.exists(template_path):
            raise FileNotFoundError(f"Template not found: {template_path}")
        with open(template_path, "r", encoding="utf-8") as f:
            return f.read()
            
    #: Placeholders that sit inside an HTML attribute rather than in text.
    #: The live preview wraps values in a <span> so the editor can address them,
    #: which turns `src="{{LOGO}}"` into `src="<span …>logo.png</span>"` — the
    #: logo showed as a broken image in every preview.
    ATTRIBUTE_KEYS = {"LOGO", "THEME_CLASS"}

    #: Placeholders whose value is deliberately HTML rather than text.
    MARKUP_KEYS = {"ITEMS_HTML", "CHECKLIST"}

    def _inject_placeholders(self, html: str, placeholders: Dict[str, str], is_live_preview: bool = False) -> str:
        for key, value in placeholders.items():
            if key.startswith("_"):
                continue
            val_str = str(value)
            if not is_live_preview or key in self.ATTRIBUTE_KEYS:
                html = html.replace(f"{{{{{key}}}}}", val_str)
                continue

            # Markup-valued placeholders still need a handle so the editor can
            # replace them — the studio's per-item edits go through ITEMS_HTML.
            # They are wrapped in a `display: contents` span so the handle does
            # not become a layout box between the flex container and its cards.
            if key in self.MARKUP_KEYS or val_str.startswith("<"):
                html = html.replace(
                    f"{{{{{key}}}}}",
                    f'<span data-field="{key}" style="display: contents">{val_str}</span>')
            else:
                html = html.replace(f"{{{{{key}}}}}", f'<span data-field="{key}">{val_str}</span>')

        # For live preview, wrap leftover unmapped placeholders so they can still be dynamically populated
        if is_live_preview:
            html = re.sub(r'\{\{([A-Z0-9_]+)\}\}', r'<span data-field="\1"></span>', html)
        else:
            # Clear remaining placeholders for static render
            html = re.sub(r'\{\{([A-Z0-9_]+)\}\}', '', html)

        return html

    def _generate_items_html(self, placeholders: Dict) -> tuple[str, bool]:
        """
        Generates HTML string for {{ITEMS_HTML}} injection point based on placeholders.
        Returns (items_html_str, is_compact_boolean).
        """
        items = placeholders.get("items")
        content_type = str(placeholders.get("content_type", "")).lower()
        html_parts = []
        item_count = 0

        if items and isinstance(items, list) and len(items) > 0:
            item_count = len(items)
            for idx, it in enumerate(items, 1):
                if not isinstance(it, dict):
                    continue
                num = it.get("number") or f"{idx:02d}"
                title = str(it.get("title", "")).strip()
                desc = str(it.get("description", "")).strip()

                if content_type in ("interview_qna", "qna") or "question" in it:
                    q = title or it.get("question", "")
                    a = desc or it.get("answer", "")
                    html_parts.append(
                        f'<div class="card interview-card">'
                        f'<div class="interview-q">{q}</div>'
                        f'<div class="interview-a">{a}</div>'
                        f'</div>'
                    )
                elif content_type in ("do_vs_dont", "myths_vs_facts", "comparison") or ("negative" in it and "positive" in it):
                    neg = it.get("negative") or title
                    pos = it.get("positive") or desc
                    neg_label = "Avoid" if content_type == "do_vs_dont" else "Myth"
                    pos_label = "Do This" if content_type == "do_vs_dont" else "Fact"
                    html_parts.append(
                        f'<div class="content-row">'
                        f'<div class="card comparison-card is-negative">'
                        f'<span class="card-label">{neg_label}</span><p class="card-text">{neg}</p>'
                        f'</div>'
                        f'<div class="card comparison-card is-positive">'
                        f'<span class="card-label">{pos_label}</span><p class="card-text">{pos}</p>'
                        f'</div>'
                        f'</div>'
                    )
                else:
                    card_body = f"<strong>{title}</strong> — {desc}" if (title and desc) else (title or desc)
                    # Optional short real-world illustration for this point.
                    example = str(it.get("example", "")).strip()
                    example_html = (
                        f'<p class="card-example">'
                        f'<span class="example-label">In practice</span>{example}</p>'
                    ) if example else ""
                    html_parts.append(
                        f'<div class="card point-card">'
                        f'<span class="point-number">{num}</span>'
                        f'<div class="card-body"><p class="card-text">{card_body}</p>'
                        f'{example_html}</div>'
                        f'</div>'
                    )
        else:
            # Fallback: check POINT_1, POINT_2, POINT_3...
            idx = 1
            while True:
                pt = placeholders.get(f"POINT_{idx}") or placeholders.get(f"point_{idx}") or placeholders.get(f"point{idx}")
                if not pt:
                    break
                num = f"{idx:02d}"
                html_parts.append(
                    f'<div class="card point-card">'
                    f'<span class="point-number">{num}</span>'
                    f'<div class="card-body"><p class="card-text">{pt}</p></div>'
                    f'</div>'
                )
                idx += 1
                item_count += 1

        tip = placeholders.get("TIP")
        if tip and str(tip).strip():
            html_parts.append(
                f'<div class="card tip-card card-row">'
                f'<span class="badge-icon">★</span>'
                f'<div class="card-body">'
                f'<span class="card-label">Pro Tip</span>'
                f'<p class="card-text">{tip}</p>'
                f'</div>'
                f'</div>'
            )

        checklist = placeholders.get("CHECKLIST")
        if checklist and str(checklist).strip():
            chk_items = str(checklist).strip()
            if not chk_items.startswith("<li>"):
                lines = [l.strip() for l in chk_items.split("\n") if l.strip()]
                chk_items = "".join([f'<li><span class="check-box"></span>{line}</li>' for line in lines])
            html_parts.append(
                f'<div class="card checklist-card">'
                f'<span class="card-label">Quick Checklist</span>'
                f'<ul class="checklist-list">{chk_items}</ul>'
                f'</div>'
            )

        # Only genuine content items justify pre-emptive compaction. Counting
        # html_parts instead meant a 4-item post plus a Pro Tip card hit the
        # threshold and rendered at the 18px compact floor while it had room
        # for full 23px body text. Anything that does overflow is still caught
        # and compacted by the measured cascade in render().
        is_compact = item_count >= 6
        return "\n".join(html_parts), is_compact

    #: What the Design Studio may change, and which design token each control
    #: writes to. Overriding tokens rather than element selectors is what makes
    #: one set of controls work across every archetype: the templates consume
    #: these names, so nothing here needs to know which one is being rendered.
    #:
    #: The previous version emitted rules like `.title { font-size: … }` nested
    #: inside a `:root { … }` block — invalid CSS — and aimed them at
    #: `.motivation-text` and `.editorial-title`, classes that belonged to
    #: templates deleted two commits earlier. None of the controls did anything.
    OVERRIDE_TOKENS = {
        "accentColor":  ("--accent", ""),
        "bgColor":      ("--bg", ""),
        "textColor":    ("--text-primary", ""),
        "headingFont":  ("--font-display", ""),
        "bodyFont":     ("--font-body", ""),
        "titleSize":    ("--text-display", "px"),
        "bodySize":     ("--text-body", "px"),
        "exampleSize":  ("--text-example", "px"),
    }

    #: Only the four families this project self-hosts. A free-text font box
    #: would let an operator name something the renderer cannot load, and the
    #: render would silently fall back — one of the reasons renders must not
    #: touch the network.
    FONT_CHOICES = {
        "display":   "'Sora', 'Inter', sans-serif",
        "editorial": "'Fraunces', Georgia, serif",
        "body":      "'Inter', sans-serif",
        "mono":      "'JetBrains Mono', monospace",
    }

    @classmethod
    def override_css(cls, overrides: Dict | None) -> str:
        """A `<style>` block setting only the tokens the operator changed."""
        if not overrides:
            return ""

        lines = []
        for key, (token, unit) in cls.OVERRIDE_TOKENS.items():
            # camelCase from the browser, snake_case from a stored document.
            snake = re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower()
            value = overrides.get(key) or overrides.get(snake)
            if value in (None, ""):
                continue
            if key.endswith("Font"):
                value = cls.FONT_CHOICES.get(str(value), value)
            lines.append(f"  {token}: {value}{unit} !important;")

        if not lines:
            return ""
        body = "\n".join(lines)
        # `:root, body` and not `:root` alone. The theme classes declare these
        # same tokens on <body>, and a custom property is inherited from the
        # nearest ancestor that declares it — so a value set only on <html> is
        # shadowed by body.theme-dark for everything inside the page, and
        # !important does not help because the conflict is between two
        # elements rather than within one. Every colour and font control in the
        # studio was inert for this reason.
        return (f'<style id="custom-visual-overrides">\n'
                f':root, body {{\n{body}\n}}\n</style>\n')

    def build_html(self, template_name: str, placeholders: Dict[str, str],
                   visual_overrides: Dict = None, base_href: str = None,
                   is_live_preview: bool = False, group=None) -> str:
        html = self._read_template(template_name)

        # Build ITEMS_HTML dynamically if placeholder or items array is present
        ph_copy = dict(placeholders)
        if "ITEMS_HTML" not in ph_copy or not ph_copy["ITEMS_HTML"]:
            items_html, is_compact = self._generate_items_html(ph_copy)
            ph_copy["ITEMS_HTML"] = items_html
            if is_compact:
                ph_copy["_is_compact"] = True

        html = self._inject_placeholders(html, ph_copy, is_live_preview=is_live_preview)

        # Inject density variant class if needed
        if ph_copy.get("_is_compact"):
            html = html.replace('<section class="content">', '<section class="content is-compact">', 1)
        
        # Build custom styles if overrides provided
        # The group's own palette first, then anything the operator changed
        # for this one post on top. Both write the same tokens, so a post-level
        # accent simply shadows the community's — and clearing the post's
        # overrides falls back to the brand rather than to the stylesheet
        # default, which is what "reset to this community's brand" means.
        overrides = {
            **self.brand_tokens(group),
            **(visual_overrides or placeholders.get("_visual_overrides", {}) or {}),
        }
        custom_css = self.override_css(overrides)

        # The preview runs in an iframe and the editor talks to it by
        # postMessage. Style changes write the same design tokens the static
        # render does, so what the operator sees here is what gets exported —
        # the two used to diverge because the live path and the render path
        # each built their own CSS.
        live_script = ""
        if is_live_preview:
            token_map = json.dumps(
                {key: [token, unit] for key, (token, unit) in self.OVERRIDE_TOKENS.items()})
            fonts = json.dumps(self.FONT_CHOICES)
            live_script = """
<script>
(function () {
  var TOKENS = __TOKENS__;
  var FONTS  = __FONTS__;

  window.addEventListener('message', function (e) {
    if (!e.data) return;

    if (e.data.type === 'UPDATE_FIELD') {
      var el = document.querySelector('[data-field="' + e.data.key + '"]');
      if (!el) return;
      // ITEMS_HTML and CHECKLIST are deliberately markup; everything else is
      // text and must not be injected as HTML.
      if (e.data.key === 'CHECKLIST' || e.data.key === 'ITEMS_HTML') {
        el.innerHTML = e.data.value;
      } else {
        el.textContent = e.data.value;
      }
      return;
    }

    if (e.data.type === 'UPDATE_STYLE') {
      var style = document.getElementById('live-custom-styles');
      if (!style) {
        style = document.createElement('style');
        style.id = 'live-custom-styles';
        document.head.appendChild(style);
      }
      var rules = [];
      Object.keys(TOKENS).forEach(function (key) {
        var value = e.data[key];
        if (value === undefined || value === null || value === '') return;
        if (/Font$/.test(key)) value = FONTS[value] || value;
        rules.push('  ' + TOKENS[key][0] + ': ' + value + TOKENS[key][1] + ' !important;');
      });
      // `:root, body` — see override_css. The theme class sets these same
      // tokens on <body>, which shadows anything set only on <html>.
      style.textContent = rules.length
        ? ':root, body {\\n' + rules.join('\\n') + '\\n}' : '';
      return;
    }

    if (e.data.type === 'MEASURE') {
      // Runs the exporter's own fit cascade, so the preview shows the layout
      // that will actually be produced — and reports back whether even the
      // cascade could not make it fit.
      var result = cfFit();
      parent.postMessage({ type: 'MEASURED', ...result }, '*');
    }
  });

  // Fit once on load as well. Opening this preview in its own tab sends no
  // MEASURE, and an unfitted page there would show the last card on the
  // footer for a graphic that exports perfectly well.
  //
  // Timing matters more than it looks. This script is injected into <head>,
  // so it runs before .page exists. DOMContentLoaded is still too early:
  // nothing has been laid out with the real faces yet, so document.fonts.ready
  // resolves against an empty pending set, cfFit measures fallback metrics —
  // which are narrower — decides the page fits, and adds no class. The real
  // faces then load, the text grows, and the page overflows with no tier
  // applied. That is exactly the state the preview was stuck in.
  //
  // So: wait for load, then for fonts, and re-fit if more faces arrive after.
  function cfFitWhenReady() {
    ((document.fonts && document.fonts.ready) || Promise.resolve())
      .then(function () { cfFit(); });
  }
  if (document.readyState === 'complete') {
    cfFitWhenReady();
  } else {
    window.addEventListener('load', cfFitWhenReady);
  }
  if (document.fonts) {
    document.fonts.addEventListener('loadingdone', cfFitWhenReady);
  }
})();
</script>
""".replace("__TOKENS__", token_map).replace("__FONTS__", fonts)
            live_script = f"<script>{FIT_JS}</script>\n" + live_script

        injections = ""
        if base_href:
            injections += f'<base href="{base_href}">\n'
        if custom_css:
            injections += custom_css + "\n"
        if live_script:
            injections += live_script + "\n"

        if injections:
            if "<head>" in html:
                html = html.replace("<head>", f"<head>\n{injections}", 1)
            else:
                html = f"{injections}\n" + html

        return html

    def build_multipage_pdf_html(self, template_name: str, placeholders: Dict[str, str],
                                 visual_overrides: Dict = None, base_href: str = None,
                                 group=None) -> tuple[str, int]:
        """
        Builds a multi-page HTML document where items[] are chunked across multiple .page blocks,
        each with full header and footer chrome, connected with CSS page-break rules.
        Returns (multipage_html, total_pages).
        """
        items = placeholders.get("items", [])
        if not isinstance(items, list) or len(items) <= 5:
            return self.build_html(template_name, placeholders,
                                   visual_overrides=visual_overrides,
                                   base_href=base_href, group=group), 1

        chunk_size = 5
        item_chunks = [items[i:i + chunk_size] for i in range(0, len(items), chunk_size)]
        total_pages = len(item_chunks)

        page_htmls = []
        for page_idx, chunk in enumerate(item_chunks, 1):
            ph_chunk = dict(placeholders)
            ph_chunk["items"] = chunk
            ph_chunk["PAGE"] = f"{page_idx} of {total_pages}"
            
            chunk_html = self.build_html(template_name, ph_chunk,
                                         visual_overrides=visual_overrides,
                                         base_href=base_href, group=group)
            if page_idx > 1:
                chunk_html = chunk_html.replace('<div class="page">', '<div class="page" style="page-break-before: always; break-before: page;">', 1)
            page_htmls.append(chunk_html)

        combined_html = "\n".join(page_htmls)
        return combined_html, total_pages

    def render(self, template_name: str, placeholders: Dict[str, str],
               export_type: str, visual_overrides: Dict = None, group=None) -> str:
        """
        Renders the given template with injected placeholders.
        export_type should be "PNG" or "PDF".
        Returns the absolute path to the generated file.
        """
        import tempfile
        from pathlib import Path
        
        base_uri = Path(self.templates_dir).absolute().as_uri() + "/"
        items_count = len(placeholders.get("items", [])) if isinstance(placeholders.get("items"), list) else 0

        # For PDF with >5 items, generate multi-page HTML directly
        if export_type.upper() == "PDF" and items_count > 5:
            html, expected_pages = self.build_multipage_pdf_html(
                template_name, placeholders, visual_overrides=visual_overrides,
                base_href=base_uri, group=group)
        else:
            html = self.build_html(template_name, placeholders,
                                   visual_overrides=visual_overrides,
                                   base_href=base_uri, is_live_preview=False,
                                   group=group)
            expected_pages = 1

        ph_copy = dict(placeholders)
        ph_copy["_expected_pages"] = expected_pages

        # The built HTML covers the template file, every placeholder and any
        # visual override — but NOT the stylesheets it merely links to, so the
        # digest of those has to go into the key as well. Without it, editing
        # styles.css serves stale renders until the process restarts.
        cache_key = hashlib.sha256(
            f"{export_type.upper()}\x00{expected_pages}\x00"
            f"{self._stylesheet_digest()}\x00{html}".encode("utf-8")
        ).hexdigest()

        file_id = str(uuid.uuid4())[:8]
        temp_html_path = os.path.join(tempfile.gettempdir(), f"temp_render_{file_id}.html")
        with open(temp_html_path, "w", encoding="utf-8") as f:
            f.write(html)
            
        temp_uri = Path(temp_html_path).absolute().as_uri()

        def _paint(browser) -> str:
            """Runs on a pool worker thread that owns `browser`."""
            context = browser.new_context(
                viewport={"width": 1080, "height": 1350},
                device_scale_factor=2,
            )
            try:
                page = context.new_page()

                # Every asset the page needs is local, so "load" is sufficient —
                # "networkidle" additionally waits out a 500ms quiet window and
                # cost ~1s per render back when styles.css pulled Google Fonts.
                page.goto(temp_uri, wait_until="load")

                # Webfonts must be applied BEFORE any layout measurement: font
                # metrics change scrollHeight, so measuring overflow first makes
                # the compact-mode decision on the wrong numbers. Resolve to a
                # boolean — document.fonts.ready itself yields a FontFaceSet,
                # which Playwright cannot serialise back across the wire.
                page.evaluate("() => document.fonts.ready.then(() => true)")

                # The fit cascade. Defined once, at module level, so the live
                # preview in the Design Studio runs exactly this and cannot
                # drift from what gets exported.
                fit = page.evaluate(f"() => {{ {FIT_JS}; return cfFit(); }}")
                if fit.get("overflow"):
                    log.warning(
                        "Content still overflows by %spx after the fit cascade "
                        "(%s). The graphic will be readable but tight.",
                        fit.get("by"), template_name,
                    )

                if export_type.upper() == "PNG":
                    out = os.path.join(self.base_output_dir, "images", f"post_{file_id}.png")
                    page.screenshot(path=out)
                elif export_type.upper() == "PDF":
                    out = os.path.join(self.base_output_dir, "pdfs", f"post_{file_id}.pdf")
                    page.pdf(
                        path=out,
                        width="1080px",
                        height="1350px",
                        print_background=True,
                        margin={"top": "0", "bottom": "0", "left": "0", "right": "0"},
                    )
                else:
                    raise ValueError(f"Unsupported export type: {export_type}")
                return out
            finally:
                # Always close, even if the screenshot raised — otherwise a
                # failed render leaks a browser context for the process lifetime.
                try:
                    context.close()
                except Exception:
                    pass

        try:
            cached = _CACHE.get(cache_key)
            if cached and os.path.exists(cached):
                # Deterministic input, deterministic output: copy the previous
                # result to a fresh path rather than returning a shared one, so
                # deleting one post's asset can never break another's.
                ext = "pdf" if export_type.upper() == "PDF" else "png"
                sub = "pdfs" if ext == "pdf" else "images"
                output_path = os.path.join(self.base_output_dir, sub, f"post_{file_id}.{ext}")
                os.makedirs(os.path.dirname(output_path), exist_ok=True)
                shutil.copyfile(cached, output_path)
                log.info("Render cache hit for %s (%s)", template_name, export_type)
                return output_path

            output_path = _POOL.submit(_paint)
            self.validate_asset(output_path, export_type, html, expected_pages=expected_pages)
            _CACHE.put(cache_key, output_path)
            return output_path
        except Exception as e:
            if isinstance(e, RuntimeError):
                raise e
            raise RuntimeError(f"Rendering failed: {str(e)}") from e
        finally:
            if os.path.exists(temp_html_path):
                try:
                    os.remove(temp_html_path)
                except Exception:
                    pass



    def validate_asset(self, filepath: str, export_type: str, html_content: str, expected_pages: int = 1):
        """Validates that the output file exists, is non-empty, and satisfies style/dimension criteria."""
        # 1. Output exists & file size > 0
        if not os.path.exists(filepath):
            raise ValueError(f"Asset file was not created: {filepath}")
        if os.path.getsize(filepath) == 0:
            raise ValueError(f"Asset file is empty: {filepath}")

        # 2. Template assets exist
        logo_light = os.path.join(self.templates_dir, "logo_light.png")
        logo_dark = os.path.join(self.templates_dir, "logo_dark.png")
        if not os.path.exists(logo_light) and not os.path.exists(logo_dark):
            raise ValueError("Brand logos (logo_light.png/logo_dark.png) are missing in design_templates/.")

        css_file = os.path.join(self.templates_dir, "styles.css")
        if not os.path.exists(css_file):
            raise ValueError("CSS stylesheet (styles.css) is missing in design_templates/.")

        # 3. Check for unresolved placeholders like {{TITLE}}
        import re
        clean_html = re.sub(r'<!--.*?-->', '', html_content, flags=re.DOTALL)
        unresolved = re.findall(r'\{\{([A-Z0-9_]+)\}\}', clean_html)
        if unresolved:
            raise ValueError(f"Unresolved placeholders found in HTML template: {unresolved}")

        # 4. Validate PNG Image dimensions using Pillow (1080px standard or 2160px at 2x scale)
        if export_type.upper() == "PNG":
            try:
                from PIL import Image
                with Image.open(filepath) as img:
                    width, height = img.size
                    if width not in (1080, 2160):
                        raise ValueError(f"PNG viewport width is {width}px instead of 1080px or 2160px (2x scale).")
            except Exception as e:
                raise ValueError(f"PNG dimension validation failed: {e}")
                
        # 5. Validate PDF structure & Page Count
        elif export_type.upper() == "PDF":
            try:
                with open(filepath, 'rb') as f:
                    content = f.read()
                # Basic scan for pages in PDF binary format
                pages = content.count(b"/Type /Page") or content.count(b"/Type/Page") or content.count(b"/Parent")
                if pages == 0:
                    raise ValueError("Generated PDF file has invalid structure or contains 0 pages.")
                if expected_pages > 1 and pages < expected_pages:
                    raise ValueError(f"PDF page count ({pages}) is less than expected multi-page count ({expected_pages}).")
            except Exception as e:
                raise ValueError(f"PDF structure validation failed: {e}")

# Register shutdown hook to clean up the browser process cleanly
atexit.register(RenderService.shutdown_shared_browser)

