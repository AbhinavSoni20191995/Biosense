# Architecture review: making the BioSense web app the primary interface

Written before any code changed, against this repository at
`91e9db8` and against **omnigent 0.16.0** installed from PyPI and
probed live on a loopback server. Every Omnigent claim below was checked against
the installed package source or an actual HTTP call; nothing here rests on an
endpoint I assumed existed.

What the review is for: today a visitor to the BioSense web app gets a
deterministic synthetic stand-in loop, and the real AI orchestration lives behind
`omnigent run discovery_loop` in a terminal. Those are two products. This sets
out how one of them becomes infrastructure under the other.

---

## 1. How the BioSense app starts runs today

`biosense/production/app.py` is a `ThreadingHTTPServer` over the stdlib. The only
way to start work is:

```
POST /api/runs  {"prompt": "<free text, ≤2000 chars>"}
```

`Registry.start` does four things, in this order:

1. `prompt.parse(text)` → `(request, provenance)`. A deterministic regex/keyword
   parser. It produces a `production_request` document and a provenance block
   splitting what it read from the text from what it assumed. **It forces
   `bioreactor_source: synthetic_standin`** — no input reaches a wet-lab path.
2. Caps: `MAX_CONCURRENT = 2`, `MAX_RUNS_TRACKED = 40`, `MAX_PROMPT = 2000`,
   `MAX_EVENTS_PER_RUN = 4000`, `RUN_TTL_S = 3600`.
3. Spawns a daemon thread running `engine.safe_run_loop(request, loop_dir,
   standin=…, truth=…, on_event=run.add)`. `engine.preflight` refuses anything
   whose `bioreactor_source` is not `synthetic_standin` and refuses to
   self-approve a protocol when the gates need a named human.
4. `report.write_report(loop_dir, …)` writes `reasoning_report.html` beside the
   artifacts.

Events live in `Run.events` (in process, each stamped `seq` and `t`) and are
pushed to the browser over SSE at `GET /api/runs/<id>/events`, with a
`Condition`-based wait, a 15 s keep-alive comment, and a terminal
`event: closed`. `webapp/console.html` consumes that with `EventSource`.

The run artifacts are the durable record: JSON under `runs/app-<stamp>-<id>/`,
served read-only by the same handler through `/api/loops` and
`/api/loops/<id>` (shapes imported from `serve.py`).

Everything else the console shows is read from disk, not from the run:
`/api/projects` (project profiles), `/api/datasets` (public datasets only),
`/api/hypotheses` (quantified hypotheses scraped out of built benchmark
bundles), `/api/analysis-tools`, `/api/sim/*`.

**Nothing on this path calls a model.** That is stated in the module docstring,
enforced by there being no model client in the process, and relied on by
`deploy/README.md` as the reason the console is safe on a public URL.

## 2. How `discovery_loop` runs through Omnigent today

`discovery_loop/` is an Omnigent agent bundle:

```yaml
spec_version: 1
name: biosense_discovery_loop
instructions: prompt.md
executor: {type: omnigent, config: {harness: claude-sdk}}
async: true
os_env:
  type: caller_process
  cwd: .
  sandbox: {type: auto, write_paths: [./runs], cwd_allow_hidden: [.venv], allow_network: false}
tools:
  agents: [literature, bioinformatics, analysis, biosimulator, outcome]
```

A person types `omnigent run discovery_loop` **from the repository root**, which:

- spawns (or reuses) a local Omnigent server, default `127.0.0.1:6767`;
- spawns a local **runner** with `OMNIGENT_RUNNER_WORKSPACE` set to the CLI's
  `os.getcwd()` — this is what makes `.venv/bin/python -m biosense.production.cli`
  resolve and what makes `./runs` the repository's own `runs/`;
- uploads the bundle, creates a session, binds the runner, and attaches a REPL;
- the person then re-types the scientific question into the REPL.

The orchestrator's `prompt.md` dispatches sub-agents with `sys_session_send`,
runs deterministic tools as shell commands
(`.venv/bin/python -m biosense.production.cli advise|propose-decision|report`,
`… bioinformatics.cli annotate|plan|execute`), and writes every artifact under
`runs/<loop-dir>/`. **Those files are the integration surface** — the agent
computes nothing itself; it chooses, and the CLI validates and refuses.

So the two systems already share one contract: the JSON artifacts under `runs/`.
What is missing is a way for the web app to *start* such a session and *read*
its progress.

## 3. The safest integration boundary

**The boundary is the Omnigent session, and the artifacts stay the source of
scientific truth.**

```
browser → BioSense backend → OmnigentRuntime adapter → /v1/sessions (SDK)
                                                       → runner → discovery_loop
                                                       → specialist agents
                                                       → biosense CLI tools
                                                       → runs/<dir>/*.json
          BioSense backend ← validated artifacts (K.require_valid) ← runs/<dir>
          browser          ← display payloads + mapped stage events
```

Three properties make this the safest line to draw:

- **One direction of trust.** BioSense sends a *structured* request and reads
  *schema-validated* artifacts. It never parses model prose into a number. A
  file that fails `contracts.require_valid` is ignored and reported as
  unreadable, never rendered.
- **No new authority.** The agent still cannot approve a protocol, still cannot
  reach a bioreactor, still cannot change a target after results exist — those
  refusals live in `production/*.py` and are untouched. The web app gains the
  ability to *start* a session, not to widen what a session may do.
- **Mode stays explicit.** `runtime_mode` is recorded on the run, carried in
  every event, and shown as a badge. A synthetic run and a real one are never
  the same object with a different flag buried in it.

What is explicitly *not* the boundary: embedding Omnigent in the BioSense
process, or having the agent POST into BioSense. The first couples two
lifecycles that fail differently; the second would make the agent a writer to
the product's own API.

## 4. Can the BioSense backend use an official Omnigent API/client?

