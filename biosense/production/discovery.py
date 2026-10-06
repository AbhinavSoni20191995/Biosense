"""The structured discovery request, and the brief an agent is given.

What a person fills in before pressing *Run AI Discovery* — the project, the
objective, the biological scope, the datasets, the internal knowledge, the
process constraints — already has contracts. `ResearchContext` carries a
strictness setting and the fields a mismatch can be in; a DatasetManifest carries
a visibility; expert knowledge is private and not citable. Concatenating all of
that into one text blob would throw every one of those distinctions away and
leave a model to re-derive them from prose, which is exactly the failure this
repository is built to avoid.

So `build()` produces a validated DiscoveryRequest and `render_brief()` turns it
into the message an orchestrator receives. Two properties of that rendering
matter more than its wording:

* **The objective is data, never syntax.** It is written into the brief inside a
  JSON document, and the brief travels as the body of an HTTP request. It is
  never interpolated into a command line, an argv or a shell string, and there is
  no code path in BioSense that would let it become one. `tests/test_discovery.py`
  asserts this with a request whose objective is a shell metacharacter soup.
* **The brief names where artifacts go.** An agent that writes its analysis
  somewhere BioSense does not read produces a run that looks empty. The brief
  states the loop directory, the contracts to write, and the tools to write them
  with — so ingestion reads files that passed a schema, rather than parsing a
  model's prose into a number.
"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

from .. import contracts as K
from .. import parameters as PR
from .. import projects as PJ
from ..evidence import context as CTX
from . import runtime as RT

MAX_OBJECTIVE = 4000
MAX_DATASETS = 24
MAX_KNOWLEDGE = 24
REQUEST_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$')

# The contracts the agents are asked to write, and what each one is for. Stated
# in the brief itself so an orchestrator does not have to infer the shape of the
# handoff from the prompt it was given weeks earlier.
ARTIFACTS = (
    ('analysis_plan.json', 'analysis_plan',
     'the question, the dataset ids, the tool, and the uncertainty it resolves'),
    ('analysis_result.json', 'analysis_result',
     'what the tool computed, written by `bioinformatics.cli analyse run` — never by hand'),
    ('quantified_hypothesis.json', 'quantified_hypothesis',
     'the parameter change, its expected effects as Estimates, and the evidence behind each'),
    ('research_context.json', 'research_context',
     'the scope that was applied, so a context mismatch can be shown rather than absorbed'),
)


def _clean(text, *, field, limit, minimum=0):
    s = (text or '').strip()
    if len(s) < minimum:
        raise K.ContractError(
            f'{field} needs at least {minimum} characters; got {len(s)}')
    if len(s) > limit:
        raise K.ContractError(f'{field} is {len(s)} characters; the limit is {limit}')
    return s or None


# How much each specialist may spend. What a run costs is mostly the literature
# agent's turns — every search, fetch and extracted claim is a model turn — so
# the budgets are what a person turns to get a faster or a deeper answer. They
# bound the search; they never license a number the evidence does not support.
EFFORT = {
    'quick': {'label': 'Quick', 'searches': 4, 'full_texts': 2, 'minutes': 5,
              'bio_queries': 3, 'hypotheses': 1, 'rounds': 1},
    'standard': {'label': 'Standard', 'searches': 8, 'full_texts': 4, 'minutes': 10,
                 'bio_queries': 6, 'hypotheses': 2, 'rounds': 2},
    'thorough': {'label': 'Thorough', 'searches': 14, 'full_texts': 8, 'minutes': 20,
                 'bio_queries': 10, 'hypotheses': 3, 'rounds': 3},
}
DEFAULT_EFFORT = 'standard'
# How the literature is searched. One agent across every lever is the default
# and what every run so far did. By stage, the same agent is dispatched once per
# process stage in parallel and the results are merged: the literature phase
# takes as long as its slowest stage instead of all of them, each agent reads a
# narrower question, and the cost is more model calls and a reconciliation step.
LITERATURE_MODES = ('single', 'by_stage')
DEFAULT_LITERATURE_MODE = 'single'
MAX_STAGE_SHARDS = 4


def build(*, project_id, objective, runtime_mode, research_context=None, dataset_ids=(),
          expert_knowledge_ids=(), process_constraints=None, uncertainty=None, control=None,
          candidate_values=None, title=None, notes=None, requested_by=None, request_id=None,
          projects_dir=None, effort=None, literature_mode=None):
    """A validated DiscoveryRequest, or a refusal naming what is wrong.

    Everything is checked against something real: the project against the profile
    directory, every parameter name against the canonical registry, the research
    context against its own contract. A request that names a parameter the
    project does not have is refused here rather than producing a candidate
    nobody can set.
    """
    project = PJ.load(project_id, projects_dir)
    objective = _clean(objective, field='objective', limit=MAX_OBJECTIVE, minimum=10)
    mode = RT.normalise_mode(runtime_mode)

    ds = [str(d).strip() for d in (dataset_ids or []) if str(d).strip()]
    ek = [str(k).strip() for k in (expert_knowledge_ids or []) if str(k).strip()]
    if len(ds) > MAX_DATASETS:
        raise K.ContractError(f'{len(ds)} datasets named; the limit is {MAX_DATASETS}')
    if len(ek) > MAX_KNOWLEDGE:
        raise K.ContractError(f'{len(ek)} knowledge entries named; the limit is {MAX_KNOWLEDGE}')

    if research_context is not None:
        K.require_valid('research_context', research_context)

    constraints = _constraints(process_constraints, project)
    cands = _values('candidate_values', candidate_values, project)
    ctrl = dict(control) if control else None

    effort = (effort or DEFAULT_EFFORT).strip().lower()
    if effort not in EFFORT:
        raise K.ContractError(f'effort must be one of {sorted(EFFORT)}; got {effort!r}')
    literature_mode = (literature_mode or DEFAULT_LITERATURE_MODE).strip().lower()
    if literature_mode not in LITERATURE_MODES:
        raise K.ContractError(f'literature_mode must be one of {LITERATURE_MODES}; '
                              f'got {literature_mode!r}')

    rid = request_id or f'disc-{uuid.uuid4().hex[:12]}'
    if not REQUEST_ID.match(rid):
        raise K.ContractError(f'request_id {rid!r} is not a usable identifier')

    req = {
        'schema_version': K.PRODUCTION_VERSION,
        'request_id': rid,
        'created_at': K.now_iso(),
        'title': _clean(title, field='title', limit=200),
        'project_id': project.project_id,
        'objective': objective,
        'research_context': research_context,
        'dataset_ids': ds,
        'expert_knowledge_ids': ek,
        'process_constraints': constraints,
        'uncertainty': _uncertainty(uncertainty),
        'control': ctrl,
        'candidate_values': cands,
        'runtime_mode': mode,
        'effort': effort,
        'literature_mode': literature_mode,
        'requested_by': _clean(requested_by, field='requested_by', limit=200),
        'notes': _clean(notes, field='notes', limit=2000),
    }
    K.require_valid('discovery_request', req)
    return req


def _uncertainty(u):
    if not u:
        return None
    kind, ref, statement = u.get('kind'), (u.get('ref') or '').strip(), (
        u.get('statement') or '').strip()
    if kind not in ('hypothesis', 'evidence_gap', 'diagnosis'):
        raise K.ContractError(
            "an uncertainty's kind must be hypothesis, evidence_gap or diagnosis")
    if not ref or len(statement) < 10:
        raise K.ContractError(
            'an uncertainty needs a reference and a statement of what is missing, in at '
            'least 10 characters. "Something about the dose" is not a question a run can '
            'be checked against.')
    return {'kind': kind, 'ref': ref, 'statement': statement}


def _values(field, values, project):
    """Canonicalise parameter names and refuse one this project does not expose."""
    if not values:
        return None
    out = {}
    for name, value in values.items():
        pid = PR.resolve(name)
        if not project.has(pid):
            raise K.ContractError(
                f'{field}: {project.project_id} does not expose {pid!r}. A parameter is not '
                f'available here merely because the canonical registry defines it; this '
                f"project's parameters are: {', '.join(sorted(project.parameter_ids))}.")
        out[pid] = value
    return out


def _constraints(c, project):
    if not c:
        return None
    bounds = []
    for row in c.get('parameter_bounds') or []:
        pid = PR.resolve(row['parameter_id'])
        if not project.has(pid):
            raise K.ContractError(
                f'process_constraints names {pid!r}, which {project.project_id} does not have')
        lo, hi = row.get('minimum'), row.get('maximum')
        if lo is not None and hi is not None and lo >= hi:
            raise K.ContractError(f'process_constraints/{pid}: minimum must be below maximum')
        if lo is None and hi is None:
            raise K.ContractError(
                f'process_constraints/{pid}: a constraint with neither a minimum nor a '
                f'maximum constrains nothing')
        if not row.get('source'):
            raise K.ContractError(
                f'process_constraints/{pid}: say where the limit comes from — equipment, '
                f'safety, literature, an internal experiment or a design choice. A bound '
                f'with no origin is a guess wearing the clothes of a constraint.')
        bounds.append({'parameter_id': pid, 'minimum': lo, 'maximum': hi,
                       'source': row['source'], 'basis': row.get('basis')})
    notes = _clean(c.get('notes'), field='process_constraints.notes', limit=2000)
    if not bounds and not notes:
        return None
    return {'notes': notes, 'parameter_bounds': bounds}


def from_benchmark_config(cfg, *, runtime_mode, projects_dir=None, request_id=None):
    """A DiscoveryRequest from a committed BenchmarkConfig.

    The two documents were always nearly the same thing — a benchmark
    configuration is the structured form of "what shall we look into". This lets
    a person run a published benchmark from the browser, under either runtime,
    without the configuration being retyped into a different shape and drifting.
    """
    K.require_valid('benchmark_config', cfg)
    return build(
        project_id=cfg['project_id'], objective=cfg['objective'],
        runtime_mode=runtime_mode,
        research_context=_context_doc(cfg.get('research_context')),
        dataset_ids=cfg.get('datasets') or [],
        expert_knowledge_ids=cfg.get('expert_knowledge') or [],
        uncertainty=cfg.get('uncertainty'), control=cfg.get('control'),
        candidate_values=cfg.get('candidate_values'),
        title=cfg.get('title'), notes=cfg.get('notes'),
        request_id=request_id, projects_dir=projects_dir)


def _context_doc(raw):
    """A research context from a benchmark config's shorthand, or None.

    A benchmark config carries the fields without the envelope the contract
    wants, so it is rebuilt through `context()` rather than patched: that way the
    strictness rule and the field validation apply to it exactly as they would to
    one a person filled in.
    """
    if not raw:
        return None
    if raw.get('schema_version'):
        K.require_valid('research_context', raw)
        return raw
    return CTX.context(
        strictness=raw.get('strictness', 'prefer'),
        species=raw.get('species') or (), cell_types=raw.get('cell_types') or (),
        tissues=raw.get('tissues') or (), states=raw.get('states') or (),
        disease_context=raw.get('disease_context') or (),
        modalities=raw.get('modalities') or (), assays=raw.get('assays') or (),
        therapy_context=raw.get('therapy_context') or (),
        exclude=raw.get('exclude') or (), notes=raw.get('notes'))


# ── what the person is told before anything runs ────────────────────────
def summarise(req, *, projects_dir=None):
    """The request in plain language, for the confirmation step and the report."""
    project = PJ.load(req['project_id'], projects_dir)
    lines = [f'Project: {project.name} ({project.project_id} v{project.version})',
             f'Objective: {req["objective"]}']
    ctx = req.get('research_context')
    if ctx:
        scope = []
        for key in ('species', 'cell_types', 'states', 'tissues', 'disease_context'):
            if ctx.get(key):
                scope.append(f'{key.replace("_", " ")}: {", ".join(ctx[key])}')
        lines.append(f'Scope ({ctx["strictness"]}): ' + ('; '.join(scope) or 'no fields set'))
    else:
        lines.append('Scope: none declared. Evidence from any species, cell type or state '
                     'may be used, and no context mismatch can be flagged.')
    if req['dataset_ids']:
        lines.append(f'Datasets: {", ".join(req["dataset_ids"])}')
    if req['expert_knowledge_ids']:
        lines.append(f'Expert knowledge: {", ".join(req["expert_knowledge_ids"])} '
                     f'(private; may narrow a range, never supply a cited value)')
    if req.get('uncertainty'):
        lines.append(f'Uncertainty: {req["uncertainty"]["statement"]}')
    else:
        lines.append('Uncertainty: not stated, so the run has to identify one and show it.')
    c = req.get('process_constraints') or {}
    for b in c.get('parameter_bounds') or []:
        rng = f'{b["minimum"] if b["minimum"] is not None else "?"}–' \
              f'{b["maximum"] if b["maximum"] is not None else "?"}'
        lines.append(f'Constraint: {b["parameter_id"]} within {rng} ({b["source"]})')
    if c.get('notes'):
        lines.append(f'Constraint note: {c["notes"]}')
    lines.append(f'Runtime: {RT.LABELS[req["runtime_mode"]]}')
    b = EFFORT[req.get('effort') or DEFAULT_EFFORT]
    if (req.get('literature_mode') or DEFAULT_LITERATURE_MODE) == 'by_stage':
        lines.append('Literature: one agent per process stage, in parallel, merged afterwards')
    lines.append(f'Effort: {b["label"]} — about {b["searches"]} searches and {b["full_texts"]} '
                 f'full texts for literature, {b["bio_queries"]} queries for bioinformatics, '
                 f'about {b["minutes"]} minutes per specialist')
    return '\n'.join(lines)


def privacy(req):
    """Whether this request's lineage is private, and which inputs make it so.

    Decided from the request alone, before anything runs, because the answer
    changes where a benchmark may be written and whether a result may be
    published at all.
    """
    private = []
    if req['expert_knowledge_ids']:
        private += [f'expert_knowledge:{k}' for k in req['expert_knowledge_ids']]
    from ..data import registry as DREG
    for did in req['dataset_ids']:
        try:
            m = DREG.load(did)
        except (K.ContractError, OSError, ValueError):
            continue
        if m and m.get('visibility') == 'private':
            private.append(f'dataset:{did}')
    return {'has_private_lineage': bool(private), 'private_sources': private,
            'export_policy': 'private' if private else 'public_safe'}


# ── the brief handed to the orchestrator ────────────────────────────────
def _literature_plan(req, project, loop_dir, budget):
    """The literature dispatch the brief asks for, and what to do once it returns."""
    common = ('`args:` starting `DISCOVERY EVIDENCE:`, then the objective, the project, the '
              'scope, {focus}, its run directory (`{folder}`) and what to bring back: cited claims '
              'and labelled best guesses with their confidence')
    if (req.get('literature_mode') or DEFAULT_LITERATURE_MODE) != 'by_stage' or \
            len(project.stages) < 2:
        return (('Send the literature agent its task with `sys_session_send` — `agent: '
                 '"literature"`, `title: "literature-it1"`, '
                 + common.format(focus='the levers you most need evidence on',
                                 folder=f'{loop_dir}/literature/')), '')
    stages = list(project.stages)[:MAX_STAGE_SHARDS]
    per_s = max(3, round(budget['searches'] * 0.6))
    per_t = max(1, round(budget['full_texts'] / 2))
    rows = '\n'.join(
        f'    - `title: "literature-{st["stage_id"]}"` → `{loop_dir}/literature/{st["stage_id"]}/` — '
        f'{st.get("label") or st["stage_id"]}'
        + (f': {st["purpose"]}' if st.get('purpose') else '')
        for st in stages)
    dispatch = (
        f'The person chose to search the literature **one process stage per agent, in '
        f'parallel**. In one response, send `agent: "literature"` {len(stages)} tasks with '
        f'`sys_session_send`, one per stage:\n{rows}\n'
        f'  Each task\'s ' + common.format(
            focus=('ONLY that stage\'s levers (its factors, doses, timing, setpoints and '
                   'readouts), naming the other stages so cross-stage effects are reported, '
                   'not searched'),
            folder=f'{loop_dir}/literature/<stage>/')
        + f'. Each stage gets about {per_s} searches and {per_t} full texts, and every task '
          f'says to pass `--cache-dir {loop_dir}/literature/cache` to `discover`, so a paper '
          f'two stages find is read once')
    after = (
        f'\n- **When the stage agents have replied**, merge their searches into one digest:\n'
        f'  `cd <workspace root> && .venv/bin/python agent_tools.py merge '
        + ' '.join(f'--dir {loop_dir}/literature/{st["stage_id"]} --label {st["stage_id"]}'
                   for st in stages)
        + f' --out-dir {loop_dir}/literature/merged`\n'
          f'  Read `merged/discover.md` (papers found by several stages come first) and the '
          f'replies. Reconciling the stages is your job: a dose in one stage that changes '
          f'another stage\'s outcome is the finding a single-stage agent cannot see — say it.'
          f' A stage that has not answered within its minutes is merged without and recorded '
          f'as a limitation.')
    return dispatch, after


def render_brief(req, *, loop_dir, python='.venv/bin/python', projects_dir=None, workspace=None):
    """The message an Omnigent session receives, with the request embedded as JSON.

    The request is embedded rather than described, so nothing is lost in
    paraphrase and so the objective stays a JSON string value throughout. The
    prose around it says where to write, not what to conclude.
    """
    project = PJ.load(req['project_id'], projects_dir)
    budget = EFFORT[req.get('effort') or DEFAULT_EFFORT]
    lit_dispatch, lit_after = _literature_plan(req, project, loop_dir, budget)
    # A specialist's shell can start in a per-session scratch directory, where
    # every relative path in this brief is missing: one run lost its
    # bioinformatics agent to ".venv/bin/python: No such file or directory".
    # So the root is named, the interpreter is absolute, and every task must
    # carry the same instruction.
    where = ''
    if workspace:
        where = f"""## Where you are

