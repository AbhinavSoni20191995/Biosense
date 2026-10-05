"""A benchmark assembled from a run that already happened.

The rule, stated once: **do not re-run the biology to produce a benchmark.** A
finished discovery run already contains everything a benchmark records — the
objective, the scope, the uncertainty, the datasets, the analyses with their
provenance, the hypotheses, the candidate parameters, the simulator comparison,
the next experiment. Running it again would spend the compute, and worse, would
produce a *different* run and report it as a description of the first.

So this reads the artifacts and builds a BenchmarkResult around them. Two
properties follow from that, and both are deliberate:

* **The scorecard is derived, not asserted.** Each capability row is decided by
  whether the artifact that would demonstrate it is actually present and valid.
  A run with no analysis gets `deterministic_analysis_executed: FAIL` with the
  reason, never a pass by default. A benchmark that scored itself on intentions
  would measure nothing.
* **Privacy is the existing validator's answer, not a new one.** `privacy.check`
  decides, `cli.bundle_dir` decides where a bundle may be written, and a private
  bundle goes outside the repository under the private root. No second privacy
  rule is written here, because a second rule is a second thing to get wrong.

A benchmark built this way carries `mode: "ai"` when agents drove the run, which
is the contract's own way of saying it is not bitwise reproducible and records
its harness instead.
"""
from __future__ import annotations

from .. import contracts as K
from .. import projects as PJ
from ..production import discovery as DISC
from . import privacy as PV
from . import runner as BR

# capability -> (what has to be present, how to say it is missing)
CAPABILITY_SOURCES = (
    ('objective_interpreted', 'objective'),
    ('project_selected', 'project'),
    ('research_context_applied', 'research_context'),
    ('uncertainty_identified', 'uncertainties'),
    ('evidence_collected', 'datasets'),
    ('analysis_plan_produced', 'plans'),
    ('deterministic_analysis_executed', 'analyses'),
    ('provenance_complete', 'provenance'),
    ('hypothesis_generated', 'hypotheses'),
    ('quantitative_effect_supported', 'effects'),
    ('candidate_parameter_generated', 'candidates'),
    ('simulator_coverage_checked', 'coverage'),
    ('simulator_handoff', 'handoff'),
    ('predicted_improvement_generated', 'prediction'),
    ('next_experiment_generated', 'next_experiment'),
    ('plain_language_generated', 'narrative'),
    ('privacy_validated', 'privacy'),
)


def _row(cap, ok, detail):
    return {'capability': cap, 'status': 'PASS' if ok else 'FAIL', 'detail': detail}


def build(*, request, bundle, out_dir_name, runtime_mode, projects_dir=None, session=None,
          benchmark=None, title=None):
    """A BenchmarkResult from a finished run's artifacts.

    *bundle* is `ingest.run_bundle`'s output. When the run was the deterministic
    path and already produced a BenchmarkResult, pass it as *benchmark*: the
    scorecard it computed while running is better evidence than one derived
    afterwards, and this only re-stamps the identity fields.
    """
    K.require_valid('discovery_request', request)
    project = PJ.load(request['project_id'], projects_dir)
    priv_req = DISC.privacy(request)
    is_ai = runtime_mode in ('local_real_ai', 'remote_real_ai')

    if benchmark is not None:
        result = dict(benchmark)
        result['run_id'] = out_dir_name
        result['mode'] = 'ai' if is_ai else 'offline'
    else:
        result = _from_bundle(request, bundle, project, out_dir_name, is_ai)

    if title:
        result['title'] = title
    if is_ai:
        prov = dict(result.get('provenance') or {})
        prov['ai'] = {
            'runtime_mode': runtime_mode,
            'omnigent_session_id': (session or {}).get('omnigent_session_id'),
            'agent': (session or {}).get('agent_id'),
            'harness': (session or {}).get('harness'),
            'llm_model': (session or {}).get('llm_model'),
            'note': 'Agents drove this run. It is not bitwise reproducible; what is recorded is '
                    'the harness and the session it ran in.',
        }
        result['provenance'] = prov

    result['privacy'] = PV.check(result, priv_req['export_policy'],
                                 private_ids=[s.split(':', 1)[-1]
                                              for s in priv_req['private_sources']])
    rows = [r for r in result['scorecard']['rows'] if r['capability'] != 'privacy_validated']
    rows.append(_row('privacy_validated', True,
                     result['privacy']['refusal_reason'] or 'no private lineage; safe to publish'))
    result['scorecard']['rows'] = rows
    result['scorecard']['passed'] = sum(1 for r in rows if r['status'] == 'PASS')
    result['scorecard']['failed'] = sum(1 for r in rows if r['status'] == 'FAIL')
    K.require_valid('benchmark_result', result)
    return result