**Yes.** `omnigent 0.16.0` ships `omnigent_client`, documented in its own
`__init__` docstring as the "Headless HTTP/SSE client for invoking agents …
frontends layer on top of this". The relevant surface, all public:

| Call | HTTP underneath |
|---|---|
| `OmnigentClient(base_url=…, headers=…, auth=…)` | — |
| `client.sessions.resolve_agent(name)` → `RegisteredAgent(id, harness)` | `GET /v1/agents` (paged) |
| `client.sessions.resolve_online_runner(harness=…)` → `runner_id \| None` | `GET /v1/runners` |
| `client.sessions.create_from_agent_id(id, title=…, workspace=…)` | `POST /v1/sessions` (JSON) |
| `client.sessions.create(bundle_bytes, …)` | `POST /v1/sessions` (multipart) |
| `client.sessions.bind_runner(sid, runner_id=…)` | `PATCH /v1/sessions/{id}` |
| `client.sessions.post_event(sid, {"type":"message","data":{…}})` | `POST /v1/sessions/{id}/events` |
| `client.sessions.stream(sid)` → `AsyncIterator[ServerStreamEvent]` | `GET /v1/sessions/{id}/stream` (SSE) |
| `client.sessions.get(sid)` → snapshot | `GET /v1/sessions/{id}` |
| `client.sessions.interrupt(sid)` | posts `{"type":"interrupt"}` |
| `client.files.for_session(sid).list()/get_content(id)` | session file resources |

I verified this end to end, not from the docstrings. Against a real local server
(`omnigent server --host 127.0.0.1 --port 6767 --agent discovery_loop`):

```
agent: RegisteredAgent(id='bf2eacde…', harness='claude-sdk')
runner: None
session: ece4bb4d… idle claude-sdk
post_event failed: ServerError No runner bound for session
stream: session.heartbeat, session.changed_files.invalidated, session.presence
```

So: agent resolution works, session creation works, the SSE stream works, and
the no-runner case fails loudly with a message BioSense can turn into a precise
error state. **No subprocess is needed on the critical path.**

One real cost, and it decides the packaging: `omnigent-client==0.16.0` declares
`Requires-Dist: omnigent==0.16.0`, so installing the client installs the whole
platform — roughly a hundred packages (fastapi, sqlalchemy, uvicorn, litellm,
tiktoken, …) and an exact pin on the Omnigent version. `biosense-ai` has three
base dependencies today, and `AGENTS.md` requires the scientific tools to stay
independent of the Omnigent runtime. Therefore:

- the client goes in an **optional extra** (`uv sync --extra omnigent`);
- it is imported **lazily, in one module only**
  (`biosense/production/omnigent_runtime.py`);
- when the extra is absent and the user asks for real AI, the app returns
  `REAL AI RUNTIME UNAVAILABLE / reason: sdk_not_installed` with the install
  line — it does not fall back to synthetic, and ordinary CI, the lean Railway
  image and `uv sync --locked` are unaffected.

## 5. Does a trusted server/session API exist?

Yes, and it is the one above: `/v1/sessions`. Its contract, from
`omnigent_client/_sessions.py` and the server routes:

- **snapshot + live-tail, no replay.** The server deliberately does not buffer
  past events; a client that reconnects opens a new stream and reconciles with
  `sessions.get(sid)`. BioSense therefore keeps its own append-only event log per
  run (it already does, for the synthetic path) and treats the Omnigent stream as
  the input to that log, not as the log.
- **one dispatch precondition:** a session must be bound to an online runner
  before a turn can be posted. That is the `No runner bound for session` error
  above, and it is a first-class BioSense error state.
- **events are typed.** `ServerStreamEvent` is a Pydantic discriminated union
  (~50 variants) with a frozen set of wire `type` literals; unknown types are
  logged and skipped by the SDK rather than poisoning the stream.
- **input shape** is `SessionEventInput{type, data}`; for a user turn,
  `{"type": "message", "data": {"role": "user", "content":
  [{"type": "input_text", "text": …}]}}`.

A second, older API exists (`/v1/responses`, via `client.session(model=…)`). It
is a chat helper that predates the sessions route; its own module docstring says
new code should use `sessions`. We use `sessions`.

## 6. When a subprocess wrapper would be unavoidable

Only for *lifecycle*, never for *invocation*, and the design does not need it:

| Thing | Needs a process? | Our answer |
|---|---|---|
| Create a run, send the objective, stream events | no | official SDK |
| Starting the local Omnigent **server** | yes, it is a server | the person runs `omnigent start` once; BioSense detects and reports it |
| Registering a local **runner/host** | yes | same |
| Remote server + remote runner | no | configured URL + token |

`omnigent_client.LocalServer` exists and would spawn
`python -m omnigent.cli server …` for us, but it is built for tests: ephemeral
sqlite in a tempdir, a 15 s readiness window, SIGINT teardown, and no runner. A
web app that silently spawns a credentialled agent platform on a user's machine
is also a surprise we should not deliver. So BioSense **never** spawns Omnigent.
It probes the configured URL and, if nothing answers, says exactly which command
to run.

That means the prohibited pattern never appears. There is no
`subprocess(...)`, no `shell=True`, no executable selection, and **no user text
ever reaches an argv or a shell string** — the objective travels as a JSON value
inside an HTTP request body. Tests assert this (a prompt containing
`; rm -rf /`, backticks and `$(…)` must survive byte-identical into the posted
event payload, and no `subprocess` symbol may be imported by the runtime
modules).

## 7. Local-runtime flow

```
person (once, in a terminal)           BioSense (every run, from the browser)
──────────────────────────────         ──────────────────────────────────────
omnigent start                         GET  /api/runtime        → probe
  ├─ local server 127.0.0.1:6767       POST /api/discovery      → structured request
  └─ host/runner, workspace = cwd        ├─ resolve_agent('biosense_discovery_loop')
uv run … biosense.production.app        ├─ resolve_online_runner(harness='claude-sdk')
                                        ├─ create_from_agent_id(workspace=<repo>)
                                        ├─ bind_runner
                                        ├─ post_event(message = rendered brief)
                                        └─ stream → mapped stages → SSE → browser
                                       ingest runs/<dir>/*.json → validated artifacts
```

