"""Building and validating an AnalysisPlan.

One rule carries this module: **an analysis that answers no stated question
cannot be constructed.** `uncertainty_ref` is required by the schema, checked
here against the loop record it claims to come from, and `plan()` refuses without
it. That is the structural difference between a bioinformatics capability that
serves a decision and a dashboard that produces findings nobody asked for.

The other half is honest refusal. A plan is checked against the tool registry and
against the manifests it names *before* anything executes, so the failure mode is
"this dataset does not record which column holds the condition" rather than a
confident comparison of the wrong thing.
"""
from __future__ import annotations

from .. import contracts as K
from ..data import registry as DREG
from . import registry as TREG


def uncertainty_from_hypothesis(h, *, source_artifact=None, raised_by='analysis_agent'):
    """Turn an analysis-report hypothesis into an uncertainty reference."""
    return {'kind': 'hypothesis', 'ref': h['hypothesis_id'], 'statement': h['statement'],
            'raised_by': raised_by, 'source_artifact': source_artifact}


def evidence_gap(ref, statement, *, raised_by='orchestrator', source_artifact=None):
    """An uncertainty that is not a hypothesis: something the loop knows it lacks."""
    if not (ref or '').strip() or len((statement or '').strip()) < 10:
        raise K.ContractError('an evidence gap needs an identifier and a statement of what is '
                              'missing, in at least 10 characters')
    return {'kind': 'evidence_gap', 'ref': ref, 'statement': statement,
            'raised_by': raised_by, 'source_artifact': source_artifact}


def resolve_uncertainty(ref, *, analysis_report=None, recorded_gaps=()):
    """Check that an uncertainty reference points at something real.

    A hypothesis reference must name a hypothesis in the analysis report it came
    from. An evidence gap must be one the loop recorded. Neither is taken on
    trust, because "cite an uncertainty" is only a constraint if the citation is
    checked.
    """
    if not isinstance(ref, dict):
        raise K.ContractError('uncertainty_ref must be an object with kind, ref and statement')
    kind, rid = ref.get('kind'), ref.get('ref')
    if kind == 'hypothesis':
        if analysis_report is None:
            return  # nothing to check it against; the schema still requires the fields
        known = {h['hypothesis_id'] for h in analysis_report.get('diagnosis', [])}
        if rid not in known:
            raise K.ContractError(
                f'uncertainty_ref names hypothesis {rid!r}, which is not in the analysis report '
                f'{analysis_report.get("analysis_id")!r}. Known: '
                f'{", ".join(sorted(known)) or "none"}. An analysis is planned against an '
                f'uncertainty the loop actually has.')
    elif kind == 'evidence_gap':
        gaps = {g.get('ref') if isinstance(g, dict) else g for g in recorded_gaps}
        if gaps and rid not in gaps:
            raise K.ContractError(
                f'uncertainty_ref names evidence gap {rid!r}, which this loop has not recorded. '
                f'Record the gap first: an analysis is planned against a question that exists.')
    else:
        raise K.ContractError(f'uncertainty kind must be hypothesis or evidence_gap, got {kind!r}')


def plan(*, plan_id, question, uncertainty_ref, why_requested, dataset_ids, analysis_type,
         tool, decision_relevance, parameters_that_may_change, created_by='bioinformatics_agent',
         modality=None, comparison=None, steps=(), expected_outputs=(), limitations=(),
         loop_id=None, iteration=None, analysis_report=None, recorded_gaps=(), dirs=None):
    """Build a validated AnalysisPlan, or refuse and say what is missing."""
    if not uncertainty_ref:
        raise K.ContractError(
            'an AnalysisPlan requires uncertainty_ref: the hypothesis or recorded evidence gap '
            'this analysis would reduce. BioSense does not run an analysis because it can; it '
            'runs one because a decision is waiting on it. Record the uncertainty first.')
    resolve_uncertainty(uncertainty_ref, analysis_report=analysis_report,
                        recorded_gaps=recorded_gaps)
    if not dataset_ids:
        raise K.ContractError('an AnalysisPlan needs at least one dataset')

    manifests = [DREG.require(d, dirs=dirs) for d in dataset_ids]
    modalities = {m['modality'] for m in manifests}
    if modality is None:
        if len(modalities) > 1:
            raise K.ContractError(
                f'the datasets have different modalities ({", ".join(sorted(modalities))}); '
                f'name the modality this analysis is about')
        modality = modalities.pop()
    elif modality not in modalities:
        raise K.ContractError(f'plan modality {modality!r} is not among the datasets\' '
                              f'modalities ({", ".join(sorted(modalities))})')

    spec = TREG.get(tool)
    if modality not in spec.modalities:
        raise K.ContractError(
            f'{spec.name} does not accept {modality!r} data (it accepts '
            f'{", ".join(spec.modalities)})')
    if analysis_type not in spec.analysis_types:
        raise K.ContractError(
            f'{spec.name} does not perform {analysis_type!r} (it performs '
            f'{", ".join(spec.analysis_types)})')

    p = {
        'schema_version': K.PRODUCTION_VERSION,
        'plan_id': plan_id, 'created_at': K.now_iso(), 'created_by': created_by,
        'loop_id': loop_id, 'iteration': iteration,
        'question': question, 'uncertainty_ref': dict(uncertainty_ref),
        'why_requested': why_requested,
        'dataset_ids': list(dataset_ids), 'modality': modality,
        'analysis_type': analysis_type,
        'comparison': comparison,
        'steps': list(steps) or [f'read the registered table(s) for {", ".join(dataset_ids)}',
                                 'check the metadata the tool requires is present',
                                 f'run {spec.name} v{spec.version}',
                                 'correct for multiplicity across readouts (Benjamini-Hochberg)',
                                 'map the result to candidate process parameters, or say why not'],
        'tool': {'name': spec.name, 'parameters': dict((comparison or {}).get('options') or {}),
                 'external': bool(spec.external_dependency)},
        'required_metadata': list(spec.required_metadata),
        'expected_outputs': list(expected_outputs) or list(spec.outputs),
        'decision_relevance': decision_relevance,
        'parameters_that_may_change': list(parameters_that_may_change),
        'limitations': list(limitations),
    }
    K.require_valid('analysis_plan', p)
    check_ready(p, manifests)
    return p


def check_ready(p, manifests=None, *, dirs=None):
    """Refuse a plan whose datasets cannot support it, naming the missing field.

    This is the step that turns "do not guess missing metadata" from an
    instruction into a property: the plan does not run, and the message says
    exactly what to supply.
    """
    manifests = manifests or [DREG.require(d, dirs=dirs) for d in p['dataset_ids']]
    spec = TREG.get(p['tool']['name'])
    problems = []
    for m in manifests:
        design = m.get('experimental_design') or {}
        for fld in spec.required_metadata:
            if not design.get(fld):
                why = next((x['why_it_matters'] for x in m.get('missing_metadata') or []
                            if x['field'] == fld), 'required by this tool')
                problems.append(f'{m["dataset_id"]}: experimental_design.{fld} is missing — {why}')
        accepted = {t.lower() for t in spec.file_types}
        present = {f['file_type'].lower() for f in m['files']}
        if not (accepted & present):
            problems.append(
                f'{m["dataset_id"]}: {spec.name} reads {", ".join(sorted(spec.file_types))} and '
                f'this dataset carries {", ".join(sorted(present))}. Register the processed '
                f'form the tool reads, or choose a tool that reads what you have.')
    if problems:
        raise K.ContractError(
            'the plan cannot run on these datasets: ' + '; '.join(problems) +
            '. Supply the missing metadata rather than letting the analysis assume it.')
    return True
