"""
tests/test_schedule_timezone.py
===============================
A scheduled time must come back the way it was typed.

Times are stored in UTC, which is right. The defect was that only one direction
converted: the approve route turned the operator's local time into UTC, while
the display formatted that UTC value with no conversion at all. A post set for
midnight IST came back reading 18:30 — and because that string is fed straight
into a `datetime-local` input, re-saving the form converted it again, moving the
post another 5 hours 30 minutes earlier on every edit.

These run offline. What is under test is the round trip, not the database.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from engine.group_config import load_group_config          # noqa: E402
from services.storage import post_store as ps              # noqa: E402

GROUP = "placement_prep"
FMT = "%Y-%m-%dT%H:%M"


def _approve_route_would_store(typed: str, group) -> datetime:
    """Mirror of dashboard.app._parse_local_schedule."""
    naive = datetime.strptime(typed, FMT)
    return naive.replace(tzinfo=group.tz).astimezone(timezone.utc)


@pytest.fixture
def group():
    return load_group_config(GROUP)


@pytest.mark.parametrize("typed", [
    "2026-09-01T00:00",   # midnight — the reported case
    "2026-09-01T09:30",
    "2026-09-01T23:59",
    "2026-12-31T00:15",   # crosses the year when converted to UTC
])
def test_a_scheduled_time_survives_the_round_trip(typed, group):
    """What the operator typed is what the form shows them afterwards."""
    stored = _approve_route_would_store(typed, group)
    shown = ps._fmt_local(stored, FMT, GROUP)
    assert shown == typed, (
        f"typed {typed}, stored {stored.isoformat()}, but the form shows {shown}"
    )


def test_resaving_an_untouched_form_does_not_move_the_post(group):
    """The compounding failure: every edit shifted the post by another 5.5h."""
    typed = "2026-09-01T00:00"
    stored = _approve_route_would_store(typed, group)

    for edit in range(1, 4):
        shown = ps._fmt_local(stored, FMT, GROUP)
        stored = _approve_route_would_store(shown, group)
        assert ps._fmt_local(stored, FMT, GROUP) == typed, (
            f"after {edit} save(s) with no change, the time moved to "
            f"{ps._fmt_local(stored, FMT, GROUP)}"
        )


def test_midnight_is_stored_on_the_previous_utc_day(group):
    """Sanity check on the direction of the offset, so a sign error is caught."""
    stored = _approve_route_would_store("2026-09-01T00:00", group)
    assert stored.strftime("%Y-%m-%dT%H:%M") == "2026-08-31T18:30"


def test_two_groups_render_the_same_instant_on_their_own_clocks():
    """Display is per-tenant: the conversion must use the post's own group."""
    instant = datetime(2026, 8, 31, 18, 30, tzinfo=timezone.utc)
    for gid in ("placement_prep", "cat_prep"):
        g = load_group_config(gid)
        expected = instant.astimezone(g.tz).strftime(FMT)
        assert ps._fmt_local(instant, FMT, gid) == expected


def test_a_naive_stored_value_is_read_as_utc():
    """Some rows predate timezone-aware storage; treating them as server-local
    would silently shift them."""
    naive = datetime(2026, 8, 31, 18, 30)
    aware = naive.replace(tzinfo=timezone.utc)
    assert ps._fmt_local(naive, FMT, GROUP) == ps._fmt_local(aware, FMT, GROUP)


def test_an_unset_schedule_renders_empty():
    assert ps._fmt_local(None, FMT, GROUP) == ""


def test_the_display_path_does_not_format_utc_directly():
    """Guard the fix itself: to_display must convert, not call strftime raw."""
    import inspect
    source = inspect.getsource(ps.to_display)
    assert '_fmt_dt(post.scheduled_for' not in source, (
        "Scheduled Time is being formatted without converting to the group's "
        "timezone — this is the original defect."
    )
    assert '_fmt_local(post.scheduled_for' in source
