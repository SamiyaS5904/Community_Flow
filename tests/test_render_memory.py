"""
tests/test_render_memory.py
===========================
The pool must give its memory back.

A Chromium is the largest single thing this process owns — measured at ~110 MB
across its three processes, plus ~110 MB for Playwright's node driver. Holding
one between renders was what took the deployed service over its instance's
memory limit and got it restarted mid-job.

These run offline with a stand-in browser: what is under test is the pool's
decision to let an idle browser go and to relaunch on the next job, not
Chromium itself.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from services import render_service as rs  # noqa: E402


class FakeBrowser:
    """Stands in for a Chromium, and records that it was closed."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakePlaywright:
    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


@pytest.fixture
def pool(monkeypatch):
    """A one-worker pool that launches stand-ins and idles out fast."""
    monkeypatch.setattr(rs, "_IDLE_SHUTDOWN", 0.3)
    monkeypatch.setattr(rs, "PLAYWRIGHT_INSTALLED", True)

    launched: list[tuple[FakePlaywright, FakeBrowser]] = []
    p = rs._RenderPool(1)

    def fake_launch():
        pair = (FakePlaywright(), FakeBrowser())
        launched.append(pair)
        with p._live_lock:
            p._live += 1
        return pair

    monkeypatch.setattr(p, "_launch", fake_launch)
    p.launched = launched
    yield p
    p._jobs.put(None)


def test_an_idle_browser_is_closed(pool):
    """After the idle window with no work, nothing is still held."""
    assert pool.submit(lambda b: "rendered") == "rendered"
    assert pool._live == 1, "a browser should be held immediately after a render"

    time.sleep(rs._IDLE_SHUTDOWN * 4)

    assert pool._live == 0, "an idle browser must not be held indefinitely"
    pw, browser = pool.launched[0]
    assert browser.closed, "the browser itself must be closed, not just forgotten"
    assert pw.stopped, "Playwright's driver process must be stopped too"


def test_the_next_render_relaunches(pool):
    """Releasing the browser costs a cold launch, not a broken pool."""
    pool.submit(lambda b: 1)
    time.sleep(rs._IDLE_SHUTDOWN * 4)
    assert pool._live == 0

    assert pool.submit(lambda b: 2) == 2, "the pool must still work after idling out"
    assert len(pool.launched) == 2, "the second render should have launched a fresh browser"
    assert pool._live == 1


def test_a_busy_pool_keeps_its_browser(pool):
    """Work arriving inside the idle window must not pay for a relaunch."""
    for _ in range(4):
        pool.submit(lambda b: None)
        time.sleep(rs._IDLE_SHUTDOWN / 3)

    assert len(pool.launched) == 1, "a steadily-used pool should launch exactly once"


def test_idle_shutdown_can_be_disabled():
    """A machine with the memory for it can opt out."""
    import importlib
    import os

    os.environ["RENDER_IDLE_SECONDS"] = "0"
    try:
        importlib.reload(rs)
        assert rs._IDLE_SHUTDOWN is None, "0 must mean 'block forever', not 'idle out at once'"
    finally:
        os.environ.pop("RENDER_IDLE_SECONDS", None)
        importlib.reload(rs)
