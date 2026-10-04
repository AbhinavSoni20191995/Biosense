# The BioSense web app: the prompt console plus the read-only artifact API.
#
# What this image can do: accept a typed question, run the loop against a
# SYNTHETIC STAND-IN reactor, stream it, and write a reasoning report.
#
# What it cannot do, enforced in code rather than by convention: reach a real
# bioreactor (the engine refuses any request whose bioreactor_source is not
# synthetic_standin, and the prompt parser forces that field), approve a protocol
# as a named person, serve any file whose name contains "truth", or call a model
# - it holds no model credentials and nothing on this path makes an LLM request.
#
# Running the agents themselves (`omnigent run discovery_loop`) is a separate
# container that DOES hold credentials and must sit behind authentication.
# See deploy/README.md.
FROM python:3.12-slim

# uv from PyPI rather than `COPY --from=ghcr.io/astral-sh/uv`. Both give the same
# pinned binary; PyPI is reachable from more build environments, including ones
# whose egress policy does not allow the GitHub container registry.
RUN pip install --no-cache-dir uv==0.12.23

WORKDIR /app
ENV UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PYTHONUNBUFFERED=1

# Dependencies first, so editing the source does not reinstall numpy and scipy.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project

# Two files live at the repo root and are reached from inside the package:
# biosense.production.protocol imports convert_unit from agent_tools, and the
# contract registry resolves the literature handoff schema as
# schemas/../output.schema.json. Without either, the image starts and then fails
# on the first request.
COPY agent_tools.py output.schema.json ./
COPY biosense/ ./biosense/
COPY standins/ ./standins/
COPY schemas/ ./schemas/
COPY qc_profiles/ ./qc_profiles/
COPY bioinfo_knowledge/ ./bioinfo_knowledge/
COPY analysis_agent/ ./analysis_agent/
COPY examples/ ./examples/
COPY webapp/ ./webapp/
RUN uv sync --locked

# RUNS_DIR holds the loop directories. On Railway, mount a volume here so the
# artifacts and their reports survive a redeploy; without one the filesystem is
# ephemeral and every run is lost on the next deploy.
ENV RUNS_DIR=/data/runs \
    PORT=8000
RUN mkdir -p /data/runs

# A non-root user: this process accepts input from the internet and starts work.
# It needs a writable home and cache directory, because uv initialises its cache
# even with --no-sync and fails to start without one. uid 1000 because Hugging
# Face Spaces mounts its storage for that uid; nothing else depends on the number.
ENV UV_CACHE_DIR=/home/biosense/.cache/uv
RUN useradd --uid 1000 --create-home --home-dir /home/biosense biosense \
 && mkdir -p "$UV_CACHE_DIR" \
 && chown -R biosense /data /app /home/biosense
USER biosense

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",8000)}/healthz')"

# 0.0.0.0 because the platform routes to the container from outside.
CMD ["sh", "-c", "exec uv run --frozen --no-sync python -m biosense.production.app \
     --runs \"$RUNS_DIR\" --static webapp --host 0.0.0.0 --port \"$PORT\""]