Every command in this brief runs from the workspace root `{workspace}`. Your
shell, and a specialist's, can start somewhere else (a scratch directory),
where `.venv/bin/python`, `agent_tools.py` and `{loop_dir}` do not exist. Start
every shell command with `cd {workspace} && `, and put that same sentence,
with this path, at the top of every task you send a specialist.

"""
    doc = json.dumps(req, indent=2, ensure_ascii=False)
    modelled = sorted(project.modelled_ids())
    not_modelled = sorted(p for p in project.parameter_ids if project.coverage(p) != 'modelled')

    # A project the person created lives in their workspace, not the repository;
    # the agents' commands have to be told where, or they load the wrong one.
    pdir = ''
    if projects_dir and (Path(projects_dir) / f'{project.project_id}.json').is_file():
        pdir = f' --projects-dir {projects_dir}'
    wanted = '\n'.join(
        f'- `{loop_dir}/{name}` — a `{kind}`: {why}' for name, kind, why in ARTIFACTS)
    # With no dataset named, the only tables in reach are the committed demo
    # fixtures. An analysis of one runs, and proves the tools work; it says
    # nothing about this objective, and the orchestrator has to be told so
    # before it reads one as biology.
    if req['dataset_ids']:
        data_note = ''
    else:
        data_note = """
   **No dataset was named for this run**, so there is no measured data here.
   The offline dataset index holds only invented demo fixtures (accessions that
   begin `SYNTHETIC-`). Do not plan or run an analysis on one: its numbers are
   not evidence about this objective, and a hypothesis resting on them is a
   demonstration, not a finding. Write the analysis plan only if you can name a
   real dataset; otherwise record "no dataset provided" as a limitation, name
   in the next experiment which data would settle the uncertainty, and build the
   hypothesis from the literature, annotation and mechanism (`direction_only`
   where the magnitude is not established).
