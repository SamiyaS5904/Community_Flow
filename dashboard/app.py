"""
dashboard/app.py
=================
Flask Dashboard for Carrot Owl Content Platform.
"""
import sys
import os
import uuid
import time
import threading
import atexit
import hmac
import logging
from pathlib import Path
from functools import wraps
from datetime import datetime, timedelta, timezone
import json

log = logging.getLogger(__name__)

# Make platform importable
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from flask import Flask, render_template, request, redirect, url_for, flash, send_file, session, render_template_string, Response, send_from_directory, jsonify

from engine.config import config
from engine.workflow import PlatformWorkflow
from engine.prompts import render as render_prompt
from services.storage.models import PostState
from apscheduler.schedulers.background import BackgroundScheduler
from services.render_service import PLAYWRIGHT_INSTALLED

# ---------- Multi-Tenant Workflow Proxy ----------
DEFAULT_GROUP = "placement_prep"
#: Archetype used when a post has not chosen one. The list layout carries the
#: widest range of content, so it is the safe default.
DEFAULT_TEMPLATE = "archetypes/list.html"
STATEMENT_TEMPLATE = "archetypes/statement.html"


def _parse_local_schedule(value: str, group):
    """Read a `YYYY-MM-DDTHH:MM` from the form in the group's own timezone.

    The dashboard's datetime-local input has no zone, and the old code read
    every one of them as IST regardless of which community the post belonged to.
    """
    from datetime import datetime as _dt
    try:
        naive = _dt.strptime(str(value), "%Y-%m-%dT%H:%M")
    except (TypeError, ValueError):
        return None
    return naive.replace(tzinfo=group.tz).astimezone(timezone.utc)
WORKFLOWS = {}

def get_workflow(group_id=None):
    """Return the workflow for a tenant.

    Outside a request there is no session to read, and quietly falling back to
    the default group is how one community's content ended up published to
    another's chat. Background threads and scheduler jobs must pass group_id.
    """
    if not group_id:
        try:
            from flask import session
            group_id = session.get('active_group', DEFAULT_GROUP)
        except RuntimeError:
            raise RuntimeError(
                "get_workflow() was called with no group_id and outside a request "
                "context, so there is no tenant to resolve. Background threads and "
                "scheduler jobs must pass group_id explicitly."
            ) from None


    if group_id not in WORKFLOWS:
        import copy
        try:
            from engine.group_config import load_group_config
            gconf = load_group_config(group_id)
            c = copy.copy(config)
            c.ACTIVE_GROUP_ID = group_id
            c.TELEGRAM_CHAT_ID = gconf.telegram_chat_id
            c.TELEGRAM_ADMIN_CHAT_ID = gconf.telegram_admin_chat_id
            c.ACTIVE_GROUP_ID = group_id
            WORKFLOWS[group_id] = PlatformWorkflow(c, gconf)  # Step 1: pass GroupConfig
        except Exception as e:
            log.warning("Falling back to the default config for %r: %s", group_id, e)
            from engine.group_config import load_group_config
            try:
                fallback_gconf = load_group_config("placement_prep")
            except Exception:
                fallback_gconf = None
            if fallback_gconf:
                WORKFLOWS[group_id] = PlatformWorkflow(config, fallback_gconf)
            else:
                raise
    return WORKFLOWS[group_id]

class WorkflowProxy:
    def __getattr__(self, name):
        return getattr(get_workflow(), name)

workflow = WorkflowProxy()

# The post list is read straight from Postgres on every request.
#
# There used to be a 30-second in-process cache here, from when reads went to
# the Google Sheets API and cost a second each. Against Postgres it bought
# nothing and broke correctness: the dict is module-level, so each Gunicorn
# worker held its own copy. Deleting a post cleared the cache in the worker
# that served the delete; the next request landed on a different worker and
# that one still had the post. Deleted posts reappeared on refresh, edits
# looked like they had not saved, and a hard refresh "fixed" it only because it
# happened to hit the worker that knew.
#
# Checking whether the cache is still valid would cost the same round trip as
# just reading, so there is nothing left for it to save.
_ROW_CACHE: dict = {}
_ROW_CACHE_LOCK = threading.Lock()


def get_all_posts_cached(group_id: str) -> list:
    """Every post for the active tenant. The name is kept because a dozen call
    sites use it; there is no cache behind it any more."""
    try:
        from flask import session
        current_group = session.get("active_group", DEFAULT_GROUP)
    except RuntimeError:
        current_group = group_id or DEFAULT_GROUP
    return get_workflow(current_group).storage.get_all_posts(current_group)


def get_row_cache() -> dict:
    """Return a snapshot of the current {post_id: row_number} cache."""
    with _ROW_CACHE_LOCK:
        return dict(_ROW_CACHE)

def invalidate_sheet_cache():
    """Kept so the call sites do not all have to change. Only the row cache is
    left, and that one is genuinely per-process scratch."""
    with _ROW_CACHE_LOCK:
        _ROW_CACHE.clear()

_MEMBER_COUNT_CACHE = {"count": "--", "ts": 0, "group_id": None}
_MEMBER_COUNT_TTL = 300  # 5 minutes

def get_member_count_cached():
    now = time.time()
    try:
        from flask import session
        current_group = session.get('active_group', 'placement_prep')
    except RuntimeError:
        current_group = 'placement_prep'
        
    wf = get_workflow(current_group)
    if not wf.config.TELEGRAM_CHAT_ID:
        return "--"
        
    if now - _MEMBER_COUNT_CACHE["ts"] > _MEMBER_COUNT_TTL or _MEMBER_COUNT_CACHE["count"] == "--" or _MEMBER_COUNT_CACHE["group_id"] != current_group:
        try:
            _MEMBER_COUNT_CACHE["count"] = wf.telegram.get_member_count(wf.config.TELEGRAM_CHAT_ID)
        except Exception:
            _MEMBER_COUNT_CACHE["count"] = "--"
        _MEMBER_COUNT_CACHE["ts"] = now
        _MEMBER_COUNT_CACHE["group_id"] = current_group
    return _MEMBER_COUNT_CACHE["count"]

def background_generate_single(job_id, slot, recent_topics, group_id):
    def update_status(msg):
        JOB_STATUS[job_id] = msg
    try:
        wf = get_workflow(group_id)
        wf.generate_single_content(slot, recent_topics, save_to_sheets=True, status_callback=update_status)
        JOB_STATUS[job_id] = "Completed"
        invalidate_sheet_cache()
    except Exception as e:
        JOB_STATUS[job_id] = f"Failed: {str(e)}"
        invalidate_sheet_cache()

def background_generate_queue(job_id, date_str, group_id):
    def update_status(msg):
        JOB_STATUS[job_id] = msg
    try:
        wf = get_workflow(group_id)
        wf.generate_queue(date_str, status_callback=update_status)
        JOB_STATUS[job_id] = "Completed"
        invalidate_sheet_cache()
    except Exception as e:
        JOB_STATUS[job_id] = f"Failed: {str(e)}"
        invalidate_sheet_cache()


app = Flask(__name__)

# Fail closed: if FLASK_SECRET_KEY is not set, refuse to start.
# A missing key means any attacker can forge valid session cookies and bypass login.
_flask_secret = os.environ.get("FLASK_SECRET_KEY", "")
if not _flask_secret:
    raise RuntimeError(
        "FATAL: FLASK_SECRET_KEY environment variable is not set. "
        "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\" "
        "and add it to your .env file before starting the app."
    )
app.secret_key = _flask_secret

@app.errorhandler(404)
def page_not_found(e):
    return render_template_string("""
    {% extends "base.html" %}
    {% block content %}
    <div class="text-center py-5 bg-white rounded shadow-sm">
        <i class="bi bi-exclamation-octagon text-warning" style="font-size: 4rem;"></i>
        <h2 class="fw-bold mt-3 text-dark">404 - Page Not Found</h2>
        <p class="text-muted">The page you are looking for does not exist or has been moved.</p>
        <a href="/" class="btn btn-primary fw-bold mt-2">Back to Dashboard</a>
    </div>
    {% endblock %}
    """), 404

@app.errorhandler(500)
def internal_server_error(e):
    log.exception("Internal server error: %s", e)
    return render_template_string("""
    {% extends "base.html" %}
    {% block content %}
    <div class="text-center py-5 bg-white rounded shadow-sm">
        <i class="bi bi-shield-slash-fill text-danger" style="font-size: 4rem;"></i>
        <h2 class="fw-bold mt-3 text-dark">500 - Internal Server Error</h2>
        <p class="text-muted">Something went wrong on our end. The administrators have been notified.</p>
        <a href="/" class="btn btn-primary fw-bold mt-2">Back to Dashboard</a>
    </div>
    {% endblock %}
    """), 500

# Simple Authentication Decorator
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

@app.context_processor
def inject_groups():
    try:
        from engine.group_config import list_available_groups, load_group_config
        available = list_available_groups()
        groups = []
        for gid in available:
            try:
                gconf = load_group_config(gid)
                groups.append({"id": gid, "name": gconf.name})
            except:
                pass
        
        active_id = session.get('active_group', DEFAULT_GROUP)
        active_name = "Unknown Workspace"
        for g in groups:
            if g["id"] == active_id:
                active_name = g["name"]

        counts = _workflow_counts(active_id)
        healthy, detail = _reconciler_health()
        return dict(
            available_groups=groups,
            active_group_id=active_id,
            active_group_name=active_name,
            reconciler_ok=healthy,
            reconciler_detail=detail,
            **counts,
        )
    except Exception as e:
        log.warning("Template context could not be built: %s", e)
        return dict(available_groups=[], active_group_id=DEFAULT_GROUP,
                    active_group_name="Workspace", pending_count=0,
                    scheduled_count=0, reconciler_ok=False,
                    reconciler_detail="Dashboard context could not be built.")


#: The sentinels a path column carries when there is no file behind it.
#: "Failed" is the dangerous one: it is truthy, so `if post["Image Path"]`
#: reported an asset that does not exist.
_NO_FILE = {"n/a", "pending", "failed", "", "none"}


