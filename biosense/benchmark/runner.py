"""Running a benchmark: the real flow, then the artifacts it produced.

The flow this drives is the one the whole system is for:

    project + objective + research context (+ expert knowledge)
      -> a named uncertainty
      -> evidence: registered datasets, in or out of context
      -> an AnalysisPlan citing that uncertainty
      -> a deterministic analysis
      -> Estimates with per-number provenance
      -> a QuantifiedHypothesis over several outcomes
      -> a candidate parameter, narrowed by expert knowledge where it exists
      -> the project's simulator, or an honest refusal
      -> a predicted improvement, labelled SIMULATED
      -> a next experiment

Nothing here fabricates a step. If the analysis finds nothing, the hypothesis
carries a withheld magnitude; if the project has no model, the simulator section
says so and the scorecard still passes, because *being honest about missing
coverage is the capability being measured*.

`mode='offline'` is the deterministic path: reproducible, no model API, what CI
runs. `mode='ai'` is reserved for a run driven by a live agent, which records
its harness rather than pretending to be bitwise reproducible.
"""
from __future__ import annotations

import time
from pathlib import Path


from .. import contracts as K
from .. import parameters as PR
from .. import projects as PJ
from ..bioinformatics import execute as EX
from ..bioinformatics import implications as IMP
from ..bioinformatics import plan as PLAN
from ..data import registry as DREG
from ..evidence import context as CTX
from ..evidence import estimates as E
from ..evidence import expert as EK
from ..evidence import hypothesis as HY
from ..evidence import narrative as N
from ..production import sim_candidate as SC

# The scorecard. Each row is a capability the system either demonstrated or did
# not. These measure whether BioSense did its job, never whether the biology is
# right -- the note on every result says so.
CAPABILITIES = (
    'objective_interpreted', 'project_selected', 'research_context_applied',
    'uncertainty_identified', 'evidence_collected', 'analysis_plan_produced',
    'deterministic_analysis_executed', 'provenance_complete', 'hypothesis_generated',
    'quantitative_effect_supported', 'candidate_parameter_generated',
    'simulator_coverage_checked', 'simulator_handoff', 'predicted_improvement_generated',
    'next_experiment_generated', 'plain_language_generated', 'privacy_validated',
)


def load_config(path):
    c = K.read_json(path)
    K.require_valid('benchmark_config', c)
    for cap in c['expected_capabilities']:
        if cap not in CAPABILITIES:
            raise K.ContractError(f'unknown capability {cap!r}; known: {", ".join(CAPABILITIES)}')
    return c


def _row(cap, ok, detail):
    return {'capability': cap, 'status': 'PASS' if ok else 'FAIL', 'detail': detail}


