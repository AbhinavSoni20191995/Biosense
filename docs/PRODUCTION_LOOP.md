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
| `alternate_plans` | orchestrator, at a person's request | the other leads as experiments: each a claim, its arms, what it costs to run and what either outcome means; confidence capped by cited / mechanistic / judgement; never changes round 1 |
| `breakthrough_design` | orchestrator, after round 1 or at a person's request | the experiment closest to the aim if the apparatus were not the limit: each lifted constraint (now, lifted to, why it limits), what is kept (aim, QC limits, safety bounds — always), the apparatus, stages with provenance per quantity, arms with one control, readouts, a decision rule, a judgement-typed expected range, feasibility and risks; status `concept`, never runnable here or approved |
| `bench_setup` | literature agent | media per stage, reagents, handling, consumables, safety, do's and don'ts with their reasons; a supplier or catalogue number only where a source states it, everything else "use your own qualified stock" |
| `baseline_protocol` | literature agent | the published route stage by stage: every stage of the project, what goes in the medium, the dose where a source gives one with the sentence quoted; `reported` / `adapted` / `design_choice` / `gap` per factor, and a stage nobody addressed stated rather than missing |
| `loop_decision` | orchestrator agent, or the policy | type, `authored_by`, `reasoning`, `considered` alternatives, `hypotheses` with their basis, `tool_calls`, `consult_ids`, the `allowed_actions` it was validated against, the `policy_advice` it may have overridden, `advances_iteration`, `status` |
| `communication` | communication agent | the run told plainly for the final report: executive-summary paragraphs, one caption per protocol stage (what it is for, what it looks like at the bench, what to watch for), closing; **every number in the prose must already stand in the protocol** — the builder refuses one that does not — and it changes nothing and claims no approval |

## The whole route, two runs compared, and a copy for the bench

Three things a protocol document cannot do on its own, and where each lives:

**Every stage, not only the one that changed** (`baseline_protocol`,
`biosense/evidence/baseline.py`). A protocol carries a dose only for a
parameter the project registered. A project made from the quick form has the
vessel controls and no biology, so a run that researched all four stages still
renders three of them as "no inducer set". The literature specialist writes the
established route instead — every stage of the project, what goes into the
medium, the dose where a source gives one and the sentence it rests on. A
`reported` factor cites a source; `adapted` says which context it came from;
`design_choice` carries a rationale and no citation; `gap` carries **no value**
and says what to search. A stage nobody addressed comes back stated, not
missing. These rows reach the plan marked **PUB**, never as protocol values:
registering one as a parameter is still a person's act.

**Two runs of the same question** (`biosense/production/compare_runs.py`,
`/api/discovery/compare?a=…&b=…`). A run is a language model reading papers, so
two runs of one question are two samples, not one answer repeated. The
comparison reads what each run wrote: the inputs first (a different model,
objective, effort or dataset explains everything that follows, so it is
reported before any output is), then lever by lever — agreed, same direction
with a different dose, pushed opposite ways, or raised by one run only. A lever
one run of two found is a candidate for the round plan, not a finding. Nothing
in it calls either run the better one; agreement means the pipeline is stable,
not that the biology is right.

**A copy somebody can sign** (`biosense/production/lab_protocol.py`,
`?format=docx` or `?format=pdf`). The same protocol as a Word document or a
PDF in the order a scientist reads it: what is still undecided first; the
whole process on one page — each stage, its days, what goes into the medium,
the vessel settings, what to measure; the materials with their working
concentrations and basis; each stage as lines to tick off, in the order they
happen, the dose in bold and its letter after it; the settings an operator
enters; what to measure; the round as one row per vessel; then the reasoning
(what the run is testing), the letters, the limitations, and an empty approval
block. "PROPOSED — NOT APPROVED" is at the top and the bottom, and every
number keeps its letter. One `build` writes to either writer: the Word one
(`biosense/production/docx.py`) is about two hundred lines of OOXML on the
standard library, the PDF one (`biosense/production/pdf.py`) lays the same
blocks onto A4 with the built-in fonts, so neither is a dependency.

**The bench copy can be changed from the Ask conversation**
(`biosense/evidence/protocol_edits.py`, the third kind of turn, below).

## The final report: everything at the end, as one document

