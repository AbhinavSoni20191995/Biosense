"""Write a hypothesis or a research context the way BioSense will accept it.

The discovery brief asks the agents for `quantified_hypothesis.json` and
`research_context.json`, and tells them never to hand-write either: a file that
fails its schema is shown to nobody, and a hand-typed approximation of these
shapes is worse than no file. Until this module there was no command that wrote
them, so an agent that followed the brief exactly could never produce a
hypothesis — a real run said so in as many words.

The agent supplies the science as a small JSON draft: the statement, the
uncertainty it resolves, the lever and its direction, each expected effect, the
evidence rows and the next experiment. Everything derived is computed here, by
the same builders the deterministic path uses: simulator coverage from the
project, the claim level, the confidence and its reasons, the trade-offs, the
arithmetic of each Estimate. A draft that asks for something the evidence does
not permit — a simulated effect for a parameter the model has no term for, a
magnitude with no values behind it — is refused with the reason.

    python -m biosense.evidence.cli template hypothesis
    python -m biosense.evidence.cli hypothesis --draft draft.json --project <id> --out <file>
    python -m biosense.evidence.cli context --strictness prefer --species human \\
        --cell-types macrophage --out <file>
    python -m biosense.evidence.cli simulate --project <id> --set mcsf_ng_ml=50 --out <file>

`simulate` runs control against candidate through the project's own model, for
the parameters that model covers, and says which it skipped and why. Its effects
are labelled SIMULATED and can be passed straight into a hypothesis draft
(`"from_simulation": "<file>"`). A project with no model returns no prediction
and the reason, never a number.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .. import contracts as K
from .. import parameters as PR
from .. import projects as PJ
from . import context as CX
from . import estimates as E
from . import design as DC
from . import hypothesis as HY

TEMPLATE = {
    'hypothesis_id': 'H01',
    'statement': 'Raising M-CSF during myeloid differentiation may increase macrophage yield.',
    'uncertainty': {'kind': 'evidence_gap', 'ref': 'U1',
                    'statement': 'Whether M-CSF dose limits macrophage yield in this process.'},
    'parameter_id': 'mcsf_ng_ml',
    'parameter_label': None,
    'direction': 'increase',
    'current_value': None,
    'candidate_value': None,
    'search_range': None,
    'effects': [
        {'metric': 'macrophage_yield', 'unit': 'cells', 'direction': 'increase',
         'higher_is_better': True, 'estimate_type': 'derived',
         'reason': 'the literature supports the direction; no source here gives a magnitude for '
                   'this process'},
        {'_comment': 'a best guess: when the evidence points somewhere but measures no size. '
                     'A range and a confidence (low / moderate / high; moderate needs 1 evidence '
                     'ref, high 2), labelled BEST GUESS everywhere and never a reported value',
         'metric': 'cd14_fraction', 'unit': '%', 'higher_is_better': True,
         'best_guess': {'low': 2, 'high': 10, 'central': None, 'confidence': 'low',
                        'rationale': 'Which sources, in which context, and what is missing.',
                        'would_change_it': 'A dose series measuring CD14+ fraction.'},
         'evidence_refs': ['PMID:<id>']},
        {'_comment': 'a measured effect: point at the analysis result and the readout it tested',
         'from_analysis_result': 'data/runs/<run>/analysis_result.json', 'readout': '<readout>',
         'unit': '%', 'higher_is_better': True},
    ],
    'evidence': [
        {'evidence_class': 'published_literature', 'stance': 'supportive',
         'summary': 'What the source reports, in one sentence.', 'ref': 'PMID:<id>',
         'strength': 'moderate', 'visibility': 'public', 'context_match': 'not_assessed',
         'relevance': 'direct', 'bearing': None},
        {'_comment': 'evidence that bears on the claim without testing it: say how. relevance is '
                     'direct / indirect / mechanistic / analogous / background; context_match is '
                     'in_context / partial_match / context_mismatch / not_assessed',
         'evidence_class': 'published_literature', 'stance': 'supportive',
         'summary': 'CSF1R signalling drives terminal macrophage differentiation in a related system.',
         'ref': 'PMID:<id>', 'strength': 'weak', 'visibility': 'public',
         'context_match': 'partial_match', 'relevance': 'mechanistic',
         'bearing': 'M-CSF is the CSF1R ligand; more receptor engagement would be expected to '
                    'raise the fraction completing differentiation, which is the readout here.'},
    ],
    'next_experiment': {'summary': 'A dose series against the current setting.',
                        'conditions': ['current dose', 'candidate dose'],
                        'measure': ['macrophage yield', 'CD14+ / CD163+ fraction', 'viability'],
                        'replicates': 3, 'rationale': 'It turns the direction into a magnitude.'},
    'limitations': ['Direction only: no magnitude is established for this process.'],
    'status': 'proposed',
    'research_context': None,
}
EFFECT_KEYS = ('metric', 'unit')


# What an agent writes for a direction-only effect's type. "direction_only" is
# the SHAPE of such an effect, not a type, and a run lost its hypothesis to the
# schema refusing it; the shape is already what this path builds, so the word
# means "derived" (computed from the evidence), and a guess means judgement.
_DIRECTION_TYPES = {None: 'derived', '': 'derived', 'direction_only': 'derived',
                    'direction': 'derived', 'directional': 'derived', 'qualitative': 'derived',
                    'literature': 'derived', 'reported': 'derived',
                    'best_guess': 'judgement', 'guess': 'judgement', 'judgment': 'judgement',
                    'expert_judgement': 'judgement'}


def _direction_type(word):
    w = (word or '').strip().lower().replace(' ', '_').replace('-', '_')
    if w in E.TYPES:
        return w
    if w in _DIRECTION_TYPES:
        return _DIRECTION_TYPES[w]
    raise K.ContractError(f'estimate_type {word!r} is not one of {E.TYPES}; for an effect with a '
                          f'direction and no size, leave it out (it is then "derived")')


def _effect(spec, base_dir):
    """One effect from its draft form. Three shapes, each refused if incomplete."""
    if not isinstance(spec, dict):
        raise K.ContractError(f'an effect must be an object, got {spec!r}')
    spec = {k: v for k, v in spec.items() if not k.startswith('_')}
    if spec.get('from_simulation'):
        path = Path(spec['from_simulation'])
        if not path.is_absolute():
            path = (base_dir / path) if (base_dir / path).exists() else path
        sim = K.read_json(path)
        want = spec.get('metric')
        rows = [e for e in sim.get('effects') or [] if not want or e.get('metric') == want]
        if not rows:
            raise K.ContractError(
                f'{path} has no simulated effect{" for " + repr(want) if want else ""}: '
                f'{sim.get("prediction_note") or "the simulator produced no prediction"}')
        K.require_valid('estimate', rows[0])
        return rows[0]
    if spec.get('from_analysis_result'):
        path = Path(spec['from_analysis_result'])
        if not path.is_absolute():
            path = (base_dir / path) if (base_dir / path).exists() else path
        result = K.read_json(path)
        rows = [r for r in result.get('statistics') or []
                if r.get('readout') == spec.get('readout')]
        if not rows:
            raise K.ContractError(
                f'{path} has no statistics row for readout {spec.get("readout")!r}; it has: '
                f'{", ".join(sorted({r.get("readout", "?") for r in result.get("statistics") or []})) or "none"}')
        ref = (result.get('analysis_id') or result.get('plan_id') or path.name)
        return E.from_statistics_row(rows[0], spec.get('unit') or '%', source_ref=ref,
                                     higher_is_better=spec.get('higher_is_better'),
                                     label=spec.get('label'))
    missing = [k for k in EFFECT_KEYS if not spec.get(k)]
    if missing:
        raise K.ContractError(f'an effect needs {", ".join(missing)}: {spec!r}')
    if spec.get('best_guess') is not None:
        g = spec['best_guess']
        if not isinstance(g, dict):
            raise K.ContractError('best_guess is {"low", "high", "confidence", "rationale", '
                                  '"central"?, "would_change_it"?}')
        return E.best_guess(spec['metric'], spec['unit'], low=g.get('low'), high=g.get('high'),
                            central=g.get('central'), confidence=g.get('confidence'),
                            rationale=g.get('rationale'), direction=spec.get('direction'),
                            would_change_it=g.get('would_change_it'), label=spec.get('label'),
                            higher_is_better=spec.get('higher_is_better'),
                            evidence_refs=spec.get('evidence_refs') or (),
                            limitations=spec.get('limitations') or ())
    if spec.get('baseline') is not None or spec.get('candidate') is not None:
        def val(v):
            if not isinstance(v, dict) or 'value' not in v or 'estimate_type' not in v:
                raise K.ContractError('baseline and candidate are {"value", "estimate_type", '
                                      '"source_ref"}; a number with no type and no source is '
                                      'not an Estimate')
            return E.value(v['value'], v['estimate_type'], source_ref=v.get('source_ref'),
                           n=v.get('n'), sd=v.get('sd'), note=v.get('note'))
        return E.estimate(spec['metric'], spec['unit'], val(spec.get('baseline')),
                          val(spec.get('candidate')), label=spec.get('label'),
                          higher_is_better=spec.get('higher_is_better'),
                          evidence_refs=spec.get('evidence_refs') or (),
                          limitations=spec.get('limitations') or ())
    return E.direction_only(spec['metric'], spec['unit'], spec.get('direction', 'unknown'),
                            _direction_type(spec.get('estimate_type')),
                            reason=spec.get('reason') or '',
                            label=spec.get('label'),
                            higher_is_better=spec.get('higher_is_better'),
                            evidence_refs=spec.get('evidence_refs') or (),
                            limitations=spec.get('limitations') or ())


def _search_range(raw):
    """A search range in the shape the contract wants, from the shapes agents write.

    `[lo, hi]`, `{"min", "max"}`, `{"low", "high"}` and `{"lower", "upper"}` all
    mean the same thing. The basis is the one field that cannot be guessed: it
    says where the range came from, and when the draft leaves it out it is
    recorded as not stated rather than invented.
    """
    if raw is None:
        return None
    if isinstance(raw, (list, tuple)):
        if len(raw) != 2:
            raise K.ContractError('search_range as a list is [lower, upper]')
        raw = {'lower': raw[0], 'upper': raw[1]}
    if not isinstance(raw, dict):
        raise K.ContractError('search_range is {"lower", "upper", "basis"} or [lower, upper]')
    lo = next((raw[k] for k in ('lower', 'low', 'min', 'from') if raw.get(k) is not None), None)
    hi = next((raw[k] for k in ('upper', 'high', 'max', 'to') if raw.get(k) is not None), None)
    if lo is None or hi is None:
        raise K.ContractError('search_range needs both ends: lower and upper')
    try:
        lo, hi = float(lo), float(hi)
    except (TypeError, ValueError):
        raise K.ContractError('search_range ends must be numbers') from None
    out = {'lower': min(lo, hi), 'upper': max(lo, hi),
           'basis': str(raw.get('basis') or raw.get('source') or raw.get('why')
                        or 'not stated in the draft').strip()}
    if raw.get('narrowed_by'):
        out['narrowed_by'] = [str(x) for x in raw['narrowed_by']]
    return out


def build_hypothesis(draft, *, project_id, projects_dir=None, base_dir=None, created_by=None):
    """A validated QuantifiedHypothesis from an agent's draft. Raises ContractError."""
    if not isinstance(draft, dict):
        raise K.ContractError('the draft must be a JSON object')
    base_dir = Path(base_dir or '.')
    project = PJ.load(project_id, projects_dir)
    unc = draft.get('uncertainty') or draft.get('uncertainty_ref')
    if not isinstance(unc, dict) or not unc.get('statement'):
        raise K.ContractError('a hypothesis exists to resolve something: give "uncertainty" '
                              'as {"kind", "ref", "statement"}')
    unc = {'kind': unc.get('kind') or 'evidence_gap', 'ref': unc.get('ref') or 'U1',
           'statement': unc['statement']}
    effects = [_effect(e, base_dir) for e in draft.get('effects') or []]
    evidence = [HY.evidence_row(
        r.get('evidence_class'), r.get('stance') or 'supportive', r.get('summary') or '',
        ref=r.get('ref'), strength=r.get('strength') or 'not_assessed',
        visibility=r.get('visibility') or 'public',
        context_match=r.get('context_match') or 'not_assessed',
        relevance=r.get('relevance'), bearing=r.get('bearing'),
        context_note=r.get('context_mismatch_note'))
        for r in draft.get('evidence') or [] if isinstance(r, dict)]
    nxt = draft.get('next_experiment')
    if isinstance(nxt, str):
        nxt = {'summary': nxt}
    if not isinstance(nxt, dict) or not nxt.get('summary'):
        raise K.ContractError('give "next_experiment" with at least a "summary": the '
                              'experiment is how an unknown magnitude stops being unknown')
    ctx = draft.get('research_context')
    if isinstance(ctx, str):
        ctx = K.read_json(Path(ctx) if Path(ctx).is_absolute() else base_dir / ctx)
    return HY.hypothesis(
        draft.get('hypothesis_id') or 'H01', draft.get('statement') or '',
        project=project, uncertainty_ref=unc, parameter_id=draft.get('parameter_id'),
        direction=draft.get('direction') or 'unknown', effects=effects, evidence=evidence,
        next_experiment={k: v for k, v in nxt.items() if not k.startswith('_')},
        current_value=draft.get('current_value'), candidate_value=draft.get('candidate_value'),
        search_range=_search_range(draft.get('search_range')), research_context=ctx,
        limitations=draft.get('limitations') or (),
        created_by=created_by or draft.get('created_by') or 'orchestrator',
        status=draft.get('status') or 'proposed', supersedes=draft.get('supersedes'),
        superseded_reason=draft.get('superseded_reason'), stage=draft.get('stage'),
        parameter_label=draft.get('parameter_label'))


