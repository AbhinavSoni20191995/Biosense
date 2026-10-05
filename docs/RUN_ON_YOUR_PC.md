# Running BioSense on your own computer

**BioSense is a web application.** You start it once in a terminal and everything
else happens in the browser.

There are two paths, and the difference is only whether the AI is real.

| | **Simple path** | **Real AI** |
|---|---|---|
| What you get | the whole interface: projects, discovery, simulator, protocol, benchmarks | the same interface, with the discovery agents actually doing the work |
| Needs an API key | no | yes |
| Needs internet | only to install, once | yes |
| Extra software | none | Omnigent, plus a model provider |

Start with the **simple path**. It costs nothing, it needs no credentials, and if
it works you know the whole toolchain is sound before credentials enter the
picture.

---

## Step 0 — install the two prerequisites

**Git** and **uv**. Nothing else: uv fetches the right Python (3.12) itself, so
you do not need to install or manage Python separately.

**macOS / Linux**

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**Windows** (PowerShell)

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Close and reopen the terminal afterwards, then check it is found:

```bash
uv --version
```

If that says "command not found", uv installed to `~/.local/bin` (macOS/Linux)
and that directory is not on your PATH. Add it rather than reinstalling:

```bash
export PATH="$HOME/.local/bin:$PATH"       # add to ~/.zshrc or ~/.bashrc to keep it
```

---

## A. The simple path

```bash
git clone https://github.com/AbhinavSoni20191995/Biosense.git
cd Biosense
uv sync --locked
uv run --frozen python -m biosense.production.app --runs runs --static webapp
```

Open **<http://127.0.0.1:8000>**. That is the whole setup.

Then, in the browser:

1. **Pick a project** (or create one — see *Making your own process* below).
2. **State your objective** in your own words.
3. Optionally open *Research context, data and constraints* to set the evidence
   scope, tick the datasets you want used, and name the values you want tested.
4. Leave the runtime on **SYNTHETIC DEMO**.
5. Press **Run AI discovery**.

You will see the twelve stages tick through, then the hypothesis, the evidence,
the analyses, the candidate parameters and one recommended protocol. From there
you can open a candidate in the **Simulator**, export the protocol, or build a
**benchmark** from the run.

The other tabs:

- **Quick loop** — the older free-text stand-in loop. Type a question, watch it
  run in about a second and a half. Good for a demo.
- **Simulator** — the same reactor with the knobs in your hands. Move a setpoint,
  watch the vessel and the instruments respond day by day, compare two
  conditions. See [SIMULATOR_MODE.md](SIMULATOR_MODE.md), and
  [BIOSIMULATOR_MODEL.md](BIOSIMULATOR_MODEL.md) for how it computes an output.
- **Data** — what is registered, what can be analysed, and what cannot.
- **Runs** — everything on disk.

No branch flag: the default branch is the current one. The repository also
carries older branches from earlier in the project's history — `soni_demo2` and
a session branch — and cloning either of those gets code that predates all of
this.

Worth doing once, to confirm the clone is sound before you trust anything it
prints:

```bash
uv run --frozen python -m unittest          # expect: Ran 785 tests ... OK
```

---

### Notes that save time

- **`--frozen` matters.** It tells uv to use the locked dependency versions and
  not resolve anything. Without it a dependency can move under you between runs.
- **The port.** Add `--port 8080` if 8000 is taken.
- **It stays on your machine.** The server binds `127.0.0.1`, so nothing outside
  your computer can reach it. Only pass `--host 0.0.0.0` if you have read
  `deploy/README.md` and mean to expose it.
- **Runs persist.** Everything lands in `runs/`, which is git-ignored. The
  console lists previous loops and will serve their reports again.

### Getting the reports as PDF

The console always writes HTML. The PDF step needs a Chromium-family browser —
Chrome, Chromium, Edge or Brave — and the code looks for one on PATH and in the
usual install locations on Windows, macOS and Linux.

```bash
uv run --frozen python -m biosense.production.cli report \
  --loop-dir runs/<your-loop> --out report.html --pdf report.pdf
```

If it prints *"no Chromium found"*, it has **not** written a broken file; the
HTML is complete and prints to PDF from any browser with Ctrl/Cmd-P. If your
browser is somewhere unusual, point at it directly:

```bash
export BIOSENSE_CHROME="/path/to/chrome"            # macOS/Linux
$env:BIOSENSE_CHROME="C:\path\to\chrome.exe"        # Windows PowerShell
```

### Reproducing the committed BACH2 comparison

```bash
uv run --frozen python scripts/run_ipsc_tcell_example.py --out reports
```

This rewrites everything in `reports/`. It runs both loops from scratch, so the
numbers should come out identical — the whole path is deterministic.

---

## Making your own process

The project that ships is an example. To make your own, press **create project**
and you are offered:

- **the universal bioreactor set** — impeller, dissolved oxygen, feed schedule,
  feed interval, seeding density, temperature, stage length. Same meaning in
  every project.
- **your own parameters**, each with a declared relationship to the simulator:
  *design variable only* (real, not predicted — the honest default), *AI-proposed
  response* (the agents propose how it behaves from cited claims; predicts, and
  says `DE NOVO · UNCALIBRATED`), or *you describe it* (your own experience;
  predicts, and any value resting on it is a design choice).

Projects you create live in your workspace under the private data root. They are
never written into the repository and never served as files.

---

## B. Real AI

Real AI needs an **Omnigent runtime**, because that is what orchestrates the
specialist agents, and a **model provider**, because that is what they think
with. BioSense never starts either for you: it detects them and tells you which
part is missing.

### 1. Install Omnigent

