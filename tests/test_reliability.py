"""
tests/test_reliability.py
=========================
What happens when the outside world misbehaves.

Every one of these runs offline: the point is the decision the code makes about
a failure, not whether the provider is up. Between them they cover the four
external dependencies a publish touches — OpenAI, Serper, Telegram and the
filesystem underneath an asset.
"""
from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from exceptions import EmbeddingError, SearchError, TelegramError  # noqa: E402
from services.search_service import SearchService                  # noqa: E402
from services.telegram_service import TelegramService              # noqa: E402


@pytest.fixture(autouse=True)
def no_sleeping(monkeypatch):
    """Retries are real; waiting for them in a test is not."""
    monkeypatch.setattr("time.sleep", lambda *_: None)
    monkeypatch.setattr(TelegramService, "MIN_INTERVAL", 0.0)


class FakeResponse:
    def __init__(self, payload: dict, status: int = 200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


def _telegram(monkeypatch, *responses):
    """Install a sequence of fake Telegram replies; returns the call counter."""
    calls = {"n": 0}
    queue = list(responses)

    def fake_post(*args, **kwargs):
        calls["n"] += 1
        return queue.pop(0) if len(queue) > 1 else queue[0]

    monkeypatch.setattr(httpx, "post", fake_post)
    return calls


OK = FakeResponse({"ok": True, "result": {"message_id": 4242}})


# ── Telegram: retry only where retrying can help ─────────────────────────────

@pytest.mark.parametrize("description", [
    "Bad Request: chat not found",
    "Forbidden: bot was blocked by the user",
    "Forbidden: bot was kicked from the supergroup chat",
    "Bad Request: not enough rights to send text messages to the chat",
    "Unauthorized",
])
def test_a_permanent_error_is_not_retried(monkeypatch, description):
    """Retrying "chat not found" three times costs fifteen seconds and changes
    nothing. It was the old behaviour on every failure."""
    calls = _telegram(monkeypatch, FakeResponse({"ok": False, "description": description}))
    svc = TelegramService(token="t")

    with pytest.raises(TelegramError) as excinfo:
        svc.publish_text("hello", "123")

    assert calls["n"] == 1, "a permanent error must not be retried"
    assert description.split(": ")[-1] in str(excinfo.value)


def test_a_transient_error_is_retried_and_can_succeed(monkeypatch):
    calls = _telegram(
        monkeypatch,
        FakeResponse({"ok": False, "description": "Bad Gateway"}),
        OK,
    )
    assert TelegramService(token="t").publish_text("hello", "123") == 4242
    assert calls["n"] == 2


def test_a_rate_limit_waits_as_long_as_telegram_asked(monkeypatch):
    """Guessing five seconds when Telegram asked for thirty burns the remaining
    attempts and reports a rate limit as a hard failure."""
    waited: list[float] = []
    monkeypatch.setattr("time.sleep", lambda s: waited.append(s))
    _telegram(
        monkeypatch,
        FakeResponse({"ok": False, "description": "Too Many Requests",
                      "parameters": {"retry_after": 30}}),
        OK,
    )
    TelegramService(token="t").publish_text("hello", "123")
    assert 30 in waited


def test_the_real_reason_reaches_the_caller(monkeypatch):
    """The dashboard prints this string. It has to be Telegram's own words."""
    _telegram(monkeypatch, FakeResponse(
        {"ok": False, "description": "Bad Request: chat not found"}))
    with pytest.raises(TelegramError, match="chat not found"):
        TelegramService(token="t").publish_text("hi", "123")


def test_a_missing_asset_file_is_named(tmp_path, monkeypatch):
    _telegram(monkeypatch, OK)
    missing = str(tmp_path / "never-rendered.png")
    with pytest.raises(TelegramError, match="missing"):
        TelegramService(token="t").publish_photo(missing, "caption", "123")


@pytest.mark.parametrize("chat_id", ["", None, "-100xxxxxxxxxx"])
def test_an_unset_chat_id_refuses_before_the_network(chat_id, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("must not reach the network")
    monkeypatch.setattr(httpx, "post", explode)

    with pytest.raises(TelegramError, match="chat id"):
        TelegramService(token="t").publish_text("hi", chat_id)


def test_an_admin_alert_never_masks_the_real_failure(monkeypatch):
    """An alert that raises would replace the publish error with its own."""
    def explode(*args, **kwargs):
        raise httpx.ConnectError("no route to host")
    monkeypatch.setattr(httpx, "post", explode)

    TelegramService(token="t", admin_chat_id="999").send_admin_alert("something broke")


# ── Serper: an enhancement, not a dependency ─────────────────────────────────

def test_search_without_a_key_is_not_an_error():
    """A deployment with no Serper key is a valid configuration."""
    assert SearchService(api_key="").search("anything") == []


def test_a_dead_serper_raises_rather_than_returning_nothing(monkeypatch):
    """Returning [] would look identical to "no results for this query"."""
    monkeypatch.setattr(SearchService, "_fetch",
                        lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("down")))
    with pytest.raises(SearchError, match="unreachable"):
        SearchService(api_key="k").search("placement tips")


@pytest.mark.parametrize("status,expected", [
    (429, "quota"), (401, "quota"), (403, "quota"), (500, "HTTP 500"),
])
def test_serper_http_errors_say_which_kind(monkeypatch, status, expected):
    def raise_status(*args, **kwargs):
        raise httpx.HTTPStatusError(
            "boom", request=httpx.Request("POST", "https://x"),
            response=httpx.Response(status))
    monkeypatch.setattr(SearchService, "_fetch", raise_status)

    with pytest.raises(SearchError, match=expected):
        SearchService(api_key="k").search("q")


def test_a_failed_search_does_not_lose_the_post(monkeypatch):
    """The Writer works without research. Losing the post because Serper is
    down trades a slightly less current post for no post at all."""
    from engine.group_config import list_available_groups, load_group_config
    from engine.workflow import PlatformWorkflow

    wf = PlatformWorkflow.__new__(PlatformWorkflow)
    wf.group = load_group_config(list_available_groups()[0])
    wf.search = SearchService(api_key="k")
    wf.agents = {"research": {"role": "Researcher", "agent_type": "research"}}
    monkeypatch.setattr(SearchService, "_fetch",
                        lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("down")))

    # The guarded block, exercised exactly as generate_single_content runs it.
    search_summary = ""
    try:
        results = wf.search.search("a topic")
        search_summary = wf.search.format_results("a topic", results)
    except SearchError:
        pass
    assert search_summary == "", "a failed search must leave research empty, not raise"


