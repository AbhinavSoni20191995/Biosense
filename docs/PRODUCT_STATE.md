# Projects, runs, and what the browser is for

Three sentences hold the design:

> **Projects are durable scientific workspaces.**
> **Runs are durable server-side jobs.**
> **The browser observes them.**

Everything below follows from those, and the parts that used to contradict them
are named rather than quietly fixed.

## Projects

A project is a validated `ProjectProfile` written into the owning account's
workspace under the private data root. It is a **file**, which is what makes it
survive a browser refresh, a browser restart, a BioSense restart and a Railway
redeploy — provided the private data root is on the mounted volume, which the
hosted image now sets (`/app/data/private`) and a test asserts.

**Create project** asks for what somebody knows on day one — a name, a species, a
cell type, a starting cell, a process context, a goal — and fills in the rest
from the shared vessel: the expansion / differentiation / maturation stages and
the universal bioreactor parameters, which mean the same thing in every project.

What it will not default is biology. A project created this way has **no
simulator**, **no modelled parameter** and no cell-type-specific knob invented for
it, and it says so in its own limitations. A template can be chosen instead,
which brings a described process and, where it has one, a mechanistic model.

Switching project is a view change. It cancels nothing, touches no run, and the
selector shows what is still working in each one:

```
Human Macrophage Optimization   ● 1 active
T-cell Expansion
Microglia Differentiation       ● 2 active
```

The project panel shows the biology, the stages, the parameter count, the
simulator (or its absence), the active investigations with a way into each, the
recent runs, the hypotheses the project has produced and its limitations. It is a
panel, not project-management software.

## One authoritative run

There is exactly one run object per run, and every view renders it.

```
RunStore  ──  run_id · project_id · owner_id · objective · status · stage
              events · agents · timeline · artifacts · limitations · result
                 │                    │
          AI Discovery            Runs page
       (start one, watch it)   (history, monitoring)
```

`GET /api/discovery` answers from one list: the runs this process holds and the
records on disk, merged, scoped to the caller, with the project each belongs to.
`GET /api/discovery/<id>` is the full state. Both pages read those, and the live
viewer is one component used by both — so they cannot disagree about whether
something is running, which they could when each page had its own idea of a run.

A run appears under **Runs** as `RUNNING` the moment it is created, before the
first agent has said anything.

## A run is a background job

The browser does not own the lifecycle:

```
Browser ──POST──▶ server creates a durable run ──▶ Omnigent executes
   ▲                                                      │
   └──────────────── observes (SSE, snapshots) ◀──────────┘
```

Opening Runs, opening Data, opening the Simulator, switching project, creating
another project, refreshing, closing the browser or losing the network changes
**what is visible** and nothing about what is executing. Only **Stop run**, with
its confirmation, ends a healthy run — and then the run is recorded as
`CANCELLED`, keeping whatever the agents had already written, never as an answer.

SSE is observation. If the stream dies, Omnigent keeps going; when a client
reconnects it asks for events after the last sequence number it saw, so the gap
is replayed rather than lost, and no second Omnigent session is ever created.

Browser refresh and backend restart are different things:

| | What happens |
|---|---|
| Browser refresh | execution continues; the page re-attaches to the same run id |
| Backend restart | durable metadata recovers; a run that was in flight comes back as **INTERRUPTED**, never as COMPLETE |

## The live viewer

The failure it exists to fix: during a real Codex run the agents worked for a
quarter of an hour while the page looked static. It now shows, from typed events
and tool calls only — never from the model's prose:

* **status and engine** — `REAL AI — CODEX`, `RUNNING`, `WAITING ON AGENT`
* **an indeterminate activity bar**, meaning only *the system is active*. There is
  no percentage anywhere, because the work left is not proportional to the stages
  left and a bar implying otherwise lies once a second
* **elapsed** and **last activity** — the two numbers that answer "is it stuck?"
* **stages**, as a count (`3 / 8`), never a fraction
* **agents**: orchestrator, literature, bioinformatics, analysis, biosimulator,
  outcome — each queued, running, complete or failed, with the task it was given
* **a timeline** of what happened and when
* **limitations**: tool refusals and analysis limitations kept in front of the
  reader as `ANALYSIS LIMITATION` / `TOOL REFUSAL`, because "the metadata join is
  unsupported" is a finding about the data, not a log line

