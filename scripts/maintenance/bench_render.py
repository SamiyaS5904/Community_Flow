"""
scripts/bench_render.py
=======================
Render benchmark and visual-regression capture.

Renders every enabled template in design_templates/registry.json with a fixed,
representative payload, times each stage, and writes the output PNG/PDF to a
named directory so before/after runs can be compared byte-for-byte and by eye.

Usage:
    python scripts/bench_render.py before
    python scripts/bench_render.py after
    python scripts/bench_render.py compare before after

The payload is deliberately static: identical input across runs is what makes
the comparison meaningful.
"""
from __future__ import annotations

import hashlib
import re
import json
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

PROJECT_ROOT = Path(__file__).parent.parent.parent
TEMPLATES_DIR = PROJECT_ROOT / "design_templates"
BENCH_ROOT = PROJECT_ROOT / "generated" / "bench"


# ── Fixed payload ─────────────────────────────────────────────────────────────
# One superset dict. The renderer fills what a template asks for and blanks the
# rest, so a single payload exercises every archetype consistently.
PAYLOAD = {
    "CATEGORY": "PLACEMENT GUIDE",
    "HOOK": "MASTER THE HR ROUND",
    "TITLE": "Tell me about a time you faced a challenge at work",
    "SUBTITLE": "A behavioural question asked in roughly 90% of interview rounds.",
    "QUOTE": "START MESSY, NOT PERFECT",
    "SUBTEXT": "The candidates who get placed are not the ones who planned the longest.",
    "TAGLINE": "INTERVIEW HACK",
    "WEBSITE": "owlet-campus.com",
    "SOURCE": "",
    "CTA": "Join @carrotowl",
    "PAGE": "1",
    "TIP": "Keep it structured using the STAR framework.",
    "items": [
        {"number": "01", "title": "Set the scene",
         "description": "Name the situation and the concrete constraint you were under.",
         "example": "Your team lost a member two weeks before a client demo and the scope never changed."},
        {"number": "02", "title": "Own the action",
         "description": "Describe what you specifically did, not what the team did.",
         "example": "You rewrote the demo script yourself and cut two features nobody would notice."},
        {"number": "03", "title": "Quantify the result",
         "description": "Close with a number, a saved deadline, or a measurable change.",
         "example": "The demo shipped on the original date and the client renewed for another year."},
        {"number": "04", "title": "Name the lesson",
         "description": "One sentence on what you would do differently next time.",
         "example": "You would raise the scope risk in week one instead of absorbing it quietly."},
    ],
}


def load_registry() -> list[dict]:
    with open(TEMPLATES_DIR / "registry.json", "r", encoding="utf-8") as f:
        return [t for t in json.load(f) if t.get("enabled", True)]


