<div align="center">

<img src="webapp/assets/biosense-logo.png" alt="BioSenseAI" width="300">

### Ask a cell-production question in plain language.
### Watch agents work it. Read why every choice was made.

<a href="docs/RUN_ON_YOUR_PC.md"><b>Run it on your PC</b></a> ·
<a href="docs/IPSC_TCELL_EXAMPLE.md"><b>Worked example</b></a> ·
<a href="reports/"><b>Example reports</b></a> ·
<a href="deploy/README.md"><b>Hosting</b></a>

</div>

---

## The problem

Optimising how a cell product is grown is a loop: read the literature, design a
protocol, run it, read the result, change one thing, run it again. Each turn of
that loop takes days of bench time, and the reasoning behind each change usually
lives in someone's head or a lab notebook.

Language models are good at the reading and the reasoning. They are **not** good
at being trusted with the arithmetic, the pass/fail calls, or the authority to
spend a week of someone's cells. So this project splits those jobs apart.

## What BioSense is for

The bottleneck in cell-based therapy is not ideas — it is that biological
manufacturing and experimental optimisation stay slow, fragmented and
trial-and-error, with the literature, the datasets, the process variables and the
measurements all living in different places and joined up by hand.

BioSense turns a high-level biological objective into an iterative
**design → run → measure → decide** workflow:

- **One objective in, a loop out.** A scientist states the aim and the
  constraints once; the system turns that into protocols, runs, measurements and
  the decision about what to change next.
- **Specialised agents, orchestrated.** Separate agents for literature evidence,
  bioinformatics, data analysis, simulation and experimental planning, each doing
  one job and handing on a checkable document. *(All five ship in
  [`discovery_loop/`](discovery_loop/).)*
- **Prior knowledge combined with new results.** Every iteration carries the
  cited evidence and the annotations forward, alongside what the last run
  actually measured.
- **Measurement-driven, not schedule-driven.** Sensor and analytical channels —
  viable cell density, viability, glucose, lactate, marker purity, release tests
  — feed the analysis, which decides whether the data can even carry a
  conclusion before it decides anything else.
- **The scientist stays in the loop by design.** Ask a question, define the
  desired outcome, review the assumptions and the reasoning. The complexity sits
  underneath; the judgement stays with the person.
- **Fewer wasted iterations.** The search reverts anything that does not beat an
  arm's best result, so a run is never spent re-walking ground already covered.

**The long-term goal** is a closed-loop discovery and manufacturing system that
learns from every experiment and helps move safer, more effective cell therapies
to patients faster. Three pieces of that are *not* built yet, and the repository
does not pretend otherwise: the loop runs against a **synthetic stand-in, not a
real bioreactor**; **live model-driven orchestration has not been run**; and each
loop starts fresh, so there is **no learning carried across runs**. Everything
below describes what actually runs today.

## The idea, in one picture

**The agent chooses. Separate, deterministic code decides what it is allowed to
choose — and refuses the rest.**

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/loop-dark.svg">
  <img alt="The orchestrator agent reads the analysis and proposes one action. A decision envelope made of deterministic code checks that action against what the verdict permits, and refuses anything outside it. Protocols go through a named human approver before any wet-lab run, and the analysis that feeds the next decision is computed, never written by a model." src="docs/assets/loop-light.svg" width="100%">
</picture>

Everything a model writes is a **proposal**. Everything that counts as a fact —
the verdict, the QC calls, the metrics, the comparison between arms — is
computed. If the agent proposes something the verdict does not permit, the
envelope refuses it and says why.

| The model does | The code does |
|---|---|
| Reads papers, extracts cited claims | Computes every metric and verdict |
| Forms a hypothesis about why a run failed | Decides which actions the verdict permits |
| Chooses one action and explains it | Refuses anything outside that set |
| Writes the brief for the next protocol | Enforces that only a revision costs an iteration |

---

## Try it in three commands