def _write(doc, out):
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    K.write_json_atomic(out, doc)
    return out


def cmd_hypothesis(a):
    draft = K.read_json(Path(a.draft))
    h = build_hypothesis(draft, project_id=a.project, projects_dir=a.projects_dir,
                         base_dir=Path(a.draft).parent, created_by=a.created_by)
    out = _write(h, a.out)
    print(json.dumps({'written': str(out), 'hypothesis_id': h['hypothesis_id'],
                      'claim_level': h['claim_level'],
                      'claim_level_reason': h['claim_level_reason'],
                      'parameter': h['parameter']['parameter_id'],
                      'registered': h['parameter']['registered'],
                      'simulator_coverage': h['parameter']['simulator_coverage'],
                      'confidence': h['confidence']}, indent=2))
    return 0


def cmd_context(a):
    c = CX.context(strictness=a.strictness, species=a.species, cell_types=a.cell_types,
                   tissues=a.tissues, states=a.states, disease_context=a.disease_context,
                   modalities=a.modalities, assays=a.assays, exclude=a.exclude,
                   notes=a.notes, context_id=a.context_id)
    out = _write(c, a.out)
    print(json.dumps({'written': str(out), 'strictness': c['strictness']}, indent=2))
    return 0


def simulate(project_id, settings, *, projects_dir=None, seed=7):
    """Control versus candidate through the project's own model. Never invents."""
    from ..production import sim_candidate as SC
    project = PJ.load(project_id, projects_dir)
    values = {PR.resolve(k): v for k, v in settings.items()}
    control = {}
    for pid, v in project.defaults().items():
        pp = project.parameter(pid)
        if pp.modelled and pp.simulator_mapping and isinstance(v, (int, float)):
            control[pp.simulator_mapping] = v
    candidates = [{'parameter': pid, 'direction': 'revisit', 'basis': 'requested by the agent',
                   'confidence': 'low', 'arm_scope': None, 'suggested_range': None}
                  for pid in values]
    out = SC.compare_conditions(project, control=control, candidates=candidates,
                                candidate_values=values, seed=seed)
    return {'project_id': project.project_id, 'requested': values,
            'prediction': out['prediction'], 'prediction_note': out['prediction_note'],
            'applied': out['handoff']['applied'], 'skipped': out['handoff']['skipped'],
            'clamped': out.get('clamped') or [], 'effects': out['effects'],
            'relative_effects': out.get('relative_effects') or [],
            'note': 'SIMULATED by an uncalibrated model unless the project says otherwise. A '
                    'simulation is evidence about the model, not about the cells.'}


