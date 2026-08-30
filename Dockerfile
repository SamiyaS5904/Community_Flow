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

# Worker count and browser pool multiply: each Gunicorn worker runs its own
# render pool, so total Chromium processes = workers × RENDER_POOL_SIZE. Three
# workers at the default pool size of two is six browsers, which will exhaust a
# small instance. One browser per worker is the right default for a 1–2 GB box;
# raise RENDER_POOL_SIZE only with the memory to back it.
#
# The 300s timeout covers the worst case: the LLM chain with retries (~120s)
# plus a cold Chromium render, with headroom.
ENV RENDER_POOL_SIZE=1
CMD gunicorn -w 3 --timeout 300 -b 0.0.0.0:${PORT:-5000} dashboard.app:app
