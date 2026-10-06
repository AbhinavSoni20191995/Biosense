"""Reading a finished run off disk, and refusing to render what did not validate.

A discovery run — synthetic or agent-driven — leaves JSON documents in a
directory. This turns that directory into the payload the interface renders.

The rule the whole module turns on: **a file is shown only if it passed its
schema.** `contracts.require_valid` decides, not a key check and not a guess at
what the file was meant to be. A document that fails is counted in `rejected`
with the first few violations, so the interface can say "three artifacts could
not be read" instead of quietly showing a run that looks thinner than it was.
Parsing a model's prose into a number is not a fallback that exists here.

The second rule is about where the bytes are: a private dataset is identified by
its path, and `data.roots.is_private_path` is consulted before anything is read.
Nothing under the private root is loaded by this module, whatever it is called.

Evidence is grouped by the classes the repository already defines, and each row
keeps the three facts that must never be merged: what it **is**
(`evidence_class`), what it was computed **from** (`source_evidence_class`), and
who may **see** it (`visibility`). An analysis over a private FACS table is a
derived analysis with a private source — not "a private dataset", and not
"public because the numbers came out".
"""
from __future__ import annotations

from pathlib import Path

from .. import contracts as K
from .. import parameters as PR
from .. import projects as PJ
from ..data import roots as DR

MAX_BYTES = 8 * 1024 * 1024
MAX_FILES = 400
FORBIDDEN = 'truth'
DRAFT_SUFFIX = '.draft.json'

# filename pattern -> contract. Checked in order; the first contract a document
# validates against wins, and a document that validates against none is rejected.
RECOGNISED = (
    ('analysis_result', 'analysis_result'),
    ('analysis_plan', 'analysis_plan'),
    ('quantified_hypothesis', 'quantified_hypothesis'),
    ('hypothesis', 'quantified_hypothesis'),
    ('design_choices', 'design_choices'),
    ('research_context', 'research_context'),
    ('bioinformatics_report', 'bioinformatics_report'),
    ('bioinfo', 'bioinformatics_report'),
    ('analysis_report', 'analysis_report'),
    ('protocol', 'production_protocol'),
    ('request', 'production_request'),
    ('discovery_request', 'discovery_request'),
    ('benchmark', 'benchmark_result'),
)
# The order evidence is shown in: what was measured first, what a model said last.
EVIDENCE_ORDER = ('published_literature', 'public_dataset', 'private_user_dataset',
                  'expert_knowledge', 'derived_analysis', 'real_measurement',
                  'simulation', 'synthetic_fixture')
EVIDENCE_LABEL = {
    'published_literature': 'Published literature', 'public_dataset': 'Public data',
    'private_user_dataset': 'Private data', 'expert_knowledge': 'Expert knowledge',
    'derived_analysis': 'Derived analysis', 'simulation': 'Simulation',
    'real_measurement': 'Real measurement', 'synthetic_fixture': 'Synthetic fixture',
}


def _readable(path):
    """Whether this file may be opened at all. Path first, name second."""
    p = Path(path)
    if FORBIDDEN in p.name.lower():
        return False
    return not DR.is_private_path(p)


def _load(path):
    if not _readable(path):
        return None, 'not readable from here'
    try:
        if path.stat().st_size > MAX_BYTES:
            return None, f'{path.stat().st_size} bytes exceeds the {MAX_BYTES}-byte limit'
        return K.read_json(path), None
    except (OSError, ValueError) as e:
        return None, f'{type(e).__name__}: {e}'


def classify(doc, name=''):
    """Which contract this document satisfies, or None.

    The filename only decides what to *try first*. Validation decides. A file
    called `analysis_result.json` holding something else is rejected rather than
    rendered as an analysis.
    """
    if not isinstance(doc, dict):
        return None
    low = (name or '').lower()
    ordered = [kind for frag, kind in RECOGNISED if frag in low]
    ordered += [kind for _, kind in RECOGNISED if kind not in ordered]
    for kind in ordered:
        if not K.schema_errors(kind, doc):
            return kind
    return None