The run's record is exact and scattered — the bench copy, the landscape page,
the specialists' sections — and the person taking the result to a lab meeting
or a collaborator was assembling it by hand. `biosense/production/final_report.py`
composes it (`?format=docx|pdf&view=report`, or
`production.cli final-report --run <run-dir>`): a cover with the provenance
key, an executive summary, the process stage by stage as it looks at the
bench, the round, then the full bench protocol and the literature and
bioinformatics sections as chapters, from the same blocks the standalone
exports write. It computes nothing, upgrades no provenance letter, says
PROPOSED — NOT APPROVED where the bench copy says it, and ends in the same
empty approval block. `?format=svg` (and `final-report`'s `process_flow.svg`)
is the process as a picture: the stages to scale, what goes into each, a gap
drawn as a gap — for the run page and anywhere that can show an image, which
the Word and PDF writers deliberately cannot.

The words between the tables come from the **communication agent**
(`discovery_loop/agents/communicator/`, `biosense/evidence/communication.py`;
the fifth kind of Ask turn, "Write the run up", or a dispatch at the end of a
live run). It writes exactly one file, `communication.json`: executive-summary
paragraphs and one plain-language caption per protocol stage — what the stage
is for, what somebody actually does in it, what to watch for. Its hard rule is
that **a number in the prose must already stand in the protocol**: every
numeric token is checked against the numbers the validated protocol carries
(identifiers like PMC123 and a DOI keep their digits; small counting numbers
pass), and a caption with any other number is refused by name — the checker
also refuses overclaiming ("proves", "optimal") and any claim of approval. The
narrative is optional and never load-bearing: the report renders without it
with an assembled summary instead, and every value prints from the protocol's
own rows beside the prose, never from the prose.

What a person reads is written for a person. The run page and the exported round page lead with the communication agent's `key_points` when it wrote them; otherwise `plain_plan.key_points` assembles the summary from the plan, and either way a lever is named by its label ("dissolved oxygen"), never by its parameter id (`do_setpoint`), which is how the agents and the tools address it. The same holds for the "decide these before the round starts" list and for an arm's changes.

## From a recommendation to something somebody can run

An arms table is not a protocol. "A2: GM-CSF 50 ng/mL replacing M-CSF" does not
say which medium, made up how, which reagent at what stock concentration,
seeded how, fed how often, for how long, or what ruins the run — and the run
has usually read the papers that answer most of it. `bench_setup`
(`biosense/evidence/bench.py`) is where that goes: media per stage, reagents,
handling step by step, consumables, safety, and the do's and don'ts, each with
what goes wrong if it is ignored, because a rule with no reason is folklore.

Its hard rule is sourcing. A **supplier or catalogue number may only be
recorded where a source states it**, with the sentence quoted; a paper's
methods section usually does. Anything else is `sourcing: "your_lab"`, which
says to use your own qualified stock and names nobody. A catalogue number looks
like a fact, is rarely checked before ordering, and a wrong one costs a week,
so one with nothing behind it is refused outright rather than caveated.

The answer is laid out in the order somebody acts in: **the hypothesis** (the
claim, with the one-line action it implies inside it — not an instruction with
the science folded away behind "Why"), **round 1 at the bench** as a section of
its own with everything above, then **the current estimated parameters** for
the vessel in use. On a wide screen the panels that fit beside each other do,
because a long page of half-empty cards is one nobody scrolls to the end of.

Stage lengths are the project's own and **editable**: `d0–7` was a fixed
property of the profile, so every plan drew the same ranges whatever the real
process does. `POST /api/projects/<id>/stages/<stage>/days` sets it inside the
bounds the profile declares, in the person's own copy, and the plan redraws.

## The other leads, as experiments

A run surfaces more than one round can hold. The developmental map alone comes
back with a Notch ligand from the pulmonary endothelium, a surfactant lipid
load, GM-CSF as a timed surge rather than a constant, and a list of what must
not be in the medium — each with its source. One becomes the hypothesis; the
rest become prose, and nobody puts a vessel on prose.

So the Ask conversation has a second kind of turn (and a third, below). A
**question** answers from the run's files and changes nothing, and the
fingerprint proves it. **"Work this up as an alternate plan"** writes exactly
one file: `alternate_plans`
(`biosense/evidence/alternates.py`), each item a claim, the arms that would
settle it, what to measure, what it costs to run — vessels, a construct, a
reagent nobody has — and what to do with either outcome. The same fingerprint
is then read the other way round: anything *else* that moved is named on the
turn.

Two rules make it safe to offer:

* **round 1 is never touched.** Its arms and decision rules were fixed before
  any result existed and its commitment digest records that, so an alternate
  that could rewrite it would make the promise worthless. These live in their
  own file, labelled proposed, beside it;
* **an alternate that restates the main hypothesis is refused.** One that moves
  the same lever the same way without saying properly how it differs costs a
  vessel to learn nothing.

Confidence is capped by what it rests on: `judgement` — the orchestrator's own
reasoning — is low, a mechanism nobody tested here is moderate at best, and
`cited` needs refs from the run's own evidence. A judgement is a real answer
and says so, rather than acquiring a citation that does not test the change.


## Past the apparatus

Round 1 is designed inside the process as it is: the vessel, its working
volume, the settings it can hold, the request's bounds. That is right for
round 1, and it is also why a run can end with "the levers are timing levers
and a continuous harvest cannot time them". The constrained design says how
close the process can get; it does not say how close the aim could be got.

So the orchestrator's brief ends with one more design step, and the Ask
conversation has a fourth kind of turn: **"Design past the apparatus"** writes
exactly one file, `breakthrough_design`
(`biosense/evidence/breakthrough.py`). The checker holds four rules:

* **only the apparatus is lifted.** A lifted constraint of kind target, qc or
  safety is refused, and so is one that names a bound the request records as
  a `safety_constraint`. The aim, the QC limits and those bounds are listed
  under `kept` by BioSense, whatever the draft says;
* **every quantity keeps its provenance**, exactly as in a protocol: reported
  with refs, adapted or design choice with a rationale, a gap with no value
  and what would settle it;
* **one control**: the round-1 process carried into the new apparatus, so the
  apparatus' own effect can be told from the biology's;
* **the expected outcome is a judgement**: an Estimate typed `judgement`,
  whose confidence cannot exceed the design's own, which is capped by its
  evidence basis. The toy model does not cover the new apparatus and is not
  quoted as if it did.

It is labelled `concept`, `runnable_on_current_apparatus: false` and
`ready_for_wet_lab: false`, and does not change the main hypothesis, the
protocol or round 1. It becomes a protocol only when one is written for the
new apparatus and a named person approves it.

## The bench copy, changed in the same conversation

The run wrote one protocol for one vessel. The person reading it will run it in
a 6-well plate at 2 mL, seeds at the density their lab uses, wants a line
saying which medium lot to record, and does not want the alternates appendix in
the copy that goes to the hood. None of that is science, and none of it should
cost a run — so the Ask conversation has a third kind of turn. **"Change the
protocol document"** writes exactly one file, `protocol_edits`
(`biosense/evidence/protocol_edits.py`), which every export of that run then
carries: the Word file, the PDF and the vessel layout. The same file can be
given directly on the run page (`POST /api/discovery/<id>/protocol-edits`),
which is also the only way for a demonstration run, which has no orchestrator.

What it may hold, and the rule for each:

| | checked how | on the page as |
|---|---|---|
| `culture_format`, `working_volume_ml` | a format this system knows; a volume in range | the vessel section, amounts per vessel |
| a value (`values[]`) | `design.choice`: a parameter the project exposes, in its unit, inside the project's bounds, a rationale, `derived_from`, a confidence — the same refusals a run's own design choice gets, including the per-cm² hint | **D**, with who asked; the run's value travels as "was" |
| a note (`notes[]`) | the person's words, on a stage or the whole run; may not claim approval | **P** — the person's instruction, never a value |
| `omit[]` | a named section; the open values, the steps and the approval block cannot be left out | absent |

The run's protocol summary, hypotheses and round plan are not rewritten — the
edits are a layer `protocol_edits.apply` lays over the display payload at
export time, named as the person's request on the first page and listed in
full under "Changes requested after the run" — and the fingerprint names
anything *else* the orchestrator touched. Nothing here is approved; the
approver reads the edits as the person's, and `?edits=none` exports the
protocol as the run wrote it.

## Plate, tube, tank: the vessel the protocol is for

Nobody starts in a bioreactor. The work starts in a **6-well plate**, because
that is where eight conditions cost what one bioreactor run costs and you can
look at them; it moves to a **CERO 3D rotating tube** when suspension
aggregates are the point and a plate cannot make them; it ends in a
**stirred tank** when the number of cells is the point. `biosense/formats.py`
holds the three, and a protocol written for one can be laid out in another —
chosen at the start of a run (`culture_format` on the request, which the brief
then tells the agents) or applied to a finished one
(`/api/discovery/<id>/translate?to=…`, or `?format=docx&as=…` for the bench
copy).

What travels between vessels is less than people assume, and that is the whole
content of the module:

| | travels? |
|---|---|
| a concentration (ng/mL, µM) | yes — and the amount per vessel is worked out from the working volume |
| a time, a temperature, a fraction | yes |
| an impeller speed, a tube rotation rate | **no**, in either direction: different geometry, different shear, no conversion exists |
| a dissolved-oxygen setpoint | **no** into a plate or a tube: neither has a probe or a control loop, so the number would describe nothing |
| cells/mL → cells/cm² | **no**: which one matters depends on whether the culture is adherent, which the vessel does not know |

Every number about a vessel carries its basis — `catalogue` for published
labware geometry (a 6-well well is 9.6 cm²), `convention` for standard practice
(2 mL of medium in it), `design_choice` for what the person sets. A CERO tube's
working volume has **no default**: it is nominally 50 mL, never run full, and
how full is a decision, so per-vessel amounts appear only once somebody gives
one.

The simulator follows the same rule. The project's model was written for a
stirred tank, so in a plate or a tube its mixing and oxygen terms refer to
equipment that is not there: `formats.simulatable()` drops them, names each one
dropped, and labels what is left as the tank's dose response read in a vessel
it was not fitted in — a hypothesis about the model, not a prediction about
that format. `evidence.cli simulate --culture-format` refuses outright when
every setting given was a tank control.

A translated protocol is a **starting point in the new vessel, not the same
process**. Oxygen transfer, shear, settling and evaporation all change and the
cells respond to them, so the first run in a new format is a comparison against
the old one — never a continuation, and never evidence that the protocol
transferred. Every translation says so.

## A sleeping computer

A run's time limit is a bill cap, and a suspended process buys nothing: no turn
runs, no token is spent. So a laptop that sleeps must not cost a run.

- While a real run is working the process holds a wake lock (`awake.py`):
  `caffeinate -i -w <pid>` on macOS, `systemd-inhibit` on Linux,
  `SetThreadExecutionState` on Windows. It is a request, not a guarantee — a
  closed lid or a flat battery wins — and a machine that cannot hold one says
  so in the run's timeline instead of implying it can.
- When the machine sleeps anyway, `clock.py` measures it: the wall clock counts
  the sun and the monotonic clock counts this machine running, so their drift
  is the time it was away. The deadline, the wrap-up warning and the pause hold
  all move by exactly that, and the run says so in its timeline. Windows counts
  suspended time in its monotonic clock, so no gap is visible there and the cap
  stays wall-clock — a weaker promise, stated rather than implied.
- The page notices a tick that arrives late, rebuilds its event stream and
  re-reads the run, so waking up does not show a frozen run.

For a run that must continue while you walk away from the machine entirely,
run the server somewhere that does not sleep; nothing in a suspended process
executes, whatever this does for its bookkeeping.

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

# two runs of the same question, compared by code
uv run --frozen python -m biosense.production.compare_runs \
   data/runs/ai-20261012-aaa111 data/runs/ai-20261012-bbb222 --markdown
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

## The data analyst, public data of any species, and modelled new parameters

- **Data analyst (AI).** Once `bioinformatics` has registered a dataset, the
  orchestrator sends it to the `analyst` agent. It inspects the data
  (`analyse inspect`), plans against the columns and groups actually there,
  repairs a plan a tool refuses (up to three times), runs it with the
  deterministic tools — or, when none can express the analysis, a script it
  writes that runs here and is labelled AGENT-WRITTEN at low confidence — and
  writes an interpretation (`analyse interpret`) whose confidence is capped by
  the result, the dataset's species and processing route, and how close the
  experiment is to this process. Numbers always come from a tool or a script.
- **Public data, any species.** `datasets fetch-geo` uses NCBI's processed
  counts where they exist (human, mouse) and otherwise the depositors' own
  table, for any species, and records an evidence weight: human via NCBI can
  support high confidence, another mammal or a depositor's table moderate or
  low, a non-mammal low.
- **Modelling a new parameter.** A person states how a parameter the model has
  no term for acts — shape, target, constants, a bounded size and why — when
  registering it or afterwards from its basis panel. It is stored on their
  project as EXPERT-DECLARED. The Simulator ("Compare the protocol with and
  without them") and `evidence.cli simulate --modelled-parameters` play it as
  an assumed change in growth or differentiation in its stage.
- **Live tracker.** The Loop Tracker follows a running AI run: agents, hand-offs,
  events, and the protocol when it lands.