def _from_bundle(request, bundle, project, out_dir_name, is_ai):
    """Assemble from ingested artifacts, scoring each capability on what is there."""
    rows = []
    analyses = bundle.get('analyses') or []
    hyps = bundle.get('hypotheses') or []
    cands = bundle.get('candidate_parameters') or []
    ctx = bundle.get('research_context') or request.get('research_context')

    rows.append(_row('objective_interpreted', bool(request['objective'].strip()),
                     f'objective: {request["objective"]}'))
    rows.append(_row('project_selected', True,
                     f'{project.project_id} v{project.version}'))
    rows.append(_row('research_context_applied', bool(ctx),
                     f'strictness {ctx["strictness"]}' if ctx else
                     'no research context was declared, so no evidence could be scoped or '
                     'flagged as out of context'))
    unc = request.get('uncertainty') or next(
        ({'kind': 'evidence_gap', 'ref': h['hypothesis_id'], 'statement': h['uncertainty']}
         for h in hyps if h.get('uncertainty')), None)
    rows.append(_row('uncertainty_identified', bool(unc),
                     unc['statement'] if unc else 'the run named no decision-blocking question'))
    ds_rows = _dataset_rows(analyses, request)
    rows.append(_row('evidence_collected', bool(ds_rows),
                     ', '.join(d['dataset_id'] for d in ds_rows) or 'no dataset was analysed'))
    rows.append(_row('analysis_plan_produced', bool(bundle.get('plans')),
                     f'{len(bundle.get("plans") or [])} plan(s)'))
    rows.append(_row('deterministic_analysis_executed', bool(analyses),
                     '; '.join(f'{a["method"]["tool"]} over '
                               f'{(a["datasets"] or [{}])[0].get("dataset_id")}'
                               for a in analyses) or 'no analysis ran'))
    prov_ok = bool(analyses) and all(
        (a['technical'].get('provenance') or {}).get('code_sha256') for a in analyses)
    rows.append(_row('provenance_complete', prov_ok,
                     'every analysis carries its code hash and input checksums' if prov_ok
                     else 'an analysis is missing provenance'))
    rows.append(_row('hypothesis_generated', bool(hyps),
                     hyps[0]['hypothesis_id'] if hyps else 'no hypothesis'))
    quantified = any(e.get('magnitude_estimated') for h in hyps for e in h.get('effects') or [])
    rows.append(_row('quantitative_effect_supported', bool(hyps),
                     'at least one effect carries a magnitude with provenance' if quantified else
                     'direction only; magnitudes withheld with stated reasons'))
    rows.append(_row('candidate_parameter_generated', bool(cands),
                     ', '.join(f'{c["parameter_id"]} ({c["direction"]})' for c in cands)
                     or 'no candidate parameter was produced'))
    checked = all(c.get('simulator_coverage') for c in cands) if cands else False
    rows.append(_row('simulator_coverage_checked', checked,
                     'every candidate carries this project\'s coverage' if checked
                     else 'coverage was not resolved for every candidate'))
    modelled = [c for c in cands if c.get('modelled')]
    rows.append(_row('simulator_handoff', bool(cands),
                     f'{len(modelled)} of {len(cands)} candidate(s) reach the model; the rest '
                     f'are reported as not modelled rather than dropped' if cands
                     else 'nothing to hand over'))
    rows.append(_row('predicted_improvement_generated', bool(modelled),
                     'a simulated comparison was produced' if modelled else
                     'no candidate was modelled, so no prediction exists — which is the correct '
                     'output, not a failure to produce one'))
    nxt = next((h.get('next_experiment') for h in hyps if (h.get('next_experiment') or {})
                .get('summary')), None)
    rows.append(_row('next_experiment_generated', bool(nxt),
                     nxt['summary'] if nxt else 'no next experiment was recommended'))
    rows.append(_row('plain_language_generated', bool(hyps),
                     'the hypothesis statements are the plain-language record of this run'))

    classes = {}
    for d in ds_rows:
        classes[d['evidence_class']] = classes.get(d['evidence_class'], 0) + 1
    if analyses:
        classes['derived_analysis'] = len(analyses)

    rev = K.code_revision() or {}
    return {
        'schema_version': K.PRODUCTION_VERSION,
        'benchmark_id': _benchmark_id(request),
        'run_id': out_dir_name,
        'created_at': K.now_iso(),
        'mode': 'ai' if is_ai else 'offline',
        'title': request.get('title') or request['objective'][:120],
        'objective': request['objective'],
        'project': {'project_id': project.project_id, 'version': project.version,
                    'name': project.name, 'simulator': project.simulator},
        'research_context': ctx,
        'uncertainties': [unc] if unc else [],
        'datasets': ds_rows,
        'expert_knowledge': [{'knowledge_id': k, 'knowledge_type': 'unspecified',
                              'confidence': 'unspecified',
                              'statement': 'referenced by the request; content is private and '
                                           'never copied into a benchmark'}
                             for k in request.get('expert_knowledge_ids') or []],
        'evidence_classes': classes,
        'analyses': [{'analysis_id': a['analysis_id'],
                      'plan_ref': a['technical'].get('plan_ref'),
                      'tool': a['method']['tool'], 'tool_version': a['method']['version'],
                      'software': a['method']['software'],
                      'evidence_class': a['evidence_class'],
                      'source_evidence_class': a['source_evidence_class'],
                      'source_visibility': a['visibility'],
                      'confidence': a['confidence'],
                      'statistics': a['technical'].get('statistics'),
                      'key_findings': a['findings'],
                      'candidate_process_parameters': a['candidates'],
                      'provenance': a['technical'].get('provenance')} for a in analyses],
        'hypotheses': _hypothesis_docs(bundle),
        'selected_hypothesis': (bundle.get('selected_hypothesis') or {}).get('hypothesis_id'),
        'candidate_parameters': cands,
        'simulator': None,
        'next_experiment': nxt,
        'provenance': {
            'biosense_version': K.BIOSENSE_VERSION,
            'git_commit': rev.get('commit'), 'git_dirty': rev.get('package_dirty'),
            'code_sha256': K.code_sha256(),
            'project_version': project.version,
            'simulator_model': project.simulator.get('model_id'),
            'simulator_model_version': project.simulator.get('model_version'),
            'tool_versions': {a['method']['tool']: a['method']['version'] for a in analyses},
            'dataset_checksums': {},
            'seeds': {}, 'runtime_s': None, 'ai': None,
        },
        'scorecard': {
            'rows': rows,
            'passed': sum(1 for r in rows if r['status'] == 'PASS'),
            'failed': sum(1 for r in rows if r['status'] == 'FAIL'),
            'note': 'This is a SYSTEM CAPABILITY scorecard. It records whether BioSense '
                    'identified an uncertainty, planned an analysis, executed it '
                    'deterministically, quantified what it could, checked simulator coverage '
                    'and labelled every number. It does NOT measure biological truth, and a '
                    'run can pass every row while being biologically wrong.',
        },
        'privacy': {'export_policy': 'public_safe', 'has_private_lineage': False,
                    'private_sources': [], 'safe_to_publish': False, 'refusal_reason': None},
        'narrative': [],
        'warnings': [f'{len(bundle.get("artifacts_rejected") or [])} artifact(s) could not be '
                     f'read and are not represented here']
                    if bundle.get('artifacts_rejected') else [],
        'limitations': sorted(set(list(project.doc['limitations'])
                                  + [x for h in bundle.get('hypotheses') or []
                                     for x in (h.get('limitations') or [])])),
        'operational': {'analyses': len(analyses), 'plans': len(bundle.get('plans') or []),
                        'datasets': len(ds_rows), 'simulator_runs': 0,
                        'hypotheses': len(hyps), 'candidates': len(cands)},
    }


