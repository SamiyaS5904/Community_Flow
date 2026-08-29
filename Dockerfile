# Dockerfile for Render.com Deployment
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies required by Playwright
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browser and OS dependencies
RUN playwright install chromium
RUN playwright install-deps chromium

# Copy the rest of the application
COPY . .

# Run as a non-root user for container security hardening.
RUN useradd --no-create-home --shell /bin/false appuser \
    && chown -R appuser:appuser /app
USER appuser

# Run the Flask app using Gunicorn on the port provided by Render.
# Worker count: 3 — allows dashboard requests to be served while a background
#   batch-generation thread is occupying one worker.
# Timeout: 300s — LLM pipeline worst case (3 retries × 8s × 4 agents = ~120s)
#   plus Chromium rendering (~30–60s) with generous headroom.
CMD gunicorn -w 3 --timeout 300 -b 0.0.0.0:$PORT dashboard.app:app
