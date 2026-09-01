"""
tests/test_reconciler.py
========================
The publish loop.

These use a fake workflow, so nothing reaches Telegram. What they check is the
behaviour the per-post scheduler could not give: the tenant comes from the row,
a post cannot publish before its assets exist, and a post cannot be sent twice.

Run these with the dashboard stopped. The reconciler is deliberately global —
it claims due posts for every tenant, which is the whole point of it — so a
dev server pointed at the same database will claim these fixtures first and the
tests will fail on posts that were, correctly, already published.
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("FLASK_SECRET_KEY", "test-secret-not-used-for-anything")

from dashboard.jobs import PublishError, preflight, publish_due_posts  # noqa: E402
from services.storage import repositories as repo                      # noqa: E402
from services.storage.models import PostState                          # noqa: E402


def _database_available() -> bool:
    import time
    from services.storage.db import healthcheck
    for attempt in range(3):
        try:
            if healthcheck()[0]:
                return True
        except Exception:
            pass
        if attempt < 2:
            time.sleep(2)
    return False


needs_db = pytest.mark.skipif(
    not _database_available(), reason="no reachable DATABASE_URL")


# ── preflight (offline) ──────────────────────────────────────────────────────

def test_preflight_refuses_a_post_with_no_chat():
    with pytest.raises(PublishError, match="No Telegram chat"):
        preflight({"Group": "g", "Generated Content": "hi"}, chat_id="")


def test_preflight_refuses_empty_content():
    with pytest.raises(PublishError, match="no content"):
        preflight({"Group": "g", "Generated Content": "   "}, chat_id="123")


@pytest.mark.parametrize("content_type,key", [("pdf", "PDF Path"), ("image", "Image Path")])
@pytest.mark.parametrize("value", [None, "", "N/A", "pending", "Failed"])
def test_preflight_refuses_a_declared_asset_that_is_not_there(content_type, key, value):
    post = {"Group": "g", "Generated Content": "body", "Content Type": content_type, key: value}
    with pytest.raises(PublishError, match="rendered"):
        preflight(post, chat_id="123")


def test_preflight_passes_a_complete_post():
    preflight(
        {"Group": "g", "Generated Content": "body", "Content Type": "image",
         "Image Path": "/tmp/a.png"},
        chat_id="123",
    )


# ── the loop ─────────────────────────────────────────────────────────────────

class FakeWorkflow:
    """Records what it was asked to publish instead of calling Telegram."""

    def __init__(self, group_id: str, chat_id: str = "chat-123", succeed: bool = True):
        self.group = type("G", (), {"id": group_id})()
        self.chat_id = chat_id
        self.succeed = succeed
        self.published: list[tuple[str, str]] = []
        from services.storage.post_store import PostStore
        self.storage = PostStore()

    def _chat_id_for(self, group_id: str) -> str:
        return self.chat_id

    def publish_post(self, post_id, content, pdf_path=None, img_path=None):
        self.published.append((post_id, self.group.id))
        if not self.succeed:
            # The real publish_post records the reason on the row before it
            # returns False — see every failure path in engine/workflow.py. A
            # fake that just returns False leaves the post in PUBLISHING, which
            # the real one never does, and would have the tests describing
            # behaviour the system does not have.
            from services.storage.db import session_scope as _scope
            with _scope() as s:
                repo.mark_publish_failed(s, post_id, "telegram refused the message")
            return False
        with_session = __import__("services.storage.db", fromlist=["session_scope"]).session_scope
        with with_session() as s:
            repo.mark_published(s, post_id, f"tg-{post_id[:6]}")
        return True


@pytest.fixture
def tenants():
    suffix = uuid.uuid4().hex[:8]
    a, b = f"rec_a_{suffix}", f"rec_b_{suffix}"
    yield a, b
    from services.storage.db import session_scope
    with session_scope() as s:
        for gid in (a, b):
            for post in repo.list_posts(s, gid):
                repo.delete_post(s, post.id)


def _make(group_id: str, **fields):
    from services.storage.db import session_scope
    with session_scope() as s:
        return repo.create_post(s, group_id, **fields).id


PAST = lambda m=5: datetime.now(timezone.utc) - timedelta(minutes=m)      # noqa: E731
FUTURE = lambda h=2: datetime.now(timezone.utc) + timedelta(hours=h)      # noqa: E731


@needs_db
def test_a_due_post_publishes(tenants):
    a, _ = tenants
    post_id = _make(a, state=PostState.APPROVED, scheduled_for=PAST(),
                    content="body", topic="t")
    workflows = {a: FakeWorkflow(a)}

    summary = publish_due_posts(lambda gid: workflows[gid])
    assert summary.get("published") == 1
    assert workflows[a].published == [(post_id, a)]

    from services.storage.db import session_scope
    with session_scope() as s:
        assert repo.get_post(s, post_id).state == PostState.PUBLISHED


@needs_db
def test_each_post_goes_to_its_own_group(tenants):
    """The defect the per-post scheduler had: a scheduler thread has no
    session, so every post went to the default community's chat."""
    a, b = tenants
    id_a = _make(a, state=PostState.APPROVED, scheduled_for=PAST(), content="A")
    id_b = _make(b, state=PostState.APPROVED, scheduled_for=PAST(), content="B")

    workflows = {a: FakeWorkflow(a), b: FakeWorkflow(b)}
    publish_due_posts(lambda gid: workflows[gid])

    assert workflows[a].published == [(id_a, a)]
    assert workflows[b].published == [(id_b, b)]


