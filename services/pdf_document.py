"""
services/pdf_document.py
========================
Composes a multi-page PDF guide from the PDF Writer's structured output.

The previous multi-page path chunked one list template across N pages and
concatenated whole HTML documents to do it — each page carried its own
<html><head>, and every page looked identical. There was no cover, no
introduction and no close; a reader landed straight in the middle of a list.

A guide here has a shape:

    1        cover        title, subtitle, two things it will teach, read time
    2        introduction what this covers and why it matters now
    3..n     sections     one idea per page: heading, prose, a worked example
    last     close        checklist, one-line summary, call to action

Pages are `.page`-sized divs in a single document separated by CSS page
breaks, which is what Chromium's print pipeline actually expects.
"""
from __future__ import annotations

import html
import logging
from typing import Any

log = logging.getLogger(__name__)

#: Sections beyond this are dropped rather than silently overflowing their page.
MAX_SECTIONS = 8
#: A section whose prose runs past this is split across two pages.
PROSE_SPLIT_CHARS = 620
#: Roughly how much one page holds, in the same character-weight units.
#: Tuned so two short sections share a page and one long section keeps its own.
PAGE_WEIGHT = 1180
#: What a second section on the same page costs in rule, gap and padding.
SECTION_GAP_WEIGHT = 260


def _esc(value: Any) -> str:
    """Escape model output before it becomes markup.

    Mapper output is injected unescaped elsewhere because ITEMS_HTML is
    deliberately HTML. Here everything is prose, so an apostrophe or a stray
    angle bracket must not be able to break the layout.
    """
    return html.escape(str(value or "").strip())


def _paragraphs(text: str) -> list[str]:
    raw = str(text or "").replace("\r", "")
    parts = [p.strip() for p in raw.split("\n\n") if p.strip()]
    if not parts:
        parts = [p.strip() for p in raw.split("\n") if p.strip()]
    return parts