Install [uv](https://docs.astral.sh/uv/) (it fetches Python for you), then:

```bash
git clone https://github.com/AbhinavSoni20191995/Biosense.git
cd Biosense && uv sync --locked
uv run --frozen python -m biosense.production.app --runs runs --static webapp
```

Open **<http://127.0.0.1:8000>** and type a question.

> No API key. No internet after install. Nothing calls a model. A full
> 25-iteration run finishes in about **1.5 seconds**.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/console-dark.png">
  <img alt="The BioSense console: a question box, the result as large numbers per arm, a search-trajectory chart, and an export button for the full report." src="docs/assets/console-light.png" width="100%">
</picture>

While it runs, the agents announce what they are doing:

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/agents-dark.png">
  <img alt="Transient cards naming each agent as it works: the analysis agent reporting a verdict, the orchestrator committing a decision, the reporter writing the report." src="docs/assets/agents-light.png" width="100%">
</picture>

Full setup, including Windows → **[docs/RUN_ON_YOUR_PC.md](docs/RUN_ON_YOUR_PC.md)**

---

## A worked result

Two questions, one run each:

1. *Optimise wild-type T cells grown from iPSC.*
2. *Now do it with a **BACH2 knockout** that must reach the same target.*

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/trajectory-dark.png">
  <img alt="Two lines climbing toward a dashed target line at 25. The wild-type line starts near 20 and reaches the target quickly; the knockout line starts near 7 and takes far longer, with visible steps backwards where moves were reverted." src="docs/assets/trajectory-light.png" width="100%">
</picture>

| | Arms | Iterations used | Outcome |
|---|---|---|---|
| Wild type alone | WT | **3** of 30 | 25.7 — target met |
| With the knockout | WT + BACH2 KO | **25** of 30 | 25.7 and 26.0 — both met |

The number to look at is **3 against 25**. Adding one knocked-out arm multiplied
the search eightfold, and the loop says why in its own words:

> IL-7 cannot be set to one shared value: moving it 16 → 25.6 ng/mL improved
> BACH2_KO and degraded WT. The arms are being given separate values of this
> parameter from here on.

That is the finding: **no single shared recipe could serve both genotypes.** The
search established it from measurements rather than assuming it, then split the
parameter per arm. The knockout ended up needing more IL-7 at every stage and a
weaker TCR stimulus.

Reports, committed and readable without running anything →
**[`reports/`](reports/)** · the full write-up, including everything this does
*not* show → **[docs/IPSC_TCELL_EXAMPLE.md](docs/IPSC_TCELL_EXAMPLE.md)**

---

## Every run explains itself

The console shows the answer. The exported report carries the justification:

- **every decision** — its reasoning, the alternatives the envelope refused and
  *why each was refused*, the hypotheses raised and the basis for each;
- **every search move** — what it changed, for which arm, what happened, and
  whether it survived or was reverted;
- **every quantity** in the protocol with its provenance, so the line between a
  cited value and a chosen one is visible at a glance;
- **references** with DOIs, what each was used for, and whether its full text was
  ever actually retrieved.

A decision made by deterministic code is labelled as such. The report never
presents a rule as a model's reasoning.

---

## What this is **not**

An evaluator should know the limits before the features.

- **The bioreactor is a synthetic stand-in.** It is a phenomenological model, not
  a digital twin. No number this produces is a measurement of any real cell.
- **The stand-in was built to reward the levers the annotations suggest.** That
  is what makes the demonstration legible, and exactly what makes it worthless as
  biology. A real experiment could reward the opposite.
- **No protocol value is attributed to a publication.** The citations support the
  *direction* of a lever; every number is a design choice. The reports show that
  count rather than hiding it.
- **The cited BACH2 work is in peripheral and engineered T cells**, not
  iPSC-derived ones. The knowledge set separates what a paper reports
  (`local_annotation`) from a transfer to this cell type (`inference`).
- **One replicate per arm.** Every between-arm difference is directional only.
- **A loop that reaches a target has not produced a validated process.**
  Confirmation runs, replicate design and human QA sign-off are all outside it.
- **Live model-driven orchestration has not been run yet.** The deterministic
  path is fully exercised; the Omnigent path is set up and validated but a first
  live session remains a genuine test.

---

## Safety properties, enforced in code

Not conventions — things the software refuses to do:

| Rule | Where |
|---|---|
| A wet-lab run always needs a **named human approver**, whatever the autonomy mode says | `production/autonomy.py` |
| The web app refuses any request that is not a synthetic stand-in | `production/engine.py` |
| Agents never read the stand-in's hidden answers; no server ever serves a file named `truth` | `production/serve.py`, `report.py` |
| A gap in a protocol blocks the wet lab | `production/protocol.py` |
| Targets and QC limits can never be changed after results are seen | `production/orchestrator.py` |
| An unannotated gene returns `found: false` with the public queries to run — never a guessed effect | `bioinformatics/tools.py` |

```bash
uv run --frozen python -m unittest     # 233 tests
bash scripts/check.sh                  # + offline loop smoke tests + agent-spec validation
```

---

## Where things are

| Path | What it holds |
|---|---|
| [`webapp/console.html`](webapp/console.html) | the console you see above |
| [`biosense/production/`](biosense/production/) | the loop: designer, optimiser, analysis, envelope, reports |
| [`standins/`](standins/) | synthetic stand-in reactors |
| [`discovery_loop/`](discovery_loop/) | the Omnigent agent bundle for the live, model-driven path |
| [`bioinfo_knowledge/`](bioinfo_knowledge/) | gene annotations, each with its own confidence and citation |
| [`reports/`](reports/) | the committed example reports, HTML and PDF |
| [`docs/`](docs/) | setup, the worked example, the production loop in depth |

**Deeper reading:** [the production loop](docs/PRODUCTION_LOOP.md) ·
[the worked example](docs/IPSC_TCELL_EXAMPLE.md) ·
[running it yourself](docs/RUN_ON_YOUR_PC.md) ·
[the literature agent](docs/LITERATURE_AGENT.md) ·
[bioinformatics knowledge sets](bioinfo_knowledge/README.md) ·
[max-mode instruments](analysis_agent/README.md) ·
[hosting](deploy/README.md) · [Omnigent setup](OMNIGENT_SETUP.md)

---

<div align="center">
<sub>Not clinical or manufacturing guidance. Apache-2.0.</sub>
</div>