@needs_db
def test_a_post_whose_asset_is_missing_is_not_published(tenants):
    a, _ = tenants
    _make(a, state=PostState.APPROVED, scheduled_for=PAST(),
          content="body", wants_image=True)          # no image_path
    workflows = {a: FakeWorkflow(a)}

    summary = publish_due_posts(lambda gid: workflows[gid])
    assert summary.get("published", 0) == 0
    assert workflows[a].published == []


@needs_db
def test_a_future_post_waits(tenants):
    a, _ = tenants
    _make(a, state=PostState.APPROVED, scheduled_for=FUTURE(), content="body")
    workflows = {a: FakeWorkflow(a)}

    publish_due_posts(lambda gid: workflows[gid])
    assert workflows[a].published == []


@needs_db
def test_a_second_pass_does_not_resend(tenants):
    a, _ = tenants
    _make(a, state=PostState.APPROVED, scheduled_for=PAST(), content="body")
    workflows = {a: FakeWorkflow(a)}

    publish_due_posts(lambda gid: workflows[gid])
    publish_due_posts(lambda gid: workflows[gid])
    assert len(workflows[a].published) == 1, "a published post must never be sent again"


@needs_db
def test_a_rejected_post_is_never_picked_up(tenants):
    """Rejection needs no job cleanup: the post simply stops matching."""
    a, _ = tenants
    _make(a, state=PostState.REJECTED, scheduled_for=PAST(), content="body")
    workflows = {a: FakeWorkflow(a)}

    publish_due_posts(lambda gid: workflows[gid])
    assert workflows[a].published == []


@needs_db
def test_a_failing_publish_records_the_reason(tenants):
    a, _ = tenants
    post_id = _make(a, state=PostState.APPROVED, scheduled_for=PAST(), content="body")

    def boom(gid):
        raise RuntimeError("telegram is down")

    summary = publish_due_posts(boom)
    assert summary["failed"] == 1

    from services.storage.db import session_scope
    with session_scope() as s:
        post = repo.get_post(s, post_id)
        assert post.state == PostState.PUBLISH_FAILED
        assert "telegram is down" in (post.error or "")


@needs_db
def test_a_post_with_no_chat_fails_rather_than_going_somewhere_else(tenants):
    a, _ = tenants
    post_id = _make(a, state=PostState.APPROVED, scheduled_for=PAST(), content="body")
    workflows = {a: FakeWorkflow(a, chat_id="")}

    publish_due_posts(lambda gid: workflows[gid])
    assert workflows[a].published == []

    from services.storage.db import session_scope
    with session_scope() as s:
        post = repo.get_post(s, post_id)
        assert post.state == PostState.PUBLISH_FAILED
        assert "chat" in (post.error or "").lower()


@needs_db
def test_a_post_stranded_mid_publish_is_returned_to_the_queue(tenants):
    """A process killed between claim and send must not park the post forever."""
    from services.storage.db import session_scope
    a, _ = tenants
    post_id = _make(a, state=PostState.PUBLISHING, scheduled_for=PAST(), content="body")
    with session_scope() as s:
        repo.get_post(s, post_id).updated_at = datetime.now(timezone.utc) - timedelta(hours=1)

    workflows = {a: FakeWorkflow(a)}
    summary = publish_due_posts(lambda gid: workflows[gid])
    assert summary["released"] >= 1
    assert workflows[a].published == [(post_id, a)]