def read_run(run_dir, *, max_files=MAX_FILES):
    """Every recognised artifact under *run_dir*, grouped by contract.

    Returns {'by_kind': {...}, 'rejected': [...], 'files_seen': n}. Nothing is
    reshaped: a caller that wants the raw analysis gets the document the tool
    wrote.
    """
    root = Path(run_dir)
    by_kind, rejected, seen = {}, [], 0
    if not root.is_dir():
        return {'by_kind': by_kind, 'rejected': rejected, 'files_seen': 0,
                'run_dir': root.name, 'missing': True}
    for path in sorted(root.rglob('*.json')):
        if seen >= max_files:
            break
        if path.name.endswith(DRAFT_SUFFIX):
            # An agent's input to a BioSense CLI, not an artifact: the CLI
            # writes the validated document beside it.
            continue
        seen += 1
        doc, why = _load(path)
        rel = str(path.relative_to(root))
        if doc is None:
            if why != 'not readable from here':
                rejected.append({'file': rel, 'why': why})
            continue
        kind = classify(doc, path.name)
        if kind is None:
            # Not everything in a loop directory is a contract document: the
            # loop's own state and decisions have their own shapes and are read
            # by serve.load_loop. Only files that look like an artifact and fail
            # are reported, so the count means something.
            if any(frag in path.name.lower() for frag, _ in RECOGNISED):
                errs = K.schema_errors(
                    next(k for f, k in RECOGNISED if f in path.name.lower()), doc)
                rejected.append({'file': rel, 'why': '; '.join(errs[:3]) or 'unrecognised shape'})
            continue
        by_kind.setdefault(kind, []).append({'file': rel, 'doc': doc})
    return {'by_kind': by_kind, 'rejected': rejected, 'files_seen': seen,
            'run_dir': root.name, 'missing': False}


# ── display payloads ────────────────────────────────────────────────────
def analysis_card(result):
    """One AnalysisResult as the card the interface draws.

    The simple view gets the question, the dataset, the method and the
    interpretation; everything that makes it checkable — the statistics, the QC,
    the provenance chain — goes under `technical`, which is shown but not first.
    """
    K.require_valid('analysis_result', result)
    comparison = result.get('comparison') or {}
    method = result.get('method') or {}
    # The finding carries the number; the basis carries the statistics that make
    # it checkable. They are shown as two lines rather than one, so the simple
    # view can read the first without the second disappearing.
    findings = [{'text': f.get('finding') or f.get('statement') or f.get('summary') or str(f),
                 'basis': f.get('basis')}
                if isinstance(f, dict) else {'text': str(f), 'basis': None}
                for f in (result.get('key_findings') or [])]
    implications = [{'text': i.get('implication') or str(i), 'rests_on': i.get('rests_on'),
                     'alternatives': i.get('alternative_explanations') or []}
                    if isinstance(i, dict) else {'text': str(i), 'rests_on': None,
                                                 'alternatives': []}
                    for i in (result.get('process_implications') or [])]
    return {
        'analysis_id': result['analysis_id'],
        'question': result['question'],
        'uncertainty': (result.get('uncertainty_ref') or {}).get('statement'),
        'datasets': [{'dataset_id': d.get('dataset_id'), 'title': d.get('title'),
                      'visibility': d.get('visibility'),
                      'evidence_class': d.get('evidence_class')}
                     for d in (result.get('datasets') or [])],
        'method': {'tool': method.get('tool'), 'version': method.get('tool_version'),
                   'software': method.get('software'),
                   'analysis_type': method.get('analysis_type'),
                   'label': _method_label(method)},
        'comparison': {'group_column': comparison.get('group_column'),
                       'control': comparison.get('control'),
                       'treatment': comparison.get('treatment'),
                       'readouts': comparison.get('readouts') or [],
                       'independent_units': comparison.get('independent_units'),
                       'independence_note': comparison.get('independence_note'),
                       'paired': comparison.get('paired')} if comparison else None,
        'findings': findings,
        'implications': implications,
        'candidates': [_candidate_row(c) for c in
                       (result.get('candidate_process_parameters') or [])],
        'confidence': result.get('confidence'),
        'citable': result.get('citable'),
        'evidence_class': result.get('evidence_class'),
        'source_evidence_class': result.get('source_evidence_class'),
        'visibility': result.get('source_visibility'),
        'decision_relevance': result.get('decision_relevance'),
        'limitations': result.get('limitations') or [],
        'technical': {
            'statistics': result.get('statistics'),
            'quality_control': result.get('quality_control'),
            'provenance': result.get('provenance'),
            'plan_ref': result.get('plan_ref'),
            'parent_datasets': result.get('parent_dataset_ids') or [],
        },
    }


_METHOD_LABELS = {
    'population_comparison': 'Population comparison',
    'expression_comparison': 'Differential expression',
    'pseudobulk_comparison': 'Pseudobulk comparison',
    'peak_overlap': 'Peak overlap',
}


