# Production discovery loop

A person states an aim and constraints once. From there a reasoning orchestrator
gathers evidence, has a protocol designed, reads what the bioreactor returned,
forms hypotheses, and decides what happens next — returning to the person where
their judgement is required or where their answer would change the outcome.

```
                          ┌─────────────── person ───────────────┐
                          │ aim · constraints · autonomy mode    │
                          │ protocol approval · consult answers  │
                          └───────────────┬──────────────────────┘
                                          ▼
   literature ◀────── design / revise brief ──── ORCHESTRATOR ──── gene question ──▶ bioinformatics
   (cited claims,     ──── protocol + handoff ──▶  (reasoning     ◀── levers, untestable ───
    protocol)                                       agent)            questions
                                          │
                        approved run sheet ▼                      verdict · diagnosis
                                     bioreactor ──▶ measurements ──▶ analysis ──┐
                                 (people, or a                                  │
                                  labelled stand-in)                            │
                                          └───────────────◀────────────────────┘
```

Every agent choice passes through a deterministic envelope before it takes
effect. The LLM agents reason, explain and choose; the tools in
`biosense/production/` and `biosense/bioinformatics/` compute every validation,
metric and verdict, and refuse anything outside the envelope. People run the
bioreactor; nothing here touches an actuator.

## Contracts (`schemas/`)

| Contract | Written by | Key content |
|---|---|---|
| `production_request` | person, with the orchestrator's help | question; product; desired output (metric, value, day, tolerance); QC profile and overrides; genotype arms; measurement mode; **`human_in_the_loop`**; **`bioreactor_source`**; **`bioinformatics`** access; budgets |
| `production_protocol` | literature agent | day origin, insights, culture system, stages and steps, genotype arms/effects/adjustments, measurement plan; every quantity carries provenance `reported` / `adapted` / `design_choice` / `gap`; `approval` written only by `approve-protocol` |
| `bioreactor_run` | operator, or a stand-in | per arm and replicate: input cells, deviations, harvest (viable cells, viability, marker %, residual pluripotency), release tests; max mode adds the integrated-machine record |
| `bioinformatics_report` | bioinformatics tools | per gene: found or not, annotated effects with direction, conditions, confidence and source; implicated protocol levers; assays that cannot run here; limitations |
| `analysis_report` | `analyze` | verdict, per-arm target and QC, genotype comparison, diagnosis hypotheses, data integrity |
| `human_consult` | orchestrator | question, why it matters, the assay that cannot run here, expected gain, options, **blocking**, and **what happens if nobody answers** |
| `loop_decision` | orchestrator agent, or the policy | type, `authored_by`, `reasoning`, `considered` alternatives, `hypotheses` with their basis, `tool_calls`, `consult_ids`, the `allowed_actions` it was validated against, the `policy_advice` it may have overridden, `advances_iteration`, `status` |

## How much the person stays in the loop

One setting in the request, resolved by `biosense/production/autonomy.py`:

| Mode | Protocol approval | Decisions | Consults |
|---|---|---|---|
| `full` | required | written `pending_human`; nothing downstream acts until `approve-decision` | raised whenever useful |
| `checkpoints` | required | commit on validation; the person is notified | raised when the answer would change the outcome |
| `autonomous` | waived **only** for a synthetic stand-in reactor | commit on validation | the orchestrator judges, or `never` if turned off |

One rule outranks the mode: **a wet-lab run always needs a named human to
approve the protocol.** Cells, reagents and operator time are spent in the
physical world and nothing here can unspend them. The override is recorded in
`safety_overrides` and printed by `autonomy --request`, so it is visible rather
than silent.

```bash
python -m biosense.production.cli autonomy --request examples/cart/request.cart_d10.json
```

## The decision envelope

`advise` returns what the orchestrator may choose for one analysis. The verdict
fixes the envelope; inside it the agent reasons.