class PdfDocument:
    """Turns the PDF Writer's JSON into page markup."""

    def __init__(self, doc: dict, brand: dict):
        self.doc = doc or {}
        self.brand = brand or {}

    # ── chrome ───────────────────────────────────────────────────────────────

    def _header(self, category: str = "") -> str:
        return f"""
  <header class="header">
    <div class="brand">
      <img class="brand-logo" src="{_esc(self.brand.get('LOGO', 'logo_light.png'))}" alt="">
      <div class="brand-text">
        <span class="brand-name">{_esc(self.brand.get('BRAND_NAME'))}</span>
        <span class="brand-sub">{_esc(self.brand.get('BRAND_SUB'))}</span>
      </div>
    </div>
    <span class="pill category-pill">{_esc(category or self.doc.get('CATEGORY'))}</span>
  </header>
  <hr class="divider header-divider">"""

    def _footer(self, page_no: int, total: int) -> str:
        return f"""
  <footer class="footer">
    <hr class="divider footer-divider">
    <div class="footer-inner">
      <div class="footer-left">
        <span class="footer-website">{_esc(self.brand.get('WEBSITE'))}</span>
      </div>
      <div class="footer-right">
        <span class="cta-pill">{_esc(self.brand.get('CTA'))}</span>
        <span class="page-number">{page_no} / {total}</span>
      </div>
    </div>
  </footer>"""

    def _page(self, body: str, page_no: int, total: int, category: str = "") -> str:
        return (f'<div class="pdf-page">{self._header(category)}\n{body}\n'
                f'{self._footer(page_no, total)}\n</div>')

    # ── page bodies ──────────────────────────────────────────────────────────

    def _cover(self, page_no: int, total: int) -> str:
        points = self.doc.get("COVER_POINTS") or []
        points_html = ""
        if points:
            items = "".join(
                f'<div class="cover-point">'
                f'<span class="cover-point-mark">{i:02d}</span>'
                f'<p class="cover-point-text">{_esc(p)}</p></div>'
                for i, p in enumerate(points[:2], 1)
            )
            points_html = f'<div class="cover-points">{items}</div>'

        reading = _esc(self.doc.get("READING_TIME"))
        meta = ""
        if reading:
            meta = (f'<div class="pdf-cover-meta"><span>{reading}</span>'
                    f'<span class="dot">&bull;</span>'
                    f'<span>{_esc(self.brand.get("BRAND_NAME"))}</span></div>')

        return f"""
  <section class="pdf-cover-body">
    <span class="cover-eyebrow">{_esc(self.doc.get('CATEGORY'))}</span>
    <h1 class="pdf-cover-title">{_esc(self.doc.get('TITLE'))}</h1>
    <p class="pdf-cover-subtitle">{_esc(self.doc.get('SUBTITLE'))}</p>
    {points_html}
    {meta}
  </section>"""

    def _intro(self, page_no: int, total: int) -> str:
        blocks = "".join(
            f'<p class="pdf-prose">{_esc(p)}</p>'
            for p in _paragraphs(self.doc.get("INTRO"))
        )
        why = self.doc.get("WHY_IT_MATTERS")
        why_html = ""
        if why:
            why_html = (
                '<div class="pdf-example">'
                '<span class="pdf-example-label">Why this matters now</span>'
                f'<p class="pdf-example-text">{_esc(why)}</p></div>'
            )
        return f"""
  <span class="pdf-section-index">Introduction</span>
  <h2 class="pdf-section-heading">What this guide covers</h2>
  <div class="pdf-body">
    {blocks}
    {why_html}
  </div>"""

    def _section(self, section: dict, index: int, total_sections: int,
                 part: int = 1, parts: int = 1) -> str:
        heading = _esc(section.get("heading"))
        label = f"Section {index} of {total_sections}"
        if parts > 1:
            label += f" · part {part}"

        body = "".join(
            f'<p class="pdf-prose">{_esc(p)}</p>'
            for p in section.get("_paragraphs", [])
        )
        example = section.get("example")
        example_html = ""
        # The example belongs with the last part of a split section, not the first.
        if example and part == parts:
            example_html = (
                '<div class="pdf-example">'
                '<span class="pdf-example-label">In practice</span>'
                f'<p class="pdf-example-text">{_esc(example)}</p></div>'
            )
        # Wrapped so packed sections can be spaced apart: without it the next
        # section's label sits directly on the previous example card.
        return f"""
  <div class="pdf-section">
    <span class="pdf-section-index">{label}</span>
    <h2 class="pdf-section-heading">{heading}</h2>
    <div class="pdf-body">
      {body}
      {example_html}
    </div>
  </div>"""

    def _close(self) -> str:
        checklist = self.doc.get("CHECKLIST") or []
        if isinstance(checklist, str):
            checklist = [c.strip("-• ") for c in checklist.splitlines() if c.strip()]
        items = "".join(
            f'<li><span class="pdf-checkbox"></span><span>{_esc(c)}</span></li>'
            for c in checklist[:8]
        )
        list_html = f'<ul class="pdf-checklist">{items}</ul>' if items else ""

        summary = self.doc.get("SUMMARY")
        summary_html = ""
        if summary:
            summary_html = (
                '<div class="pdf-summary">'
                '<span class="pdf-example-label">In one line</span>'
                f'<p class="pdf-example-text">{_esc(summary)}</p></div>'
            )
        cta = self.doc.get("CTA")
        cta_html = f'<p class="pdf-prose">{_esc(cta)}</p>' if cta else ""

        return f"""
  <span class="pdf-section-index">Before you close this</span>
  <h2 class="pdf-section-heading">Your checklist</h2>
  <div class="pdf-body">
    {list_html}
    {summary_html}
    {cta_html}
  </div>"""

    # ── assembly ─────────────────────────────────────────────────────────────

    def _prepared_sections(self) -> list[dict]:
        """Sections with prose split into page-sized parts."""
        raw = self.doc.get("SECTIONS") or []
        prepared: list[dict] = []
        for section in raw[:MAX_SECTIONS]:
            if not isinstance(section, dict):
                continue
            paragraphs = _paragraphs(section.get("body"))
            if not paragraphs and not section.get("heading"):
                continue

            # Split long prose across pages rather than letting it run off one.
            chunks: list[list[str]] = [[]]
            length = 0
            for paragraph in paragraphs:
                if length and length + len(paragraph) > PROSE_SPLIT_CHARS:
                    chunks.append([])
                    length = 0
                chunks[-1].append(paragraph)
                length += len(paragraph)

            prepared.append({**section, "_chunks": chunks or [[]]})
        return prepared

    @staticmethod
    def _weight(section: dict, chunk: list[str]) -> int:
        """Rough height of a section, in characters of prose equivalent.

        The model tends to write shorter sections than the prompt asks for, and
        one short section alone on a 1350px page leaves nearly half of it empty.
        Rather than demand longer prose — which produces padding, not value —
        the layout packs short sections together.
        """
        weight = len(section.get("heading", "")) * 3     # headings are large type
        weight += sum(len(p) for p in chunk)
        if section.get("example"):
            weight += 240                                 # the example card
        return weight

    def _pack(self, sections: list[dict]) -> list[list[dict]]:
        """Group section parts into pages that are comfortably full."""
        units: list[dict] = []
        for index, section in enumerate(sections, 1):
            parts = len(section["_chunks"])
            for part, chunk in enumerate(section["_chunks"], 1):
                units.append({
                    "section": section, "index": index, "part": part,
                    "parts": parts, "chunk": chunk,
                    "weight": self._weight(section, chunk),
                })

        pages: list[list[dict]] = []
        current: list[dict] = []
        used = 0
        for unit in units:
            # A split section always starts its own page: continuing one
            # halfway down a page under a different heading reads as a mistake.
            starts_page = unit["parts"] > 1
            # Joining a section to a page is not free: it brings a rule, a gap
            # above it and a gap below. Ignoring that is what pushed a packed
            # page 126px past the canvas.
            cost = unit["weight"] + (SECTION_GAP_WEIGHT if current else 0)
            if current and (starts_page or used + cost > PAGE_WEIGHT):
                pages.append(current)
                current, used = [], 0
                cost = unit["weight"]
            current.append(unit)
            used += cost
        if current:
            pages.append(current)
        return pages

    def build_pages(self) -> list[str]:
        sections = self._prepared_sections()
        packed = self._pack(sections)

        # Count first: every footer needs the real total.
        total = 1 + (1 if self.doc.get("INTRO") else 0) + len(packed) + 1

        pages: list[str] = []
        page_no = 1
        pages.append(self._page(self._cover(page_no, total), page_no, total))
        page_no += 1

        if self.doc.get("INTRO"):
            pages.append(self._page(self._intro(page_no, total), page_no, total))
            page_no += 1

        for group in packed:
            body = "\n".join(
                self._section(
                    {**unit["section"], "_paragraphs": unit["chunk"]},
                    unit["index"], len(sections), unit["part"], unit["parts"],
                )
                for unit in group
            )
            pages.append(self._page(body, page_no, total))
            page_no += 1

        pages.append(self._page(self._close(), page_no, total))
        return pages

    def build_html(self, skeleton: str, theme_class: str = "theme-light") -> tuple[str, int]:
        """Fill the pdf.html skeleton. Returns (html, page_count)."""
        pages = self.build_pages()
        out = skeleton.replace("{{PAGES_HTML}}", "\n".join(pages))
        out = out.replace("{{THEME_CLASS}}", theme_class)
        return out, len(pages)