def _method_label(method):
    """A readable name for the method, falling back to the tool's own id.

    Falls back rather than inventing: a tool this table does not know about shows
    its real name, which is still something a reader can look up.
    """
    t = method.get('analysis_type') or ''
    return _METHOD_LABELS.get(t) or (method.get('tool') or t or None)


def _candidate_row(c):
    if not isinstance(c, dict):
        return {'parameter_id': str(c)}
    pid = c.get('parameter_id') or c.get('parameter')
    resolved = PR.resolve(pid, required=False) if pid else None
    return {'parameter_id': resolved or pid, 'direction': c.get('direction'),
            'confidence': c.get('confidence'), 'rationale': c.get('rationale')
            or c.get('reason'), 'unresolved': resolved is None and pid is not None}


def hypothesis_card(h, *, project=None):
    """One QuantifiedHypothesis, with its coverage resolved against the project.

    `simulator_coverage` is read from the project rather than trusted from the
    document: a hypothesis that claims a parameter is modelled when this
    project's model has no term for it must not produce a prediction, and the
    project is the thing that knows.
    """
    K.require_valid('quantified_hypothesis', h)
    p = dict(h['parameter'])
    pid = p.get('parameter_id')
    if project is not None and pid:
        coverage = project.coverage(pid)
        p['simulator_coverage'] = coverage
        p['in_project'] = coverage != 'not_in_project'
        if coverage != 'modelled':
            p['coverage_note'] = (
                'This project\'s model has no term for this parameter, so no effect is '
                'predicted for it. It remains a real design variable you can set in the lab.'
                if coverage == 'not_modelled' else
                'This project has no mechanistic model, so nothing is predicted here.'
                if coverage == 'no_simulator' else
                'This parameter is not one this project exposes.')
    effects = [dict(e) for e in (h.get('expected_effects') or [])]
    return {
        'hypothesis_id': h['hypothesis_id'],
        'statement': h['statement'],
        'status': h.get('status') or 'proposed',
        'supersedes': h.get('supersedes'),
        'superseded_reason': h.get('superseded_reason'),
        'project_id': h.get('project_id'),
        'uncertainty': (h.get('uncertainty_ref') or {}).get('statement')
                       if isinstance(h.get('uncertainty_ref'), dict) else h.get('uncertainty_ref'),
        'parameter': p,
        'effects': effects,
        'trade_offs': h.get('trade_offs') or [],
        'evidence': [_evidence_row(e) for e in (h.get('evidence') or [])],
        'confidence': h.get('confidence'),
        'confidence_basis': h.get('confidence_basis') or [],
        'next_experiment': h.get('next_experiment') or {},
        'limitations': h.get('limitations') or [],
        'may_change_protocol': h.get('may_change_protocol', False),
    }


def _evidence_row(e):
    cls = e.get('evidence_class')
    return {'evidence_class': cls, 'label': EVIDENCE_LABEL.get(cls, cls),
            'stance': e.get('stance'), 'strength': e.get('strength'),
            'summary': e.get('summary'), 'ref': e.get('ref'),
            'visibility': e.get('visibility'),
            'context_match': e.get('context_match'),
            'context_mismatch_note': e.get('context_mismatch_note'),
            'relevance': e.get('relevance'), 'bearing': e.get('bearing')}


def evidence_panel(bundle):
    """Every piece of evidence the run touched, grouped by what it is.

    Built from the artifacts rather than from a narrative, so a group that is
    empty is empty because nothing of that kind was found — which is itself worth
    showing, and is why empty groups are kept with a count of zero.
    """
    groups = {cls: [] for cls in EVIDENCE_ORDER}
    for entry in bundle['by_kind'].get('analysis_result', []):
        r = entry['doc']
        groups.setdefault(r.get('evidence_class') or 'derived_analysis', []).append({
            'evidence_class': r.get('evidence_class'),
            'label': EVIDENCE_LABEL.get(r.get('evidence_class')),
            'summary': r['question'],
            'ref': r['analysis_id'],
            'source_evidence_class': r.get('source_evidence_class'),
            'visibility': r.get('source_visibility'),
            'confidence': r.get('confidence'),
            'context_match': 'not_assessed',
        })
    for entry in bundle['by_kind'].get('quantified_hypothesis', []):
        for row in entry['doc'].get('evidence') or []:
            groups.setdefault(row.get('evidence_class') or 'published_literature', []).append(
                _evidence_row(row))
    out = []
    for cls in EVIDENCE_ORDER:
        rows = groups.get(cls) or []
        out.append({'evidence_class': cls, 'label': EVIDENCE_LABEL[cls],
                    'count': len(rows), 'items': rows})
    for cls, rows in groups.items():
        if cls not in EVIDENCE_ORDER and rows:
            out.append({'evidence_class': cls, 'label': EVIDENCE_LABEL.get(cls, cls),
                        'count': len(rows), 'items': rows})
    return out