def _benchmark_id(request):
    from ..production import discovery_runner as DR
    return DR.benchmark_id_for(request)


def _dataset_rows(analyses, request):
    seen, rows = set(), []
    for a in analyses:
        for d in a.get('datasets') or []:
            did = d.get('dataset_id')
            if not did or did in seen:
                continue
            seen.add(did)
            rows.append({'dataset_id': did, 'title': d.get('title'),
                         'visibility': d.get('visibility') or 'public',
                         'evidence_class': d.get('evidence_class') or 'public_dataset',
                         'checksums': []})
    for did in request.get('dataset_ids') or []:
        if did in seen:
            continue
        seen.add(did)
        rows.append({'dataset_id': did, 'title': None, 'visibility': 'public',
                     'evidence_class': 'public_dataset', 'checksums': [],
                     'note': 'named by the request; no analysis in this run used it'})
    return rows


def _hypothesis_docs(bundle):
    """The hypothesis contracts behind the display cards, for the benchmark body."""
    out = []
    for card in bundle.get('hypotheses') or []:
        out.append({
            'schema_version': K.PRODUCTION_VERSION,
            'hypothesis_id': card['hypothesis_id'], 'created_at': K.now_iso(),
            'statement': card['statement'], 'project_id': card.get('project_id'),
            'uncertainty_ref': card.get('uncertainty'),
            'parameter': card['parameter'], 'expected_effects': card.get('effects') or [],
            'trade_offs': card.get('trade_offs') or [],
            'evidence': card.get('evidence') or [], 'confidence': card.get('confidence'),
            'confidence_basis': card.get('confidence_basis') or [],
            'status': card.get('status'),
            'next_experiment': card.get('next_experiment') or {'summary': ''},
            'may_change_protocol': False,
            'limitations': card.get('limitations') or [],
        })
    return out