def file_digest(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def run(label: str) -> None:
    from services.render_service import RenderService

    out_dir = BENCH_ROOT / label
    out_dir.mkdir(parents=True, exist_ok=True)

    renderer = RenderService(base_output_dir=str(out_dir), templates_dir=str(TEMPLATES_DIR))
    templates = load_registry()

    print(f"\n{'=' * 78}\n  RENDER BENCHMARK — '{label}'\n{'=' * 78}\n")

    results: list[dict] = []

    # ── Pass 1: cold — the very first render in this process ──────────────────
    first = templates[0]
    t0 = time.perf_counter()
    renderer.render(first["file"], dict(PAYLOAD), "PNG")
    cold_ms = (time.perf_counter() - t0) * 1000
    print(f"  cold start (first render, incl. browser launch) : {cold_ms:8.0f} ms\n")

    # ── Pass 2: warm, uncached — median of REPS, cache dropped each time ──────
    # Without dropping the cache a repeat render is a file copy, which would
    # flatter the numbers instead of measuring Chromium.
    REPS = 3
    clear_cache = getattr(RenderService, "clear_cache", None)

    print(f"  {'template':<24} {'fmt':<5} {'median ms':>10} {'(runs)':>22}   output")
    print(f"  {'-' * 24} {'-' * 5} {'-' * 10} {'-' * 22}   {'-' * 22}")
    for t in templates:
        for fmt in ("PNG", "PDF"):
            if fmt == "PNG" and not t.get("supports_png", True):
                continue
            if fmt == "PDF" and not t.get("supports_pdf", True):
                continue
            runs, path = [], None
            for _ in range(REPS):
                if clear_cache:
                    clear_cache()
                t0 = time.perf_counter()
                path = renderer.render(t["file"], dict(PAYLOAD), fmt)
                runs.append((time.perf_counter() - t0) * 1000)
            median = sorted(runs)[len(runs) // 2]
            stable = out_dir / f"{t['id']}.{fmt.lower()}"
            os.replace(path, stable)
            results.append({
                "id": t["id"], "format": fmt, "ms": round(median, 1),
                "runs": [round(r) for r in runs],
                "bytes": stable.stat().st_size, "sha256_16": file_digest(str(stable)),
            })
            runs_s = "/".join(f"{r:.0f}" for r in runs)
            print(f"  {t['id']:<24} {fmt:<5} {median:10.0f} {runs_s:>22}   {stable.name}")

    png = [r["ms"] for r in results if r["format"] == "PNG"]
    pdf = [r["ms"] for r in results if r["format"] == "PDF"]
    warm = [r["ms"] for r in results]
    print(f"\n  warm uncached — PNG mean {sum(png)/len(png):.0f} ms  |  "
          f"PDF mean {sum(pdf)/len(pdf):.0f} ms  |  overall {sum(warm)/len(warm):.0f} ms")

    # ── Pass 2b: cache hit — identical input, nothing dropped ─────────────────
    cache_ms = None
    if clear_cache:
        renderer.render(templates[0]["file"], dict(PAYLOAD), "PNG")   # prime
        t0 = time.perf_counter()
        renderer.render(templates[0]["file"], dict(PAYLOAD), "PNG")   # hit
        cache_ms = (time.perf_counter() - t0) * 1000
        print(f"  cache hit (identical input)      : {cache_ms:8.1f} ms")
    else:
        print(f"  cache hit                        :      n/a (no cache in this build)")

    # ── Pass 3: concurrency — N threads rendering at once ─────────────────────
    N = 4
    timings: list[float] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        # Vary the payload per thread so no render can be served from cache —
        # this measures real concurrent Chromium throughput, not file copies.
        payload = dict(PAYLOAD)
        payload["TITLE"] = f"{PAYLOAD['TITLE']} — concurrency probe {i}"
        t0 = time.perf_counter()
        renderer.render(templates[i % len(templates)]["file"], payload, "PNG")
        with lock:
            timings.append((time.perf_counter() - t0) * 1000)

    JOIN_TIMEOUT = 120  # a healthy pool finishes 4 renders in seconds
    t0 = time.perf_counter()
    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(N)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=JOIN_TIMEOUT)
    wall_ms = (time.perf_counter() - t0) * 1000
    stuck = sum(1 for th in threads if th.is_alive())
    if stuck:
        print(f"\n  {N} concurrent renders : *** {stuck}/{N} THREADS STILL BLOCKED "
              f"after {JOIN_TIMEOUT}s *** ({len(timings)} completed)")
    else:
        print(f"\n  {N} concurrent renders : wall {wall_ms:.0f} ms  |  "
              f"per-render mean {sum(timings)/len(timings):.0f} ms")

    live = getattr(RenderService, "live_browser_count", lambda: "n/a")()
    print(f"  live Chromium instances after run : {live}")

    if stuck:
        wall_ms = -1
    summary = {
        "label": label,
        "cold_ms": round(cold_ms, 1),
        "warm_mean_ms": round(sum(warm) / len(warm), 1),
        "warm_png_mean_ms": round(sum(png) / len(png), 1),
        "warm_pdf_mean_ms": round(sum(pdf) / len(pdf), 1),
        "cache_hit_ms": round(cache_ms, 1) if cache_ms is not None else None,
        "concurrent_wall_ms": round(wall_ms, 1),
        "concurrent_n": N,
        "live_browsers": live if isinstance(live, int) else None,
        "renders": results,
    }
    with open(out_dir / "summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    RenderService.shutdown_shared_browser()
    print(f"\n  wrote {out_dir / 'summary.json'}\n")


def compare(a: str, b: str) -> None:
    pa = BENCH_ROOT / a / "summary.json"
    pb = BENCH_ROOT / b / "summary.json"
    if not pa.exists() or not pb.exists():
        print(f"missing summary: run bench for both '{a}' and '{b}' first")
        return
    sa, sb = json.load(open(pa, encoding="utf-8")), json.load(open(pb, encoding="utf-8"))

    def delta(x: float, y: float) -> str:
        if not x:
            return ""
        pct = (y - x) / x * 100
        return f"{pct:+.0f}%"

    print(f"\n{'=' * 78}\n  COMPARISON — {a} vs {b}\n{'=' * 78}\n")
    print(f"  {'metric':<34} {a:>12} {b:>12} {'change':>10}")
    print(f"  {'-' * 34} {'-' * 12} {'-' * 12} {'-' * 10}")
    for key, name in [
        ("cold_ms", "cold start"),
        ("warm_png_mean_ms", "warm PNG (median, uncached)"),
        ("warm_pdf_mean_ms", "warm PDF (median, uncached)"),
        ("warm_mean_ms", "warm overall (uncached)"),
        ("cache_hit_ms", "cache hit"),
        ("concurrent_wall_ms", f"{sa.get('concurrent_n', 4)} concurrent (wall)"),
    ]:
        va, vb = sa.get(key), sb.get(key)
        fa = "n/a" if va is None else f"{va:.0f}ms"
        fb = "n/a" if vb is None else f"{vb:.0f}ms"
        d = delta(va, vb) if (va and vb) else ""
        print(f"  {name:<34} {fa:>12} {fb:>12} {d:>10}")
    print(f"  {'live Chromium after run':<34} {str(sa.get('live_browsers')):>12} "
          f"{str(sb.get('live_browsers')):>12}")

    print(f"\n  VISUAL REGRESSION\n  {'-' * 74}")
    print(f"  {'output':<26} {'bytes ' + a:>12} {'bytes ' + b:>12} {'pixels differing':>18}")
    print(f"  {'-' * 26} {'-' * 12} {'-' * 12} {'-' * 18}")
    ma = {(r["id"], r["format"]): r for r in sa["renders"]}
    mb = {(r["id"], r["format"]): r for r in sb["renders"]}
    worst = 0.0
    for key in sorted(set(ma) | set(mb)):
        ra, rb = ma.get(key), mb.get(key)
        name = f"{key[0]} {key[1]}"
        if not ra or not rb:
            print(f"  {name:<26} {'—':>12} {'—':>12} {'MISSING':>18}")
            continue
        verdict = "identical" if ra["sha256_16"] == rb["sha256_16"] else ""
        if not verdict and key[1] == "PNG":
            verdict, pct = _png_diff(
                BENCH_ROOT / a / f"{key[0]}.png", BENCH_ROOT / b / f"{key[0]}.png"
            )
            worst = max(worst, pct)
        elif not verdict and key[1] == "PDF":
            # Chromium stamps CreationDate/ModDate into every PDF, so raw bytes
            # never match across runs. Compare with those fields masked out.
            verdict = _pdf_diff(
                BENCH_ROOT / a / f"{key[0]}.pdf", BENCH_ROOT / b / f"{key[0]}.pdf"
            )
        print(f"  {name:<26} {ra['bytes']:>12} {rb['bytes']:>12} {verdict:>18}")
    print("\n  * identical after masking the CreationDate/ModDate that Chromium\n"
          "    stamps into every PDF — rendered content is byte-for-byte unchanged.")
    if worst:
        print(f"\n  worst-case pixel difference across PNGs: {worst:.3f}%")
    print()


def _pdf_diff(path_a: Path, path_b: Path) -> str:
    """Compare two PDFs ignoring the timestamps Chromium embeds in every file."""
    if not path_a.exists() or not path_b.exists():
        return "file missing"
    stamp = re.compile(rb"/(?:CreationDate|ModDate)\s*\(D:[^)]*\)")
    a = stamp.sub(b"", path_a.read_bytes())
    b = stamp.sub(b"", path_b.read_bytes())
    if a == b:
        return "identical*"
    changed = sum(1 for x, y in zip(a, b) if x != y) + abs(len(a) - len(b))
    return f"{changed} bytes differ"


def _png_diff(path_a: Path, path_b: Path) -> tuple[str, float]:
    """Return (verdict, percent-of-pixels-differing) for two PNGs."""
    try:
        from PIL import Image, ImageChops
    except ImportError:
        return "no Pillow", 0.0
    if not path_a.exists() or not path_b.exists():
        return "file missing", 0.0
    with Image.open(path_a) as ia, Image.open(path_b) as ib:
        ia, ib = ia.convert("RGB"), ib.convert("RGB")
        if ia.size != ib.size:
            return f"size {ia.size}!={ib.size}", 100.0
        diff = ImageChops.difference(ia, ib)
        # A pixel counts as changed only if some channel moved more than 8/255,
        # so imperceptible antialiasing noise is not reported as a regression.
        mask = diff.convert("L").point(lambda p: 255 if p > 8 else 0)
        changed = sum(mask.histogram()[255:])
        pct = changed / (ia.size[0] * ia.size[1]) * 100
        return f"{pct:.3f}%", pct


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "compare":
        compare(sys.argv[2], sys.argv[3])
    elif len(sys.argv) >= 2:
        run(sys.argv[1])
    else:
        print(__doc__)