# ── several communities sharing one slot ─────────────────────────────────────
#
# The question these answer: if four communities each have a post due at 08:00
# and the host was asleep until the cron call woke it, does one pass deliver all
# four — each to its own chat — or does the first one win and the rest wait for
# the next tick?

@pytest.fixture
def many_tenants():
    """Four communities, cleaned up afterwards."""
    suffix = uuid.uuid4().hex[:8]
    ids = [f"rec_m{i}_{suffix}" for i in range(4)]
    yield ids
    from services.storage.db import session_scope
    with session_scope() as s:
        for gid in ids:
            for post in repo.list_posts(s, gid):
                repo.delete_post(s, post.id)


@needs_db
def test_four_communities_due_at_the_same_instant_all_publish(many_tenants):
    """One pass clears the whole slot, not one community per tick."""
    slot = PAST(2)                      # every post due at the same moment
    posts = {gid: _make(gid, state=PostState.APPROVED, scheduled_for=slot,
                        content=f"body for {gid}", topic="shared slot")
             for gid in many_tenants}
    workflows = {gid: FakeWorkflow(gid, chat_id=f"chat-{gid}")
                 for gid in many_tenants}

    summary = publish_due_posts(lambda gid: workflows[gid])

    assert summary["published"] == 4, (
        f"only {summary['published']} of 4 went out in one pass: {summary}"
    )
    from services.storage.db import session_scope
    with session_scope() as s:
        for gid, pid in posts.items():
            assert repo.get_post(s, pid).state == PostState.PUBLISHED, (
                f"{gid} was left behind")


@needs_db
def test_each_of_the_four_reaches_its_own_chat(many_tenants):
    """A shared slot must not collapse four communities into one chat."""
    slot = PAST(2)
    posts = {gid: _make(gid, state=PostState.APPROVED, scheduled_for=slot,
                        content="body", topic="shared slot")
             for gid in many_tenants}
    workflows = {gid: FakeWorkflow(gid, chat_id=f"chat-{gid}")
                 for gid in many_tenants}

    publish_due_posts(lambda gid: workflows[gid])

    for gid, pid in posts.items():
        assert workflows[gid].published == [(pid, gid)], (
            f"{gid} received {workflows[gid].published}")


@needs_db
def test_one_community_failing_does_not_hold_up_the_others(many_tenants):
    """A broken chat or a Telegram error must cost that community only.

    Without per-post error handling inside the loop, the first failure would end
    the pass and every later community would silently wait for the next tick.
    """
    slot = PAST(2)
    broken = many_tenants[1]
    posts = {gid: _make(gid, state=PostState.APPROVED, scheduled_for=slot,
                        content="body", topic="shared slot")
             for gid in many_tenants}
    workflows = {gid: FakeWorkflow(gid, chat_id=f"chat-{gid}",
                                   succeed=(gid != broken))
                 for gid in many_tenants}

    summary = publish_due_posts(lambda gid: workflows[gid])

    assert summary["published"] == 3 and summary["failed"] == 1, summary
    from services.storage.db import session_scope
    with session_scope() as s:
        for gid, pid in posts.items():
            state = repo.get_post(s, pid).state
            if gid == broken:
                assert state == PostState.PUBLISH_FAILED
            else:
                assert state == PostState.PUBLISHED, f"{gid} was held up"


@needs_db
def test_a_second_pass_after_a_shared_slot_sends_nothing_again(many_tenants):
    """The cron service retrying after a timeout must not repost all four."""
    slot = PAST(2)
    for gid in many_tenants:
        _make(gid, state=PostState.APPROVED, scheduled_for=slot,
              content="body", topic="shared slot")
    workflows = {gid: FakeWorkflow(gid, chat_id=f"chat-{gid}")
                 for gid in many_tenants}

    first = publish_due_posts(lambda gid: workflows[gid])
    second = publish_due_posts(lambda gid: workflows[gid])

    assert first["published"] == 4
    assert second["published"] == 0, f"the retry resent posts: {second}"
    for gid in many_tenants:
        assert len(workflows[gid].published) == 1, (
            f"{gid} was sent {len(workflows[gid].published)} times")
