# Dashboard image: the read-only artifact API plus the tracker page.
# It serves what a loop has written. It does not run agents and holds no model
# credentials, so a public URL cannot start work, approve a protocol or spend
# tokens. Running the loop itself is a separate container; see deploy/README.md.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PYTHONUNBUFFERED=1

# Dependencies first, so edits to the source do not reinstall numpy and scipy.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project

COPY biosense/ ./biosense/
COPY standins/ ./standins/
COPY schemas/ ./schemas/
COPY qc_profiles/ ./qc_profiles/
COPY analysis_agent/ ./analysis_agent/
COPY webapp/ ./webapp/
RUN uv sync --locked

# RUNS_DIR is where loop directories live. On Railway, mount a volume here so
# artifacts survive a redeploy; without one the filesystem is ephemeral.
ENV RUNS_DIR=/data/runs \
    PORT=8000
RUN mkdir -p /data/runs

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",8000)}/healthz')"

# 0.0.0.0 because the platform routes to the container from outside.
CMD ["sh", "-c", "exec uv run --frozen --no-sync python -m biosense.production.serve \
     --runs \"$RUNS_DIR\" --static webapp --host 0.0.0.0 --port \"$PORT\""]