def _factor(text):
    """'IL-34:stage=myeloid,growth=1.1,diff=1.2' -> an added factor's assumed effect."""
    return dict(_genotype(text), kind='factor')


def _genotype(text):
    """'BACH2_KO:growth=0.8,diff=1.2' -> {label, growth_ratio, diff_ratio}."""
    if ':' not in text:
        raise argparse.ArgumentTypeError('expected LABEL:growth=<ratio>,diff=<ratio>')
    label, rest = text.split(':', 1)
    out = {'label': label.strip(), 'growth_ratio': 1.0, 'diff_ratio': 1.0}
    for part in rest.split(','):
        if not part.strip():
            continue
        if '=' not in part:
            raise argparse.ArgumentTypeError(f'expected key=value in {text!r}')
        k, v = (x.strip() for x in part.split('=', 1))
        if k == 'stage':
            out['stage'] = v
            continue
        key = {'growth': 'growth_ratio', 'diff': 'diff_ratio',
               'differentiation': 'diff_ratio'}.get(k)
        if not key:
            raise argparse.ArgumentTypeError(f'unknown effect {k!r}: growth, diff or stage')
        try:
            out[key] = float(v)
        except ValueError:
            raise argparse.ArgumentTypeError(f'{v!r} is not a number') from None
    return out