| Verdict | Allowed | Refused, with the reason |
|---|---|---|
| `SAFETY_HOLD` | `escalate_to_human` only (mandatory) | everything else: not a recipe problem, and results from this line are confounded |
| `DATA_UNRELIABLE` | `repeat_measurement`, `escalate_to_human`, information actions | `revise_protocol`, `protocol_succeeded`, `complete_qc`: the numbers cannot carry a conclusion |
| `FAILED`, budget left | `revise_protocol`, information actions, `repeat_measurement`, `escalate_to_human` | `protocol_succeeded` |
| `FAILED`, budget spent | `stop_budget`, information actions, `escalate_to_human` | `revise_protocol` |
| `TARGET_MET_QC_INCOMPLETE` | `complete_qc`, information actions, `escalate_to_human` | `protocol_succeeded`: untested is never a pass |
| `SUCCESS` | `protocol_succeeded`, `escalate_to_human` | `stop_budget`, and information actions — there is nothing left to gather for |

**Only `revise_protocol` spends an iteration.** `request_bioinformatics`,
`request_literature` and `consult_human` are capped per iteration
(`max_info_actions_per_iteration`, default 4) so gathering cannot become
stalling.

`validate_decision` additionally refuses: an action outside the set; reasoning
under 60 characters; a hypothesis with no basis; a cited tool result or consult
that does not exist; a revision brief with no failed criterion, no lever or
nothing to keep fixed; an information action with no instruction; and any
revision while a **blocking** consult is open. The decision records the allowed
set and the policy's advice even when the agent chose differently, so the
choice stays auditable.

The deterministic policy remains available as `decide`, which is what CI and the
offline demos use. It authors the same contract with `authored_by: "policy"`.

## Bioinformatics

`biosense/bioinformatics/` reports what an annotation set records — and nothing
more. A gene absent from every loaded set returns `found: false` with the exact
public queries that would annotate it; it never gets a guessed effect, and
absence is never read as absence of effect.

```bash
python -m biosense.bioinformatics.cli annotate --genes EXH1 --perturbation knockout \
  --cell-type "CAR-T cell" --protocol runs/loop1/it1/protocol.approved.json --out runs/loop1/it1/bioinfo.json
python -m biosense.bioinformatics.cli sets
python -m biosense.bioinformatics.cli plan --gene MYGENE
```

Each effect carries a confidence that distinguishes `synthetic_fixture` (an
invented demo entry) from `local_annotation` (curated with a citation) from
`live_annotation` (fetched from a public database). The `--protocol` cross-check
answers the question that actually matters: **is this implicated lever already
differentiated between the engineered arm and the control?** An implicated lever
that is not yet differentiated is the actionable finding.

Knowledge sets live in `bioinfo_knowledge/` with their own README. Live lookups
(Ensembl, UniProt, Open Targets, STRING) run only when a request sets
`bioinformatics.live_lookups` and you pass
`--i-have-network-permission`; they return raw records for a human to curate,
never auto-derived effects. CI never touches the network.

The most useful thing the tools produce is usually not a direction of effect but
the line between a question the loop can answer with another run and a question
it cannot answer at all with this machine. The latter become consults.

## Consults

A consult is raised when a person's answer would materially change the next
protocol. It must state the question plainly, why it matters, what it would
change, whether the loop is waiting, and **what happens if nobody answers**.
Options make answering a choice rather than an essay.

The answer's `evidence_status` decides what the protocol may do with it:
expert judgement becomes a **design choice**; an unpublished in-house result or
an unretrieved publication may become an `adapted` value with the rationale
naming the consult. A declined consult stays a design choice. Answers are never
overwritten.

```bash
python -m biosense.production.cli consult-raise  --loop-dir runs/loop1 --consult c.json
python -m biosense.production.cli consult-list   --loop-dir runs/loop1 --status open
python -m biosense.production.cli consult-answer --loop-dir runs/loop1 --consult-id consult-00 \
  --answered-by "Your Name" --content "..." --evidence-status unpublished_in_house_result --citable-as adapted
```

## Measurement modes