def _items_summary(wf, post_id: str) -> str:
    """The points that ended up on the graphic, so the caption can avoid them.

    Telling the caption writer to "not repeat the points" only works if it can
    see which points those are.
    """
    import json as _json
    path = os.path.join(wf.config.PROJECT_ROOT, "generated", "placeholders", f"{post_id}.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = _json.load(fh)
    except Exception:
        return "(not available)"

    lines = []
    for item in (data.get("items") or []):
        if isinstance(item, dict) and item.get("title"):
            lines.append(f"- {item['title']}")
    for key in ("TITLE", "HOOK", "SUBTITLE", "TIP"):
        if data.get(key):
            lines.append(f"- {key.title()}: {data[key]}")
    return "\n".join(lines) if lines else "(not available)"


def pdf_path_is_real(value) -> bool:
    """True when a path column actually points at a rendered file."""
    return str(value or "").strip().lower() not in _NO_FILE


def wants_json() -> bool:
    """Whether this request came from app.js rather than a browser navigation."""
    return (request.headers.get("X-Requested-With") == "fetch"
            or request.accept_mimetypes.best == "application/json")


def respond(message: str, *, ok: bool = True, detail: str = "",
            redirect_to: str = "index", status: int | None = None, **extra):
    """One reply for both callers of every action route.

    A fetch gets JSON and the page updates the row it changed; a plain form
    POST still gets the flash-and-redirect it always did. Without this the two
    diverge — which is how a route ends up working in the UI and silently
    doing nothing when JavaScript is off.
    """
    if wants_json():
        payload = {"ok": ok, "message": message, "detail": detail, **extra}
        return jsonify(payload), (status or (200 if ok else 400))
    flash(f"{message} {detail}".strip(), "success" if ok else "danger")
    return redirect(url_for(redirect_to, **({"tab": extra["tab"]} if "tab" in extra else {})))


def _workflow_counts(group_id: str) -> dict:
    """How much work is waiting, for the sidebar badges.

    Counted in the database rather than by loading every post: the sidebar
    renders on every page, and pulling the full post list to call len() on two
    filtered slices of it was the single most expensive thing on the page.
    """
    try:
        from services.storage.db import session_scope
        from services.storage import repositories as repo
        with session_scope() as s:
            return {
                "pending_count": repo.count_posts(s, group_id, [PostState.NEEDS_REVIEW]),
                "scheduled_count": repo.count_posts(s, group_id, [PostState.APPROVED]),
            }
    except Exception as exc:
        log.warning("Could not count pending work: %s", exc)
        return {"pending_count": 0, "scheduled_count": 0}


def _reconciler_health() -> tuple[bool, str]:
    """Whether posts are actually being published right now.

    The topbar used to show a hardcoded "Online" badge, which meant the page
    had loaded — not that anything was reaching Telegram. This reports the
    thing the operator actually cares about.
    """
    job = scheduler.get_job("publish-reconciler") if scheduler.running else None
    if job is None:
        return False, "The publish reconciler is not running; approved posts will not go out."
    when = getattr(job, "next_run_time", None)
    if when:
        return True, f"Publish reconciler active — next pass at {when:%H:%M:%S}."
    return True, "Publish reconciler active."

@app.route("/switch_group/<group_id>")
@login_required
def switch_group(group_id):
    session['active_group'] = group_id
    invalidate_sheet_cache()
    flash(f"Switched to workspace: {group_id}", "success")
    return redirect(url_for('index'))

@app.route("/add_group")
@login_required
def add_group():
    return render_template("add_group.html")

@app.route("/api/generate_group", methods=["POST"])
@login_required
def api_generate_group():
    niche = request.form.get("niche", "")
    audience = request.form.get("audience", "")
    
    try:
        wf = get_workflow()
        from agents.definitions import planner_agent
        
        prompt = render_prompt(
            "bootstrap/group_config", niche=niche, audience=audience
        )
        
        result_json = wf._call_agent_json(planner_agent(wf.group), prompt)
        # Handle case if it returns a list instead of dict
        if isinstance(result_json, list) and len(result_json) > 0:
            result_json = result_json[0]
            
        return {"success": True, "result": result_json}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.route("/save_group", methods=["POST"])
@login_required
def save_group():
    group_id = request.form.get("group_id", "").strip().lower().replace(" ", "_")
    name = request.form.get("name", "").strip()
    tagline = request.form.get("tagline", "").strip()
    chat_id_env = request.form.get("telegram_chat_id_env", "TELEGRAM_CHAT_ID").strip()
    color = request.form.get("primary_color", "#FF6B35").strip()
    hashtags = [h.strip() for h in request.form.get("hashtags", "").split(",") if h.strip()]
    tone = request.form.get("tone", "").strip()
    # Carried through from step 1. It was never read here, so interpolating it
    # into the generated YAML raised NameError on every submission and the
    # "Add AI Community" button could not create a group at all.
    audience = request.form.get("audience", "").strip() or f"Members of {name}"
    blueprint_text = request.form.get("blueprint_text", "").strip()
    
    if not group_id or not name:
        flash("Group ID and Name are required.", "danger")
        return redirect(url_for('add_group'))
        
    group_dir = config.GROUPS_DIR / group_id
    if group_dir.exists():
        flash(f"Workspace '{group_id}' already exists!", "danger")
        return redirect(url_for('add_group'))
        
    try:
        group_dir.mkdir(parents=True, exist_ok=True)
        
        # 1. Save config.yaml
        yaml_content = f"""group:
  id: {group_id}
  name: "{name}"
  tagline: "{tagline}"
  description: "Generated by AI"
  telegram_chat_id_env: {chat_id_env}
  telegram_admin_chat_id_env: TELEGRAM_ADMIN_CHAT_ID

brand:
  primary_color: "{color}"
  secondary_color: "#2D2D2D"
  accent_color: "#FFD700"
  footer: "Powered by AI Automation"
  hashtags_always: {json.dumps(hashtags)}

audience:
  description: "{audience}"
  tone: "{tone}"
  avoid: ["In conclusion", "As an AI"]

posting:
  posts_per_day: 3
  max_posts_per_day: 5
  approval_mode: true

content_categories:
  - id: "educational"
    name: "Educational Content"
    search_required: true
    frequency_weight: 3
  - id: "motivation"
    name: "Motivation & Tips"
    search_required: false
    frequency_weight: 1

cta:
  min_educational_before_cta: 2
  max_cta_per_day: 1
  available_ctas:
    - id: "join_channel"
      text: "Join our community for daily updates!"
      active: true

post_format:
  word_count:
    min: 150
    max: 400
  emoji_policy: "Use 2-3 emojis naturally."
"""
        with open(group_dir / "config.yaml", "w", encoding="utf-8") as f:
            f.write(yaml_content)
            
        # 2. Save blueprint
        with open(group_dir / "content_blueprint.md", "w", encoding="utf-8") as f:
            f.write(blueprint_text)
            
        # 3. Switch to new group
        session['active_group'] = group_id
        invalidate_sheet_cache()
        flash(f"Workspace '{name}' created successfully! You can now start generating content.", "success")
        return redirect(url_for('index'))
        
    except Exception as e:
        flash(f"Failed to create workspace: {e}", "danger")
        return redirect(url_for('add_group'))

from collections import OrderedDict


class _JobLog(OrderedDict):
    """The job store, with a lid on it.

    Every generation, render and publish writes an entry here and nothing ever
    removed one, so in a process that stays up for weeks this grew without
    limit — and /api/jobs serialised the whole thing on every poll. The entries
    are small, so this was never the cause of an out-of-memory restart, but it
    is the kind of growth that has no natural end.

    A few hundred is far more history than the dashboard shows; the oldest job
    beyond that is of no interest to anyone.
    """

    CAPACITY = 200

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self.move_to_end(key)
        while len(self) > self.CAPACITY:
            self.popitem(last=False)


JOB_STATUS = _JobLog()

from datetime import timezone as _utc

# The scheduler now runs one interval job, not a date-job per post, so it needs
# no tenant timezone of its own — it runs in UTC and each post's schedule is
# interpreted in its own group's zone when it is set.
scheduler = BackgroundScheduler(timezone=_utc.utc)
scheduler.start()
atexit.register(lambda: scheduler.shutdown())

from dashboard import jobs as background_jobs

# One reconciler for the whole process, instead of a date-job per post. It
# holds no state: every tick it asks the database which posts are approved,
# due and have their assets, so a restart loses nothing and a rejected post
# simply stops matching.
background_jobs.register(scheduler, get_workflow)

# Screens added from here on live in their own blueprint. get_workflow and
# login_required are passed in rather than imported, so a route module never
# imports this one back.
from dashboard.routes import cycles as cycles_routes  # noqa: E402
from dashboard.routes import topics as topics_routes    # noqa: E402
topics_routes.register(app, get_workflow, login_required)
cycles_routes.register(app, get_workflow, login_required)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        expected_password = os.environ.get("DASHBOARD_PASSWORD", "")
        if not expected_password:
            import logging
            logging.getLogger(__name__).warning(
                "DASHBOARD_PASSWORD is not set in .env — login is disabled. "
                "Set DASHBOARD_PASSWORD in your .env file."
            )
        if username == "admin" and expected_password and hmac.compare_digest(password, expected_password):
            session['logged_in'] = True
            flash("Logged in successfully.", "success")
            return redirect(url_for("index"))
        else:
            flash("Invalid credentials.", "danger")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.pop('logged_in', None)
    flash("Logged out successfully.", "info")
    return redirect(url_for("login"))

@app.route("/")
@login_required
def index():
    wf = get_workflow()
    group_id = wf.config.ACTIVE_GROUP_ID
    all_posts = get_all_posts_cached(group_id)
    
    today_str = datetime.now().strftime("%Y-%m-%d")
    week_ago_str = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    
    pending = [p for p in all_posts if p.get("Approval Status") == "pending"]
    approved = [p for p in all_posts if p.get("Approval Status") == "approved" and p.get("Publish Status") != "published"]
    missed = [p for p in all_posts if p.get("Approval Status") == "missed"]
    published = [p for p in all_posts if p.get("Publish Status") == "published"]
    failed = [p for p in all_posts if p.get("Publish Status") == "failed"]
    
    today_posts = [p for p in all_posts if p.get("Date") == today_str]
    week_posts = [p for p in all_posts if p.get("Date", "") >= week_ago_str]
    
    # Advanced Metrics
    planned_today = len([p for p in today_posts if p.get("Approval Status") != "rejected"])
    completed_today = len([p for p in today_posts if p.get("Publish Status") == "published"])
    progress_percent = int((completed_today / planned_today * 100)) if planned_today > 0 else 0
    
    # Calculate a default schedule time (tomorrow at 10 AM)
    tomorrow = datetime.now() + timedelta(days=1)
    default_schedule_time = tomorrow.replace(hour=10, minute=0).strftime("%Y-%m-%dT%H:%M")
    
    category_counts = {}
    for p in all_posts:
        cat = p.get("Content Type", "Unknown")
        category_counts[cat] = category_counts.get(cat, 0) + 1
        
    # Dashboard Next Scheduled Post Calculation
    now = datetime.now()
    future_posts = []
    for p in approved:
        try:
            st = datetime.strptime(p.get("Scheduled Time"), "%Y-%m-%dT%H:%M")
            if st > now - timedelta(hours=2):
                future_posts.append((st, p))
        except:
            pass
    
    future_posts.sort(key=lambda x: x[0])
    next_post = future_posts[0][1] if future_posts else None
    
    # Check job status
    job_status = "Not Queued"
    if next_post:
        # Queue state comes from the post itself now, not from a jobstore.
        if next_post.get("Assets Ready") is False:
            job_status = "Waiting on its asset"
        else:
            job_status = f"Queued for {next_post.get('Scheduled Time', '')[-5:]}"
            
    # Fetch Telegram Member Count (Cached)
    member_count = get_member_count_cached()
        
    # Load the archetype registry and count how often each has been used.
    template_counts = {}
    for p in all_posts:
        t_used = p.get("Template Used", "").strip()
        if t_used:
            template_counts[t_used] = template_counts.get(t_used, 0) + 1

    templates = []
    registry_path = Path("design_templates/registry.json")
    if registry_path.exists():
        with open(registry_path, "r", encoding="utf-8") as f:
            templates = json.load(f)
        for t in templates:
            t["usage_count"] = template_counts.get(t.get("file"), 0)

    # Generated asset counts
    pdfs_generated = len([p for p in all_posts if p.get("PDF Path") and p.get("PDF Path") not in ["N/A", "pending", "Failed", ""]])
    images_generated = len([p for p in all_posts if p.get("Image Path") and p.get("Image Path") not in ["N/A", "pending", "Failed", ""]])

    # Average generation time
    gen_times = []
    for p in all_posts:
        gt = p.get("Generation Time", "").replace("s", "").strip()
        try:
            if gt and gt != "N/A":
                gen_times.append(float(gt))
        except:
            pass
    avg_gen_time = round(sum(gen_times) / len(gen_times), 2) if gen_times else 0.0

    # Today's publishing timeline
    today_timeline = []
    for p in today_posts:
        time_val = p.get("Scheduled Time", "")
        if time_val and "T" in time_val:
            time_label = time_val.split("T")[1]
        else:
            time_label = p.get("Time", "ASAP")
        
        pub_status = p.get("Publish Status", "").lower()
        app_status = p.get("Approval Status", "").lower()
        
        if pub_status == "published":
            status_text = "Published"
            status_color = "success"
        elif pub_status == "failed":
            status_text = "Failed"
            status_color = "danger"
        elif app_status == "approved":
            status_text = "Scheduled"
            status_color = "warning"
        elif app_status == "pending":
            status_text = "Pending Approval"
            status_color = "info"
        elif app_status == "rejected":
            status_text = "Rejected"
            status_color = "secondary"
        else:
            status_text = "Planned"
            status_color = "secondary"
            
        today_timeline.append({
            "time": time_label,
            "topic": p.get("Topic", "Untitled"),
            "category": p.get("Content Type", "General"),
            "status": status_text,
            "color": status_color
        })
    today_timeline.sort(key=lambda x: x["time"])

    # Unique templates used today
    today_templates_used = list(set([p.get("Template Used") for p in today_posts if p.get("Template Used") and p.get("Template Used") != "N/A"]))

    # AI usage telemetry
    from services.openai_service import OpenAIService
    ai_stats = OpenAIService.get_stats()

    # Load placeholders for UI editing
    placeholders_dir = os.path.join(wf.config.PROJECT_ROOT, "generated", "placeholders")
    for posts_list in [pending, approved, missed]:
        for post in posts_list:
            post_id = post.get("Post ID")
            post_type = post.get("Content Type", "")
            t_name = post.get("Template Used", DEFAULT_TEMPLATE)
            if not t_name or t_name == "N/A":
                t_name = STATEMENT_TEMPLATE if ("motivation" in post_type.lower() or "quote" in post_type.lower()) else DEFAULT_TEMPLATE
                post["Template Used"] = t_name

            p_file = os.path.join(placeholders_dir, f"{post_id}.json")
            p_data = {}
            if os.path.exists(p_file):
                try:
                    with open(p_file, "r", encoding="utf-8") as f:
                        p_data = json.load(f)
                except Exception as e:
                    log.warning("Could not load placeholders for %s: %s", post_id[:8], e)
                    p_data = {}

            # If post requires assets (Image or PDF) and placeholders are empty, create default keys
            if (post_type.upper() in ["IMAGE", "PDF"] or post.get("Image Path") or post.get("PDF Path")) and not p_data:
                req_keys = wf._get_template_placeholders(t_name)
                for k in req_keys:
                    if k == "LOGO":
                        p_data[k] = "logo_light.png" if "dark" in t_name.lower() or "motivation" in t_name.lower() else "logo_dark.png"
                    elif k == "WEBSITE":
                        p_data[k] = "carrotowleducation.com"
                    elif k == "CTA":
                        p_data[k] = "Join @carrotowl"
                    elif k == "PAGE":
                        p_data[k] = "1"
                    elif k in ["TITLE", "QUOTE"]:
                        p_data[k] = post.get("Topic", "")
                    elif k in ["TAGLINE", "CATEGORY"]:
                        p_data[k] = post.get("Content Type", "PLACEMENT PREP").upper()
                    elif k in ["HOOK", "SUBTITLE", "SUBTEXT"]:
                        p_data[k] = post.get("Generated Content", "")[:120]
                    else:
                        p_data[k] = ""
                try:
                    os.makedirs(placeholders_dir, exist_ok=True)
                    with open(p_file, "w", encoding="utf-8") as f:
                        json.dump(p_data, f, indent=4)
                except Exception:
                    pass

            post["placeholders"] = p_data

    return render_template(
        "index.html", 
        all_posts=all_posts,
        pending=pending, 
        approved=approved, 
        missed=missed,
        published=published, 
        failed=failed, 
        today_posts=today_posts,
        week_posts=week_posts,
        planned_today=planned_today,
        completed_today=completed_today,
        progress_percent=progress_percent,
        next_post=next_post,
        job_status=job_status,
        total=len(all_posts),
        category_counts=category_counts,
        default_schedule_time=default_schedule_time,
        member_count=member_count,
        templates=templates,
        pdfs_generated=pdfs_generated,
        images_generated=images_generated,
        avg_gen_time=avg_gen_time,
        today_timeline=today_timeline,
        today_templates_used=today_templates_used,
        ai_stats=ai_stats,
        playwright_ok=PLAYWRIGHT_INSTALLED
    )

@app.route("/analytics")
@login_required
def analytics():
    from services.openai_service import OpenAIService
    group_id = workflow.config.ACTIVE_GROUP_ID
    all_posts = get_all_posts_cached(group_id)

    total = len(all_posts)
    published_posts = [p for p in all_posts if p.get("Publish Status") == "published"]
    failed_posts = [p for p in all_posts if p.get("Publish Status") == "failed"]
    pending_posts = [p for p in all_posts if p.get("Approval Status") == "pending"]

    today_str = datetime.now().strftime("%Y-%m-%d")
    week_ago_str = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    today_posts = [p for p in all_posts if p.get("Date") == today_str]
    week_posts = [p for p in all_posts if p.get("Date", "") >= week_ago_str]

    category_counts = {}
    for p in all_posts:
        cat = p.get("Content Type", "Unknown")
        category_counts[cat] = category_counts.get(cat, 0) + 1

    member_count = get_member_count_cached()

    # OpenAI usage stats from in-memory telemetry
    ai_stats = OpenAIService.get_stats()

    return render_template(
        "analytics.html",
        total=total,
        published=len(published_posts),
        failed=len(failed_posts),
        pending=len(pending_posts),
        today_count=len(today_posts),
        week_count=len(week_posts),
        category_counts=category_counts,
        member_count=member_count,
        ai_stats=ai_stats,
    )


@app.route("/generate", methods=["POST"])
@login_required
def generate():
    category = request.form.get("category", "motivation")
    custom_topic = request.form.get("topic", "").strip()
    include_pdf = request.form.get("include_pdf") == "on"
    include_image = request.form.get("include_image") == "on"
    
    # Use custom topic if provided, otherwise pass recent topics for AI to choose
    recent_topics = custom_topic if custom_topic else "Resume keywords, Email etiquette, TCS interview" 
    try:
        # Create a mock slot for single generation
        slot = {
            "category": category,
            "topic": custom_topic,
            "search_required": "news" in category.lower() or "interview" in category.lower(),
            "pdf_required": include_pdf,
            "image_required": include_image,
            "cta": False
        }
        
        job_id = str(uuid.uuid4())
        JOB_STATUS[job_id] = "Starting single generation..."
        threading.Thread(target=background_generate_single, args=(job_id, slot, recent_topics, session.get('active_group', 'placement_prep'))).start()
        
        flash(f"Single post generation started in background. Topic: {custom_topic or 'Auto'}", "success")
    except Exception as e:
        flash(f"Failed to start generation: {str(e)}", "danger")
    return redirect(url_for("index"))

@app.route("/api/status")
@login_required
def get_status():
    return jsonify(JOB_STATUS)


@app.route("/api/status/<job_id>")
@login_required
def get_job_status(job_id):
    """One job's progress. CF.track polls this to draw the bar."""
    entry = JOB_STATUS.get(job_id)
    if entry is None:
        return jsonify({"status": "error", "error": "That job is not known."}), 404
    # Older code paths still write a bare string.
    if isinstance(entry, str):
        entry = {"status": "running", "message": entry}
    return jsonify(entry)

@app.route("/generate_queue", methods=["POST"])
@login_required
def generate_queue():
    """Generate every declared slot for one or more days.

    The Quick Action buttons send `target=today|tomorrow`, which this route
    used to ignore entirely — it always looped from tomorrow, so "Generate
    Today" could never produce today's posts.
    """
    days = int(request.form.get("days", 1))
    target = request.form.get("target", "")
    # target wins when present; otherwise a multi-day run starts tomorrow so it
    # does not collide with slots that have already passed today.
    offset = 0 if target == "today" else 1

    job_id = str(uuid.uuid4())
    JOB_STATUS[job_id] = {"status": "running", "message": "Reading the plan…", "percent": 0}
    active_grp = session.get("active_group", DEFAULT_GROUP)
    start_date = datetime.now()

    def background_days(jid, grp, num_days, first_offset):
        def update_status(msg, done=None, total=None):
            # A bulk run takes minutes. Reporting only a rolling sentence gave
            # no way to tell "two of five" from "four of five", so the page had
            # nothing to draw a bar from.
            #
            # Most messages arrive from inside a single post's generation —
            # "writing caption", "checking quality" — and carry no counts. They
            # must not reset the bar to zero, so the last real position is kept
            # and only the sentence changes.
            if done is not None and total:
                position[0], position[1] = done, total
            entry = {"status": "running", "message": msg}
            done_units, total_units = position
            within = (done_units / total_units) if total_units else 0
            entry["percent"] = round(((day_index[0] + within) / num_days) * 100)
            if total_units:
                entry["done"], entry["total"] = done_units, total_units
            JOB_STATUS[jid] = entry

        day_index = [0]
        position = [0, 0]     # done, total — within the current day
        generated = failed = 0
        try:
            wf = get_workflow(grp)
            for i in range(num_days):
                day_index[0] = i
                day = start_date + timedelta(days=first_offset + i)
                date_str = day.strftime("%Y-%m-%d")
                update_status(f"Generating {date_str} ({i + 1}/{num_days})…")
                try:
                    results = wf.generate_queue(date_str, status_callback=update_status)
                    generated += len(results)
                except Exception as day_error:
                    # One bad day must not abort the rest of the run.
                    failed += 1
                    log.exception("Queue generation failed for %s", date_str)
                    update_status(f"{date_str} failed: {day_error}")
            JOB_STATUS[jid] = {
                "status": "done",
                "percent": 100,
                "message": f"{generated} post{'s' if generated != 1 else ''} ready for review.",
                "detail": (f"{failed} day(s) failed — see the log."
                           if failed else "Each one has a time already; none are approved."),
            }
        except Exception as e:
            log.exception("Queue generation aborted")
            JOB_STATUS[jid] = {"status": "error", "error": str(e)[:400]}
        finally:
            invalidate_sheet_cache()

    threading.Thread(
        target=background_days, args=(job_id, active_grp, days, offset), daemon=True
    ).start()

    when = "today" if offset == 0 else "starting tomorrow"
    return respond(
        f"Generating {days} day{'s' if days != 1 else ''} of posts.",
        detail=f"Beginning {when}. They land in Needs review with a time set, "
               f"unapproved — this page follows along.",
        job_id=job_id,
    )

# ──────────────────────────────────────────────────────────────────────────────
# Blueprint Batch Generation — new, independent of the manual generate flow
# ──────────────────────────────────────────────────────────────────────────────

# In-memory job store for blueprint batch jobs: { job_id: {...} }
BLUEPRINT_JOB_STATUS = _JobLog()

@app.route("/api/generate_from_blueprint", methods=["POST"])
@login_required
def api_generate_from_blueprint():
    """
    POST body (JSON): {"group_id": str, "mode": "day"|"week"}

    Spawns a background thread, returns {"job_id": ...} immediately.
    The client polls /api/blueprint_job_status/<job_id> for progress.
    """
    from flask import jsonify
    from engine.blueprint_engine import has_blueprint
    from engine.blueprint_engine import get_slots_range, _compute_day_number
    from engine.batch_generator import generate_from_blueprint

    data = request.get_json(force=True, silent=True) or {}
    group_id = data.get("group_id") or session.get("active_group", "placement_prep")
    mode = data.get("mode", "day")

    if mode not in ("day", "week"):
        return jsonify({"error": "mode must be 'day' or 'week'"}), 400

    try:
        wf = get_workflow(group_id)
    except Exception as e:
        return jsonify({"error": f"Could not load group '{group_id}': {e}"}), 400

    # Guard: group must have a blueprint.json
    if not has_blueprint(wf.group):
        return jsonify({
            "error": (
                f"Group '{group_id}' has no blueprint.json. "
                "Generate one via Claude and save it to "
                f"groups/{group_id}/blueprint.json before using Blueprint Batch Generation."
            )
        }), 422

    # Compute today's day number for the start_day
    from engine.blueprint_engine import _load_blueprint, _compute_day_number as _cdn
    try:
        duration, _ = _load_blueprint(wf.group)
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        start_day = _cdn(today, duration)
    except Exception as e:
        return jsonify({"error": f"Could not read blueprint: {e}"}), 500

    job_id = str(uuid.uuid4())
    num_days = 7 if mode == "week" else 1
    estimated_total = num_days * 5   # rough estimate; exact count comes from blueprint

    BLUEPRINT_JOB_STATUS[job_id] = {
        "status": "running",
        "mode": mode,
        "group_id": group_id,
        "done": 0,
        "total": estimated_total,
        "generated": 0,
        "skipped": 0,
        "failed": [],
        "last_msg": f"Starting blueprint batch ({mode})…",
    }

    def _run(jid, wf_, mode_, start_day_):
        def progress(done, total, msg):
            BLUEPRINT_JOB_STATUS[jid].update({
                "done": done,
                "total": total,
                "last_msg": msg,
            })

        try:
            summary = generate_from_blueprint(
                workflow=wf_,
                mode=mode_,
                start_day=start_day_,
                progress_callback=progress,
            )
            BLUEPRINT_JOB_STATUS[jid].update({
                "status": "complete",
                "generated": summary["generated"],
                "skipped":   summary["skipped_duplicate"],
                "failed":    summary["failed"],
                "last_msg":  (
                    f"Done! Generated={summary['generated']}, "
                    f"Skipped={summary['skipped_duplicate']}, "
                    f"Failed={len(summary['failed'])}"
                ),
            })
            invalidate_sheet_cache()
        except FileNotFoundError as fnf:
            BLUEPRINT_JOB_STATUS[jid].update({
                "status": "error",
                "last_msg": str(fnf),
            })
        except Exception as exc:
            import traceback
            BLUEPRINT_JOB_STATUS[jid].update({
                "status": "error",
                "last_msg": f"{type(exc).__name__}: {exc}",
            })
            traceback.print_exc()
            invalidate_sheet_cache()

    threading.Thread(
        target=_run,
        args=(job_id, wf, mode, start_day),
        daemon=True,
    ).start()

    return jsonify({"job_id": job_id, "total_estimate": estimated_total})


@app.route("/api/blueprint_job_status/<job_id>")
@login_required
def api_blueprint_job_status(job_id):
    """Poll endpoint — returns the current progress dict for a blueprint batch job."""
    from flask import jsonify
    job = BLUEPRINT_JOB_STATUS.get(job_id)
    if not job:
        return jsonify({"error": "job not found"}), 404
    return jsonify(job)


@app.route("/create_manual", methods=["GET", "POST"])
@login_required
def create_manual():
    if request.method == "GET":
        return redirect(url_for("index"))
    post_type = request.form.get("post_type", "Message")
    topic = request.form.get("topic", "Custom Post")
    use_ai = request.form.get("use_ai", "1") == "1"
    ai_instructions = request.form.get("ai_instructions", "").strip()
    manual_content = request.form.get("content", "").strip()
    
    include_pdf = (post_type == "PDF")
    include_image = (post_type == "Image")

    if not use_ai and manual_content:
        # User explicitly chose "Write Manually" — use their text as-is
        final_content = manual_content
    else:
        # AI mode — generate content using topic + optional AI instructions
        try:
            from agents.definitions import writer_agent, qa_agent
            import copy
            wf_current = get_workflow()
            custom_writer = copy.deepcopy(writer_agent(wf_current.group))
            
            format_hint = render_prompt("tasks/format_hint_message")
            if post_type.lower() == "poll":
                format_hint = render_prompt("tasks/poll_format_example")
            elif post_type.lower() == "pdf":
                format_hint = render_prompt("tasks/format_hint_pdf")
            elif post_type.lower() == "image":
                format_hint = render_prompt("tasks/format_hint_image")
            elif post_type.lower() == "link":
                format_hint = render_prompt("tasks/format_hint_link")
                
            # Relax strict rules for manual posts; polls need strict structure
            if post_type.lower() != "poll":
                custom_writer["instructions"] += (
                    "\n\n" + render_prompt("tasks/manual_post_override")
                )

            # Build prompt — include user's AI instructions if provided
            prompt = render_prompt(
                "tasks/manual_post_context",
                topic=topic,
                user_instructions=(
                    f"User Instructions: {ai_instructions}\n" if ai_instructions else ""
                ),
                format_hint=format_hint,
            )
            
            if post_type.lower() == "poll":
                draft = workflow.llm.generate_content(prompt=prompt, agent=custom_writer, is_json=True, group=wf_current.group)
            else:
                draft = workflow._call_agent(custom_writer, prompt)
            final_content = draft
            if post_type.lower() not in ["poll"]:
                final_content = workflow._call_agent(qa_agent(wf_current.group), f"Draft:\n{draft}")
                if final_content.startswith("APPROVED: ") or final_content.startswith("FIXED: "):
                    final_content = final_content.split(": ", 1)[-1].strip()
        except Exception as e:
            flash(f"AI generation failed: {str(e)}. Please switch to Manual mode and type your content.", "danger")
            return redirect(url_for("index"))

        
    post_id = str(uuid.uuid4())
    now = datetime.now()
    
    post_fields = {
        "id": post_id,
        "content_type": post_type,
        "category": post_type,
        "topic": topic,
        "title": topic[:120],
        "content": final_content,
        "search_used": False,
        # A post that promised an asset waits in RENDERING until it has one;
        # only then does it become approvable.
        "state": (PostState.RENDERING if (include_pdf or include_image)
                  else PostState.NEEDS_REVIEW),
        "wants_pdf": include_pdf,
        "wants_image": include_image,
    }
    
    # STEP 1: Persist the post. This MUST succeed; a render with nothing to
    # attach it to is wasted work, so a failure here stops and reports.
    # The post must appear in Pending Review regardless of whether asset generation succeeds.
    active_group = session.get("active_group", DEFAULT_GROUP)
    try:
        workflow.storage.create(active_group, **post_fields)
    except Exception as exc:
        log.exception("Could not save manual post")
        flash(f"Could not save the post: {exc}", "danger")
        return redirect(url_for("index"))

    # Cache is stale now — force a fresh read on next page load
    invalidate_sheet_cache()

    # STEP 2: Generate assets (PDF/Image) in a background thread so the page
    # responds immediately without timing out (rendering takes ~30s).
    if include_pdf or include_image:
        def _bg_generate(pid, t, fc, ipdf, iimg, ptype, pdf_cap_instructions, bg_group_id):
            try:
                wf = get_workflow(bg_group_id)
                wf.generate_assets(
                    pid, t, fc,
                    force_pdf_status="pending" if ipdf else "N/A",
                    force_img_status="pending" if iimg else "N/A",
                    category=ptype
                )
                # The graphic carries the substance; the caption is a
                # different, much shorter job. PDF posts already did this. Image
                # posts did not, so the writer's full 200-450 word post — the
                # very points now on the graphic — went out as the caption, and
                # the reader saw the same list twice.
                if ptype.lower() in ("pdf", "image"):
                    try:
                        from agents.definitions import caption_agent
                        if ptype.lower() == "pdf":
                            cap_prompt = render_prompt(
                                "tasks/pdf_caption", topic=t,
                                instructions=pdf_cap_instructions)
                        else:
                            cap_prompt = render_prompt(
                                "tasks/image_caption", topic=t,
                                instructions=pdf_cap_instructions,
                                items_summary=_items_summary(wf, pid))
                        caption = wf._call_agent(caption_agent(wf.group), cap_prompt,
                                                 use_cache=False)
                        wf.storage.update_post(pid, {"Generated Content": caption})
                    except Exception as ce:
                        # The post still has the writer's text, so it is not
                        # lost — just longer than it should be.
                        log.warning("Caption generation failed for %s: %s", pid[:8], ce)
                invalidate_sheet_cache()
            except Exception as e:
                # A background thread has nowhere to flash a message to, so
                # this used to be the end of it: the operator watched a post sit
                # at "pending" forever with no indication why.
                log.exception("Background asset generation failed for %s", pid[:8])
                try:
                    if ipdf or iimg:
                        wf.storage.set_state(pid, PostState.ASSET_FAILED,
                                             error=str(e)[:1000])
                    else:
                        # The post declared no asset, so whatever failed here
                        # does not stop it publishing.
                        wf.storage.update_post(pid, {"Error": str(e)[:1000]})
                except Exception:
                    log.exception("Could not even record the failure for %s", pid[:8])
                invalidate_sheet_cache()

        threading.Thread(
            target=_bg_generate,
            args=(post_id, topic, final_content, include_pdf, include_image, post_type,
                  ai_instructions, active_group),
            daemon=True
        ).start()
        flash("Post saved! Image/PDF is being generated in the background — refresh in ~30 seconds to see it.", "info")

    flash("Post created and added to Pending Review.", "success")
    return redirect(url_for("index", tab="pending"))

@app.route("/api/post_states")
@login_required
def post_states():
    """The current state of specific posts, for a page that is watching them.

    A post created with an image or PDF is written immediately and rendered in
    a background thread, so the page that redirects after "create" shows a post
    whose asset does not exist yet. There was nothing telling the page when it
    became ready, so the only way to find out was to keep pressing refresh —
    and each refresh showed a later stage of the same job: raw text, then
    mapped placeholders, then the finished graphic.
    """
    ids = [i for i in (request.args.get("ids") or "").split(",") if i.strip()]
    if not ids:
        return jsonify({"states": {}})

    try:
        from services.storage.db import session_scope
        from services.storage import repositories as repo
        with session_scope() as session:
            states = {}
            for post_id in ids[:50]:
                post = repo.get_post(session, post_id)
                if post is not None:
                    states[post_id] = post.state
        return jsonify({"states": states})
    except Exception as exc:
        log.warning("Could not read post states: %s", exc)
        return jsonify({"states": {}, "error": str(exc)}), 500


@app.route("/regenerate/<post_id>", methods=["POST"])
@login_required
def regenerate(post_id):
    instructions = request.form.get("instructions", "").strip()
    
    # Get existing post data
    all_posts = workflow.storage.get_all_posts(session.get('active_group', DEFAULT_GROUP))
    post = next((p for p in all_posts if p.get("Post ID") == post_id), None)
    if not post:
        flash("Post not found.", "danger")
        return redirect(url_for("index"))
        
    topic = post.get("Topic", "")
    post_type = post.get("Content Type", "")
    
    try:
        from agents.definitions import writer_agent, qa_agent
        wf_current = get_workflow()
        format_hint = render_prompt("tasks/format_hint_message")
        if post_type.lower() == "poll":
            format_hint = render_prompt("tasks/poll_format_example")
        elif post_type == "PDF":
            format_hint = render_prompt("tasks/format_hint_pdf")
        elif post_type == "Image":
            format_hint = render_prompt("tasks/format_hint_image")
        elif post_type == "Link":
            format_hint = render_prompt("tasks/format_hint_link")

        prompt = f"Topic: {topic}\nUser Custom Instructions/Draft: {instructions}\n{format_hint}\nCTA Required: True"
        # Regenerate must not consult the response cache. The topic and the
        # prompt are unchanged, so the cache key is unchanged, and the operator
        # was handed back the exact draft they had just asked to be redone.
        if post_type.lower() == "poll":
            draft = workflow.llm.generate_content(
                prompt=prompt, agent=writer_agent(wf_current.group),
                is_json=True, group=wf_current.group, use_cache=False)
        else:
            draft = workflow._call_agent(
                writer_agent(wf_current.group), prompt, use_cache=False)

        final_content = draft
        if post_type.lower() not in ["poll"]:
            final_content = workflow._call_agent(
                qa_agent(wf_current.group), f"Draft:\n{draft}", use_cache=False)
            
        workflow.storage.update_post(post_id, {"Generated Content": final_content})
        
        # Regenerate assets if applicable
        workflow.generate_assets(post_id, topic, final_content)
        
        if post_type.lower() == "pdf":
            caption_prompt = render_prompt(
                "tasks/pdf_caption", topic=topic, instructions=instructions
            )
            caption = workflow._call_agent(writer_agent(wf_current.group), caption_prompt)
            workflow.storage.update_post(post_id, {"Generated Content": caption})
            
        invalidate_sheet_cache()
        return respond("Rewritten.", detail="The draft has been replaced.")
    except Exception as e:
        log.exception("Regenerating %s failed", post_id[:8])
        invalidate_sheet_cache()
        return respond("Could not rewrite that post.", ok=False, detail=str(e))
@app.route("/approve/<post_id>", methods=["POST"])
@login_required
def approve(post_id):
    title = request.form.get("title", "Carrot Owl Post")
    content = request.form.get("content", "")
    schedule_time = request.form.get("schedule_time", "")
    
    try:
        # Fetch from cached sheets data to avoid redundant reads
        all_posts = get_all_posts_cached(workflow.config.ACTIVE_GROUP_ID)
        post = next((p for p in all_posts if p.get("Post ID") == post_id), {})
        
        pdf_status = post.get("PDF Path", "N/A")
        img_status = post.get("Image Path", "N/A")
        post_type = post.get("Content Type", "Message")

        # Does this post have a graphic at all? A poll or a plain message does
        # not, and must never enter the render path. It used to: the approve
        # route remapped placeholders for every post, `placeholders_updated`
        # then counted as "assets pending", and a poll went off to Chromium —
        # which failed and left it in asset_failed, a state it cannot be
        # approved out of. A poll that needs no image was unpublishable because
        # an image it never asked for did not render.
        wants_asset = (
            post_type.lower() in ("pdf", "image")
            or pdf_status.lower() == "pending"
            or img_status.lower() == "pending"
            or pdf_path_is_real(pdf_status)
            or pdf_path_is_real(img_status)
        )

        # Use existing paths if already generated
        pdf_path = post.get("PDF Path") if pdf_path_is_real(pdf_status) else None
        img_path = post.get("Image Path") if pdf_path_is_real(img_status) else None

        # Check and update placeholders if passed in form
        placeholders_dir = os.path.join(workflow.config.PROJECT_ROOT, "generated", "placeholders")
        placeholders_file = os.path.join(placeholders_dir, f"{post_id}.json")
        
        # Read template choice and format from form if present
        template_name = request.form.get("template_used", post.get("Template Used", DEFAULT_TEMPLATE))
        export_type = request.form.get("asset_type", post.get("Asset Type", "PNG")).upper()
        
        old_template = post.get("Template Used")
        template_changed = old_template != template_name or old_template == "N/A"
        
        required_keys = workflow._get_template_placeholders(template_name)
        
        placeholders = {}
        if os.path.exists(placeholders_file) and not template_changed:
            try:
                with open(placeholders_file, "r", encoding="utf-8") as f:
                    placeholders = json.load(f)
            except Exception:
                pass
                
        # Remap if template changed or placeholders are missing
        if template_changed or not placeholders:
            try:
                from agents.definitions import ASSET_MAPPER_AGENT
                mapper_prompt = render_prompt(
                    "tasks/remap_placeholders",
                    content=content,
                    template_name=template_name,
                    required_placeholders=", ".join(required_keys),
                )
                placeholders = workflow._call_agent_json(ASSET_MAPPER_AGENT, mapper_prompt)
            except Exception as e:
                log.warning("Auto-mapping placeholders on approval failed: %s", e)
                placeholders = {}
        
        placeholders_updated = template_changed
        for key in required_keys:
            if key in request.form:
                val_stripped = request.form.get(key).strip()
                if placeholders.get(key) != val_stripped:
                    placeholders[key] = val_stripped
                    placeholders_updated = True
            elif key not in placeholders:
                if key == "LOGO":
                    placeholders[key] = wf.renderer.brand_placeholders(wf.group).get("LOGO", "logo_light.png")
                elif key == "WEBSITE":
                    placeholders[key] = "carrotowleducation.com"
                elif key == "CTA":
                    placeholders[key] = "Join @carrotowl"
                elif key == "PAGE":
                    placeholders[key] = "1"
                else:
                    placeholders[key] = ""
                    
        if placeholders_updated:
            os.makedirs(placeholders_dir, exist_ok=True)
            with open(placeholders_file, "w", encoding="utf-8") as f:
                json.dump(placeholders, f, indent=4)

        # If assets are still pending or placeholders were updated, generate/re-render them in a background thread
        # so Approve & Schedule responds immediately without timing out
        # `placeholders_updated` alone is not a reason to render: a poll's
        # placeholders get remapped too, and rendering one produced nothing
        # anybody wanted.
        assets_pending = wants_asset and (
            pdf_status.lower() == "pending"
            or img_status.lower() == "pending"
            or placeholders_updated
        )
        if assets_pending:
            _approve_title = title
            _approve_content = content
            _approve_pdf_status = pdf_status
            _approve_img_status = img_status
            _approve_post_type = post_type
            _approve_post_id = post_id
            _approve_placeholders_updated = placeholders_updated
            def _bg_approve_assets(pid, t, c, ps, is_, pt, grp, pl_up):
                try:
                    wf = get_workflow(grp)
                    if pl_up:
                        # Re-render directly using edited placeholders
                        p_file = os.path.join(wf.config.PROJECT_ROOT, "generated", "placeholders", f"{pid}.json")
                        with open(p_file, "r", encoding="utf-8") as f:
                            p_data = json.load(f)
                        # Fetch the post to get Template Used & Asset Type
                        all_p = wf.storage.get_all_posts(wf.group.id)
                        curr_p = next((x for x in all_p if x.get("Post ID") == pid), {})
                        t_name = curr_p.get("Template Used", DEFAULT_TEMPLATE)
                        if not t_name or t_name == "N/A": t_name = DEFAULT_TEMPLATE
                        exp_type = curr_p.get("Asset Type", "PNG").upper()
                        if not exp_type or exp_type == "N/A": exp_type = "PNG"

                        rendered_path = wf.renderer.render(t_name, p_data, exp_type, group=wf.group)

                        canonical_path = rendered_path

                        updates = {}
                        if exp_type == "PDF":
                            updates["PDF Path"] = canonical_path
                        else:
                            updates["Image Path"] = canonical_path
                        updates["Template Used"] = t_name
                        updates["Asset Type"] = exp_type
                        wf.storage.update_post(pid, updates,
                                              row_cache=get_row_cache())
                    else:
                        wf.generate_assets(pid, t, c,
                            force_pdf_status=ps, force_img_status=is_, category=pt)
                    invalidate_sheet_cache()
                except Exception as e:
                    # Same gap as create_manual, with worse consequences: the
                    # post is already approved, so it would sit in the
                    # reconciler's queue with an asset that never arrived.
                    log.exception("Background asset generation failed for %s", pid[:8])
                    try:
                        if ps.lower() == "pending" or is_.lower() == "pending":
                            wf.storage.set_state(pid, PostState.ASSET_FAILED,
                                                 error=str(e)[:1000])
                        else:
                            wf.storage.update_post(pid, {"Error": str(e)[:1000]})
                    except Exception:
                        log.exception("Could not even record the failure for %s", pid[:8])
                    invalidate_sheet_cache()
            threading.Thread(
                target=_bg_approve_assets,
                args=(_approve_post_id, _approve_title, _approve_content,
                      _approve_pdf_status, _approve_img_status, _approve_post_type,
                      session.get('active_group', 'placement_prep'), _approve_placeholders_updated),
                daemon=True
            ).start()
            
        # Mark as approved, set schedule time, and save edited content
        workflow.storage.update_post(post_id, {
            "Approval Status": "approved", 
            "Scheduled Time": schedule_time,
            "Generated Content": content
        })
        
        # Approving records a time; it does not schedule anything. The
        # reconciler picks the post up on its next pass once the time has
        # passed and its assets exist. Nothing is published from inside this
        # request, so approving can no longer race its own render.
        group = get_workflow(session.get("active_group", DEFAULT_GROUP)).group

        # An empty field means "as soon as possible" — that is what the form
        # says next to it. Treating blank as unreadable made the only advertised
        # way to publish immediately fail with an error.
        if not (schedule_time or "").strip():
            when = datetime.now(timezone.utc)
        else:
            when = _parse_local_schedule(schedule_time, group)
            if when is None:
                return respond(
                    "That schedule time could not be read.", ok=False,
                    detail=f"Got {schedule_time!r}. Use the picker, or leave it "
                           f"blank to publish straight away.")

        # `when`, not the raw form value. The form gives the operator's local
        # time; the store reads a bare string as UTC. Passing the string through
        # scheduled every post one timezone offset late — 5½ hours for IST — so
        # "publish at 4pm" meant half past nine at night, and "publish now"
        # meant this evening.
        workflow.storage.update_post(post_id, {"Scheduled Time": when})
        local = when.astimezone(group.tz).strftime("%d %b, %H:%M")
        invalidate_sheet_cache()
        if when <= datetime.now(timezone.utc):
            detail = "Its time has already passed, so it goes out within the minute."
        else:
            detail = f"Queued for {local} ({group.timezone})."
        return respond("Approved.", detail=detail, state=PostState.APPROVED,
                       state_label="approved", tab="scheduled")
    except Exception as e:
        log.exception("Approving %s failed", post_id[:8])
        invalidate_sheet_cache()
        return respond("Could not approve that post.", ok=False, detail=str(e))
        
@app.route("/update_placeholders/<post_id>", methods=["POST"])
@login_required
def update_placeholders(post_id):
    wf = get_workflow()
    all_posts = get_all_posts_cached(wf.config.ACTIVE_GROUP_ID)
    post = next((p for p in all_posts if p.get("Post ID") == post_id), None)
    if not post:
        flash("Post not found.", "danger")
        return redirect(url_for("index"))
        
    # Get chosen template and asset type from the form
    template_name = request.form.get("template_used", post.get("Template Used", DEFAULT_TEMPLATE))
    export_type = request.form.get("asset_type", post.get("Asset Type", "PNG")).upper()
    
    # Check if template changed
    old_template = post.get("Template Used")
    template_changed = old_template != template_name or old_template == "N/A"
    
    placeholders_dir = os.path.join(wf.config.PROJECT_ROOT, "generated", "placeholders")
    placeholders_file = os.path.join(placeholders_dir, f"{post_id}.json")
    
    # Get the required placeholders for the chosen template
    required_keys = wf._get_template_placeholders(template_name)
    
    # Load existing placeholders
    placeholders = {}
    if os.path.exists(placeholders_file) and not template_changed:
        try:
            with open(placeholders_file, "r", encoding="utf-8") as f:
                placeholders = json.load(f)
        except Exception:
            pass

    # If template changed or placeholders are missing, auto-map with the Content Mapper
    if template_changed or not placeholders:
        try:
            from agents.definitions import ASSET_MAPPER_AGENT
            draft_content = request.form.get("content", post.get("Generated Content", ""))
            mapper_prompt = render_prompt(
                "tasks/remap_placeholders",
                content=draft_content,
                template_name=template_name,
                required_placeholders=", ".join(required_keys),
            )
            placeholders = wf._call_agent_json(ASSET_MAPPER_AGENT, mapper_prompt)
        except Exception as e:
            log.warning("Auto-mapping placeholders failed: %s", e)
            placeholders = {}

    # Override/merge with whatever the user submitted in the form
    for key in required_keys:
        if key in request.form:
            placeholders[key] = request.form.get(key).strip()
        elif key not in placeholders:
            # Set standard defaults
            if key == "LOGO":
                placeholders[key] = wf.renderer.brand_placeholders(wf.group).get("LOGO", "logo_light.png")
            elif key == "WEBSITE":
                placeholders[key] = "carrotowleducation.com"
            elif key == "CTA":
                placeholders[key] = "Join @carrotowl"
            elif key == "PAGE":
                placeholders[key] = "1"
            else:
                placeholders[key] = ""
                
    # Save the updated/remap placeholders JSON
    os.makedirs(placeholders_dir, exist_ok=True)
    with open(placeholders_file, "w", encoding="utf-8") as f:
        json.dump(placeholders, f, indent=4)
        
    # Re-render the image/PDF with updated placeholders
    try:
        rendered_path = wf.renderer.render(template_name, placeholders, export_type, group=wf.group)

        canonical_path = rendered_path

        updates = {}
        if export_type == "PDF":
            updates["PDF Path"] = canonical_path
            updates["Image Path"] = "N/A"
        else:
            updates["Image Path"] = canonical_path
            updates["PDF Path"] = "N/A"

        updates["Template Used"] = template_name
        updates["Asset Type"] = export_type

        # Also sync "Generated Content" if user edited it
        if "content" in request.form:
            updates["Generated Content"] = request.form.get("content", "")

        wf.storage.update_post(post_id, updates,
                              row_cache=get_row_cache())
        flash("Graphics updated and re-rendered successfully!", "success")
        invalidate_sheet_cache()
    except Exception as e:
        flash(f"Failed to re-render graphics: {str(e)}", "danger")
        invalidate_sheet_cache()
        
    return redirect(url_for("index", tab="pending"))

@app.route("/reject/<post_id>", methods=["POST"])
@login_required
def reject(post_id):
    try:
        wf = get_workflow()
        if not wf.storage.update_post(post_id, {"Approval Status": "rejected"}):
            return respond("That post no longer exists.", ok=False,
                           detail="It may have been deleted in another tab.",
                           status=404, tab="pending")

        # No job to cancel: a rejected post simply stops matching the
        # reconciler's query.

        invalidate_sheet_cache()
        return respond("Post discarded.", detail="It will not be published.",
                       state=PostState.REJECTED, state_label="rejected", tab="pending")
    except Exception as e:
        log.exception("Rejecting %s failed", post_id[:8])
        invalidate_sheet_cache()
        return respond("Could not discard that post.", ok=False, detail=str(e),
                       tab="pending")

@app.route("/delete/<post_id>", methods=["POST"])
@login_required
def delete_post(post_id):
    try:
        wf = get_workflow()
        if not wf.storage.delete_post(post_id):
            return respond("That post no longer exists.", ok=False,
                           detail="It may have been deleted in another tab.",
                           status=404, tab="pending")
        invalidate_sheet_cache()
        return respond("Post deleted.", detail="Removed from the database for good.",
                       tab="pending")
    except Exception as e:
        log.exception("Deleting %s failed", post_id[:8])
        invalidate_sheet_cache()
        return respond("Could not delete that post.", ok=False, detail=str(e),
                       tab="pending")

@app.route("/publish/<post_id>", methods=["POST"])
@login_required
def publish(post_id):
    content = request.form.get("content", "")
    pdf_path = request.form.get("pdf_path", "")
    img_path = request.form.get("img_path", "")
    
    try:
        wf = get_workflow()
        success = wf.publish_post(post_id, content, pdf_path, img_path)
        invalidate_sheet_cache()
        if success:
            return respond("Published to Telegram.",
                           state=PostState.PUBLISHED, state_label="published")
        # publish_post has already written the real reason to the post row.
        post = wf.storage.get_post_by_id(post_id) or {}
        return respond("Telegram refused that post.", ok=False,
                       detail=post.get("Error") or "See the post for details.",
                       state=PostState.PUBLISH_FAILED, state_label="publish failed")
    except Exception as e:
        log.exception("Publishing %s failed", post_id[:8])
        invalidate_sheet_cache()
        return respond("Could not publish that post.", ok=False, detail=str(e))

@app.route("/output/<file_type>/<post_id>")
@login_required
def serve_output(file_type, post_id):
    import io as _io
    all_posts = get_all_posts_cached(workflow.config.ACTIVE_GROUP_ID)
    post = next((p for p in all_posts if p.get("Post ID") == post_id), None)

    if not post:
        return "Post not found in database.", 404

    path = post.get("PDF Path") if file_type == "pdf" else post.get("Image Path")

    if not path or path in ["N/A", "pending", "Failed", ""]:
        return f"No {file_type.upper()} asset has been planned or generated for this post.", 404

    # ── Legacy local-path fallback ─────────────────────────────────────────────
    if not os.path.exists(path):
        return (
            f"The {file_type.upper()} file was generated at '{path}' but is missing from disk. "
            "Please click Regenerate on the dashboard to recreate it."
        ), 404

    as_attachment = request.args.get("download") == "true"
    # `conditional` gives the response an ETag and Last-Modified, so a browser
    # that already has the file gets a 304 instead of the bytes.
    response = send_file(path, as_attachment=as_attachment, conditional=True)

    # no-cache does NOT mean "do not store" — it means "ask me before reusing
    # this". That distinction is the whole bug: the header used to be
    # max-age=86400, and an asset URL does not change when the asset is
    # re-rendered, so a browser kept showing yesterday's PNG for a day. The
    # only way to see a re-render was a hard refresh. Revalidating costs one
    # conditional request and usually returns 304.
    response.headers["Cache-Control"] = "private, no-cache, must-revalidate"
    return response


@app.route("/design_templates/<path:filename>")
def serve_design_template_asset(filename):
    """Serve static CSS, images, and fonts from design_templates/ directory for iframe previews."""
    templates_dir = os.path.join(workflow.config.PROJECT_ROOT, "design_templates")
    return send_from_directory(templates_dir, filename)


@app.route("/render/preview/<post_id>")
@login_required
def render_preview_html(post_id):
    """Render the live populated HTML template for in-browser visual preview and Canva-lite editing."""
    wf = get_workflow()
    all_posts = get_all_posts_cached(wf.config.ACTIVE_GROUP_ID)
    post = next((p for p in all_posts if p.get("Post ID") == post_id), None)
    if not post:
        return "Post not found in database.", 404

    template_name = post.get("Template Used", DEFAULT_TEMPLATE)
    if not template_name or template_name == "N/A":
        cat = post.get("Content Type", "").lower()
        if "motivation" in cat or "quote" in cat:
            template_name = STATEMENT_TEMPLATE
        else:
            template_name = DEFAULT_TEMPLATE

    placeholders_dir = os.path.join(wf.config.PROJECT_ROOT, "generated", "placeholders")
    placeholders_file = os.path.join(placeholders_dir, f"{post_id}.json")
    placeholders = {}
    if os.path.exists(placeholders_file):
        try:
            with open(placeholders_file, "r", encoding="utf-8") as f:
                placeholders = json.load(f)
        except Exception:
            placeholders = {}

    required_keys = wf._get_template_placeholders(template_name)
    if not placeholders:
        try:
            from agents.definitions import asset_mapper_agent
            draft_content = post.get("Generated Content", "")
            mapper_prompt = render_prompt(
                "tasks/remap_placeholders",
                content=draft_content,
                template_name=template_name,
                required_placeholders=", ".join(required_keys),
            )
            mapped = wf._call_agent_json(asset_mapper_agent(wf.group), mapper_prompt)
            if isinstance(mapped, list) and len(mapped) > 0:
                mapped = mapped[0]
            if isinstance(mapped, dict):
                placeholders = mapped
        except Exception as e:
            log.warning("Auto-mapping placeholders for the preview failed: %s", e)
            placeholders = {}

    # Fill in anything the mapper left empty. Brand chrome comes from this
    # tenant's config — the logo, website and CTA were hardcoded here to one
    # community's values, so every other group previewed under Carrot Owl's
    # branding and then rendered under its own.
    chrome = wf.renderer.brand_placeholders(wf.group)
    for key in required_keys:
        if placeholders.get(key):
            continue
        if key in chrome:
            placeholders[key] = chrome[key]
        elif key == "PAGE":
            placeholders[key] = "1"
        elif key in ("TITLE", "QUOTE", "STATEMENT"):
            placeholders[key] = post.get("Topic", "")
        elif key in ("TAGLINE", "CATEGORY", "EYEBROW"):
            placeholders[key] = (post.get("Content Type") or wf.group.name).upper()
        elif key in ("HOOK", "SUBTITLE", "SUBTEXT"):
            placeholders[key] = post.get("Generated Content", "")[:120]
        else:
            placeholders[key] = ""

    os.makedirs(placeholders_dir, exist_ok=True)
    with open(placeholders_file, "w", encoding="utf-8") as f:
        json.dump(placeholders, f, indent=4)

    # Build intermediate HTML with base href pointing to /design_templates/ and live listener
    html = wf.renderer.build_html(
        template_name,
        placeholders,
        visual_overrides=placeholders.get("_visual_overrides", {}),
        base_href="/design_templates/",
        is_live_preview=True,
        group=wf.group,
    )
    return Response(html, mimetype="text/html")


@app.route("/api/save_asset_state/<post_id>", methods=["POST"])
@login_required
def save_asset_state(post_id):
    """Asynchronously saves updated placeholder content, visual styles, and metadata."""
    data = request.get_json() or {}
    placeholders = data.get("placeholders", {})
    visual_overrides = data.get("visual_overrides", {})
    template_used = data.get("template_used")
    asset_type = data.get("asset_type")
    content = data.get("content")

    if visual_overrides:
        placeholders["_visual_overrides"] = visual_overrides

    wf = get_workflow()
    placeholders_dir = os.path.join(wf.config.PROJECT_ROOT, "generated", "placeholders")
    os.makedirs(placeholders_dir, exist_ok=True)
    placeholders_file = os.path.join(placeholders_dir, f"{post_id}.json")
    with open(placeholders_file, "w", encoding="utf-8") as f:
        json.dump(placeholders, f, indent=4)

    updates = {}
    if template_used:
        updates["Template Used"] = template_used
    if asset_type:
        updates["Asset Type"] = asset_type
    if content is not None:
        updates["Generated Content"] = content

    if updates:
        wf.storage.update_post(post_id, updates, row_cache=get_row_cache())
        invalidate_sheet_cache()

    return jsonify({"status": "ok", "message": "Design and content saved successfully"})


@app.route("/api/render_asset/<post_id>", methods=["POST"])
@login_required
def api_render_asset(post_id):
    """Render the final PNG/PDF from the currently saved placeholder JSON.

    Called by the "Render Now" button in the Visual Design Studio.
    Runs synchronously in a background thread and returns immediately with
    a job_id so the client can poll /api/render_asset_status/<job_id>.
    """
    wf = get_workflow()
    group_id = wf.config.ACTIVE_GROUP_ID

    # Fetch post metadata
    post = wf.storage.get_post_by_id(post_id)
    if not post:
        return jsonify({"status": "error", "message": "Post not found"}), 404

    template_name = post.get("Template Used", DEFAULT_TEMPLATE)
    if not template_name or template_name == "N/A":
        template_name = DEFAULT_TEMPLATE
    export_type = post.get("Asset Type", "PNG").upper()
    if not export_type or export_type == "N/A":
        export_type = "PNG"

    placeholders_dir = os.path.join(wf.config.PROJECT_ROOT, "generated", "placeholders")
    placeholders_file = os.path.join(placeholders_dir, f"{post_id}.json")

    if not os.path.exists(placeholders_file):
        return jsonify({"status": "error", "message": "No saved placeholder data found. Save the design first."}), 400

    try:
        with open(placeholders_file, "r", encoding="utf-8") as f:
            placeholders = json.load(f)
    except Exception as e:
        return jsonify({"status": "error", "message": f"Could not read placeholder data: {e}"}), 500

    if not placeholders:
        return jsonify({"status": "error", "message": "Placeholder data is empty. Edit and save the design first."}), 400

    job_id = str(uuid.uuid4())
    JOB_STATUS[job_id] = "rendering"

    def _do_render(jid, wf_, pid, tmpl, p_data, exp_type, tab):
        try:
            rendered_path = wf_.renderer.render(tmpl, p_data, exp_type, group=wf_.group)
            canonical_path = rendered_path
            # Persist the new path
            field = "PDF Path" if exp_type == "PDF" else "Image Path"
            wf_.storage.update_post(pid, {field: canonical_path, "Template Used": tmpl, "Asset Type": exp_type})
            invalidate_sheet_cache()
            JOB_STATUS[jid] = f"done:{canonical_path}"
        except Exception as exc:
            import traceback
            traceback.print_exc()
            JOB_STATUS[jid] = f"error:{exc}"

    threading.Thread(
        target=_do_render,
        args=(job_id, wf, post_id, template_name, placeholders, export_type, group_id),
        daemon=True
    ).start()

    return jsonify({"status": "ok", "job_id": job_id})


@app.route("/api/render_asset_status/<job_id>")
@login_required
def api_render_asset_status(job_id):
    """Poll endpoint for the /api/render_asset background job."""
    raw = JOB_STATUS.get(job_id)
    if raw is None:
        return jsonify({"status": "error", "message": "Job not found"}), 404
    if raw == "rendering":
        return jsonify({"status": "rendering"})
    if raw.startswith("done:"):
        path = raw[5:]
        return jsonify({"status": "done", "path": path})
    if raw.startswith("error:"):
        return jsonify({"status": "error", "message": raw[6:]})
    return jsonify({"status": raw})


@app.route("/regenerate_assets/<post_id>", methods=["POST"])
@login_required
def regenerate_assets(post_id):
    wf = get_workflow()
    all_posts = get_all_posts_cached(wf.config.ACTIVE_GROUP_ID)
    post = next((p for p in all_posts if p.get("Post ID") == post_id), None)
    if not post:
        flash("Post not found.", "danger")
        return redirect(url_for("index"))
        
    post_type = post.get("Content Type", "")
    pdf_required = post.get("PDF Path") != "N/A" and post.get("PDF Path") != ""
    image_required = post.get("Image Path") != "N/A" and post.get("Image Path") != ""

    def _bg_regen(pid, p_topic, p_content, p_type, pdf_req, img_req):
        try:
            wf.generate_assets(
                pid,
                p_topic,
                p_content,
                force_pdf_status="pending" if pdf_req else "N/A",
                force_img_status="pending" if img_req else "N/A",
                category=p_type
            )
            invalidate_sheet_cache()
        except Exception as exc:
            log.error(f"Background asset regeneration failed for post {pid}: {exc}")
            invalidate_sheet_cache()

    threading.Thread(
        target=_bg_regen,
        args=(
            post_id,
            post.get("Topic", "Untitled"),
            post.get("Generated Content", ""),
            post_type,
            pdf_required,
            image_required
        ),
        daemon=True
    ).start()

    return respond("Re-rendering that asset.",
                   detail="The preview updates as soon as it is done.",
                   state=PostState.RENDERING, state_label="rendering")

@app.route("/templates/preview/<template_id>")
@login_required
def preview_template(template_id):
    registry_path = Path("design_templates/registry.json")
    if not registry_path.exists():
        return "Registry not found", 404
        
    with open(registry_path, "r", encoding="utf-8") as f:
        templates = json.load(f)
        
    tmpl = next((t for t in templates if t["id"] == template_id), None)
    if not tmpl:
        return "Template not found", 404
        
    template_name = tmpl["file"]
    required_placeholders = workflow._get_template_placeholders(template_name)
    
    dummy_data = {
        # The preview uses the active group's own chrome, like a real render.
        "LOGO": workflow.renderer.brand_placeholders(workflow.group).get("LOGO", "logo_light.png"),
        "CATEGORY": "PLACEMENT GUIDE",
        "HOOK": "MASTER THE HR ROUND",
        "TITLE": "Tell me about a time you faced a challenge at work?",
        "SUBTITLE": "A classic behavior question asked in 90% of interview rounds.",
        "POINT_1": "Clearly outline the situation and the core challenge you faced.",
        "POINT_2": "Describe the specific actions you took to resolve it.",
        "POINT_3": "Highlight the positive results and key learning takeaways.",
        "TIP": "Pro Tip: Keep it structured using the STAR framework.",
        "CHECKLIST": '<li><span class="check-box"></span>Keep it under 2 minutes</li><li><span class="check-box"></span>Focus on professional growth</li>',
        "WEBSITE": "owlet-campus.com",
        "CTA": "Join @carrotowl",
        "PAGE": "1",
        "TAGLINE": "INTERVIEW HACK",
        "QUOTE": "HOW TO HANDLE WORKPLACE CHALLENGES",
        "SUBTEXT": "Be honest about the situation, focus heavily on your actions, and always conclude with the quantifiable business impact.",
        "PERSON_IMAGE": "logo_light.png"
    }
    
    placeholders = {k: dummy_data[k] for k in required_placeholders if k in dummy_data}
    for req in required_placeholders:
        if req not in placeholders:
            placeholders[req] = ""
            
    try:
        export_type = tmpl["supported_asset_types"][0]
        preview_path = workflow.renderer.render(template_name, placeholders, export_type,
                                                group=workflow.group)
        if preview_path and os.path.exists(preview_path):
            return send_file(preview_path)
    except Exception as e:
        return f"Failed to generate template preview: {e}", 500
        
    return "Failed to render preview", 500

@app.route("/templates/toggle/<template_id>", methods=["POST"])
@login_required
def toggle_template(template_id):
    registry_path = Path("design_templates/registry.json")
    if not registry_path.exists():
        return "Registry not found", 404
        
    with open(registry_path, "r", encoding="utf-8") as f:
        templates = json.load(f)
        
    for tmpl in templates:
        if tmpl["id"] == template_id:
            tmpl["enabled"] = not tmpl["enabled"]
            break
            
    with open(registry_path, "w", encoding="utf-8") as f:
        json.dump(templates, f, indent=2)
        
    return "OK", 200

@app.route("/templates/get_code/<template_id>")
@login_required
def get_template_code(template_id):
    registry_path = Path("design_templates/registry.json")
    if not registry_path.exists():
        return "Registry not found", 404
        
    with open(registry_path, "r", encoding="utf-8") as f:
        templates = json.load(f)
        
    tmpl = next((t for t in templates if t["id"] == template_id), None)
    if not tmpl:
        return "Template not found", 404
        
    template_path = Path("design_templates") / tmpl["file"]
    if not template_path.exists():
        return "Template HTML file not found", 404
        
    with open(template_path, "r", encoding="utf-8") as f:
        code = f.read()
        
    return {
        "id": tmpl["id"],
        "name": tmpl["name"],
        "file": tmpl["file"],
        "theme": workflow.group.theme,
        "priority": tmpl.get("priority", 1),
        "supported_categories": tmpl.get("supported_categories", []),
        "code": code
    }, 200

@app.route("/templates/save_code/<template_id>", methods=["POST"])
@login_required
def save_template_code(template_id):
    registry_path = Path("design_templates/registry.json")
    if not registry_path.exists():
        return "Registry not found", 404
        
    data = request.json or {}
    code = data.get("code")
    name = data.get("name")
    priority = data.get("priority", 1)
    categories = data.get("supported_categories", [])
    
    with open(registry_path, "r", encoding="utf-8") as f:
        templates = json.load(f)
        
    tmpl = next((t for t in templates if t["id"] == template_id), None)
    if not tmpl:
        return "Template not found", 404
        
    if code is not None:
        template_path = Path("design_templates") / tmpl["file"]
        with open(template_path, "w", encoding="utf-8") as f:
            f.write(code)
            
    if name:
        tmpl["name"] = name
    tmpl["priority"] = int(priority)
    if categories:
        tmpl["supported_categories"] = categories
        
    with open(registry_path, "w", encoding="utf-8") as f:
        json.dump(templates, f, indent=2)
        
    return "OK", 200

@app.route("/serve_file")
@login_required
def serve_file():
    filepath = request.args.get("path")
    if not filepath:
        return "Missing path", 400
    # Security: resolve the path and confirm it is inside the generated/ output directory.
    # This prevents path traversal (e.g. ../../.env or absolute paths to sensitive files).
    base_output_dir = (Path(config.PROJECT_ROOT) / "generated").resolve()
    try:
        safe_path = Path(filepath).resolve()
        if not str(safe_path).startswith(str(base_output_dir)):
            return "Forbidden", 403
    except Exception:
        return "Invalid path", 400
    if not safe_path.exists():
        return "File not found", 404
    return send_file(safe_path)

if __name__ == "__main__":
    # Relaunch inside virtual environment if run with global python
    import subprocess
    venv_python = Path(__file__).parent.parent / ".venv" / "Scripts" / "python.exe"
    if venv_python.exists() and Path(sys.executable).resolve() != venv_python.resolve():
        log.info("Relaunching inside the virtual environment...")
        result = subprocess.run([str(venv_python)] + sys.argv)
        sys.exit(result.returncode)
    os.environ["FLASK_ENV"] = "development"
    app.run(debug=True, port=5000)