TERM_TEMPLATE = {
    'terms': [{
        'parameter_id': 'gmcsf_maturation_ng_ml',
        'label': 'GM-CSF (maturation)', 'unit': 'ng/mL', 'stage': 'maturation',
        'minimum': 0, 'maximum': 100,
        'meaning': 'GM-CSF given while monocytes mature into macrophages',
        'response_model': {
            'shape': 'saturating', 'target': 'transition_efficiency', 'stage': 'maturation',
            'half_max': 10, 'max_effect': 0.4,
            'basis': 'Two cited protocols report maturation efficiency rising with GM-CSF up '
                     'to about 50 ng/mL with little gain beyond; half-max read as ~10 ng/mL.',
            'references': ['PMID:00000000', 'PMC0000000']}}],
    'note': ('One entry per lever the base reactor has no term for. shape: bell (optimum, '
             'tolerance) | saturating (half_max) | threshold (threshold) | linear (slope). '
             'target: growth | death | viability | transition_efficiency | harvest. '
             'max_effect is the largest relative change the term may make (bounded). Every term '
             'cites the claims its constants come from; a lever with none is a gap, not a term. '
             'A lever the project lacks also needs label, unit, stage, minimum and maximum.'),
}


def proposed_terms(project_id, draft, *, projects_dir=None, run_id=None):
    """Validate the terms a run proposes and describe each, without touching the project."""
    from ..production import project_builder as PB
    from ..production import response_model as RM
    project = PJ.load(project_id, projects_dir)
    terms = draft.get('terms') if isinstance(draft, dict) else draft
    if not terms:
        raise K.ContractError('the draft proposes no terms; see `template term`')
    with_terms, pids = PB.with_proposed_terms(project, terms, run_id=run_id)
    out = []
    for pid in pids:
        q = with_terms.parameter(pid)
        out.append({'parameter_id': pid, 'label': q.label, 'unit': q.unit,
                    'stage': q.stage, 'minimum': q.minimum, 'maximum': q.maximum,
                    'response_model': q.response_model,
                    'description': RM.describe(q.response_model)})
    return {'kind': 'proposed_terms', 'project_id': project.project_id, 'run_id': run_id,
            'created_at': K.now_iso(), 'terms': out,
            # kept so a person can add a term to the project exactly as proposed
            'drafts': list(terms),
            'status': ('PROPOSED on this run only. A person adds a term to the project; until '
                       'then nothing outside this run uses it.'),
            'note': ('Each term is a shape and constants an agent read from cited claims, '
                     'evaluated by deterministic code and bounded. DE NOVO · UNCALIBRATED: '
                     'nothing was fitted to data.')}