def candidate_parameters(bundle, *, project):
    """Candidate parameters, deduplicated, each with this project's coverage.

    A parameter the project does not expose is kept and marked, never dropped:
    "we cannot model that" and "nobody suggested that" look identical once a row
    is removed, and they are not the same answer.
    """
    rows, seen = [], {}
    for entry in bundle['by_kind'].get('quantified_hypothesis', []):
        h = entry['doc']
        p = h.get('parameter') or {}
        pid = p.get('parameter_id')
        if not pid:
            continue
        key = (pid, p.get('candidate_value'))
        if key in seen:
            seen[key]['hypotheses'].append(h['hypothesis_id'])
            continue
        coverage = project.coverage(pid) if project else p.get('simulator_coverage')
        known = project.has(pid) if project else True
        row = {
            'parameter_id': pid,
            'label': p.get('label') or (project.parameter(pid).label if known else pid),
            'unit': p.get('unit') or (project.parameter(pid).unit if known else None),
            'stage': p.get('stage') or (project.parameter(pid).stage if known else None),
            'current_value': p.get('current_value'),
            'candidate_value': p.get('candidate_value'),
            'direction': p.get('direction'),
            'suggested_range': _range(p, project, pid, known),
            'confidence': h.get('confidence'),
            'reason': h.get('statement'),
            'simulator_coverage': coverage,
            'modelled': coverage == 'modelled',
            'evidence_sources': sorted({e.get('evidence_class') for e in
                                        (h.get('evidence') or []) if e.get('evidence_class')}),
            'hypotheses': [h['hypothesis_id']],
            'hypothesis_status': h.get('status') or 'proposed',
        }
        seen[key] = row
        rows.append(row)
    return rows


def _range(p, project, pid, known):
    lo, hi = p.get('range_low'), p.get('range_high')
    if lo is None and hi is None and project is not None and known:
        q = project.parameter(pid)
        lo, hi = q.minimum, q.maximum
    if lo is None and hi is None:
        return None
    return {'minimum': lo, 'maximum': hi}


def run_bundle(run_dir, *, project_id=None, projects_dir=None):
    """Everything the interface needs for one finished run.

    The single entry point: read the directory, validate what is there, and
    return the cards. A run with nothing in it comes back with empty lists and
    `artifacts_ingested: 0`, which the interface says out loud rather than
    rendering as a result.
    """
    bundle = read_run(run_dir)
    project = None
    pid = project_id
    if pid is None:
        for entry in bundle['by_kind'].get('quantified_hypothesis', []):
            pid = entry['doc'].get('project_id')
            if pid:
                break
    if pid:
        try:
            project = PJ.load(pid, projects_dir)
        except K.ContractError:
            project = None

    analyses = [analysis_card(e['doc']) for e in bundle['by_kind'].get('analysis_result', [])]
    hyps = [hypothesis_card(e['doc'], project=project)
            for e in bundle['by_kind'].get('quantified_hypothesis', [])]
    contexts = [e['doc'] for e in bundle['by_kind'].get('research_context', [])]
    count = sum(len(v) for v in bundle['by_kind'].values())
    return {
        'run_dir': bundle['run_dir'],
        'project_id': pid,
        'project': project.summary() if project else None,
        'research_context': contexts[0] if contexts else None,
        'analyses': analyses,
        'hypotheses': hyps,
        'selected_hypothesis': _selected(hyps),
        'candidate_parameters': candidate_parameters(bundle, project=project),
        'evidence': evidence_panel(bundle),
        'plans': [e['doc'] for e in bundle['by_kind'].get('analysis_plan', [])],
        'artifacts_ingested': count,
        'artifacts_rejected': bundle['rejected'],
        'files_seen': bundle['files_seen'],
        'missing': bundle.get('missing', False),
    }


def _selected(cards):
    """The hypothesis to show first: supported before proposed, never a rejected one."""
    rank = {'supported': 0, 'proposed': 1, 'superseded': 2, 'contradicted': 3, 'rejected': 4}
    live = [c for c in cards if c['status'] not in ('rejected', 'contradicted')]
    pool = live or cards
    if not pool:
        return None
    return sorted(pool, key=lambda c: (rank.get(c['status'], 9),
                                       -{'high': 2, 'moderate': 1}.get(c['confidence'], 0)))[0]