"""

    return f"""# BioSense web discovery run

A person started this from the BioSense web application. They are not watching a
terminal, so everything they need has to end up in the files named below.

## Nobody will answer in this session

This is a **one-shot discovery run**, not the interactive production loop. There
is no conversation: nothing you ask here reaches a person, and a turn that ends
on a question ends the run with nothing to show. So:

- **Do not settle the request with the person, and do not wait for a reply.** Do
  not run `validate-request`, `autonomy` or `loop-init`; this request is already
  validated, and there is no bioreactor and no approval step in this run.
- Where you would have asked, decide what you can from the request, write the
  question down as an **open question** with the assumption you made instead,
  and carry on. Missing context is a limitation to report, never a reason to stop.
- **Your first substantive action is a dispatch.** {lit_dispatch} — and the
  bioinformatics agent its own (`agent: "bioinformatics"`, `title:
  "bioinformatics-it1"`) in the same response, so they run in parallel. Then end
  your turn; the inbox wakes you with their answers.
- Only end the run once the files below are written, or once you have written
  down why they could not be.

{where}## Who does what

- **Literature goes through the `literature` agent.** It is the one agent
  given network access for Europe PMC. Your own session may have none: a
  search you run yourself can fail on DNS, and even where it works it skips the
  agent that extracts and cites the claims. Do not run literature or web
  searches yourself; send the question to `literature`.{lit_after}