Two preconditions BioSense checks and reports rather than assuming:

- **The runner's workspace must contain the runs directory.** The runner resolves
  its cwd from `OMNIGENT_RUNNER_WORKSPACE` (always authoritative; the bundle's
  `os_env.cwd: .` is only a session-create-time boundary). `omnigent start`
  sets it to the directory it was launched from. So the agent's `./runs` is the
  repository's `runs/`, which is what BioSense reads. If `--runs` points
  somewhere outside the workspace, local real AI is refused with that reason —
  not started with artifacts nobody will ever read.
- **A loopback server needs no credentials.** `_apply_bind_auth_defaults` sets
  `OMNIGENT_LOCAL_SINGLE_USER=1` for a loopback bind with the default header
  provider, so requests with no identity header fall back to the reserved
  `local` user. Verified: every call in §4 ran unauthenticated.

## 8. Remote-runtime flow

Identical from `resolve_agent` onwards; three things differ.

- **Configuration.** `BIOSENSE_RUNTIME_MODE=remote`,
  `BIOSENSE_OMNIGENT_SERVER=https://…`. The URL is validated (scheme http/https,
  host present, no credentials in the URL, https required unless
  `BIOSENSE_OMNIGENT_ALLOW_INSECURE=1`). Nothing is hard-coded: there is no
  Railway or Omnigent URL anywhere in application logic.
- **Authentication.** Required; see §9.
- **Artifacts.** This is the honest limitation. The runner's filesystem is not
  BioSense's, so `runs/<dir>/*.json` is not readable over the network. The
  remote path therefore delivers **progress, the stage timeline, the narrative
  and the session reference** fully, and ingests structured artifacts only
  where they reach BioSense — the session's own file resources
  (`client.files.for_session(sid)`), when the agent attaches them. I am not
  inventing a filesystem endpoint for this. A remote run whose artifacts never
  arrive is labelled `artifacts_not_ingested` with the reason, rather than
  rendering an empty report that looks like a finding. Closing that gap properly
  means having the orchestrator attach its artifacts as session files, which is
  a bundle-prompt change and is listed as deferred work, not claimed here.

## 9. Authentication requirements

What omnigent 0.16.0 actually supports (`omnigent/server/auth.py`):

| Provider | Selected by | How a backend authenticates |
|---|---|---|
| `header` (default) | bare `omnigent server` | `X-Forwarded-Email` from a trusted proxy — or nothing at all when `OMNIGENT_LOCAL_SINGLE_USER=1` (loopback default) |
| `accounts` | `OMNIGENT_AUTH_ENABLED=1`, no OIDC config; also the non-loopback bind default | `Authorization: Bearer <session JWT>` |
| `oidc` | `OMNIGENT_OIDC_*` | same bearer, minted by the IdP flow |
| Databricks-fronted | detected from the probe | workspace tokens via the Databricks CLI |

`_check_cookie` accepts the `__Host-ap_session` cookie **or** falls back to
`Authorization: Bearer <jwt>` for CLI clients — so a bearer token is the
supported server-to-server credential.

For a machine principal there is a proper grant: the OAuth 2.0
client-credentials branch of `POST /oauth/token`
(`omnigent/server/routes/client_credentials.py`), opt-in via
`OMNIGENT_MACHINE_CLIENT_ID` / `_SECRET_HASH` / `_SUB` on the Omnigent side. The
minted token is confined by a fail-closed path allowlist that includes exactly
what we need — `/health`, `/v1/agents`, `/v1/runners`, `/v1/sessions` — and
nothing administrative. That is the recommended remote posture.

BioSense's rules:

- the token comes from `BIOSENSE_OMNIGENT_TOKEN` or
  `BIOSENSE_OMNIGENT_TOKEN_FILE`, never from a request, never from the browser;