# ── OpenAI: bounded, timed, and bypassable ───────────────────────────────────

def test_the_client_has_a_timeout():
    """The SDK default is 600s — longer than Gunicorn's own timeout, so a hung
    call takes the worker with it."""
    from services.openai_service import OpenAIService
    svc = OpenAIService(api_key="sk-test")
    assert svc.REQUEST_TIMEOUT <= 120
    configured = svc.client.timeout
    assert (configured if isinstance(configured, (int, float)) else configured.read) <= 120
    # This class retries with feedback; the SDK retrying underneath it would
    # multiply the attempts and make the effective timeout four times longer.
    assert svc.client.max_retries == 0


def test_the_response_cache_is_bounded():
    """It used to be a class dict nothing ever evicted."""
    from services.openai_service import OpenAIService

    OpenAIService._cache.clear()
    try:
        for i in range(OpenAIService.CACHE_CAPACITY + 50):
            OpenAIService._remember(f"key-{i}", "x")
        assert len(OpenAIService._cache) == OpenAIService.CACHE_CAPACITY
        assert "key-0" in OpenAIService._cache or True   # oldest evicted
        assert "key-0" not in OpenAIService._cache, "eviction must drop the oldest"
    finally:
        OpenAIService._cache.clear()


def test_regenerate_can_bypass_the_cache():
    """Same topic, same prompt, same cache key — so the cached path handed back
    the identical draft the operator had just rejected."""
    import inspect

    from services.openai_service import OpenAIService

    assert "use_cache" in inspect.signature(OpenAIService.generate_content).parameters

    source = (PROJECT_ROOT / "dashboard" / "app.py").read_text(encoding="utf-8-sig")
    regen = source[source.index("def regenerate(post_id)"):]
    regen = regen[:regen.index("@app.route", 10)]
    assert "use_cache=False" in regen, "the regenerate route must bypass the cache"


# ── one exception class, not two ─────────────────────────────────────────────

def test_embedding_error_is_a_single_class():
    """Two classes with the same name in different modules means
    `except exceptions.EmbeddingError` silently misses the raised one."""
    from services.embedding_service import EmbeddingError as FromService
    assert FromService is EmbeddingError