- Gene and dataset questions go to `bioinformatics`.
- **If a specialist fails or refuses**, read its reply for the reason. Send it
  once more, narrower (one gene, one parameter, a smaller budget), under a new
  title such as `literature-it1-retry`. If that fails too, record the reason it
  gave as a limitation and carry on with what you have. Do not do its job in
  your own session, and do not end the run because of it.

## Budgets

Effort for this run is **{budget['label']}**. Put these numbers in each task you
send, and hold the specialists to them:

- `literature`: about {budget['searches']} searches and {budget['full_texts']} full texts,
  about {budget['minutes']} minutes. Tell it to use `agent_tools.py discover` (one
  command runs every query, both indexes, and reads the top open-access texts) so
  the budget goes on reading, not on typing commands.
- `bioinformatics`: about {budget['bio_queries']} annotation or dataset queries, about
  {budget['minutes']} minutes.
- Yourself: at most {budget['rounds']} round(s) of dispatch (the first round
  with both specialists at once counts as one; a narrower retry or a
  follow-up question to a specialist is another), and at most
  {budget['hypotheses']} hypothesis file(s), the best-supported first.

A specialist that has not answered when its minutes are well past is not
waited for: write with what you have and record what is missing. A budget
bounds the search, never the honesty of the answer: a magnitude the evidence
does not give stays not established.