def cmd_term(a):
    doc = proposed_terms(a.project, K.read_json(Path(a.draft)), projects_dir=a.projects_dir,
                         run_id=a.run_id)
    out = _write(doc, a.out)
    print(json.dumps({'written': str(out),
                      'terms': [f'{t["label"]}: {t["description"]["summary"]} '
                                f'[{t["description"]["badge"]}]' for t in doc['terms']],
                      'next': f'simulate --project {a.project} --terms {out} --set <lever>=<value> '
                              f'...'}, indent=2))
    return 0


def cmd_round_plan(a):
    from . import round_plan as RP
    project = PJ.load(a.project, a.projects_dir)
    extra = ()
    if a.terms:
        # Levers this run proposed terms for may be tested before the project has them.
        from ..production import project_builder as PB
        tdoc = K.read_json(Path(a.terms))
        project, extra = PB.with_proposed_terms(project, tdoc.get('drafts') or [],
                                                run_id=tdoc.get('run_id'))
    doc = RP.build(K.read_json(Path(a.draft)), project, run_id=a.run_id,
                   created_by=a.created_by, extra_levers=extra)
    out = _write(doc, a.out)
    print(json.dumps({'written': str(out), 'round': doc['round'],
                      'unknowns': [u['id'] for u in doc['unknowns']],
                      'arms': [a['arm_id'] for a in doc['arms']],
                      'commitment_sha256': doc['commitment_sha256'],
                      'status': doc['status']}, indent=2))
    return 0