An agent is marked running because a **dispatch to it was observed**. A shell
command that happens to contain the word "analysis" is prose about an agent, not
an agent — showing work that is not happening is the failure this rule prevents.
The whole picture rebuilds from the event journal, so a restarted process shows
what a live one shows.

A small global indicator sits in the header of every page:

```
● 2 AI RUNS ACTIVE          ● Bioinformatics · 07:24
```

## Completion is not a turn ending

**This was the bug that made real runs useless.** The orchestrator is an *async*
agent, and its own instructions say:

> Dispatch with `sys_session_send` … **End your turn after dispatching; the inbox
> wakes you.**

So `response.completed` arrives within seconds of a run starting — the end of
turn one, before any science has happened. BioSense stopped watching at that
event and called the run finished, which is why a real run came back in thirty
seconds having written nothing and showing no hypothesis. The agents were never
given the chance to answer.

A turn ending is now recorded and watched, not treated as the end. The session is
finished when the **server** says so: Omnigent's session statuses are
`launching` / `running` / `waiting` / `idle` / `failed`, and `waiting` means
precisely "the parent turn is parked on sub-agent work". BioSense keeps the tail
open (reopening it when the server closes it), and ends the run when the session
reports a non-live status, confirmed more than once, with nothing arriving in
between — or when it fails, is stopped, or hits its deadline.

### The specialists are followed in their own sessions

Each specialist runs in a **child session**. Two things about those were wrong
before, and both made a run look stalled while its agents were working:

