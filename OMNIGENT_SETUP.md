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

## Run the production loop bundle

`literature_agent.yaml` above runs the single literature agent. The main loop is
the multi-agent bundle in `discovery_loop/`: the orchestrator plus five
sub-agent directories under `discovery_loop/agents/`.

Both uv and omnigent install to `~/.local/bin`. If `command -v omnigent` fails,
that directory is missing from PATH — add it rather than reinstalling.

Verify everything that needs no model credentials, in one command:

```bash
bash scripts/check.sh
```

It syncs the environment, runs the unit tests, runs both offline loop smoke
tests, prints the CAR-T decision trail, and parses every agent config through
Omnigent's own `omnigent.spec` parser and validator. Rung 4 is skipped when
omnigent is absent, so the script is safe in CI.

Verified on 2026-10-04 with uv 0.12.23, Python 3.12.15 and omnigent 0.16.0:
153 tests pass; `demo` and `demo-cart` exit 0; all six agent specs validate with
no errors; `tools.agents` as a list of directory names resolves against
`discovery_loop/agents/` exactly as declared.

### Watch a loop in a browser

```bash
uv run --frozen python -m biosense.production.serve --runs runs --static webapp
```

Then open http://127.0.0.1:8000. It serves `webapp/index.html` (the agent
tracker) over a read-only JSON API of whatever is in `runs/`: `/api/loops`,
`/api/loops/<loop_id>`, `/healthz`. The page polls every 4 seconds and switches
from its recorded CAR-T replay to the loop on disk. `bash scripts/check.sh` fills
`runs/` first, so this works before any live session.

Read-only is the point: the dashboard shows decisions, it never makes them.
Approving a protocol, committing a decision and answering a consult stay CLI acts
with a named person. Files whose names contain `truth` are never served, so the
stand-in's hidden answers stay hidden. See `deploy/README.md` for hosting it.

### The live session

This is the part that needs credentials. Two things must be in place.

The `claude-sdk` harness shells out to the Claude CLI, so that binary must exist:

```bash
curl -fsSL https://claude.ai/install.sh | bash
claude auth login --claudeai      # subscription; or set ANTHROPIC_API_KEY instead
```

Omnigent reads `ANTHROPIC_API_KEY` (and `OMNIGENT_ANTHROPIC_API_KEY`) straight
from the environment, so an API key needs no interactive setup at all. Then:

```bash
export PATH="$HOME/.local/bin:$PATH"
cd /path/to/biosense-ai
uv sync --locked
omnigent run discovery_loop
```

`omnigent setup` walks the same ground interactively if you would rather be
prompted, and `omnigent config list` shows which harness credentials it found.

Run it from the repository root — the bundle's `os_env.cwd` is `.`, the
sub-agents invoke `.venv/bin/python`, and `uv sync --locked` must have created
that venv first. Write access is confined to `./runs`. The orchestrator and the
bioinformatics, analysis, biosimulator and outcome agents run with
`allow_network: false`; only the literature agent has network, because only it
queries Europe PMC.

For the first live session, hand it an existing request rather than negotiating
one, so a failure points at the runtime instead of at the conversation:

```
Read discovery_loop/prompt.md. Initialise a loop from
examples/cart/request.cart_d10.json into runs/live-cart-<date>/ and work it.
It declares the synthetic stand-in reactor, so simulate-standin replaces the wet
lab; label every number accordingly. Use .venv/bin/python for all tools.
```

That request sets `bioreactor_source: synthetic_standin`, `mode: checkpoints`
and a 4-iteration budget, and its arms are a wild-type control against an
`EXH1` knockout. `EXH1` is an invented gene in a synthetic fixture: the run
demonstrates the workflow and produces no biological evidence.

What a live run is actually testing, in order: whether the harness starts;
whether `sys_session_send` reaches the sub-agents and the inbox wakes the
orchestrator; whether the sandbox lets `.venv/bin/python` run and write under
`runs/`; whether the literature agent reaches Europe PMC; and only then whether
the orchestrator's choices survive `propose-decision`. A refusal from
`validate_decision` is the system working, not a failure to debug away.

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