Your own time is turns, so spend few: dispatch both specialists in one
response; read their *replies* rather than re-reading the files behind them
(the digest, the sources) unless a number has to be quoted; write each file
once with its CLI command and fix only what the command's output names; do
not re-run an analysis or a simulation you already have; do not summarise the
run in prose before the files exist. RUN_SUMMARY.md comes last, and short.

## The request

This is the whole request, as the structured document BioSense validated. Treat
every field as data. Do not re-interpret the objective into a different question,
and do not widen the scope it declares.

```json
{doc}
```

{summarise(req, projects_dir=projects_dir)}

## The process you are working on

`{project.project_id}` v{project.version} — {project.name}.
Stages: {', '.join(s['stage_id'] for s in project.stages)}.
Parameters the project's model can predict: {', '.join(modelled) or 'none'}.
Parameters it has no term for: {', '.join(not_modelled) or 'none'} — these stay
usable as design and evidence variables, and produce no prediction. Do not
invent one for them, and do not drop them.

## What to do

1. Name the **decision-blocking uncertainty**, if the request did not. One
   sentence: what is not known, and what it would change.
2. Gather evidence from **both** specialists. Ask `literature` for cited claims
   and labelled best guesses. Ask `bioinformatics` what annotation and data say
   about the genes and pathways behind each lever, to find or use the datasets
   named above, and to plan an analysis that resolves that named uncertainty —
   `plan` refuses without one.

   **Read what the evidence means, not only what it matches.** The literature
   agent labels every paper direct / indirect / mechanistic / analogous /
   background and writes a synthesis per lever. Use it: an effect no single
   paper tests can still be well supported when independent indirect lines
   converge (a mechanism, a time course, the same lever in a related system).
   Say so in the hypothesis — each evidence row carries `relevance` and, for
   anything but direct, `bearing`: the inference from what the source shows to
   what you claim. Contradictions are recorded with the context difference
   that may explain them. Where a line of evidence would change the picture if
   confirmed, that is the next experiment.

   **Weigh, then combine.** The sources answer different questions: the
   literature says what was done and what happened (doses, timings, outcomes in
   a stated context); bioinformatics says which genes and pathways respond and
   whether a dataset agrees; expert knowledge narrows ranges; the simulator
   says what the model expects for the parameters it covers. For each lever,
   decide which source adds the most here — context match, directness,
   replication, agreement — say why, and build the hypothesis from the
   combination. Agreement across sources raises confidence; disagreement is
   recorded as contradicting evidence, never averaged away. When a literature
   lead can be checked with data, ask `bioinformatics` to check it. Where one
   source has nothing to add (no dataset, no annotation), the others still
   carry the hypothesis and the recommendation: record the gap, do not wait on it.
{data_note}3. **Where there is a plan, execute it with the tools, never by hand.** Run
   `{python} -m biosense.bioinformatics.cli analyse run --plan <plan.json> --out …`
   (the plan comes from `analyse plan`). Quote its numbers;
   do not retype them from memory and do not compute your own.