```bash
uv tool install --python 3.12 omnigent
export PATH="$HOME/.local/bin:$PATH"      # add to ~/.zshrc or ~/.bashrc to keep it
```

### 2. Give it model credentials

```bash
export ANTHROPIC_API_KEY=sk-...           # read directly, no interactive setup
# or, on a Claude subscription:
curl -fsSL https://claude.ai/install.sh | bash
claude auth login --claudeai
```

### 3. Start the runtime, then BioSense

From the repository root — the runner's working directory is where you launch
it, and that is what makes `runs/` the one BioSense reads:

```bash
omnigent start                            # local server + a runner, in the background

BIOSENSE_RUNTIME_MODE=local \
BIOSENSE_ALLOWED_RUNTIMES=synthetic,local \
  uv run --frozen python -m biosense.production.app --runs runs --static webapp
```

Open <http://127.0.0.1:8000>, choose **REAL AI — LOCAL**, and press
**Run AI discovery**. You never retype the question anywhere else.

A loopback Omnigent server runs as a single local user and needs no login, so
there is nothing else to configure.

### If it says the runtime is unavailable

It will say which of these it is, and the command that fixes it:

| What it says | What to do |
|---|---|
| The Omnigent client library is not installed | `uv sync --extra omnigent` |
| Nothing answered at the configured server | `omnigent start` |
| No online runner | `omnigent host --server http://127.0.0.1:6767` |
| The agent is not registered | `omnigent server --agent discovery_loop` |
| The server requires authentication | `omnigent login <server>`, then set `BIOSENSE_OMNIGENT_TOKEN` |
| The runs directory is outside the workspace | start BioSense with `--runs` inside the directory you launched `omnigent start` from |

**It will not fall back to the synthetic path.** A synthetic answer presented as
a real one is the one thing this product must never do.

### A remote Omnigent server

```bash
export BIOSENSE_RUNTIME_MODE=remote
export BIOSENSE_OMNIGENT_SERVER=https://your-omnigent-server
export BIOSENSE_OMNIGENT_TOKEN="$(cat ~/.omnigent/token)"   # or BIOSENSE_..._TOKEN_FILE
export BIOSENSE_ALLOWED_RUNTIMES=synthetic,remote
uv run --frozen python -m biosense.production.app --runs runs --static webapp
```

The token is read once at startup, sent as an `Authorization: Bearer` header, and
never reaches the browser, an event, a log line or an error message. BioSense
stores no password of yours: signing in through the page forwards your
credentials to that Omnigent server once and keeps only the session token it
returns, server-side.

One honest limitation: with a remote runtime the agents write their artifacts on
the runner's filesystem, which is not yours. Progress, the stage timeline and the
session reference all arrive; structured artifacts only do where they reach
BioSense. A run whose artifacts never arrive is labelled as such rather than
rendering an empty report that looks like a finding.

---

## C. The advanced CLI path

Everything above is also reachable from a terminal, and these remain fully
supported — they are simply no longer how you are expected to use the product.

```bash
# the orchestrator and its specialists, directly
omnigent run discovery_loop

# the deterministic loop, offline
uv run --frozen python -m biosense.production.cli demo --out runs/prod-demo

# benchmarks
uv run --frozen python -m biosense.benchmark.cli list
uv run --frozen python -m biosense.benchmark.cli run --config benchmarks/configs/<id>.json

# everything that needs no credentials, in one command
bash scripts/check.sh
```

For a first live session, hand the orchestrator an existing request rather than
negotiating one, so a failure points at the runtime instead of at the
conversation:

```
Read discovery_loop/prompt.md. Initialise a loop from
examples/cart/request.cart_d10.json into runs/live-cart-<date>/ and work it.
It declares the synthetic stand-in reactor, so simulate-standin replaces the wet
lab; label every number accordingly. Use .venv/bin/python for all tools.
```

### What a first live session is actually testing, in order

1. whether the harness starts at all;
2. whether messages reach the sub-agents and the orchestrator wakes;
3. whether the sandbox lets `.venv/bin/python` run and write under `runs/`;
4. whether the literature agent reaches Europe PMC;
5. and only then, whether the model's choices survive `propose-decision`.

A refusal from `validate_decision` is the system working, not a bug to debug
away. If the model proposes something outside the allowed set, the envelope
should reject it — that is the whole point of the design.

### Watching it while it runs

The console's dashboard reads the same `runs/` directory, so in a second
terminal:

```bash
uv run --frozen python -m biosense.production.serve --runs runs --static webapp
```

<http://127.0.0.1:8000> then shows the agent tracker, polling every four
seconds. It is strictly read-only: it cannot approve a protocol, commit a
decision or answer a consult, which stay CLI acts with a named person.

---

## Windows specifics

Everything in **A** works natively in PowerShell. Two differences:

- `scripts/check.sh` is a bash script. Run it under Git Bash or WSL, or just run
  `uv run --frozen python -m unittest` instead, which is most of what it covers.
- Paths inside the agent prompts use `.venv/bin/python`; on native Windows the
  interpreter is at `.venv\Scripts\python.exe`. For **B**, use WSL and avoid the
  problem entirely.

---

## If something goes wrong

| What you see | What it means |
|---|---|
| `uv: command not found` | uv installed to `~/.local/bin`; add it to PATH |
| `Ran 563 tests ... FAILED` | the clone is not sound — do not trust its output; open an issue with the failure |
| `no Chromium found` | PDF only. The HTML is complete; print it from a browser |
| `address already in use` | something else holds the port; pass `--port 8080` |
| `the engine declined to start` | a gate refused, not a crash. The message names which one |
| `decision refused: ...` | the envelope rejected a decision. Working as designed |
| Omnigent cannot find a tool | you are not in the repository root, or `uv sync` has not been run |
