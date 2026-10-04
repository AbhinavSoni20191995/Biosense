# Local setup and handoff guide

Checked against official Omnigent and uv docs on 2026-10-04. Commands target Linux
or macOS. Omnigent is the challenge platform's name; omni is its shorter CLI name.

## Separate the pieces

Codex builds/edits the project. Omnigent coordinates a running agent. The project
Python tools search and validate evidence. uv manages project dependencies and
the .venv environment. Omnigent should normally be installed as a separate tool,
not mixed into this application's environment.

## Install and establish the local environment

Official uv installer:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Restart the terminal if uv is not found. Omnigent's official bootstrap installs
its dependencies and can offer missing system prerequisites:

```bash
curl -fsSL https://raw.githubusercontent.com/omnigent-ai/omnigent/main/scripts/install_oss.sh | sh
```

Manual alternative, if prerequisites are already installed:

```bash
uv tool install --python 3.12 omnigent
```

Choose one Omnigent installation route. Its prerequisites include Git, Python
3.12+, Node 22+ and npm; pnpm is used for the web UI. Native Codex launch uses
tmux and, on Linux, bubblewrap. A Python .venv alone does not provide these.

Verified locally on 2026-10-04 with uv 0.11.2, Python 3.12.3 and
omnigent 0.16.0 (installed via `uv tool install --python 3.12 omnigent`; the
PyPI wheel bundles the web UI, so Node 22 is only needed to build from source).

From the repo root, run:

```bash
uv sync --locked
uv run --frozen python -m unittest -v
```

uv sync creates .venv; uv run uses it without manual activation. Commit
pyproject.toml, uv.lock and .python-version; exclude .venv. If integrating into
an existing project, merge configuration rather than overwriting its files.
For a new project without this scaffold, uv init --python 3.12 creates the
project metadata, followed by uv sync.

## Use Codex to build the integration

Open the project directory in your usual local Codex environment. Tell it:

"Read AGENTS.md and BUILD_TASK.md. Complete the literature-agent integration
using the existing tools. Start by inspecting this repository and current
Omnigent CLI/docs. Run the tests and a live smoke test. Preserve existing code."

BUILD_TASK.md is the project task, AGENTS.md contains repository conventions,
and agent_prompt.md contains the scientific agent's runtime instructions. They
have different jobs.

## Run Omnigent locally

```bash
omnigent setup
omnigent codex
```

Run from the repo root. Configure the Codex harness/provider credentials in the
setup flow. Using Codex to develop code is separate from selecting it as the
model harness for the scientific runtime. Omnigent may use an existing logged-in
Codex CLI. Follow setup's prompts if authentication is needed.

First tell the running session:

"Read agent_prompt.md and request.example.json. Perform a literature search
using agent_tools.py; use uv run --frozen --no-sync python for commands. Save
source files, extraction and compiled handoff under runs/. Do not edit the
source code during this research run. Preserve conflicts and missing evidence."

For a reusable configuration, the provided YAML uses the current official
instructions/executor/os_env fields. It has not been live-tested here:

```bash
omnigent run literature_agent.yaml
```

Supply the same research request in that session. The YAML currently uses the
harness's local OS/CLI tools; it does not register named Python functions. Codex
should verify OS access, write-path resolution, uv/interpreter access and
credential routing on your installed version. Run uv sync before the session;
--no-sync avoids dependency changes during research. If the sandbox blocks an
operation, inspect the actual path/network error and fix the narrow permission
or prerequisite. Do not silently remove isolation to make the demo work.

The browser-first option is omnigent start, then the local URL it prints. The
first-run CLI normally exposes a UI at localhost:6767.

## Verify the Python path before debugging the LLM

```bash
uv run --frozen python agent_tools.py search '(hiPSC OR "induced pluripotent stem cell") AND cardiomyocyte AND bioreactor' --out runs/search.json
uv run --frozen python agent_tools.py fetch PMC7076930 --out runs/source.json
uv run --frozen python agent_tools.py compile --request request.example.json --extraction example.extraction.json --sources runs/source.json --out runs/handoff.json
```

This replays manually annotated claims against a newly retrieved source. It
does not prove autonomous LLM extraction. The next smoke test is a live Omnigent
session that searches, reads, creates NEW extraction JSON and compiles it.

## Test and deploy in distinct stages

Offline CI checks code and synthetic rejection/unit cases without model keys or
internet-dependent tests. Live smoke tests check retrieval, credentials and
actual runtime tools. Evaluation checks extracted claims against annotations
and verifies biological context and applicability. Keep all three visible.

For a laptop demo, local Omnigent is sufficient. For a team with managed Databricks
access, use the enabled Omnigent/Sandbox workspace route. A local project is not
automatically present in a managed Sandbox; clone/copy it there and run uv sync.

For an always-on remote system, distinguish the coordination server from the
execution host. Use an official Omnigent server deployment template (Docker,
managed Databricks or another documented host), then register an execution host:

```bash
omnigent login https://YOUR-SERVER
omnigent host --server https://YOUR-SERVER
```

YOUR-SERVER is a placeholder. On the execution host, clone the project, sync its
environment, configure model credentials through the supported flow and verify
literature API access. A remote server with a laptop execution host still stops
executing work when the laptop is offline. Use a persistent host/cloud sandbox
for continuous execution. Enable authentication/HTTPS and preserve outputs.

This template has no public HTTP application or production deployment yet.
Docker/FastAPI/MCP are optional extensions, not required for the first local run.

Before handing off: fresh-clone tests pass; lockfile is committed; the Omnigent
version/harness is recorded; a live run succeeds; extraction accuracy is checked;
budget limits are enforced; source/run manifests and simulator handoff are saved.
Current source-license restrictions and API limits still apply.

Sources:
https://github.com/omnigent-ai/omnigent
https://github.com/omnigent-ai/omnigent/blob/main/docs/AGENT_YAML_SPEC.md
https://github.com/omnigent-ai/omnigent/blob/main/deploy/README.md
https://docs.databricks.com/aws/en/omnigent/quickstart
https://docs.astral.sh/uv/guides/projects/
https://docs.astral.sh/uv/guides/integration/github/