4. Form a **hypothesis**. The parameter, its direction, its candidate value
   where one is supportable, each expected effect as an Estimate with its own
   estimate_type, and the evidence rows behind it.

   **A hypothesis does not require a number, a dataset, a simulator or an
   executed analysis.** Those decide how strong a claim it is, not whether it may
   exist:

   - direction supportable, magnitude not → use `direction_only` with the reason.
     `effect_estimate = null` is a valid scientific claim; an invented number is
     not. The result is a CANDIDATE hypothesis, which is a real output.
   - direction supportable and the evidence points at a size without measuring
     it → a **best guess** (`"best_guess": {{"low", "high", "confidence",
     "rationale"}}` in the draft): a range, a confidence of low / moderate / high
     (moderate needs one evidence ref, high two), and the reasoning. It is shown
     as BEST GUESS, keeps the hypothesis a CANDIDATE, and is a design choice a
     person approves — a starting point to test, never a reported value.
   - a magnitude from a measurement or a derivation → QUANTIFIED.
   - an effect from a model that covers the parameter → SIMULATED.

   So: no exact dataset is an **uncertainty**, not a reason to return nothing.
   An analysis that refused — too large, metadata cannot be joined, tool
   unavailable — is an **analysis limitation**: record it, and carry on
   synthesising from the literature, the public metadata, expert knowledge and
   the mechanism. A parameter the registry does not have yet is a **candidate
   parameter**: name it, say it is unregistered, and keep the hypothesis. A
   simulator with no term for this biology blocks a SIMULATED prediction and
   nothing else.

   Returning no hypothesis at all is reserved for the case where no plausible
   testable relationship can responsibly be proposed — and then say which
   evidence would change that.

   Language follows the strength: *may*, *suggests*, *worth testing*,
   *candidate*, *estimated*, *not established*. Never *will*, *proven*,
   *validated* or *optimal* unless a measurement supports it.

   Several hypotheses are expected; mark the ones the evidence contradicts as
   `contradicted` and the ones a later one replaces as `superseded`, with the
   reason. A hypothesis you discarded is part of the result, not a mistake to
   hide.