def genotype_simulation(project_id, settings, genotype, *, projects_dir=None, seed=7,
                        effects=None, project=None):
    """Wild type against an engineered line on the reactor, with this project's values.

    On the project's own reactor model its mapped setpoints are used; a project
    with no model runs its physical setpoints on the reactor as a stand-in and
    says so. The edit is an assumed growth and differentiation ratio, and the
    result is that assumption played through the reactor — never a claim about
    the gene.
    """
    from ..production import sim_mode as SM
    project = project or PJ.load(project_id, projects_dir)
    values = {PR.resolve(k, required=False) or k: v for k, v in settings.items()}
    knobs, used, unused = {}, [], []
    for pid in sorted(project.parameter_ids):
        pp = project.parameter(pid)
        v = values.get(pid, project.defaults().get(pid))
        knob = pp.simulator_mapping or (pid if pid in SM.KNOB_BY_ID else None)
        if knob in SM.KNOB_BY_ID and isinstance(v, (int, float)):
            knobs[knob] = v
            used.append(pid)
        elif pid in values:
            unused.append(pid)
    stand_in = (project.simulator or {}).get('model_id') != SM.config()['model_id']
    doc = SM.genotype_compare({'setpoints': knobs, 'seed': seed, 'genotype': genotype,
                               'assumed_effects': effects or []})
    g = doc['genotype']
    what = 'the factor' if g.get('kind') == 'factor' else 'the edit'
    if g.get('effects'):
        from ..production import response_model as RM
        # Described from the effects as given, which still carry where each
        # term came from; the reactor's parsed copy keeps only the ratios.
        rows = effects or g['effects']
        doc.update(project_id=project.project_id, stand_in=stand_in, used=used,
                   not_represented=unused,
                   assumption=('The new parameters act through the responses stated for them '
                               'on the project' + (' or proposed by this run' if any(
                                   e.get('origin') == 'ai_proposed' for e in rows) else '')
                               + ' (' + '; '.join(
                                   f'{e["label"]}: growth ×{e["growth_ratio"]:.3g}, '
                                   f'differentiation ×{e["diff_ratio"]:.3g}'
                                   + (f' in {e["stage_mapped_from"]} (played in the base '
                                      f'reactor\'s {e["stage"]} stage)'
                                      if e.get('stage_mapped_from')
                                      else f' in {e["stage"]}' if e.get('stage') else '')
                                   + (f' [{RM.BADGE[e["origin"]]}]'
                                      if e.get('origin') in RM.BADGE else '')
                                   for e in rows) + '); uncalibrated.'))
        if stand_in:
            doc['stand_in_note'] = _base_reactor_note(project)
        return doc
    doc.update(project_id=project.project_id, stand_in=stand_in, used=used,
               not_represented=unused,
               assumption=(f'{g["label"]} assumed to change growth ×{g["growth_ratio"]:g} and '
                           f'differentiation ×{g["diff_ratio"]:g}'
                           + (f' during {g["stage_mapped_from"]} (played in the base '
                              f'reactor\'s {g["stage"]} stage)' if g.get('stage_mapped_from')
                              else f' during {g["stage"]}' if g.get('stage') else '')
                           + f'; the ratios are a labelled guess, not a measured effect of '
                             f'{what}.'))
    if stand_in:
        doc['stand_in_note'] = _base_reactor_note(project)
    return doc


def _base_reactor_note(project):
    """How a project without a calibrated model of its own is simulated.

    Not a failure to apologise for: it is how a new project gets a simulator.
    The base reactor supplies the calibrated physics and stages; the project
    adds its own terms on top — the effects stated for its new factors and
    parameters — each uncalibrated and labelled. What a reader must keep in
    mind is which part is which.
    """
    return (f'BUILT ON THE BASE REACTOR. {project.name} is simulated as the calibrated iPSC → '
            f'monocyte reactor plus this project\'s own terms. Inherited and calibrated: the '
            f'physics (agitation, oxygen, feeding, waste, aggregates) and the stages. Added by '
            f'this project and uncalibrated: the effects stated for its new factors and '
            f'parameters, played in the stage they act in. Read the comparison as the direction '
            f'those terms imply on top of the base reactor, not as a measurement of '
            f'{project.name}.')


def _setting(text):
    if '=' not in text:
        raise argparse.ArgumentTypeError(f'expected parameter=value, got {text!r}')
    k, v = text.split('=', 1)
    try:
        return k.strip(), float(v)
    except ValueError:
        raise argparse.ArgumentTypeError(f'{v!r} is not a number') from None


