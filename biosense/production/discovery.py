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
     'what the tool computed, written by `bioinformatics.cli execute` — never by hand'),
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


def build(*, project_id, objective, runtime_mode, research_context=None, dataset_ids=(),
          expert_knowledge_ids=(), process_constraints=None, uncertainty=None, control=None,
          candidate_values=None, title=None, notes=None, requested_by=None, request_id=None,
          projects_dir=None):
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
def render_brief(req, *, loop_dir, python='.venv/bin/python', projects_dir=None):
    """The message an Omnigent session receives, with the request embedded as JSON.

    The request is embedded rather than described, so nothing is lost in
    paraphrase and so the objective stays a JSON string value throughout. The
    prose around it says where to write, not what to conclude.
    """
    project = PJ.load(req['project_id'], projects_dir)
    doc = json.dumps(req, indent=2, ensure_ascii=False)
    modelled = sorted(project.modelled_ids())
    not_modelled = sorted(p for p in project.parameter_ids if project.coverage(p) != 'modelled')

    wanted = '\n'.join(
        f'- `{loop_dir}/{name}` — a `{kind}`: {why}' for name, kind, why in ARTIFACTS)

    return f"""# BioSense web discovery run

A person started this from the BioSense web application. They are not watching a
terminal, so everything they need has to end up in the files named below.

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
2. Gather evidence. Ask `literature` for cited claims. Ask `bioinformatics` to
   find or use the datasets named above and to plan an analysis that resolves
   that named uncertainty — `plan` refuses without one.
3. **Execute the analysis with the tools, never by hand.** Run
   `{python} -m biosense.bioinformatics.cli execute --plan …`. Quote its numbers;
   do not retype them from memory and do not compute your own.
4. Form a **quantified hypothesis**: the parameter, its direction, its candidate
   value, each expected effect as an Estimate with its own estimate_type, and
   the evidence rows behind it. Several hypotheses are expected; mark the ones
   the evidence contradicts as `contradicted` and the ones a later one replaces
   as `superseded`, with the reason. A hypothesis you discarded is part of the
   result, not a mistake to hide.
5. Check **simulator coverage** against the project before predicting anything,
   and run the comparison only for the parameters it models.
6. Recommend the **next experiment**: the conditions, what to measure, and why.

## Where to write it

Work inside `{loop_dir}/`. BioSense reads that directory and renders what it
finds; a file written anywhere else is invisible to the person who asked.

{wanted}

Write them with the BioSense CLIs so they carry provenance and pass their
schemas. BioSense validates every file it reads and shows nothing that fails,
so a hand-written approximation of one of these shapes is worse than no file.

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