| Mode | Per arm and replicate | What analysis can and cannot say |
|---|---|---|
| `minimal` | input viable cells; at harvest: viable cells, viability %, target marker %, residual pluripotency; optional time points and release tests | target and QC; genotype differences. Cannot detect instrument faults, artifacts or clonal drift. |
| `max` | minimal plus the daily integrated-machine record (M1–M5, see `analysis_agent/README.md`), executed setpoints and ≥4 prior runs of the line | adds kinetics, two-tier fused purity with an interval, staining integrity, sensor drift, coherence, mass balance, four-way attribution and clonal-drift detection |

Max mode is implemented for the iPSC → monocyte process. The CAR-T stand-in
reports minimal mode only and says so rather than faking the instruments.

## Analysis order

Release safety and genetic stability first, then whether the data can be
trusted, then the target, then QC, then the genotype comparison, then diagnosis.
Verdict severity: `SAFETY_HOLD` > `DATA_UNRELIABLE` > `SUCCESS` >
`TARGET_MET_QC_INCOMPLETE` > `FAILED`. A difference between an engineered arm
and the control counts only if it exceeds 2 standard errors (replicates), 10% of
the control (single run), or 1 day for timing; each predicted effect becomes
`consistent`, `contradicted`, `inconclusive`, `not_measured` or `untestable`,
with its strength stated.

## Stand-in reactors

`standins/` holds product-specific synthetic stand-ins for the wet lab. Each
reads an approved protocol, turns its literature-derived values into model
inputs, and returns a `bioreactor_run` labelled `synthetic_standin`.

| Stand-in | Process | Modes | Scenarios |
|---|---|---|---|
| `tcell` | activation → transduction → expansion | minimal | `clean`, `poor_viability`, `low_transduction` |
| `monocyte` (in `analysis_agent/protocol_runner.py`) | iPSC → monocyte, 4 stages | minimal, max | `clean`, `clonal`, `sensor_fault`, `stain_fault` |

Each arm's engineered genotype is **hidden truth** in a separate file the agents
never read, so a knockout can genuinely need different conditions than the
control and the loop has to discover that from measurements. Neither stand-in is
a validated digital twin; only relative comparisons mean anything, and their
output is never evidence.

## The CAR-T example

`examples/cart/` is a fully synthetic fixture: an invented source paper
(`sources/SYNTH-CART-001.json`), three cumulative extractions and handoffs, a
request (≥12 CAR+ T cells per input T cell at day 10, unedited control vs an
`EXH1` knockout), three protocols, and the stand-in's hidden truth — the
knockout needs a 24 h activation instead of 48 h and IL-15 at 15 instead of
5 ng/mL. Nothing in it, including the gene symbol, is real.

`python -m biosense.production.cli demo-cart --out runs/cart-demo` walks it:

| Step | Verdict | Decision | Spends an iteration? |
|---|---|---|---|
| it0 | `FAILED` — control 9.6, knockout 1.0 | `request_bioinformatics` → 2 levers implicated, 2 questions untestable here | no |
| it0 | | `consult_human` → non-blocking, with a written fallback; answered as an in-house result | no |
| it0 | | `revise_protocol` → shared cytokine change first, knockout levers into the brief | yes |
| it1 | `FAILED` — control 17.3 (met), knockout 0.7 | `revise_protocol` → control on the keep-fixed list, levers now knockout-only | yes |
| it2 | `TARGET_MET_QC_INCOMPLETE` — both met | `complete_qc` → close the QC record, do not change the recipe | no |
| release | `SUCCESS` | `protocol_succeeded` | no |

Two of four iterations spent. The decision documents in the demo are scripted
stand-ins for the orchestrator's reasoning, pushed through the same
`commit_decision` validation the live agent must pass — so the demo proves the
contracts, the envelope, the consult mechanism and the ledger, not that an LLM
reasons well.

## Commands