def cmd_simulate(a):
    if a.terms:
        # Built first: loading the project with the run's terms defines any lever
        # it introduces, so `--set <new lever>=…` resolves below.
        from ..production import project_builder as PB
        tdoc = K.read_json(Path(a.terms))
        PB.with_proposed_terms(PJ.load(a.project, a.projects_dir), tdoc.get('drafts') or [],
                               run_id=tdoc.get('run_id'))
    doc = simulate(a.project, dict(a.set or []), projects_dir=a.projects_dir, seed=a.seed)
    if a.genotype and a.factor:
        raise K.ContractError('give --genotype or --factor, not both: each is one assumed effect '
                              'against its own control')
    if a.genotype or a.factor:
        doc['genotype_simulation'] = genotype_simulation(
            a.project, dict(a.set or []), a.genotype or a.factor, projects_dir=a.projects_dir,
            seed=a.seed)
    elif a.modelled_parameters or a.terms:
        from ..production import sim_mode as SM
        project = PJ.load(a.project, a.projects_dir)
        if a.terms:
            # The run's proposed terms, on a copy of the project: the base
            # reactor plus this project's terms, played without saving anything.
            from ..production import project_builder as PB
            tdoc = K.read_json(Path(a.terms))
            project, _ = PB.with_proposed_terms(project, tdoc.get('drafts') or [],
                                                run_id=tdoc.get('run_id'))
        values = {PR.resolve(k, required=False) or k: v for k, v in (a.set or [])}
        eff = SM.effects_from_project(project, values)
        if not eff['effects']:
            raise K.ContractError('no parameter set here has a modelled effect on the project: '
                                  + '; '.join(f'{n["label"]}: {n["why"]}'
                                              for n in eff['not_applied']) or 'none set')
        doc['genotype_simulation'] = genotype_simulation(
            a.project, dict(a.set or []), None, projects_dir=a.projects_dir, seed=a.seed,
            effects=eff['effects'], project=project)
        doc['genotype_simulation']['not_applied'] = eff['not_applied']
        if a.terms:
            doc['genotype_simulation']['proposed_terms'] = str(a.terms)
    out = _write(doc, a.out)
    print(json.dumps({'written': str(out), 'prediction': doc['prediction'],
                      'applied': [r.get('parameter_id') for r in doc['applied']],
                      'skipped': [r.get('parameter_id') for r in doc['skipped']],
                      'effects': [E.render(e) for e in doc['effects']],
                      'genotype': (doc['genotype_simulation']['verdict']
                                   if doc.get('genotype_simulation') else None),
                      'note': doc['prediction_note']}, indent=2))
    return 0


KINDS_BY_NAME = {'analysis_plan': 'analysis_plan', 'analysis_result': 'analysis_result',
                 'quantified_hypothesis': 'quantified_hypothesis',
                 'research_context': 'research_context', 'discovery_request': 'discovery_request'}


def kind_of(path):
    stem = Path(path).stem
    for name, kind in KINDS_BY_NAME.items():
        if stem == name or stem.startswith(name + '_'):
            return kind
    return None


DESIGN_TEMPLATE = {
    'choices': [
        {'parameter_id': 'seed_density', 'value': 0.3, 'unit': '1e6 cells/mL',
         'rationale': 'Why this number: the reasoning from what is known to what is proposed.',
         'derived_from': ['PMID:<id> (related cell type, same vessel class)',
                          'common practice: iPSC suspension aggregates'],
         'confidence': 'low',
         'context_note': 'How the source context differs from this process.',
         'would_settle_it': 'A seeding series in this vessel, measuring viability at 24 h.',
         'risk_if_wrong': 'Too low wastes a run; too high causes aggregate necrosis.'},
    ],
}


def cmd_design(a):
    """Reasoned starting values for setpoints nothing in the run sets directly."""
    project = PJ.load(a.project, a.projects_dir)
    draft = K.read_json(a.draft)
    doc = DC.build(draft, project=project, run_id=a.run_id,
                   created_by=a.created_by or 'orchestrator')
    if a.out:
        K.write_json_atomic(a.out, doc)
    print(json.dumps({'choices': [{'parameter_id': c['parameter_id'], 'value': c['value'],
                                   'confidence': c['confidence']} for c in doc['choices']],
                      'out': a.out,
                      'note': 'Not evidence and not a measurement. Each is a person\'s to '
                              'approve, and the protocol shows it as a design choice with its '
                              'basis.'}, indent=1))
    return 0


def cmd_check(a):
    """Validate a file an agent wrote itself. The errors are the answer, in full.

    BioSense shows nothing that fails its schema, so a hand-written file is
    invisible until it is right; this is how to find out before the run ends.
    """
    kind = a.kind or kind_of(a.file)
    if not kind:
        print(json.dumps({'ok': False, 'file': a.file,
                          'error': f'cannot tell the kind from the name; pass --kind one of '
                                   f'{sorted(KINDS_BY_NAME)}'}))
        return 1
    try:
        doc = K.read_json(a.file)
    except Exception as e:  # noqa: BLE001 - the reason is the output
        print(json.dumps({'ok': False, 'file': a.file, 'kind': kind, 'error': str(e)[:300]}))
        return 1
    errors = K.schema_errors(kind, doc)
    print(json.dumps({'ok': not errors, 'file': a.file, 'kind': kind, 'errors': errors[:25],
                      'note': None if not errors else
                      'Fix every error and run check again; BioSense renders nothing that fails. '
                      'The evidence.cli hypothesis command builds this file from a draft and '
                      'cannot write an invalid one.'}, indent=1))
    return 0 if not errors else 1