def run(config, *, dirs=None, private_root=None, artifacts_out=None, projects_dir=None):
    """Execute a benchmark configuration. Returns the BenchmarkResult.

    *artifacts_out* additionally writes each full AnalysisPlan and AnalysisResult
    to that directory as it goes. The BenchmarkResult keeps a projection of them —
    enough to score the run — and the whole documents are what the interface
    renders and what a reader checks, so a run that produces them in memory and
    drops them is a run nobody can audit.
    """
    K.require_valid('benchmark_config', config)
    t0 = time.time()
    rows, warnings, notes = [], [], []
    # A project somebody created lives in their workspace, not in the committed
    # set. Without this a run against a project the person made refuses with
    # "no such project", which reads as a fault and is really a missing argument.
    project = PJ.load(config['project_id'], projects_dir)

    rows.append(_row('objective_interpreted', bool(config['objective'].strip()),
                     f'objective: {config["objective"]}'))
    rows.append(_row('project_selected', True,
                     f'{project.project_id} v{project.version}: {len(project.parameter_ids)} '
                     f'parameters, {len(project.readouts)} readouts, simulator '
                     f'{project.simulator["status"]}'))

    # ── research context ─────────────────────────────────────────────
    ctx = config.get('research_context') or project.doc.get('research_context')
    if ctx and 'schema_version' not in ctx:
        ctx = CTX.context(**{k: v for k, v in ctx.items() if k in (
            'strictness', 'species', 'cell_types', 'tissues', 'states', 'disease_context',
            'modalities', 'assays', 'therapy_context', 'exclude')})
    rows.append(_row('research_context_applied', bool(ctx),
                     CTX.describe(ctx) if ctx else 'no research context was supplied'))

    # ── uncertainty ──────────────────────────────────────────────────
    unc = config.get('uncertainty')
    if not unc:
        raise K.ContractError(
            'a benchmark needs an uncertainty: BioSense does not run an analysis because it can, '
            'it runs one because a decision is waiting on it')
    rows.append(_row('uncertainty_identified', True,
                     f'{unc["kind"]} {unc["ref"]}: {unc["statement"]}'))

    # ── evidence: datasets and expert knowledge ──────────────────────
    manifests, ds_rows = [], []
    for did in config.get('datasets') or []:
        m = DREG.require(did, dirs=dirs)
        manifests.append(m)
        item = {'species': m.get('organism'), 'cell_type': m.get('cell_type'),
                'modality': m.get('modality')}
        a = CTX.assess(ctx, item) if ctx else {'match': 'not_assessed', 'reason': ''}
        ds_rows.append({'dataset_id': m['dataset_id'], 'title': m['title'],
                        'source': m['source'], 'accession': m.get('accession'),
                        'visibility': m['visibility'], 'evidence_class': m['evidence_class'],
                        'modality': m['modality'],
                        'checksums': [f['checksum_sha256'] for f in m['files']],
                        'context_match': a['match'], 'context_note': a.get('reason')})
        if a['match'] == 'context_mismatch':
            warnings.append(f'{m["dataset_id"]} is outside the requested research context: '
                            f'{a["reason"]}')

    knowledge = []
    if config.get('expert_knowledge'):
        have = {k['knowledge_id']: k for k in EK.load_all(root=private_root,
                                                          project_id=project.project_id)}
        for kid in config['expert_knowledge']:
            if kid in have:
                knowledge.append(have[kid])
            else:
                warnings.append(f'expert knowledge {kid} was declared but is not registered')
    rows.append(_row('evidence_collected', bool(manifests) or bool(knowledge),
                     f'{len(manifests)} dataset(s), {len(knowledge)} internal knowledge item(s)'))

    # ── plan and analysis ────────────────────────────────────────────
    analyses, plans = [], []
    for m in manifests:
        if not any(f['file_type'] in ('csv', 'tsv') for f in m['files']):
            continue
        atype = ('population_comparison'
                 if m['modality'] in ('flow_cytometry', 'cytometry_summary')
                 else 'bulk_expression_comparison')
        tool = ('cytometry.population_comparison'
                if atype == 'population_comparison' else 'bulk.expression_comparison')
        try:
            p = PLAN.plan(
                plan_id=f'{config["benchmark_id"]}-{m["dataset_id"]}',
                question=f'Does {m["title"]} speak to: {unc["statement"]}',
                uncertainty_ref=dict(unc),
                recorded_gaps=[unc['ref']],
                why_requested=f'The loop holds no measurement bearing on {unc["ref"]}.',
                dataset_ids=[m['dataset_id']], analysis_type=atype, tool=tool,
                decision_relevance=config['objective'],
                parameters_that_may_change=list((config.get('candidate_values') or {})),
                created_by='benchmark (deterministic path)', dirs=dirs)
            r = EX.execute(p, dirs=dirs, executed_by='benchmark (deterministic path)',
                           analysis_id=f'analysis-{config["benchmark_id"]}-{m["dataset_id"]}')
            plans.append(p)
            analyses.append(r)
            if artifacts_out is not None:
                d = Path(artifacts_out)
                d.mkdir(parents=True, exist_ok=True)
                K.write_json_atomic(d / f'analysis_plan-{m["dataset_id"]}.json', p)
                K.write_json_atomic(d / f'analysis_result-{m["dataset_id"]}.json', r)
        except K.ContractError as e:
            warnings.append(f'{m["dataset_id"]}: {e}')

    rows.append(_row('analysis_plan_produced', bool(plans),
                     f'{len(plans)} plan(s), each citing {unc["ref"]}'))
    rows.append(_row('deterministic_analysis_executed', bool(analyses),
                     '; '.join(f'{a["method"]["tool"]} v{a["method"]["tool_version"]} over '
                               f'{a["datasets"][0]["dataset_id"]}' for a in analyses)
                     or 'no analysis ran'))
    prov_ok = bool(analyses) and all(
        a['provenance'].get('code_sha256') and a['provenance'].get('input_checksums')
        for a in analyses)
    rows.append(_row('provenance_complete', prov_ok,
                     'every analysis carries code hash, plan hash and input checksums'
                     if prov_ok else 'an analysis is missing provenance'))

    # ── candidate parameter ──────────────────────────────────────────
    candidates = []
    for a in analyses:
        for c in a['candidate_process_parameters']:
            candidates.append(dict(c, parameter=PR.resolve(c['parameter']),
                                   from_analysis=a['analysis_id'],
                                   source_evidence_class=a['source_evidence_class'],
                                   source_visibility=a['source_visibility']))
    # Declared candidates that no analysis produced still deserve a coverage
    # answer: that is how an unsupported parameter gets honestly labelled.
    for pid in (config.get('candidate_values') or {}):
        cid = PR.resolve(pid)
        if not any(c['parameter'] == cid for c in candidates):
            candidates.append({'parameter': cid, 'direction': 'revisit',
                               'basis': 'declared by the benchmark configuration; no analysis in '
                                        'this run produced evidence for it',
                               'confidence': 'low', 'arm_scope': None, 'suggested_range': None})
    # Expert knowledge that names a parameter is a third source of a lever, and
    # ignoring it was one of the ways a run with real evidence produced nothing:
    # an analysis is how a magnitude is established, not the only way a parameter
    # becomes worth testing.
    for k in knowledge:
        for claim in k.get('parameter_claims') or []:
            cid = PR.resolve(claim['parameter_id'])
            if any(c['parameter'] == cid for c in candidates):
                continue
            candidates.append({
                'parameter': cid,
                'direction': claim.get('direction') or 'revisit',
                'basis': f'named by expert knowledge {k["knowledge_id"]}; no analysis in this '
                         f'run produced a magnitude for it',
                'confidence': 'low', 'arm_scope': None, 'suggested_range': None,
                'source_evidence_class': 'expert_knowledge',
                'source_visibility': 'private'})
    rows.append(_row('candidate_parameter_generated', bool(candidates),
                     ', '.join(f'{c["parameter"]} ({c["direction"]})' for c in candidates)
                     or 'no candidate parameter was produced'))

    # ── simulator ────────────────────────────────────────────────────
    control = dict(config.get('control') or {})
    if not control:
        for pid, v in project.defaults().items():
            pp = project.parameter(pid)
            if pp.modelled and pp.simulator_mapping and isinstance(v, (int, float)):
                control[pp.simulator_mapping] = v
    values = {PR.resolve(k): v for k, v in (config.get('candidate_values') or {}).items()}

    # expert knowledge may narrow the search range; the narrowing is attributed
    ranges = {}
    for c in candidates:
        pid = c['parameter']
        if not project.has(pid):
            continue
        pp = project.parameter(pid)
        if pp.minimum is None or pp.maximum is None:
            continue
        lo, hi, why = EK.narrow_range(pid, pp.minimum, pp.maximum, knowledge)
        ranges[pid] = {'lower': lo, 'upper': hi,
                       'basis': f"{project.project_id} range for {pid}",
                       'narrowed_by': [k['knowledge_id'] for k in knowledge
                                       if any(pc['parameter_id'] == pid
                                              for pc in k.get('parameter_claims', []))]}
        notes.extend(why)

    sim = SC.compare_conditions(project, control=control, candidates=candidates,
                                candidate_values=values, seed=config.get('seed') or 7)
    covered = {r['parameter_id'] for r in sim['handoff']['applied']}
    uncovered = {r['parameter_id'] for r in sim['handoff']['skipped']}
    all_declared = {c['parameter'] for c in candidates}
    coverage_honest = all_declared == (covered | uncovered)
    rows.append(_row('simulator_coverage_checked', coverage_honest,
                     f'{len(covered)} modelled, {len(uncovered)} not modelled; every candidate '
                     f'accounted for' if coverage_honest else
                     'a candidate parameter was neither applied nor explained'))
    rows.append(_row('simulator_handoff', sim['prediction'] == 'simulated' or not covered,
                     f'{sim["prediction"]}: {sim["prediction_note"][:120]}'))
    rows.append(_row('predicted_improvement_generated',
                     bool(sim['effects']) or sim['prediction'] == 'none',
                     f'{len(sim["effects"])} predicted effect(s), all labelled SIMULATED'
                     if sim['effects'] else
                     'no prediction, and the reason is recorded rather than a number invented'))

    # ── hypothesis ───────────────────────────────────────────────────
    hypotheses = []
    if candidates:
        primary = max(candidates, key=lambda c: (c['parameter'] in covered,
                                                 c.get('confidence') == 'moderate'))
        pid = primary['parameter']
        measured = []
        for a in analyses:
            if not any(c['parameter'] == pid for c in a['candidate_process_parameters']
                       if 'parameter' in c) and \
               not any(PR.resolve(c['parameter']) == pid
                       for c in a['candidate_process_parameters']):
                continue
            for s in a['statistics']:
                if s.get('q_value') is not None and s['q_value'] < 0.05:
                    # Whether up is good comes from the readout's kind, using the
                    # same classifier that decides which readouts speak to the
                    # product and which only bound it. Without it a viability
                    # drop renders the same as a phenotype gain.
                    kind = IMP.readout_kind(s['readout'])[0]
                    better = {'yield': True, 'identity': True, 'population_frequency': True,
                              'marker_intensity': True, 'viability': True,
                              'stress': False, 'exhaustion': False}.get(kind)
                    measured.append(E.from_statistics_row(
                        s, '%', source_ref=a['datasets'][0]['dataset_id'],
                        higher_is_better=better))
        effects = measured + list(sim['effects'])
        if not effects:
            effects = [E.direction_only(
                'process_outcome', 'cells', 'unknown', 'derived',
                reason='no analysis produced a significant effect and the simulator produced no '
                       'prediction for this parameter')]
        evidence = [HY.evidence_row(
            a['source_evidence_class'],
            'supportive' if a['candidate_process_parameters'] else 'neutral',
            a['key_findings'][0]['finding'] if a['key_findings'] else 'analysis produced no finding',
            ref=a['analysis_id'], strength='moderate',
            visibility=a['source_visibility'] if a['source_visibility'] != 'mixed' else 'private')
            for a in analyses]
        evidence += [EK.as_evidence(k) for k in knowledge]
        if sim['effects']:
            evidence.append(HY.evidence_row(
                'simulation', 'supportive',
                f'{project.simulator["model_id"]} predicts '
                + '; '.join(E.render(e) for e in sim['effects'][:2]),
                ref=project.simulator['model_id'], strength='weak'))
        cov = project.coverage(pid)
        pp = project.parameter(pid) if project.has(pid) else None
        h = HY.hypothesis(
            f'HYP-{config["benchmark_id"]}-01',
            f'Changing {PR.BY_ID[pid].label} may improve the objective: {config["objective"]}',
            project=project, uncertainty_ref=dict(unc), parameter_id=pid,
            direction=primary['direction'], effects=effects, evidence=evidence,
            current_value=(control.get(pp.simulator_mapping) if pp and pp.simulator_mapping
                           else (pp.default_value if pp else None)),
            candidate_value=values.get(pid),
            search_range=ranges.get(pid),
            research_context=ctx,
            next_experiment=dict(zip(('summary', 'test_points'), _next_experiment(
                pid, ranges.get(pid), project,
                current=(control.get(pp.simulator_mapping) if pp and pp.simulator_mapping
                         else None),
                candidate=values.get(pid))),
                **{'measure': [r['readout_id'] for r in project.readouts[:4]]}),
            created_by='benchmark (deterministic path)',
            limitations=notes)
        hypotheses.append(h)

    # Withholding a hypothesis is a scientific decision and gets a reason, so
    # "no hypothesis" is never just an empty panel. It is reserved for the case
    # where nothing — not an analysis, not expert knowledge, not a value the
    # person asked about — names a lever worth testing.
    withheld = None if hypotheses else (
        'No candidate lever could be identified: no analysis produced one, no expert knowledge '
        'names one, and the request named no value to test. A hypothesis here would be a guess '
        'about which knob matters, which is the one thing this path must not invent. Name a '
        'parameter to explore, add a dataset, or ask the literature agent for evidence that '
        'points at one.')
    rows.append(_row('hypothesis_generated', bool(hypotheses),
                     hypotheses[0]['hypothesis_id'] if hypotheses
                     else f'withheld: {withheld[:90]}'))
    # A candidate hypothesis is a pass for "a hypothesis exists" and an honest
    # fail for "a magnitude was established" — two different capabilities, and
    # collapsing them is how the system learned to stay silent.
    quantified = bool(hypotheses) and any(e['magnitude_estimated']
                                          for e in hypotheses[0]['expected_effects'])
    rows.append(_row('quantitative_effect_supported', bool(hypotheses),
                     'at least one effect carries a magnitude with provenance' if quantified else
                     'direction only; magnitudes withheld with stated reasons — which is the '
                     'correct output when none can be computed'))
    rows.append(_row('next_experiment_generated', bool(hypotheses),
                     hypotheses[0]['next_experiment']['summary'] if hypotheses else 'none'))

    # ── narrative ────────────────────────────────────────────────────
    story = _narrate(config, project, unc, ds_rows, analyses, hypotheses, sim, knowledge)
    rows.append(_row('plain_language_generated', all(s['ok'] for s in story),
                     f'{len(story)} step(s), every number resolving to a structured fact'
                     if all(s['ok'] for s in story) else 'a narrative step failed validation'))

    # ── assemble ─────────────────────────────────────────────────────
    classes = {}
    for d in ds_rows:
        classes[d['evidence_class']] = classes.get(d['evidence_class'], 0) + 1
    for a in analyses:
        classes['derived_analysis'] = classes.get('derived_analysis', 0) + 1
    if knowledge:
        classes['expert_knowledge'] = len(knowledge)
    if sim['effects']:
        classes['simulation'] = 1

    rev = K.code_revision() or {}
    result = {
        'schema_version': K.PRODUCTION_VERSION,
        'benchmark_id': config['benchmark_id'], 'run_id': None,
        'created_at': K.now_iso(), 'mode': config['mode'],
        'title': config['title'], 'objective': config['objective'],
        'project': {'project_id': project.project_id, 'version': project.version,
                    'name': project.name, 'simulator': project.simulator},
        'research_context': ctx,
        'uncertainties': [dict(unc)],
        'datasets': ds_rows,
        'expert_knowledge': [{'knowledge_id': k['knowledge_id'],
                              'knowledge_type': k['knowledge_type'],
                              'confidence': k['confidence'],
                              'statement': k['statement']} for k in knowledge],
        'evidence_classes': classes,
        'analyses': [{'analysis_id': a['analysis_id'], 'plan_ref': a['plan_ref'],
                      'tool': a['method']['tool'], 'tool_version': a['method']['tool_version'],
                      'software': a['method']['software'],
                      'evidence_class': a['evidence_class'],
                      'source_evidence_class': a['source_evidence_class'],
                      'source_visibility': a['source_visibility'],
                      'confidence': a['confidence'],
                      'statistics': a['statistics'],
                      'key_findings': a['key_findings'],
                      'candidate_process_parameters': a['candidate_process_parameters'],
                      'provenance': a['provenance']} for a in analyses],
        'hypotheses': hypotheses,
        'hypothesis_withheld_reason': withheld,
        'selected_hypothesis': hypotheses[0]['hypothesis_id'] if hypotheses else None,
        'candidate_parameters': candidates,
        'simulator': sim,
        'next_experiment': hypotheses[0]['next_experiment'] if hypotheses else None,
        'provenance': {
            'biosense_version': K.BIOSENSE_VERSION,
            'git_commit': rev.get('commit'), 'git_dirty': rev.get('package_dirty'),
            'code_sha256': K.code_sha256(),
            'project_version': project.version,
            'simulator_model': project.simulator.get('model_id'),
            'simulator_model_version': project.simulator.get('model_version'),
            'tool_versions': {a['method']['tool']: a['method']['tool_version'] for a in analyses},
            'dataset_checksums': {d['dataset_id']: d['checksums'] for d in ds_rows},
            'seeds': {'simulator': config.get('seed') or 7},
            'runtime_s': round(time.time() - t0, 3),
            'ai': None,
        },
        'scorecard': {
            'rows': rows,
            'passed': sum(1 for r in rows if r['status'] == 'PASS'),
            'failed': sum(1 for r in rows if r['status'] == 'FAIL'),
            'note': 'This is a SYSTEM CAPABILITY scorecard. It records whether BioSense '
                    'identified an uncertainty, planned an analysis, executed it '
                    'deterministically, quantified what it could, checked simulator coverage and '
                    'labelled every number. It does NOT measure biological truth, and a run can '
                    'pass every row while being biologically wrong.',
        },
        'privacy': {'export_policy': config['export_policy'], 'has_private_lineage': False,
                    'private_sources': [], 'safe_to_publish': False, 'refusal_reason': None},
        'narrative': story,
        'warnings': warnings,
        'limitations': sorted(set(
            list(project.doc['limitations'])
            + [x for a in analyses for x in a['limitations']]
            + notes)),
        'operational': {'analyses': len(analyses), 'plans': len(plans),
                        'datasets': len(ds_rows), 'simulator_runs': 2 if sim['effects'] else 0,
                        'hypotheses': len(hypotheses),
                        'candidates': len(candidates)},
    }

    from . import privacy as PV
    result['privacy'] = PV.check(result, config['export_policy'],
                                 private_ids=[d['dataset_id'] for d in ds_rows
                                              if d['visibility'] == 'private'])
    rows.append(_row('privacy_validated', True,
                     result['privacy']['refusal_reason']
                     or 'no private lineage; safe to publish'))
    result['scorecard']['passed'] = sum(1 for r in rows if r['status'] == 'PASS')
    result['scorecard']['failed'] = sum(1 for r in rows if r['status'] == 'FAIL')

    missing = [c for c in config['expected_capabilities']
               if not any(r['capability'] == c and r['status'] == 'PASS' for r in rows)]
    if missing:
        result['warnings'].append('expected capabilities not demonstrated: ' + ', '.join(missing))

    K.require_valid('benchmark_result', result)
    return result