* **The parent reads `idle` while a child works.** An async orchestrator
  dispatches, ends its turn and returns to its prompt, so its own status is
  `idle` with the literature agent still searching. Ending the run at that idle
  cut the specialists off. BioSense now asks Omnigent for the session tree
  (`child_sessions_tree`, Omnigent's own "is this sub-agent busy" predicate) and
  treats any working child as "not finished". After a child finishes, the parent
  gets the full quiet period to be woken by its inbox before the run is ended.
* **A child's events never appear on the parent's stream.** The tick list and the
  agent panel only saw the orchestrator. Each working child's stream is now tailed
  beside the parent's (up to eight at once; the tree poll covers the rest), so a
  specialist's tool calls move the stage ticks and are attributed to that
  specialist. Its deltas, reasoning and turn endings are not shown.

The dispatch tool's *result* is the hand-over being accepted, not the answer, so
it no longer marks the specialist finished: the child session says when it
finished or failed. Stopping a run interrupts its working children as well as the
orchestrator. A server without the tree endpoint behaves as before, on the
parent's status alone.

### A web run has nobody to answer the orchestrator

The orchestrator's prompt (`discovery_loop/prompt.md`) was written for the
interactive production loop, and its first instruction is to *settle the request
with the person*. Given a web discovery brief it did exactly that: looked around,
asked a question, and ended its turn to wait. Nobody is in a web run to reply, so
the run finished with "the orchestrator ended without asking any specialist".

Three changes, from the cause outwards:

* the prompt now distinguishes a **web discovery run** (one-shot, already
  validated, no person, no bioreactor: skip "Start of a loop", record questions as
  open questions with the assumption made, dispatch straight away) from a
  **production loop**;
* the brief says the same in its own section, and names the exact dispatch call
  (`sys_session_send` with `agent`, `title` and `args`);
* as a safety net, if the orchestrator still ends a turn having asked no
  specialist anything, BioSense tells it **once** that nobody can answer and to
  proceed on stated assumptions. That message and what the orchestrator last said
  are both recorded in the run's stream. A second stop is the orchestrator's
  decision and the run ends on it. The message approves nothing and decides
  nothing; it restates the brief.

When a run still comes back empty, the diagnosis shows what the orchestrator said
last (its message, never its reasoning), so "asked nobody" comes with the reason.

### The agents' sandbox has to exist before a run starts

With dispatch working, the next hosted run reached both specialists, and every
shell and file tool they called failed with `linux_bwrap sandbox requires the
'bwrap' binary on PATH`. Omnigent wraps every agent tool in bubblewrap on Linux,
and the hosted image did not install it. The image now does, and a real run is
refused up front with `agent_sandbox_unavailable` when bwrap cannot start, instead
of being spent discovering it. The agents are never run without the sandbox as a
fallback.

### Sandbox, commands and builders: checked before a run, not by one

The hosted run after the bubblewrap install was refused with
`agent_sandbox_unavailable`. Reproduced in Docker:

| container | user namespaces | fresh `/proc` | what happens now |
|---|---|---|---|
| Docker default seccomp | refused | — | refused before a run, with bwrap's own words; no in-container fix exists, and the agents are never run unsandboxed (with `type: none` Omnigent hands the tools the full environment, model key included, with the network open) |
| namespaces allowed | yes | refused (masked `/proc`) | fixed automatically: `biosense.production.sandbox` detects it at boot and sets Omnigent's own `/proc`-bind switch for the runner |
| VM or permissive host | yes | yes | Omnigent's default sandbox, unchanged |

The same audit found three things no sandbox would have fixed:

* the brief told the agents to run `bioinformatics.cli execute`, which never
  existed (it is `analyse run`), and `analyse plan` refused the arguments the
  brief gave it (it needs `--evidence-gap`);
* **no command wrote `quantified_hypothesis.json` or `research_context.json`**,
  while the brief forbade hand-writing them — so even a perfect run could not
  produce a hypothesis. `biosense.evidence.cli` now builds both from an agent's
  small draft, through the same validated builders the deterministic path uses,
  and runs the simulator comparison (`simulate`);
* the stage tracker looked for `cli execute`, so "Running the analysis" could
  never tick.

`biosense.production.selfcheck` now runs that whole chain in seconds, offline,
in CI and at every boot; `--live` adds a real session in which the orchestrator
dispatches to the bioinformatics agent, which runs a tool inside the sandbox.

A run read back from disk carries `finished_at`, so its elapsed time stops where
the run did (older records without one use the last time they were written), and
its stage count is recomputed from the tick list beside it.


During a real Codex run the parent turn completed while child agents were still
writing. A run marked COMPLETE at that moment shows a result missing the files
that were still landing. So a run reports:

```
RUNNING  →  FINALIZING RESULTS  →  COMPLETE
```

`FINALIZING RESULTS` is the window where AI execution is done and BioSense is
still waiting for the run directory to go quiet, then discovering, validating and
ingesting the artifacts and building the payload the page renders. Only after
that is a run COMPLETE.

## One canonical directory

Every parent and child agent is given the same path: the run directory **relative
to the runner's workspace**, which is how the runner will resolve it. Before
this, agents were given a bare directory name and wrote under the workspace root
while BioSense read under the runs directory — the artifacts-in-two-places
problem seen in the Codex run, where the person saw neither copy.

```
RUNS_DIR/<run-dir>/   discovery_request.json   brief.md
                      research_context.json    analysis_plan*.json
                      analysis_result*.json    quantified_hypothesis*.json
                      protocol_summary.{json,md}
                      app_run.json   events.jsonl     ← the run's own record
```

## Ownership

Each project belongs to an account. Each run belongs to an account **and** a
project, recorded on the run and never inferred. A run id is not a capability:
another account asking for one by id gets a 404 from every route that can reach
it, and the run list holds your own work and nobody else's. See
[ACCOUNTS.md](ACCOUNTS.md).

## Limitations

* **Concurrency is one real run at a time** (`MAX_CONCURRENT_REAL`). A second
  real run is refused with a message saying the first is unaffected and where to
  watch it. There is no queue: the refusal is immediate and explicit.
* **Run history is a directory scan**, capped at 200 records. It is right for a
  working instance and is not a database.
* **The sandbox must permit the run directory.** The agent bundle allows
  `./runs` and `./data/runs`, which covers a laptop and the hosted image. A
  layout that puts the runs directory anywhere else makes every agent finish
  having written nothing, which looks exactly like a model that gave up.
* **Most agents run with `allow_network: false`** — only the literature agent has
  network. A real run therefore cannot search public dataset repositories, which
  limits what the bioinformatics agent can find.
* **A restarted process cannot re-attach to a live Omnigent session.** Its
  record says `INTERRUPTED`, which is the honest answer; it does not resume.
* **An anonymous workspace is a cookie.** Clearing it loses the link to those
  runs.
