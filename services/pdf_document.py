"""
services/pdf_document.py
========================
Turns the PDF Writer's outline into one flowing HTML document.

It used to paginate by hand: count the characters in each section, estimate how
much would fit on a 1350px page, and emit a fixed-height `.pdf-page` for each
group. That estimate is a guess about text the browser has not laid out yet, so
it was wrong in both directions — pages that ended half empty, and text that
ran past the bottom of its page and was clipped. It also meant the composer and
the renderer could disagree about how many pages existed.

The browser already knows exactly where a page ends. So this emits one document
and lets the print engine break it; pdf.css says where breaking is allowed.
Nothing here measures anything.
"""
from __future__ import annotations

import html as _html
import re
from typing import Any


def _esc(value: Any) -> str:
    """Escape model output. Everything here is untrusted text, not markup."""
    return _html.escape(str(value or "").strip())


def _paragraphs(text: str) -> list[str]:
    """Split prose into paragraphs on blank lines, falling back to single-line."""
    raw = str(text or "").strip()
    if not raw:
        return []
    parts = [p.strip() for p in re.split(r"\n\s*\n", raw) if p.strip()]
    return parts or [raw]


class PdfDocument:
    """Builds the guide's markup. Layout and pagination belong to CSS."""

    def __init__(self, doc: dict, brand: dict, tokens: dict | None = None):
        self.doc = doc or {}
        self.brand = brand or {}
        # The group's design tokens. Without them --bg is declared only on
        # body.theme-dark, and a custom property does not inherit upward — so
        # `html { background: var(--bg) }` resolved to nothing, the canvas
        # stayed white, and the dark guide printed inside a white border. They
        # also give a PDF the same per-community palette an image gets.
        self.tokens = tokens or {}


    # ── the parts ────────────────────────────────────────────────────────────

    def _cover(self) -> str:
        points = ""
        listed = [p for p in (self.doc.get("COVER_POINTS") or []) if str(p).strip()]
        if not listed:
            # Fall back to the section headings: they are what the guide covers.
            listed = [s.get("heading", "") for s in self._sections()][:3]
        if listed:
            rows = "\n".join(
                f'    <div class="pdf-cover-point"><b>{i:02d}</b>'
                f'<span>{_esc(point)}</span></div>'
                for i, point in enumerate(listed, 1)
            )
            points = f'\n  <div class="pdf-cover-points">\n{rows}\n  </div>'

        return f"""
<section class="pdf-cover">
  <div class="pdf-eyebrow">{_esc(self.doc.get('CATEGORY') or 'GUIDE')}</div>
  <h1>{_esc(self.doc.get('TITLE'))}</h1>
  <p class="pdf-lede">{_esc(self.doc.get('SUBTITLE'))}</p>{points}
</section>"""

    def _intro(self) -> str:
        body = "\n".join(f"  <p>{_esc(p)}</p>" for p in _paragraphs(self.doc.get("INTRO")))
        if not body:
            return ""
        return f"""
<section class="pdf-section">
  <div class="pdf-kicker">Introduction</div>
  <h2>{_esc(self.doc.get('INTRO_HEADING') or 'What this guide covers')}</h2>
{body}
</section>"""

    def _sections(self) -> list[dict]:
        return [s for s in (self.doc.get("SECTIONS") or []) if isinstance(s, dict)]

    def _section(self, section: dict, index: int, total: int) -> str:
        body = "\n".join(f"  <p>{_esc(p)}</p>" for p in _paragraphs(section.get("body")))
        example = ""
        if str(section.get("example") or "").strip():
            example = (
                '\n  <div class="pdf-example">'
                '<span class="label">In practice</span>'
                f'<p>{_esc(section["example"])}</p></div>'
            )
        return f"""
<section class="pdf-section">
  <div class="pdf-kicker">Section {index} of {total}</div>
  <h2>{_esc(section.get('heading'))}</h2>
{body}{example}
</section>"""

    def _checklist(self) -> str:
        items = [i for i in (self.doc.get("CHECKLIST") or []) if str(i).strip()]
        if not items:
            return ""
        rows = "\n".join(f"    <li>{_esc(item)}</li>" for item in items)
        return f"""
<section class="pdf-section">
  <div class="pdf-kicker">Before you close this</div>
  <h2>Your checklist</h2>
  <ul class="pdf-checklist">
{rows}
  </ul>
</section>"""

    def _close(self) -> str:
        summary = _esc(self.doc.get("SUMMARY") or self.doc.get("SUBTITLE"))
        return f"""
<section class="pdf-close">
  <div class="pdf-kicker">In one line</div>
  <h2>{summary}</h2>
  <p>{_esc(self.doc.get('CLOSING') or '')}</p>
  <span class="pdf-cta">{_esc(self.brand.get('CTA'))}</span>
</section>"""

    # ── assembly ─────────────────────────────────────────────────────────────

    def build_html(self, skeleton: str, theme_class: str = "theme-dark") -> tuple[str, int]:
        """Fill the pdf.html skeleton. Returns (html, sections + 3).

        The second value is only a hint for logging. The real page count is
        whatever Chromium decides, and asking this class to predict it is the
        mistake this rewrite removes.
        """
        sections = self._sections()
        parts = [self._cover(), self._intro()]
        parts += [self._section(s, i, len(sections)) for i, s in enumerate(sections, 1)]
        parts += [self._checklist(), self._close()]

        body = ('<div class="pdf-doc">\n'
                + "\n".join(p for p in parts if p)
                + "\n</div>")

        out = skeleton.replace("{{PAGES_HTML}}", body)
        out = out.replace("{{THEME_CLASS}}", theme_class)

        if self.tokens:
            from services.render_service import RenderService
            out = out.replace("</head>",
                              RenderService.override_css(self.tokens) + "</head>", 1)
        return out, len(sections) + 3
