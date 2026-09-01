"""
tests/test_cron_endpoint.py
===========================
The externally-driven publish trigger.

The in-process reconciler only ticks while the process is alive. On a host that
suspends an idle service the clock stops with it: measured on the live
deployment, posts due at 12:01 and 12:03 both published at 22:07 — ten hours
late, in the same minute the service woke up.

This endpoint lets an external scheduler drive the same reconciler pass. It is
the one route that cannot sit behind the session login, so its token check is
the only thing standing between the public internet and "publish everything
that is due". These tests exist to keep that check honest.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

URL = "/api/cron/publish"
TOKEN = "test-cron-token"


@pytest.fixture
def client(monkeypatch):
    import dashboard.app as app_module
    # Never touch the real reconciler — what is under test is the gate.
    monkeypatch.setattr(app_module.background_jobs, "publish_due_posts",
                        lambda _get_workflow: {"claimed": 2, "published": 2,
                                               "failed": 0, "released": 0, "skipped": 0})
    return app_module.app.test_client()


def test_a_server_with_no_token_configured_refuses(client, monkeypatch):
    """Absent configuration must close the endpoint, not open it."""
    monkeypatch.delenv("CRON_TOKEN", raising=False)
    r = client.post(URL, headers={"X-Cron-Token": "anything"})
    assert r.status_code == 503
    assert "CRON_TOKEN" in r.get_json()["error"]


def test_an_empty_token_setting_refuses(client, monkeypatch):
    """CRON_TOKEN="" must not become an empty password."""
    monkeypatch.setenv("CRON_TOKEN", "   ")
    assert client.post(URL, headers={"X-Cron-Token": "   "}).status_code == 503


def test_a_wrong_token_is_rejected(client, monkeypatch):
    monkeypatch.setenv("CRON_TOKEN", TOKEN)
    assert client.post(URL, headers={"X-Cron-Token": "wrong"}).status_code == 401


def test_no_token_is_rejected(client, monkeypatch):
    monkeypatch.setenv("CRON_TOKEN", TOKEN)
    assert client.post(URL).status_code == 401


def test_the_correct_token_runs_one_pass(client, monkeypatch):
    monkeypatch.setenv("CRON_TOKEN", TOKEN)
    r = client.post(URL, headers={"X-Cron-Token": TOKEN})
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True and body["published"] == 2


def test_a_query_parameter_token_also_works(client, monkeypatch):
    """Some free cron services cannot send headers."""
    monkeypatch.setenv("CRON_TOKEN", TOKEN)
    assert client.get(f"{URL}?token={TOKEN}").status_code == 200


def test_get_and_post_both_work(client, monkeypatch):
    """Cron services differ on which verb they send."""
    monkeypatch.setenv("CRON_TOKEN", TOKEN)
    for call in (client.get, client.post):
        assert call(URL, headers={"X-Cron-Token": TOKEN}).status_code == 200


def test_a_failing_pass_reports_instead_of_500ing_silently(monkeypatch):
    import dashboard.app as app_module
    monkeypatch.setenv("CRON_TOKEN", TOKEN)

    def boom(_get_workflow):
        raise RuntimeError("database went away")

    monkeypatch.setattr(app_module.background_jobs, "publish_due_posts", boom)
    r = app_module.app.test_client().post(URL, headers={"X-Cron-Token": TOKEN})
    assert r.status_code == 500
    body = r.get_json()
    assert body["ok"] is False and "database went away" in body["error"]


def test_the_token_is_compared_without_leaking_its_length():
    """A plain == comparison returns early on the first wrong byte, which is
    measurable over enough requests."""
    import inspect
    import dashboard.app as app_module
    source = inspect.getsource(app_module.cron_publish)
    assert "hmac.compare_digest" in source
    assert "== expected" not in source


def test_the_test_suite_never_starts_a_live_reconciler():
    """Importing the app must not start a thread that polls the real database.

    Several tests import dashboard.app to inspect a route. Starting the
    reconciler on import meant the suite ran a live poller against production
    Postgres — which would have published any post that happened to be due, to
    the real Telegram chat, mid-test. It also raced the storage tests over their
    own fixtures.
    """
    import dashboard.app as app_module

    assert app_module._UNDER_TEST is True
    assert not app_module.scheduler.running, (
        "the background scheduler is running during the test suite"
    )
    assert app_module.scheduler.get_jobs() == [], (
        "the publish reconciler is registered during the test suite"
    )