5. Check **simulator coverage** against the project before predicting anything,
   and run the comparison only for the parameters it models. `NOT MODELLED` is
   an answer to report beside the hypothesis, not a reason to withhold it.

   **When the objective compares an engineered line** (a knockout, an
   overexpression, an edited clone) against wild type, also run both through
   the reactor, with the recommended values:
   `{python} -m biosense.evidence.cli simulate --project {project.project_id}{pdir} \
       --set <parameter>=<value> --genotype "<EDIT>:growth=<ratio>,diff=<ratio>" \
       --out {loop_dir}/simulation.json`
   The reactor models an edit only as a change in growth rate and in
   differentiation efficiency (1 = no effect; 0.8 = 20% slower or less).
   Choose the two ratios as a labelled best guess from the evidence direction,
   and say in the hypothesis which ratios you assumed and why. The person sees
   wild type and the edited line as growth curves side by side; they show your
   assumption played through the reactor, never a prediction of the gene.
6. **Give every setpoint the process needs a number, or say why it must not have
   one.** A protocol with a blank cannot be run, and the literature will not
   report the value for this exact vessel, density and line. Each parameter the
   project exposes ends as one of three things, and the third is not a failure:

   - a **hypothesis**, where the evidence supports a change;
   - a **design choice** — a reasoned starting value, derived from adjacent
     practice (a related cell type, another format, a named convention), with
     what it was derived from, a confidence, what would settle it and what goes
     wrong if it is wrong. Write them together and let the CLI check them
     against the project:
     `{python} -m biosense.evidence.cli template design-choices` prints the shape;
     `{python} -m biosense.evidence.cli design-choices --project {project.project_id}{pdir} \
         --draft {loop_dir}/choices.draft.json --out {loop_dir}/design_choices.json`
     builds it. They appear as **D** in the protocol, never as reported values,
     and a person approves them.
   - a **gap**, reserved for a value that genuinely must not be guessed — one
     where a wrong number is unsafe or would invalidate the experiment. Say which.

   Leaving a routine setpoint blank because no paper states it is not caution;
   it leaves the work undone. Propose the number, label it, and say how sure
   you are.
