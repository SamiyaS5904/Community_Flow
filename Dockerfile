# CommunityFlow — production image.
#
# The only unusual requirement is Chromium: the renderer drives a real browser,
# so the image carries one and its system libraries. Everything else is a plain
# Python web service.

FROM python:3.11-slim

WORKDIR /app

# Where Playwright puts its browsers, and where it looks for them at runtime.
# This must be set BEFORE the install: engine/config.py points this variable at
# /app/.playwright-browsers when it is unset, so an install that landed in
# root's default cache left the app looking in an empty directory and every
# render failed with "Executable doesn't exist" — after a successful build.
ENV PLAYWRIGHT_BROWSERS_PATH=/app/.playwright-browsers \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Python dependencies first, so a code change does not reinstall them.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Chromium plus the system libraries it needs. install-deps must run as root.
RUN playwright install --with-deps chromium

COPY . .

# Non-root at runtime. The browser directory has to be readable by that user —
# it was installed above as root.
RUN useradd --no-create-home --shell /bin/false appuser \
    && mkdir -p /app/generated \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 5000

# ── memory ───────────────────────────────────────────────────────────────────
# This is a single-operator tool, and every Gunicorn worker is a full copy of
# it: Flask, SQLAlchemy, the OpenAI client, Playwright's bindings, and its own
# render pool. Three workers were roughly 450 MB of Python before a single
# graphic was drawn, and a Chromium render on top of that is what exceeded the
# instance's memory limit and got the service restarted.
#
# One worker with threads serves the same load. Threads share one interpreter,
# so the dashboard still answers while a render or an LLM call is in flight —
# both release the GIL waiting on I/O, which is all this app ever waits on.
# One worker also means one reconciler and one browser instead of three of
# each; the advisory lock stays as the guard for when more than one instance
# is running.
#
# Scaling up is a number change, not a redesign: on a 2 GB instance, -w 2
# --threads 8 is comfortable.
#
# --max-requests recycles the worker periodically. Chromium is spawned and
# killed repeatedly here, and a long-lived process that does that tends to
# fragment; a scheduled restart bounds it. The jitter stops a restart landing
# in the same place every time.
#
# The 300s timeout covers the worst case: the LLM chain with retries (~120s)
# plus a cold Chromium render, with headroom.
ENV RENDER_POOL_SIZE=1
CMD gunicorn -w 1 --threads 8 --timeout 300     --max-requests 400 --max-requests-jitter 50     -b 0.0.0.0:${PORT:-5000} dashboard.app:app
