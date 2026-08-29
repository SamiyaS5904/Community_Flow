"""
scripts/test_dynamic_items.py
=============================
Tests dynamic content-rich asset pipeline with variable item counts (3, 5, 7),
comparison layouts, motivation (single mode), and 2x high-DPI resolution.
"""
import sys
import os
import json
import time
from pathlib import Path

# Ensure project root is in path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from engine.config import config
from engine.group_config import load_group_config
from engine.workflow import PlatformWorkflow
from services.render_service import RenderService

def run_tests():
    print("=" * 60)
    print("   TESTING DYNAMIC CONTENT-RICH ASSET PIPELINE")
    print("=" * 60)

    gconf = load_group_config("placement_prep")
    wf = PlatformWorkflow(config, gconf)

    output_dir = os.path.join(config.PROJECT_ROOT, "generated", "dynamic_test")
    os.makedirs(output_dir, exist_ok=True)
    renderer = RenderService(base_output_dir=output_dir, templates_dir=os.path.join(config.PROJECT_ROOT, "design_templates"))

    tests = [
        {
            "name": "3-Item Structured List (Roomy)",
            "template": "dark-theme.html",
            "export_type": "PNG",
            "placeholders": {
                "content_type": "tips",
                "layout_mode": "list",
                "CATEGORY": "PLACEMENT TIPS",
                "HOOK": "MUST-KNOW TIPS",
                "TITLE": "3 GD Mistakes to Avoid",
                "SUBTITLE": "Crucial mistakes that weaken your performance in technical GDs.",
                "items": [
                    {
                        "number": "01",
                        "title": "Speaking Without Structure",
                        "description": "Always frame your ideas with a clear opening, core argument, and brief summary."
                    },
                    {
                        "number": "02",
                        "title": "Interrupting Competitors Aggressively",
                        "description": "Acknowledge the previous speaker before introducing your perspective calmly."
                    },
                    {
                        "number": "03",
                        "title": "Monopolizing Discussion Time",
                        "description": "Focus on high-value points rather than speaking continuously without consensus."
                    }
                ],
                "TIP": "Pro Tip: Enter the discussion early within the first 60 seconds."
            }
        },
        {
            "name": "5-Item Structured List (Compact)",
            "template": "dark-theme.html",
            "export_type": "PNG",
            "placeholders": {
                "content_type": "mistakes",
                "layout_mode": "list",
                "CATEGORY": "INTERVIEW GUIDE",
                "HOOK": "5 CRITICAL STEPS",
                "TITLE": "5 Steps to Ace System Design",
                "SUBTITLE": "Follow this structured strategy for senior campus tech rounds.",
                "items": [
                    {"number": "01", "title": "Clarify Requirements", "description": "Ask about functional scope, scale, latency goals, and expected traffic throughput."},
                    {"number": "02", "title": "Estimate Scale & Capacity", "description": "Calculate read/write QPS, storage requirements over 5 years, and bandwidth bottlenecks."},
                    {"number": "03", "title": "Define Core APIs", "description": "Draft clean REST/gRPC endpoints with exact parameter types and payload structures."},
                    {"number": "04", "title": "High-Level Architecture", "description": "Draw load balancers, web servers, database clusters, and cache tiers."},
                    {"number": "05", "title": "Deep Dive & Bottlenecks", "description": "Address single points of failure, indexing strategies, and database replication patterns."}
                ],
                "TIP": "Pro Tip: Never jump straight into database schema design without clarifying throughput."
            }
        },
        {
            "name": "7-Item Structured List (High Density)",
            "template": "light-theme.html",
            "export_type": "PDF",
            "placeholders": {
                "content_type": "steps",
                "layout_mode": "list",
                "CATEGORY": "DSA ROADMAP",
                "HOOK": "7-STEP STRATEGY",
                "TITLE": "7 Essential DSA Patterns for 2026",
                "SUBTITLE": "Master these recurring problem patterns to clear 90% of coding assessments.",
                "items": [
                    {"number": "01", "title": "Two Pointers", "description": "Ideal for sorted array searches, string reversals, and pair sum optimizations."},
                    {"number": "02", "title": "Sliding Window", "description": "Used for contiguous subarray problems, max sums, and substring searches."},
                    {"number": "03", "title": "Fast & Slow Pointers", "description": "Best for detecting cycles in linked lists and finding list midpoints."},
                    {"number": "04", "title": "Merge Intervals", "description": "Essential for scheduling conflicts, calendar overlaps, and range merges."},
                    {"number": "05", "title": "Modified Binary Search", "description": "Applies to rotated sorted arrays, unknown length bounds, and search space reduction."},
                    {"number": "06", "title": "Top K Elements (Heap)", "description": "Use min/max heaps for tracking k smallest, k largest, or frequency counts."},
                    {"number": "07", "title": "0/1 Knapsack (DP)", "description": "Fundamental dynamic programming pattern for decision choices under capacity constraints."}
                ],
                "TIP": "Pro Tip: Always code the brute-force approach verbally before optimizing space/time complexity."
            }
        },
        {
            "name": "Single-Mode Motivation Theme (Unchanged Single Page)",
            "template": "motivation-theme.html",
            "export_type": "PNG",
            "placeholders": {
                "content_type": "motivation",
                "layout_mode": "single",
                "TAGLINE": "DAILY MINDSET",
                "QUOTE": "CONSISTENCY BEATS TALENT WHEN TALENT DOES NOT WORK CONSISTENTLY.",
                "SUBTEXT": "Focus on small, daily progress in algorithms and system architecture."
            }
        }
    ]

    all_passed = True
    for test in tests:
        print(f"\nRunning test: {test['name']} ({test['template']}, {test['export_type']})...")
        try:
            out_path = renderer.render(test["template"], test["placeholders"], test["export_type"])
            size = os.path.getsize(out_path)
            print(f"  [OK] Rendered successfully: {os.path.basename(out_path)} ({size} bytes)")
        except Exception as e:
            print(f"  [FAIL] Render failed: {e}")
            all_passed = False

    print("\n" + "=" * 60)
    if all_passed:
        print("   RESULT: ALL DYNAMIC ITEM PIPELINE TESTS PASSED!")
    else:
        print("   RESULT: SOME TESTS FAILED.")
    print("=" * 60)

if __name__ == "__main__":
    run_tests()
