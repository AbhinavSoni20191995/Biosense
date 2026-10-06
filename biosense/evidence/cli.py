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
                            spec.get('estimate_type') or 'derived',
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


def _setting(text):
    if '=' not in text:
        raise argparse.ArgumentTypeError(f'expected parameter=value, got {text!r}')
    k, v = text.split('=', 1)
    try:
        return k.strip(), float(v)
    except ValueError:
        raise argparse.ArgumentTypeError(f'{v!r} is not a number') from None


def cmd_simulate(a):
    doc = simulate(a.project, dict(a.set or []), projects_dir=a.projects_dir, seed=a.seed)
    out = _write(doc, a.out)
    print(json.dumps({'written': str(out), 'prediction': doc['prediction'],
                      'applied': [r.get('parameter_id') for r in doc['applied']],
                      'skipped': [r.get('parameter_id') for r in doc['skipped']],
                      'effects': [E.render(e) for e in doc['effects']],
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
    print(json.dumps(TEMPLATE, indent=2))
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
    p.add_argument('--seed', type=int, default=7)
    p.add_argument('--out', required=True)
    p.set_defaults(fn=cmd_simulate)
    p = sub.add_parser('check', help='validate a BioSense artifact file and list every error')
    p.add_argument('file'); p.add_argument('--kind', choices=sorted(KINDS_BY_NAME))
    p.set_defaults(fn=cmd_check)
    p = sub.add_parser('template', help='print an example draft')
    p.add_argument('kind', choices=['hypothesis'])
    p.set_defaults(fn=cmd_template)
    a = ap.parse_args(argv)
    try:
        return a.fn(a)
    except K.ContractError as e:
        print(json.dumps({'refused': True, 'reason': str(e)}, indent=2), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