- it is sent as `Authorization: Bearer …` and is **never** included in any
  response, event, log line or error message (`/api/runtime` returns the
  server's scheme+host and a boolean `token_configured`, never the value);
- **no passwords.** BioSense does not implement `/auth/login` and stores no user
  credential;
- a 401/403 surfaces as `REAL AI RUNTIME UNAVAILABLE / reason:
  auth_required | auth_invalid` with the next step (`omnigent login <server>`,
  or set the machine client) — and **never** a silent switch to synthetic.

## 10. Streaming/event integration

Reuse the SSE machinery that already works. The BioSense run object and its
`/events` endpoint stay exactly as they are; only the *producer* changes:

```
synthetic:  engine.safe_run_loop(..., on_event=run.add)
real AI:    omnigent_runtime.drive(cfg, request, on_event=run.add)
```

`drive` runs `asyncio.run` on a private event loop inside the worker thread
(the SDK is async; the app is threaded), iterates `client.sessions.stream(sid)`,
maps each `ServerStreamEvent` to a BioSense event, and calls `on_event`. The
existing caps (`MAX_EVENTS_PER_RUN`, `RUN_TTL_S`, concurrency) apply unchanged,
with a separate, lower concurrency cap for real-AI runs because they cost money
and last minutes rather than a second. Token-delta events
(`response.output_text.delta`) are coalesced rather than forwarded one per
event, so a long turn cannot exhaust the event budget.

Because the Omnigent stream has no replay, a dropped connection is handled the
way the SDK documents: reopen the stream and reconcile with
`sessions.get(sid)`, deduplicating on our own `seq`. Our own event log is the
durable thing the browser re-reads with `?after=<n>`.

## 11. Mapping Omnigent events into BioSense run states

The map is deterministic code (`biosense/production/stages.py`), driven by
**typed events and tool calls** — never by reading the model's prose.

| Omnigent event | BioSense |
|---|---|
| `session.created` / `response.created` | stage `understanding_objective`, status `running` |
| `session.status{status}` | `launching`/`running`/`waiting`/`failed` |
| `response.output_item.done` where `item.type == "function_call"` | the stage implied by the tool, below |
| `response.output_item.done` where `item.type == "message"` | narrative line (simple view), full text (technical) |
| `response.reasoning*.delta` | technical view only |
| `response.output_text.delta` | coalesced progress text |
| `response.retry`, `response.error{source}` | `warning` / `failed` with `error.code`, `error.message` |
| `response.policy_denied` | `refusal` — the envelope said no, which is the system working |
| `response.elicitation_request` | `needs_human` — surfaced as a question, never auto-answered |
| `response.completed` | stage `complete` |
| `response.failed` / `response.cancelled` / `response.incomplete` | `failed` with the reason |
| `response.heartbeat`, `session.heartbeat`, `session.presence` | liveness only; not shown |

Tool call → stage, from the call's own name and arguments:

| Tool evidence | Stage |
|---|---|
| `sys_session_send` titled `literature-*` | `searching_literature` |
| `sys_session_send` titled `bioinformatics-*` | `searching_datasets` |
| `sys_session_send` titled `analysis-*` | `running_analysis` |
| command contains `bioinformatics.cli plan` | `planning_analysis` |
| command contains `bioinformatics.cli analyse run` | `running_analysis` |
| command contains `production.cli advise` | `synthesizing_evidence` |
| command contains `propose-decision` | `building_hypothesis` |
| command contains `sim` / `simulate-standin` | `testing_simulator` |
| command contains `production.cli report` | `generating_report` |

Stages are monotone: the ticks a user sees (`✓ done`, `● current`, `○ pending`)
only move forward, so a late literature follow-up does not rewind the display.
A stage BioSense cannot infer stays `pending` rather than being guessed.

The twelve stages are exactly the ones the product asks for:
`understanding_objective`, `identifying_uncertainty`, `searching_literature`,
`searching_datasets`, `planning_analysis`, `running_analysis`,
`synthesizing_evidence`, `building_hypothesis`, `testing_simulator`,
`generating_report`, `complete`, `failed`.

## 12. Structured artifact ingestion

**Artifacts come from files that pass their schema, never from prose.**
`biosense/production/ingest.py` walks a run directory and, for each document it
recognises, calls `contracts.require_valid(kind, doc)`:

| On disk | Contract | Rendered as |
|---|---|---|
| `analysis_plan*.json` | `analysis_plan` | the planned question and method |
| `analysis_result*.json` | `analysis_result` | an analysis card (question, dataset, method, comparison, interpretation, full statistics behind *Technical details*) |
| `quantified_hypothesis*.json` | `quantified_hypothesis` | the hypothesis card, each number badged `measured`/`derived`/`simulated`/`predicted`/`target`/`expert_knowledge` |
| `*estimate*` inside the above | `estimate` | effect rows, percentage points kept separate from relative change |
| `research_context.json` | `research_context` | the scope, with `CONTEXT MISMATCH` where `assess()` says so |
| `bioinformatics_report*.json` | `bioinformatics_report` | evidence rows |
| `loop_state.json`, `decisions/*.json` | existing loop shapes | the decision trail (already supported) |
| `reasoning_report.html` | — | the in-app report |

Evidence is grouped by the `EVIDENCE_CLASSES` the repository already defines —
`published_literature`, `public_dataset`, `private_user_dataset`,
`derived_analysis`, `simulation`, `real_measurement`, `synthetic_fixture`,
`expert_knowledge` — and each row keeps its three independent facts separate
(what it *is*, what it came *from*, who may *see* it), which the console's badge
code already does.

Unreadable or invalid files are counted and reported (`artifacts_rejected`),
not silently dropped and not rendered.

Candidate parameters are rendered through `ParameterRegistry` +
`ProjectProfile`: a parameter the active project does not expose, or that the
project marks `not_modelled`, is shown as `NOT MODELLED` with no simulated
effect. `sim_candidate.plan_handoff` already returns `applied` and `skipped`
with reasons, so nothing is silently dropped.

### 12a. The final recommended protocol

A run does not produce one hypothesis. It produces several, and some of them are
wrong — `quantified_hypothesis.status` is already
`proposed | supported | contradicted | superseded | rejected`, with `supersedes`
and `superseded_reason` so the progression is recoverable. Reading eight cards
and working out which ideas survived is not the deliverable a scientist wants at
the end; one protocol is.

So a completed run also produces a **ProtocolSummary**
(`biosense/production/protocol_summary.py`, contract
`schemas/protocol_summary.schema.json`): one page, assembled deterministically
from artifacts that already exist, never written by a model.

| Section | Built from |
|---|---|
| Process, stage by stage | `ProjectProfile.stages` |
| Each stage's setpoints | control values, overridden by the **adopted** candidates |
| Per value: what changed and why | the candidate's own hypothesis, its evidence rows, and its estimate type (`measured` / `derived` / `simulated` / `predicted` / `target` / `expert knowledge`) |
| Per value: how firm it is | the protocol provenance already tracked — `reported` / `adapted` / `design_choice` / `gap` |
| **Hypothesis ledger** | every hypothesis the run formed, with its status and the reason: adopted, contradicted by which analysis, superseded by which later one, or not adopted and why |
| Expected effect | the quantified effects, each badged, gains and costs separated by `hypothesis.trade_offs` |
| Readouts and QC | `ProjectProfile.readouts`, the request's QC profile |
| Gaps | values with no evidence — listed under **blocks the wet lab** |
| Next experiment | `hypothesis.next_experiment` |

Three rules it must hold, because a protocol is the artifact most likely to be
acted on:

- **It is a proposal, not an approval.** `quantified_hypothesis` already pins
  `may_change_protocol: false`, and a wet-lab run needs
  `approve-protocol --approved-by "<named person>"`. The summary is stamped
  `PROPOSED — NOT APPROVED` and names the approval step. Nothing in the web app
  approves anything.
- **A rejected hypothesis stays on the page.** Showing only the winner would
  hide that the system considered and discarded three alternatives, which is
  the part a reviewer most needs. The ledger is part of the protocol, not an
  appendix.
- **A gap is not a value.** A parameter with no evidence appears as `GAP`, and
  the summary says in words that the gap blocks the wet lab. It is never filled
  with a plausible number.

Where a real `ProductionProtocol` exists on disk, the summary wraps the run
sheet `protocol.render_run_sheet` already produces, rather than inventing a
second format. In the app it is a designed panel — stage columns, the changed
values highlighted against the control, a badge beside every number, the ledger
beneath — and it exports as Markdown, JSON and a printable page through the
existing export actions.

## 13. Benchmark generation from a completed run

The benchmark system already *is* this flow: `benchmarks/configs/*.json` is
`{project_id, objective, research_context, datasets, expert_knowledge,
uncertainty, candidate_values, expected_capabilities, export_policy}` — which is
the structured discovery request, and `benchmark/runner.run` executes
objective → uncertainty → evidence → plan → analysis → estimates → hypothesis →
candidate → simulator → next experiment, scoring seventeen capabilities.

Two consequences, and they are the reason this integration is small:

- **Synthetic demo and real AI take the same request and produce the same
  artifact shape.** `SYNTHETIC DEMO` runs `benchmark.runner.run` over the
  structured request; `REAL AI` runs `discovery_loop`, which writes the same
  contracts through the same CLI tools. One ingestion path, one set of cards.
- **A benchmark is assembled from a finished run, not by re-running the
  biology.** `benchmark/from_run.py` builds a `benchmark_result` from artifacts
  already on disk, derives each scorecard row from what is actually present
  (a missing capability is a `FAIL` with the reason, never a silent pass), then
  runs the **existing** privacy validator: `privacy.lineage` → `privacy.check`
  → `PUBLIC-SAFE` or `PRIVATE — DO NOT PUBLISH`, and `cli.bundle_dir` already
  writes a private bundle outside the repository, under the private data root.
  No new privacy logic is written.

In-app: `POST /api/discovery/<id>/benchmark` creates it,
`GET /api/discovery/<id>/benchmark` renders it, and the export actions hand back
`summary.md`, `benchmark.json` and the analysis bundle. The benchmark CLI stays
exactly as it is for CI and automation.

## 14. Railway implications

> **Superseded by §21.** This section described the deployment as it was while
> the hosted app offered the synthetic path only. It is kept because the argument
> it makes is still the argument for `deploy/Dockerfile`, which still exists and
> is still the right image when the claim you want is that the service holds
> nothing. The hosted product is now `deploy/Dockerfile.ai`.

- `deploy/Dockerfile` keeps `uv sync --locked` with no extras, so the image does
  not grow by a hundred packages and holds no Omnigent and no model credentials.
- `deploy/railway.json`, the healthcheck (`/healthz`), the start command, the
  static serving, `RUNS_DIR=/data/runs` and the volume all stay as they are.
- `BIOSENSE_RUNTIME_MODE` defaults to `synthetic`, and
  `BIOSENSE_ALLOWED_RUNTIMES` defaults to the modes the deployment can actually
  serve, so the runtime selector shows one option with a reason beside the others
  rather than offering a button that cannot work.

## 15. Failure modes

Each gets a short reason and a next step, and **none** of them starts a
synthetic run instead.

| Condition | Detected by | Shown as |
|---|---|---|
| Real AI not configured | mode is synthetic, or the mode is not in `BIOSENSE_ALLOWED_RUNTIMES` | `SYNTHETIC DEMO only` + which env var to set |
| SDK missing | `ImportError` on the lazy import | `sdk_not_installed` + `uv sync --extra omnigent` |
| Server unreachable | `httpx.ConnectError` on `/health` | `runtime_unreachable` + `omnigent start` (local) / check the URL (remote) |
| Auth required / expired | 401/403 | `auth_required` / `auth_invalid` + `omnigent login <server>` |
| Agent not registered | `LookupError` from `resolve_agent` | `agent_not_registered` + `--agent discovery_loop` |
| **No runner/host** | `resolve_online_runner() → None`, or `ServerError: No runner bound` (verified) | `no_runner_available` + `omnigent host --server <url>` |
| Model/provider credentials missing | `response.failed{source: "llm"\|"harness"}` | `model_auth_missing` + set `ANTHROPIC_API_KEY` |
| LLM provider down / rate limited | `response.retry`, `response.error{source: "llm"}`, `RateLimitedError` | retry notice with the attempt count; terminal failure if it ends there |
| Runs dir outside the runner workspace | path containment check | refused before the session is created, with both paths named |
| Dataset invalid / metadata missing | existing ingest refusals | the dataset's own `missing_metadata`, "asked for, never guessed" |
| External R/DESeq2 absent | existing external-tool adapter | the adapter's own refusal with the install line |
| Network permission disabled | `allow_network: false` for every agent but literature | the literature agent's own failure, reported as such |
| Simulator parameter not modelled | `ProjectProfile.coverage` | `NOT MODELLED`, no prediction produced |
| Benchmark has private lineage | `privacy.check` | `PRIVATE — DO NOT PUBLISH`, public write refused |
| Stream drops mid-run | stream exception | reconnect + reconcile via `sessions.get`; the run is not marked failed |
| Omnigent run still going when the browser closes | — | the run keeps going; the session id and the artifacts are the record |

Never shown in the browser, and never logged: the Omnigent token, model API
keys, private dataset paths, private expert-knowledge content, absolute
filesystem paths. `/api/runtime` reports scheme+host and
`token_configured: true|false`, and the run object carries only the run
directory's basename.

## 16. Files I propose changing

**New (Python)**

| File | Why |
|---|---|
| `biosense/production/runtime.py` | the `OmnigentRuntime` abstraction: three modes, config from env, URL validation, reachability probe, reason codes. No Omnigent import. |
| `biosense/production/discovery.py` | the structured BioSense discovery request (objective, project, ResearchContext, dataset refs, expert-knowledge refs, process constraints, runtime mode) and the deterministic brief rendered from it. |
| `biosense/production/omnigent_runtime.py` | the only module that imports `omnigent_client`, lazily. Create session, bind runner, post the brief as data, stream, map errors. |
| `biosense/production/stages.py` | Omnigent event → BioSense stage/event mapping (§11). |
| `biosense/production/ingest.py` | schema-validated artifact ingestion and the display payloads (§12). |
| `biosense/production/discovery_runner.py` | runs a discovery request: synthetic via `benchmark.runner.run`, real AI via the adapter. One request shape, two runtimes. |
| `biosense/production/protocol_summary.py` | the final recommended protocol and the hypothesis ledger (§12a), assembled from artifacts. |
| `biosense/benchmark/from_run.py` | assemble a `benchmark_result` from a finished run's artifacts; reuse the existing privacy validator. |
| `schemas/discovery_request.schema.json` | the request contract. |
| `schemas/protocol_summary.schema.json` | the protocol-summary contract. |

**Changed (Python)**

| File | Change |
|---|---|
| `biosense/production/app.py` | add `/api/runtime`, `/api/discovery` (POST/GET), `/api/discovery/<id>/{events,report,bundle,benchmark}`, `/api/expert-knowledge`; a separate real-AI concurrency cap. Existing endpoints untouched. |
| `pyproject.toml` + `uv.lock` | `[project.optional-dependencies] omnigent = ["omnigent-client==0.16.0"]`. Base install unchanged. |

**Changed (frontend — one app, no second frontend)**

| File | Change |
|---|---|
| `webapp/console.html` | the discovery form (project, objective, ResearchContext, private data, expert knowledge, process constraints), the runtime selector, the runtime badge, stage progress, evidence panel, analysis cards, hypothesis card, candidate parameters, **the final recommended-protocol panel and its hypothesis ledger**, in-app report, benchmark view, export actions. The existing free-text synthetic loop stays on the page. |
| `webapp/discovery.js` | that page's logic, split out so the HTML stays readable (same pattern as `simulator.js`). |
| `webapp/simulator.js` / `simulator.html` | CONTROL / CANDIDATE / CUSTOM, deep-link from a hypothesis (`OPEN IN SIMULATOR`), and `USE AS CANDIDATE` recorded as `origin: user_design_choice`. |
| `webapp/brand.css` | the badge, stage-tick and card styles the new panels need. |

**Changed (agent bundle)**

| File | Change |
|---|---|
| `discovery_loop/prompt.md` | one new section: a web-initiated discovery run, the loop directory BioSense names, and which artifacts to write where so they are ingestible. No change to any rule or refusal. |

**Changed (docs, tests, scripts)**

`README.md` (browser-first Quick Start; `omnigent run discovery_loop` moves to
Advanced), `docs/RUN_ON_YOUR_PC.md` (simple path / advanced CLI path),
`docs/OMNIGENT_INTEGRATION.md` (this document), `deploy/README.md` (the
BioSense-side runtime settings), `scripts/check.sh` (also: its first line is
currently `ut#!/usr/bin/env bash` — a stray prefix that breaks the shebang;
fixed), and new tests — `tests/test_runtime.py`,
`tests/test_omnigent_runtime.py`, `tests/test_discovery_api.py`,
`tests/test_ingest.py`, `tests/test_benchmark_from_run.py` — plus additions to
`tests/test_app_loop.py` and `tests/test_docs_accuracy.py`.

**Not changed:** `deploy/Dockerfile`, `deploy/railway.json`, every
`biosense/production/*.py` that holds a refusal (`engine`, `autonomy`,
`protocol`, `orchestrator`, `revise`, `design`, `analysis`), `biosense/data/*`,
`biosense/benchmark/privacy.py`, and `biosense/production/serve.py` — the
strictly read-only server stays strictly read-only.

## 17. The smallest coherent implementation

Coherent means a scientist can do the whole job in the browser; smallest means
nothing is built that the existing contracts already provide.

1. **`runtime.py`** — three modes, config from environment, validated URL,
   probe with reason codes. Nothing imports Omnigent yet.
2. **`discovery.py` + schema** — the structured request and its brief. User text
   is a JSON value from here to the wire.
3. **`omnigent_runtime.py`** — official SDK, lazily imported: resolve agent,
   resolve runner, create, bind, post, stream. Typed failures, no fallback.
4. **`stages.py`** — the event map of §11.
5. **`ingest.py`** — schema-validated artifacts → display payloads.
6. **`discovery_runner.py`** — one request, two runtimes: `benchmark.runner.run`
   for `SYNTHETIC DEMO`, the adapter for `REAL AI`. Same artifacts, same cards,
   different badge.
7. **`protocol_summary.py` + schema** — one recommended protocol per run, with
   the hypothesis ledger that says which ideas survived and which did not,
   stamped `PROPOSED — NOT APPROVED` (§12a).
8. **`benchmark/from_run.py`** — a benchmark assembled from a finished run,
   through the existing privacy validator.
9. **App endpoints** — `/api/runtime`, `/api/discovery*`. Nothing existing moves.
10. **One page, extended** — form, badge, stages, evidence, analyses, hypothesis,
   candidates, report, benchmark, exports; simulator gains
   control/candidate/custom and the handoff.
11. **Tests** — the runtime matrix, both configurations, auth failure, the
    real/synthetic distinction, no silent fallback, no shell injection and no
    `subprocess` import, run creation, streaming, the event→stage map, artifact
    ingestion, every display payload, the simulator handoff, unsupported
    parameters, the protocol summary and its ledger, benchmark creation and
    rendering, public/private benchmark state, every error state, private-data
    safety. The Omnigent runtime is mocked; **no live AI in CI**.
12. **Docs** — browser-first README, the two paths in `RUN_ON_YOUR_PC.md`, the
    deployment settings, this review.

What is deliberately **not** in it, and is recorded as such rather than
half-built: remote artifact ingestion beyond session files (§8); a PDF export
(optional by the brief); any physical lab actuation; any claim that a real
bioreactor loop has run. And the honesty rules do not move: a simulated number
stays `SIMULATED`, a synthetic run stays unmistakably synthetic, and a benchmark
still measures system capability, not biological truth.


---

## 18. A correction to the system diagram, made while reviewing

Raised during the review, checked against the code, and correct: the
`arch-system` diagram drew **Public datasets** and **Private datasets** flowing
straight into **Evidence synthesis**, with the deterministic tools as a parallel
branch beside them. That is not what happens, and it understates the one
property the design exists to hold.

What the code actually does (`bioinformatics/plan.py`, `bioinformatics/execute.py`,
`data/sources/geo.py`, `data/ingest.py`):

1. the bioinformatics agent **fetches** a candidate from a public repository
   (GEO/ENCODE/EBI — accession plus checksum) or **registers** a file a person
   ingested privately;
2. it writes an **AnalysisPlan** naming those `dataset_ids`, a registered tool,
   and a `uncertainty_ref` that `resolve_uncertainty` checks against something
   real — a plan answering no stated question cannot be constructed;
3. `execute.execute(plan)` — **deterministic** — loads the manifests, runs
   `check_ready`, executes the registered tool **over the data itself**, and
   assembles provenance: dataset ids, the sha256 of every input file, the plan
   hash, the package hash, the git revision;
4. the result is an **AnalysisResult**, and by the module's own lineage rule it
   is always `evidence_class: derived_analysis`, with what it was computed from
   recorded separately in `source_evidence_class` and `source_visibility`.

So a dataset is an **input to** the deterministic analysis, and the computed
result is the evidence. A raw dataset never enters synthesis on its own — which
is exactly why an analysis over a private FACS table is "a derived analysis with
a private source" rather than "a private dataset", and why it can be reasoned
about without the private bytes ever moving.

The diagram now draws that: `Bioinformatics agent --fetches-->` public datasets
and `--registers-->` private datasets; both, together with the plan from the
**Data analyst**, feed one **Deterministic analysis** box; and a single
arrow labelled `AnalysisResult — derived analysis` carries its output into
**Evidence synthesis**. The literature agent's cited claims still enter synthesis
directly, because a cited claim is already evidence and has no dataset to
compute over.

Regenerated with `scripts/make_architecture_diagrams.py`, so
`tests/test_docs_accuracy.py` still reproduces it byte for byte, and the README's
alt text was rewritten to describe the corrected flow.

---

## 19. What was built, against what this review proposed

Written after the implementation, so the two can be compared.

**Built as reviewed.** The runtime abstraction with its three modes and reason
codes; the structured `DiscoveryRequest` and the brief rendered from it; the
adapter over the official SDK, lazily imported; the event-to-stage map;
schema-validated artifact ingestion; one request answered by either runtime
through the same contracts; benchmarks assembled from a finished run through the
existing privacy validator; the HTTP surface; one page from objective to
protocol; the docs.

**Added during the work, from questions raised while building it.**

- **A protocol summary** (§12a) — a run forms several hypotheses and some are
  wrong, so one page carries the recommended process with per-value provenance
  and the ledger of what did not survive, stamped `PROPOSED — NOT APPROVED`.
- **Project creation** — the universal bioreactor set every stirred-tank process
  shares, plus parameters a project declares for itself, each with a stated
  relationship to the simulator.
- **Proposed response terms** — `de_novo_ai` and `expert_declared` coverage, so a
  parameter with evidence and no equation can still say which way and roughly
  how far. The arithmetic is four closed-form shapes in code; what a proposal
  supplies is a shape name and some constants, bounded by contract and clamped
  again in code.
- **Workspaces and sign-in** — through Omnigent's own accounts, because real AI
  already requires one. BioSense stores no password and holds the session token
  server-side.
- **Campaign authorisation** — one named person, a bounded number of iterations,
  a stated envelope, checked every round. A closed loop is a loop; a signature
  between every round would not make it safer.
- **A served glossary** — the vocabulary was in docstrings, where a scientist
  looking at a number in a browser would have had to find a repository to learn
  what the word beside it meant.

**Found while building, and fixed rather than worked around.** The `arch-system`
diagram drew datasets reaching synthesis without passing through the analysis
(§18). The loop diagram implied a person between every iteration. `scripts/check.sh`
had a stray prefix breaking its shebang. `ALLOW_INSECURE` was read from the
process environment while its siblings came from the passed configuration. A
project with no model silently downgraded a `modelled` claim instead of refusing.
A benchmark built from a web run was written into the repository's committed
public set. Stage duration is recorded on a project's stages rather than on the
parameter, so reading the parameter alone reported a gap for something the
profile states plainly. And `.term` set `font: inherit`, which beat the badge
class it sat on.

**Not built, and recorded as such.** Remote artifact ingestion beyond session
files (§8); a PDF export, which the brief left optional; any physical actuation;
and a complete live discovery session with model credentials. The adapter is
verified against a real local Omnigent server — agent resolution, session
creation, runner binding, the SSE stream and the no-runner refusal — with the
`biosense_discovery_loop` bundle registered and all six agent specs passing
Omnigent's own validator. What has not run end to end is a session with a model
behind it, and the README says so.


---

## 20. A correction found by running it: hosts are not runners

The review said a session needs an online **runner** bound before a turn can
dispatch, and that `resolve_online_runner() -> None` is the `no_runner_available`
state. That is true of one topology and wrong about the other, and the wrong one
is what `omnigent start` gives you.

Verified against a real local server. After `omnigent host --server
http://127.0.0.1:6767`:

```
GET /v1/runners   {"data":[]}
GET /v1/hosts     {"hosts":[{"host_id":"401dae...","name":"vm","status":"online",
                             "configured_harnesses":{"claude_sdk":"needs-auth", ...}}]}
```

The host is online and no runner exists, because a host-backed session gets its
runner **when it is created**: Omnigent's own `SessionCreateMetadata` documents
`host_id` as "the server triggers the host launch flow (generate binding token,
write runner_id, send launch frame)", with `workspace` required alongside it.