def cmd_template(a):
    from . import round_plan as RP
    shape = {'design-choices': DESIGN_TEMPLATE, 'term': TERM_TEMPLATE,
             'round-plan': RP.TEMPLATE}.get(a.what, TEMPLATE)
    print(json.dumps(shape, indent=2))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog='python -m biosense.evidence.cli',
                                 description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('hypothesis', help='build and validate a quantified_hypothesis from a draft')
    p.add_argument('--draft', required=True)
    p.add_argument('--project', required=True)
    p.add_argument('--projects-dir', default=None)
    p.add_argument('--created-by', default=None)
    p.add_argument('--out', required=True)
    p.set_defaults(fn=cmd_hypothesis)
    p = sub.add_parser('context', help='write a research_context')
    p.add_argument('--strictness', default='prefer', choices=CX.STRICTNESS)
    for flag in ('species', 'cell-types', 'tissues', 'states', 'disease-context',
                 'modalities', 'assays', 'exclude'):
        p.add_argument(f'--{flag}', nargs='*', default=[])
    p.add_argument('--notes', default=None)
    p.add_argument('--context-id', default=None)
    p.add_argument('--out', required=True)
    p.set_defaults(fn=cmd_context)
    p = sub.add_parser('simulate', help='control vs candidate through the project model')
    p.add_argument('--project', required=True)
    p.add_argument('--projects-dir', default=None)
    p.add_argument('--set', action='append', type=_setting, metavar='PARAMETER=VALUE')
    p.add_argument('--genotype', type=_genotype, metavar='LABEL:growth=R,diff=R',
                   help='also run wild type against an edited line with this assumed effect')
    p.add_argument('--modelled-parameters', action='store_true',
                   help='also run without and with the project\'s new parameters, each acting '
                        'through the response stated for it on the project')
    p.add_argument('--factor', type=_factor, metavar='LABEL:stage=S,growth=R,diff=R',
                   help='also run without and with an added factor (one the project has no '
                        'term for) whose assumed effect acts only in stage S')
    p.add_argument('--terms', metavar='PROPOSED_TERMS_JSON',
                   help='also run without and with the levers this run proposed terms for '
                        '(the file `term` wrote), each labelled DE NOVO')
    p.add_argument('--seed', type=int, default=7)
    p.add_argument('--out', required=True)
    p.set_defaults(fn=cmd_simulate)
    p = sub.add_parser('term', help='propose a response term for a lever the base reactor has '
                                    'no equation for (validated, never saved to the project)')
    p.add_argument('--project', required=True); p.add_argument('--projects-dir')
    p.add_argument('--draft', required=True); p.add_argument('--run-id')
    p.add_argument('--out', required=True)
    p.set_defaults(fn=cmd_term)
    p = sub.add_parser('round-plan', help='the next round at the bench: what this run could not '
                                          'settle, as arms, readouts and decision rules')
    p.add_argument('--project', required=True); p.add_argument('--projects-dir')
    p.add_argument('--draft', required=True); p.add_argument('--terms')
    p.add_argument('--run-id'); p.add_argument('--created-by')
    p.add_argument('--out', required=True)
    p.set_defaults(fn=cmd_round_plan)
    p = sub.add_parser('design-choices',
                       help='reasoned starting values for setpoints no source sets here')
    p.add_argument('--project', required=True); p.add_argument('--projects-dir')
    p.add_argument('--draft', required=True); p.add_argument('--out')
    p.add_argument('--run-id'); p.add_argument('--created-by')
    p.set_defaults(fn=cmd_design)
    p = sub.add_parser('check', help='validate a BioSense artifact file and list every error')
    p.add_argument('file'); p.add_argument('--kind', choices=sorted(KINDS_BY_NAME))
    p.set_defaults(fn=cmd_check)
    p = sub.add_parser('template', help='print an example draft')
    p.add_argument('what', choices=['hypothesis', 'design-choices', 'term', 'round-plan'])
    p.set_defaults(fn=cmd_template)
    a = ap.parse_args(argv)
    try:
        return a.fn(a)
    except K.ContractError as e:
        print(json.dumps({'refused': True, 'reason': str(e)}, indent=2), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