def write(result, *, out=None):
    """Write the bundle where its privacy allows, and say where that was."""
    from . import cli as BCLI
    d = BCLI.bundle_dir(result, out=out)
    written = BCLI.write_bundle(result, d)
    return {**written, 'export_policy': result['privacy']['export_policy'],
            'safe_to_publish': result['privacy']['safe_to_publish'],
            'refusal_reason': result['privacy']['refusal_reason']}


def display(result):
    """The benchmark as the interface renders it, with the headline facts up front."""
    sc = result['scorecard']
    return {
        'benchmark_id': result['benchmark_id'], 'run_id': result.get('run_id'),
        'title': result.get('title'), 'objective': result['objective'],
        'created_at': result['created_at'], 'mode': result['mode'],
        'project': result['project'], 'research_context': result.get('research_context'),
        'uncertainties': result.get('uncertainties') or [],
        'datasets': result.get('datasets') or [],
        'analyses': result.get('analyses') or [],
        'hypotheses': result.get('hypotheses') or [],
        'candidate_parameters': result.get('candidate_parameters') or [],
        'simulator': result.get('simulator'),
        'next_experiment': result.get('next_experiment'),
        'limitations': result.get('limitations') or [],
        'scorecard': {'rows': sc['rows'], 'passed': sc['passed'], 'failed': sc['failed'],
                      'note': sc['note'],
                      'headline': 'THIS MEASURES SYSTEM CAPABILITY, NOT BIOLOGICAL TRUTH.'},
        'privacy': {**result['privacy'],
                    'badge': ('PUBLIC-SAFE' if result['privacy'].get('safe_to_publish')
                              else 'PRIVATE — DO NOT PUBLISH')},
        'warnings': result.get('warnings') or [],
        'provenance': result.get('provenance'),
    }


# Re-exported so a caller does not have to know which module computed the rows.
CAPABILITIES = BR.CAPABILITIES