So the adapter now looks for an executor rather than a runner: a bound runner
first (the `omnigent run` topology, where the client binds it itself), then an
online host whose `configured_harnesses` advertises the agent's harness, created
with `host_id` and `workspace` so the server launches the runner. Only when
neither exists is it `no_runner_available`.

The same listing improved the error. A host that reports its harness as
`needs-auth` — which is exactly what a machine with no `ANTHROPIC_API_KEY` says —
now produces `model_auth_missing` and the command that fixes it, instead of
"no runner available", which would have sent somebody looking for the wrong
thing entirely.

Had this stayed as reviewed, real AI would have refused every time on the setup
the documentation tells people to use.

---

## 21. Running the agents in the cloud, with nothing on anybody's laptop

The question this section answers is not "can BioSense talk to Omnigent" — §19
and §20 settled that — but "can a stranger with a browser get a real run,
without a terminal and without my machine being on". The answer required one
architectural decision and no new protocol.

### The constraint that decided it

`ingest.run_bundle` reads the agents' artifacts **off the local filesystem**, and
`runtime.check_workspace` refuses a real run unless BioSense's runs directory is
inside the runner's workspace — because such a run would otherwise succeed and
produce artifacts nobody ever sees. So artifacts cannot cross a machine
boundary, and the options were:

| | Option | Verdict |
|---|---|---|
| A | the runner POSTs artifacts to a BioSense route | a new inbound write route, a new token, and an LLM that reliably posts. Fragile. |
| B | shared object storage | new infrastructure and credentials for a problem that can be deleted |
| C | a database | same, and a worse fit for file-shaped artifacts |
| D | Omnigent session files | `OutputFileDoneEvent` is in the schema, but **0.16.0 has no agent-side emission path**. Not a mechanism to build on today. |
| **E** | **no boundary** | **chosen** |

E: the Omnigent server, the executor and BioSense run in one container, so the
runner writes `/app/runs` and BioSense reads `/app/runs`. It removes the problem
rather than moving it, and the runner gets BioSense's code, dependencies,
deterministic tools and simulator by construction — it is the same image. If a
separate runtime service is ever genuinely needed, A is the migration, and the
contracts already exist to carry it.

### Why not a managed sandbox

Omnigent does support it: `host_type: "managed"` makes the server provision a
sandbox, start `omnigent host` inside it and bind the session, across providers
(`modal`, `daytona`, `e2b`, `kubernetes`, …). Two reasons it is not the answer
here: each provider needs its own paid account and credentials, and the default
sandbox image is a **generic** `omnigent-host`, which has none of BioSense's
code, `.venv`, deterministic tools or simulator. Everything E gets for free
would have to be bootstrapped into that image on every launch.