```bash
uv sync --locked
uv run --frozen python -m unittest -v                          # everything
uv run --frozen python -m unittest tests.test_agent_loop -v     # the agent loop only

# offline end-to-end, no model
uv run --frozen python -m biosense.production.cli demo-cart --out runs/cart-demo
uv run --frozen python -m biosense.production.cli demo      --out runs/mono-demo

# one step at a time (what the agents call)
C="uv run --frozen python -m biosense.production.cli"
X=examples/cart
$C validate-request --request $X/request.cart_d10.json
$C autonomy        --request $X/request.cart_d10.json
$C loop-init       --request $X/request.cart_d10.json --out runs/loop1
$C render-protocol --protocol $X/protocol.it0.synthetic.json --handoff $X/handoff.it0.synthetic.json \
   --request $X/request.cart_d10.json --out-dir runs/loop1/it0
$C approve-protocol --protocol $X/protocol.it0.synthetic.json --handoff $X/handoff.it0.synthetic.json \
   --request $X/request.cart_d10.json --approved-by "Your Name" --out runs/loop1/it0/protocol.approved.json
$C simulate-standin --standin tcell --protocol runs/loop1/it0/protocol.approved.json \
   --truth $X/standin_truth.synthetic.json --out runs/loop1/it0/run.json
$C analyze  --run runs/loop1/it0/run.json --protocol runs/loop1/it0/protocol.approved.json \
   --request $X/request.cart_d10.json --out runs/loop1/it0/analysis.json
$C advise   --analysis runs/loop1/it0/analysis.json --protocol runs/loop1/it0/protocol.approved.json \
   --request $X/request.cart_d10.json --loop-dir runs/loop1
$C propose-decision --decision my_decision.json --analysis runs/loop1/it0/analysis.json \
   --protocol runs/loop1/it0/protocol.approved.json --request $X/request.cart_d10.json \
   --loop-dir runs/loop1 --check-only
$C loop-status --loop-dir runs/loop1
```

Live: `omnigent run discovery_loop`, then for example:

> Run the production loop for examples/cart/request.cart_d10.json. Use the
> synthetic stand-in reactor. Check the autonomy summary with me first.

## Limitations

- Diagnosis rules and lever suggestions are heuristics that point at protocol
  parameters; they are not causal inference. With one replicate per arm,
  genotype differences are directional only.
- The shipped knowledge set contains only invented placeholder genes. Real
  annotations have to be curated with citations, or fetched live and then
  curated.
- QC defaults in `qc_profiles/` are configurable starting points (viability
  ≥70% follows FDA CMC guidance; others are demo defaults or left to QA).
- The orchestrator's reasoning quality is not tested by anything here. The tests
  cover the envelope, the contracts and the refusals — the things that stay true
  regardless of which model is driving.
- Live Omnigent orchestration of this loop has not been run yet.
- A loop that reaches `protocol_succeeded` has not produced a validated or
  released process.

## Shared setpoints, new levers, and the protocol as a timeline

- **Process reference.** Physical setpoints shared by suspension culture of
  iPSC and their derivatives (temperature, DO, seeding, feed, agitation with
  its vessel) fill a protocol's gaps as *design choices* naming their entry and
  basis — never as reported values. The repository ships conventions only
  (`process_reference/`); cited entries come from a run with purpose
  "Build the process reference" and are promoted by a named admin reviewer into
  the server's private data root. An unreviewed entry, or agitation with no or
  another vessel, fills at low confidence and says why.
- **Candidate levers.** A hypothesis may name a lever the project does not
  have (IL-34 for a macrophage process). It is shown as a dashed row on the
  timeline, never adopted; "Register in my project" adds it to the person's own
  copy of the project (unit, range, stage; no value) and rebuilds that run's
  protocol, which can then carry the hypothesis' value.
- **Assumed effects.** The reactor has no term for a new factor, so it is
  simulated like an edited line: a growth and a differentiation ratio a person
  or the orchestrator sets, applied only in the stage the factor is given
  (`evidence.cli simulate --factor "IL-34:stage=myeloid,growth=1,diff=1.2"`, or
  the Simulator card). Every result says it is an assumption played through an
  uncalibrated model.
- **Timeline.** The protocol is drawn as the production chain: stages sized by
  their project days, factors as bars over their stage, setpoints per stage and
  for the whole process, each with its provenance letter and a three-step
  confidence bar; a click shows its basis. The Markdown export carries the same
  per-stage table.