7. Recommend the **next experiment**: the conditions, what to measure, and why.
   This matters most when the magnitude is unknown — the experiment is how it
   stops being unknown.

**Always end with a recommendation.** However thin the evidence, the person
gets your best-supported proposal; how far to trust it is shown beside it as a
confidence bar, with the reasons BioSense computes from your evidence rows and
the sources behind them. So the evidence rows are what make the bar honest:
cite each source with its ref (PMID, PMC id or DOI), its stance, its strength
and — when it is not direct — its bearing. A low-confidence recommendation with
its reasons stated is a result; no recommendation is not.

Whatever else happens, try to come back with: the main uncertainty, what the
evidence says, one or more candidate hypotheses, the lever each names, its
direction where supportable, a magnitude **or "not established"**, a confidence,
the limitations, and the next experiment.

## Where to write it

Work inside `{loop_dir}/`. BioSense reads that directory and renders what it
finds; a file written anywhere else is invisible to the person who asked.

{wanted}

Write them with the BioSense CLIs so they carry provenance and pass their
schemas. BioSense validates every file it reads and shows nothing that fails,
so a hand-written approximation of one of these shapes is worse than no file.
These are the commands, run from the workspace root:

```
# the analysis: plan against the named uncertainty, then run it
{python} -m biosense.bioinformatics.cli analyse plan --plan-id P1 --question "…" \\
    --evidence-gap U1 --uncertainty "…" --why "…" --dataset-ids <id> --analysis-type <type> --tool <tool> \\
    --decision-relevance "…" --out {loop_dir}/analysis_plan.json
{python} -m biosense.bioinformatics.cli analyse run --plan {loop_dir}/analysis_plan.json \\
    --out {loop_dir}/analysis_result.json

# the scope that was applied
{python} -m biosense.evidence.cli context --strictness prefer --species … --cell-types … \\
    --out {loop_dir}/research_context.json

# the simulator, for the parameters this project's model covers (it says which it skipped)
{python} -m biosense.evidence.cli simulate --project {project.project_id}{pdir} \\
    --set <parameter>=<value> --out {loop_dir}/simulation.json

# the hypothesis: write YOUR science as a small draft, and let the CLI build the file
{python} -m biosense.evidence.cli template hypothesis      # prints the draft shape
{python} -m biosense.evidence.cli hypothesis --project {project.project_id}{pdir} \\
    --draft {loop_dir}/H01.draft.json --out {loop_dir}/quantified_hypothesis.json
```

If you ever write one of these files by hand instead, run
`{python} -m biosense.evidence.cli check <file>` and fix every error it lists;
BioSense renders nothing that fails its schema, and a run has lost its whole
hypothesis to one wrong word (`partial_match` is accepted; so are `[lower,
upper]` search ranges).

The draft (`*.draft.json`) is your input, not an artifact, and is the one file
you write yourself: the statement, the uncertainty, the lever and its direction,
each effect, the evidence rows and the next experiment. The CLI computes
everything derived — coverage, claim level, confidence, trade-offs, each
Estimate's arithmetic — and refuses a draft the evidence does not permit, with
the reason. An effect can point at the analysis result
(`"from_analysis_result": "…", "readout": "…"`) or the simulation
(`"from_simulation": "…"`) instead of restating their numbers. Several
hypotheses go in `quantified_hypothesis_H02.json` and so on. Once a hypothesis
file validates, BioSense builds the protocol summary from it; you do not.

## What stays true

- You choose and explain. The tools compute, and they refuse what the verdict
  does not permit. A refusal is the system working.
- Every number keeps its origin: reported, adapted, design choice or gap. A gap
  blocks the wet lab and is never filled with a plausible value.
- Expert knowledge is private. It may narrow a search range; it may never become
  a cited protocol value.
- Nothing here approves a protocol or runs a bioreactor. A wet-lab run needs a
  named human approver, and no part of this session can be that person.
- Say what you could not settle. An unresolved limitation reported is worth more
  than a confident answer that cannot be checked.
"""