### The measurement that mattered

On one machine, same server, the only difference being a model key:

```
no ANTHROPIC_API_KEY   → /v1/hosts configured_harnesses {"claude-sdk": "needs-auth"}
                         BioSense probe → model_auth_missing
with ANTHROPIC_API_KEY → {"claude-sdk": true}
                         BioSense probe → ok   (executor: host, agent registered)
```

That `ok` is the whole acceptance condition, reached with **no sandbox provider
and no laptop** — a server, a host and a key in one environment. It was then
reproduced through `deploy/start-ai.sh`, which is the container's boot sequence,
and through `scripts/start_local_ai.sh`, which is the same topology in one
command on a developer's machine.

### What that cost, stated plainly

The public container now holds a model key. The old argument — defensible
because it holds nothing — is replaced by *defensible because what it can spend
is bounded*, which is why `biosense/production/budget.py` exists and is not
optional: per-caller and per-deployment daily caps, a cooldown, one real run at a
time, a wall-clock deadline with cancellation that interrupts the Omnigent
session rather than just the stream, and a 429 that names the cap and the
uncapped alternatives. One container also means one crash takes all three
processes; the boot script supervises the runtime and the platform restarts the
container.

### Two things that are now true of a run, and were not

**A run outlives its page.** Each discovery run journals its snapshot and its
events beside its artifacts (`run_store.py`), so a reload, a lost network or a
replaced process does not lose one; the run id is enough to get it back. A run
that was in flight when the process went away comes back as `interrupted` — not
as finished, and not as still running. The substitution rule cuts both ways: a
halted run is not a result either.

**"LOCAL" is wrong in a browser.** The hosted service's runtime is
`local_real_ai` — a loopback server inside its own container — and *local* in a
browser means *your computer*. `BIOSENSE_HOSTED=1` keeps the mode name, which is
recorded, exported and tested, and changes only the word the reader sees to
`REAL AI — ONLINE`. The same flag rewrites the remedies: telling a visitor to
run `omnigent host` is useless, so a hosted failure says whose fault it is and
that the demonstration path still works.

### `/readyz`

`/healthz` stays liveness and nothing more, because a platform healthcheck on
readiness would restart the container while the runtime comes up and keep
restarting it if a key were missing — taking the demonstration path down with
it. `/readyz` answers the four parts somebody actually wants when the button does
not work: runtime reachable, agent registered, executor available, model
credentials present. All four are derived from the **single** reason code the
same probe returns, so the health page cannot disagree with the run that follows
it, and the body carries no token, no key and no path.