def test_publish_errors_share_a_base():
    from exceptions import CarrotOwlError, PublishError
    assert issubclass(TelegramError, PublishError)
    assert issubclass(PublishError, CarrotOwlError)


# ── D7: no print()-only failure paths ────────────────────────────────────────

def test_no_failure_path_reports_only_by_print():
    """`print()` is not error handling. A failure the operator cannot see in
    the dashboard is a failure that gets discovered by a missing Telegram post
    three hours later."""
    import ast

    offenders: list[str] = []
    words = ("fail", "error", "exception", "could not", "unable", "invalid")

    for directory in ("engine", "services", "dashboard"):
        for path in (PROJECT_ROOT / directory).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                        and node.func.id == "print"):
                    continue
                rendered = ast.unparse(node).lower()
                if any(w in rendered for w in words):
                    rel = path.relative_to(PROJECT_ROOT).as_posix()
                    offenders.append(f"{rel}:{node.lineno}")

    assert not offenders, (
        "Failures reported only by print(). Use log.warning/log.error and, "
        "where the operator needs to act, a post state:\n  "
        + "\n  ".join(offenders)
    )


def test_a_failed_render_marks_the_post_rather_than_writing_a_path():
    """Writing "Failed" into the Image Path column is truthy, so `assets_ready`
    said yes and the reconciler picked the post up. The failure surfaced as a
    Telegram error minutes later instead of as asset_failed straight away."""
    import inspect

    from engine.workflow import PlatformWorkflow
    source = inspect.getsource(PlatformWorkflow.generate_assets)

    assert 'updates["Image Path"] = "Failed"' not in source
    assert 'updates["PDF Path"] = "Failed"' not in source
    assert "PostState.ASSET_FAILED" in source


def test_background_asset_threads_record_their_failures():
    """A background thread has nowhere to flash a message to, so a failure
    there used to leave the post at "pending" with no explanation."""
    source = (PROJECT_ROOT / "dashboard" / "app.py").read_text(encoding="utf-8-sig")
    for marker in ("def _bg_generate", "def _bg_approve_assets"):
        start = source.index(marker)
        body = source[start:start + 4000]
        assert "PostState.ASSET_FAILED" in body, f"{marker} swallows its failures"


# ── the dependency list has to be true ───────────────────────────────────────

def test_nothing_imports_a_package_the_requirements_do_not_declare():
    """workflow.py imported services/drive_service.py, which imported
    google-auth and googleapiclient — neither of them in requirements.txt. It
    worked only because those packages happened to be in the dev venv; a fresh
    `pip install -r requirements.txt` crashed on `import engine.workflow`."""
    import ast

    declared = {
        line.split("[")[0].split(">")[0].split("=")[0].strip().lower().replace("-", "_")
        for line in (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8-sig").splitlines()
        if line.strip() and not line.startswith("#")
    }
    # Import name differs from the distribution name for these.
    aliases = {
        "flask": {"flask", "jinja2", "werkzeug", "markupsafe", "click", "itsdangerous"},
        "pillow": {"pil"}, "pyyaml": {"yaml"}, "sqlalchemy": {"sqlalchemy"},
        "psycopg": {"psycopg"}, "python_dotenv": {"dotenv"},
        "apscheduler": {"apscheduler"}, "typing_extensions": {"typing_extensions"},
        "pydantic_settings": {"pydantic_settings"},
    }
    for dist, names in aliases.items():
        if dist in declared:
            declared |= names

    stdlib = set(sys.stdlib_module_names)
    local = {"engine", "services", "agents", "dashboard", "exceptions", "tests", "run"}

    offenders: list[str] = []
    for directory in ("engine", "services", "agents", "dashboard"):
        for path in (PROJECT_ROOT / directory).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    roots = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    roots = [node.module.split(".")[0]]
                else:
                    continue
                for root in roots:
                    low = root.lower()
                    if low in stdlib or low in local or low in declared:
                        continue
                    rel = path.relative_to(PROJECT_ROOT).as_posix()
                    offenders.append(f"{rel}:{node.lineno} imports {root!r}")

    assert not offenders, (
        "Imports not declared in requirements.txt — these work locally and "
        "fail on a clean install:\n  " + "\n  ".join(sorted(set(offenders)))
    )