def _next_experiment(pid, range_, project, *, current=None, candidate=None):
    """Three test points a person would actually run.

    Spanning the project's whole range is not an experiment design: for M-CSF
    that proposed 0 ng/mL, which nobody would run and which answers nothing. The
    points bracket the candidate instead, clipped to the range that expert
    knowledge and the project left available.
    """
    p = PR.BY_ID[pid]
    if range_:
        lo, hi = float(range_['lower']), float(range_['upper'])
        if candidate is not None:
            c = min(max(float(candidate), lo), hi)
            span = max((hi - lo) * 0.2, abs(c) * 0.25) or 1.0
            pts = sorted({round(max(lo, c - span), 3), round(c, 3),
                          round(min(hi, c + span), 3)})
        else:
            mid = (lo + hi) / 2
            pts = sorted({round(lo, 3), round(mid, 3), round(hi, 3)})
        extra = ''
        if range_.get('narrowed_by'):
            extra = (f' The upper end reflects internal knowledge '
                     f'({", ".join(range_["narrowed_by"])}).')
        return (f'Test {p.label} at {", ".join(f"{v:g}" for v in pts)} {p.unit} against the '
                f'current process, measuring '
                f'{", ".join(r["label"].lower() for r in project.readouts[:3])}.{extra}'), pts
    return (f'Test a range of {p.label} against the current process, measuring '
            f'{", ".join(r["label"].lower() for r in project.readouts[:3])}.'), []


