"""
scripts/maintenance/fetch_fonts.py
==================================
Download the design system's webfonts from Google Fonts and self-host them.

Renders must never touch the network: a remote @import costs roughly a second
per render (Chromium waits for the stylesheet, then the font files) and fails
outright offline or behind an egress policy. This script pulls the woff2 files
once, writes design_templates/fonts/, and generates design_templates/fonts.css
with local @font-face rules.

All four families are variable fonts, so one file serves every weight — the
script deduplicates by content hash, which cuts the payload by roughly 3x.

Run:
    python scripts/maintenance/fetch_fonts.py
"""
from __future__ import annotations

import collections
import hashlib
import re
import sys
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).parent.parent.parent
TEMPLATES_DIR = PROJECT_ROOT / "design_templates"
FONTS_DIR = TEMPLATES_DIR / "fonts"
CSS_OUT = TEMPLATES_DIR / "fonts.css"

# Google Fonts serves woff2 only to browsers that advertise support.
UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}

CSS_URL = (
    "https://fonts.googleapis.com/css2?"
    "family=Sora:wght@600;700;800"
    "&family=Inter:wght@400;500;600;700"
    "&family=JetBrains+Mono:wght@500;600"
    "&family=Fraunces:opsz,wght@9..144,500;9..144,600;9..144,700"
    "&display=swap"
)

# The templates are English-only; other subsets would triple the payload.
KEEP_SUBSETS = {"latin", "latin-ext"}


def main() -> int:
    FONTS_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Fetching font CSS from Google Fonts…")
    css = httpx.get(CSS_URL, headers=UA, timeout=30).text
    blocks = re.findall(r"/\*\s*([\w\-\[\]]+)\s*\*/\s*(@font-face\s*\{.*?\})", css, re.S)
    print(f"  {len(blocks)} @font-face blocks; keeping subsets {sorted(KEEP_SUBSETS)}")

    kept, downloads = [], {}
    for subset, block in blocks:
        if subset not in KEEP_SUBSETS:
            continue
        family = re.search(r"font-family:\s*'([^']+)'", block).group(1)
        weight = re.search(r"font-weight:\s*([^;]+);", block).group(1).strip()
        url = re.search(r"url\((https://[^)]+\.woff2)\)", block).group(1)
        fname = f"{family.replace(' ', '')}-{weight.replace(' ', '_')}-{subset}.woff2"
        downloads[fname] = url
        kept.append({"family": family, "weight": weight, "subset": subset,
                     "fname": fname, "block": block})

    print(f"  downloading {len(downloads)} files…")
    for fname, url in downloads.items():
        (FONTS_DIR / fname).write_bytes(httpx.get(url, headers=UA, timeout=30).content)

    # Variable fonts return one identical file per weight — collapse them.
    by_hash: dict[str, list[str]] = collections.defaultdict(list)
    for f in sorted(FONTS_DIR.glob("*.woff2")):
        by_hash[hashlib.sha256(f.read_bytes()).hexdigest()].append(f.name)

    alias: dict[str, str] = {}
    for names in by_hash.values():
        canonical = re.sub(r"-\d+(_\d+)?-", "-", names[0]) if len(names) > 1 else names[0]
        if len(names) > 1:
            (FONTS_DIR / names[0]).rename(FONTS_DIR / canonical)
            for extra in names[1:]:
                (FONTS_DIR / extra).unlink()
        for n in names:
            alias[n] = canonical

    lines = [
        "/* fonts.css — self-hosted webfonts. GENERATED FILE, do not edit by hand.",
        "   Renders must not touch the network; regenerate with:",
        "     python scripts/maintenance/fetch_fonts.py",
        f"   Subsets: {', '.join(sorted(KEEP_SUBSETS))} */",
        "",
    ]
    for k in kept:
        block = re.sub(
            r"url\(https://[^)]+\.woff2\)",
            f"url('fonts/{alias[k['fname']]}')",
            k["block"],
        )
        block = re.sub(r"\n\s+", "\n  ", block.strip())
        lines.append(f"/* {k['family']} {k['weight']} · {k['subset']} */")
        lines.append(block)
        lines.append("")
    CSS_OUT.write_text("\n".join(lines), encoding="utf-8")

    files = sorted(FONTS_DIR.glob("*.woff2"))
    total_kb = sum(f.stat().st_size for f in files) / 1024
    print(f"\n  {len(files)} unique files, {total_kb:.0f} KB")
    print(f"  wrote {CSS_OUT.relative_to(PROJECT_ROOT)}")
    print("\nEnsure styles.css contains:  @import url('fonts.css');")
    return 0


if __name__ == "__main__":
    sys.exit(main())
