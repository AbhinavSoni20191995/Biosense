# Running BioSense on your own computer

Two things you can run, and they have very different requirements.

| | What you get | Needs an API key | Needs internet |
|---|---|---|---|
| **A. The console** | type a question, watch the loop, get a reasoning report | **no** | only to install, once |
| **B. The Omnigent run** | the same loop, driven by a model, with live literature search | **yes** | yes |

Start with **A**. It is the one to record for a demo, it costs nothing to run,
and if it works you know the whole toolchain is sound before credentials enter
the picture.

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

## A. The console

```bash
git clone https://github.com/AbhinavSoni20191995/Biosense.git
cd Biosense
uv sync --locked
uv run --frozen python -m biosense.production.app --runs runs --static webapp
```

No branch flag: the default branch is the current one. The repository also
carries older branches from earlier in the project's history — `soni_demo2` and
a session branch — and cloning either of those gets code that predates the
console, the charts and the reports.

Open **<http://127.0.0.1:8000>**, type a question, press *Run the loop*.

The **Simulator** tab beside it is the same reactor with the knobs in your hands:
move a setpoint, watch the vessel and the instruments respond day by day, compare
two conditions. It runs in about a tenth of a second and needs nothing extra —
see [docs/SIMULATOR_MODE.md](SIMULATOR_MODE.md).

That is the whole setup. On a clean clone this takes about a minute, most of it
downloading numpy and scipy, and a run of the BACH2 comparison then finishes in
under two seconds.

Worth doing once, to confirm the clone is sound before you trust anything it
prints:

```bash
uv run --frozen python -m unittest          # expect: Ran 269 tests ... OK
```

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

## B. The Omnigent-driven run

Everything above still applies; this adds a model on top. Install Omnigent as a
**separate tool**, not into this project's environment:

```bash
uv tool install --python 3.12 omnigent
export PATH="$HOME/.local/bin:$PATH"
omnigent --version                          # expect 0.16.0 or later
```

Give it credentials, either way round:

```bash
export ANTHROPIC_API_KEY=sk-...             # read straight from the environment
# or, on a Claude subscription:
curl -fsSL https://claude.ai/install.sh | bash
claude auth login --claudeai
```

On Windows, set the key with `$env:ANTHROPIC_API_KEY="sk-..."`, or use WSL —
Omnigent's sandbox expects a Unix-like environment and WSL is the smoother path.

Check everything that costs nothing **before** spending a token. This validates
all six agent specs through Omnigent's own parser:

```bash
bash scripts/check.sh
```

Then, from the repository root — this matters, the sub-agents call
`.venv/bin/python` and the bundle's working directory is `.`:

```bash
uv sync --locked
omnigent run discovery_loop
```

Hand it an existing request rather than negotiating one, so that a failure
points at the runtime instead of at the conversation:

```
Read discovery_loop/prompt.md. Initialise a loop from
examples/ipsc_tcell/request.bach2_d40.json into runs/live-bach2-<date>/ and work it.
It declares the synthetic stand-in, so use simulate-standin --standin ipsc_tcell.
Use .venv/bin/python for all tools. Write the reasoning report when the loop ends.
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
| `Ran 269 tests ... FAILED` | the clone is not sound — do not trust its output; open an issue with the failure |
| `no Chromium found` | PDF only. The HTML is complete; print it from a browser |
| `address already in use` | something else holds the port; pass `--port 8080` |
| `the engine declined to start` | a gate refused, not a crash. The message names which one |
| `decision refused: ...` | the envelope rejected a decision. Working as designed |
| Omnigent cannot find a tool | you are not in the repository root, or `uv sync` has not been run |