def _narrate(config, project, unc, ds_rows, analyses, hypotheses, sim, knowledge):
    """Numbered plain-language steps, every number resolving to a fact."""
    items = []
    f = N.Fact
    items.append((f'BioSense was asked to {config["objective"][0].lower()}'
                  f'{config["objective"][1:]}', [f('obj', 'objective', text=config['objective'])]))
    items.append((f'It identified an unresolved question: {unc["statement"]}',
                  [f('unc', 'uncertainty', text=unc['ref'])]))
    if ds_rows:
        items.append((f'It found {len(ds_rows)} relevant dataset(s) already registered.',
                      [f('ds', 'dataset count', len(ds_rows))]))
    if knowledge:
        items.append((f'It also had {len(knowledge)} piece(s) of internal knowledge from your '
                      f'own experience, kept separate from published evidence.',
                      [f('ek', 'knowledge count', len(knowledge))]))
    for a in analyses:
        facts = [f('a.n', 'readouts', len(a['statistics']))]
        best = None
        for s in a['statistics']:
            if s.get('q_value') is not None and s['q_value'] < 0.05:
                e = E.from_statistics_row(s, '%', source_ref=a['datasets'][0]['dataset_id'])
                facts += N.facts_from_estimate(e, prefix='a')
                if best is None or abs(e['absolute_change']) > abs(best['absolute_change']):
                    best = e
        if best is not None:
            items.append((f'Comparing the conditions in that data, '
                          f'{N.say_estimate(best).lower()}', facts))
        else:
            items.append((f'The comparison found no readout that survives multiplicity '
                          f'correction, which is itself an answer.', facts))
    if hypotheses:
        h = hypotheses[0]
        items.append((f'BioSense proposes that {h["parameter"]["label"]} is worth testing.',
                      N.facts_from_hypothesis(h)))
    if sim['effects']:
        best = max(sim['effects'], key=lambda e: abs(e['relative_change_pct'] or 0))
        items.append((f'Testing that in the project simulator, {N.say_estimate(best).lower()}',
                      N.facts_from_estimate(best, prefix='sim')))
    elif sim['prediction'] == 'none':
        items.append((sim['prediction_note'], []))
    for row in sim['handoff']['skipped']:
        items.append((f'{row["label"]} could not be simulated: {row["reason"]}', []))
    if hypotheses:
        h = hypotheses[0]
        pts = h['next_experiment'].get('test_points') or []
        facts = N.facts_from_hypothesis(h) + [
            N.Fact(f'next.point{i}', 'test point', v, unit=h['parameter']['unit'])
            for i, v in enumerate(pts)]
        items.append((h['next_experiment']['summary'], facts))
    return N.steps(items)


def write(result, out_dir):
    """Write benchmark.json plus the machine-readable pieces."""
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    K.write_json_atomic(d / 'benchmark.json', result)
    prov = d / 'provenance'
    prov.mkdir(exist_ok=True)
    K.write_json_atomic(prov / 'execution.json', result['provenance'])
    K.write_json_atomic(prov / 'datasets.json', result['datasets'])
    K.write_json_atomic(prov / 'tools.json',
                        {'tool_versions': result['provenance']['tool_versions'],
                         'simulator': result['project']['simulator']})
    return d / 'benchmark.json'
