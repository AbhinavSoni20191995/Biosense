"""The concise scientific summary, and the detailed audit bundle beside it.

Two outputs, deliberately separate:

    summary.md / report.html   decision-oriented, 1-3 pages. What were we trying
                               to improve, what was looked at, what it showed,
                               what is proposed, what it would cost, what to test.
    the bundle                 reproducibility: manifests, plans, results,
                               statistics, provenance, figures, tables.

The default is the short one. A twenty-page technical dump is not a report; it
is a refusal to decide what matters. Everything a reader needs to audit is one
directory away and linked from the summary.

Every number in the summary carries the label it earned — MEASURED, DERIVED,
SIMULATED, PREDICTED, EXPERT KNOWLEDGE — because a reader skimming a table of
improvements will otherwise take all of them for measurements.
"""
from __future__ import annotations

import csv
from pathlib import Path

from .. import contracts as K
from ..evidence import estimates as E

LABEL = {'measured': 'MEASURED', 'derived': 'DERIVED', 'simulated': 'SIMULATED',
         'predicted': 'PREDICTED', 'target': 'TARGET',
         'judgement': 'BEST GUESS'}
CLASS_LABEL = {'published_literature': 'PUBLISHED', 'public_dataset': 'PUBLIC DATA',
               'private_user_dataset': 'PRIVATE DATA', 'expert_knowledge': 'EXPERT KNOWLEDGE',
               'derived_analysis': 'DERIVED ANALYSIS', 'simulation': 'SIMULATION',
               'real_measurement': 'REAL MEASUREMENT', 'synthetic_fixture': 'SYNTHETIC FIXTURE'}


def _effect_line(e):
    label = e.get('label') or e['metric'].replace('_', ' ')
    tag = LABEL[e['estimate_type']]
    if not e['magnitude_estimated']:
        return (f'| {label} | direction {e["direction"]} | magnitude not yet estimated | '
                f'{tag} |')
    a = (e.get('baseline') or {}).get('value')
    b = (e.get('candidate') or {}).get('value')
    unit = '%' if E.is_percent(e['unit']) else f' {e["unit"]}'
    chg = (f'{e["absolute_change"]:+.4g} pp' if e['change_unit'] == E.PP
           else f'{e["absolute_change"]:+.4g}{unit}')
    rel = f' ({e["relative_change_pct"]:+.4g}%)' if e['relative_change_pct'] is not None else ''
    return f'| {label} | {a:.4g}{unit} → {b:.4g}{unit} | {chg}{rel} | {tag} |'


def summary_markdown(result):
    """The default report: what a scientist needs to decide what to do next."""
    r = result
    p = r['project']
    h = r['hypotheses'][0] if r['hypotheses'] else None
    sim = r['simulator']
    L = [f'# {r["title"]}', '',
         '> **SYNTHETIC DEMONSTRATION.**' if _is_synthetic(r) else '> **BioSense discovery run.**',
         f'> Project `{p["project_id"]}` v{p["version"]} · BioSense '
         f'{r["provenance"]["biosense_version"]} · '
         f'{"commit " + r["provenance"]["git_commit"][:10] if r["provenance"].get("git_commit") else "no commit recorded"}',
         '']

    L += ['## What we were trying to improve', '', r['objective'], '']

    ctx = r.get('research_context')
    if ctx:
        from ..evidence import context as CTX
        L += ['## Research context', '', CTX.describe(ctx), '']

    L += ['## The question that was blocking a decision', '']
    for u in r['uncertainties']:
        L += [f'**`{u["ref"]}`** — {u["statement"]}', '']

    L += ['## Evidence considered', '', '| Class | Count |', '|---|---|']
    for cls, n in sorted(r['evidence_classes'].items(), key=lambda x: -x[1]):
        L.append(f'| {CLASS_LABEL.get(cls, cls)} | {n} |')
    L.append('')
    for d in r['datasets']:
        note = (f' — **CONTEXT MISMATCH**: {d["context_note"]}'
                if d.get('context_match') == 'context_mismatch' else '')
        L.append(f'- `{d["dataset_id"]}` {d["title"]} '
                 f'({CLASS_LABEL.get(d["evidence_class"], d["evidence_class"])},'
                 f' {d["visibility"].upper()}){note}')
    for k in r.get('expert_knowledge') or []:
        L.append(f'- **EXPERT KNOWLEDGE** `{k["knowledge_id"]}` ({k["knowledge_type"]}): '
                 f'{k["statement"]}')
    L.append('')

    if r['analyses']:
        L += ['## Analysis performed', '']
        for a in r['analyses']:
            L.append(f'`{a["tool"]}` v{a["tool_version"]} — `{a["analysis_id"]}` '
                     f'({a["software"]})')
            for f in a['key_findings'][:3]:
                L.append(f'- {f["finding"]}  \n  *{f["basis"]}*')
            L.append('')

    if h:
        L += ['## Current hypothesis', '', h['statement'], '',
              f'**Confidence: {h["confidence"]}.** ' + ' '.join(h['confidence_basis']), '',
              '### Quantified effects', '',
              '| Outcome | Baseline → Candidate | Change | Provenance |', '|---|---|---|---|']
        L += [_effect_line(e) for e in h['expected_effects']]
        L.append('')
        t = h['trade_offs'][0]
        L += [f'**Trade-off.** {t["summary"]}', '']

        par = h['parameter']
        L += ['### Candidate parameter', '',
              f'`{par["parameter_id"]}` ({par["label"]}) — **{par["direction"]}**']
        if par.get('current_value') is not None and par.get('candidate_value') is not None:
            L.append(f'· {par["current_value"]:g} → {par["candidate_value"]:g} {par["unit"]}')
        if par.get('search_range'):
            sr = par['search_range']
            narrowed = (f' (narrowed by {", ".join(sr["narrowed_by"])})'
                        if sr.get('narrowed_by') else '')
            L.append(f'· search range {sr["lower"]:g}–{sr["upper"]:g} {par["unit"]}{narrowed}')
        if par['simulator_coverage'] != 'modelled':
            L.append(f'· **SIMULATOR COVERAGE: {par["simulator_coverage"].upper()}** — '
                     f'{par.get("coverage_note")}')
        L.append('')

    L += ['## Simulator prediction', '']
    if sim.get('effects'):
        changed = ' · '.join(f'{x["label"]} {x["from"]:g} → {x["to"]:g} {x["unit"]}'
                             for x in sim['candidate']['changed'])
        L += [f'Model `{sim["model"]["model_id"]}` v{sim["model"].get("model_version")} — '
              f'{changed}', '',
              '| Outcome | Control → Candidate | Change | Provenance |', '|---|---|---|---|']
        L += [_effect_line(e) for e in sim['effects']]
        L += ['', f'*{sim["prediction_note"]}*', '']
    else:
        L += [sim['prediction_note'], '']
    for row in sim['handoff']['skipped']:
        L.append(f'- **NOT MODELLED** `{row["parameter_id"]}`: {row["reason"]}')
    L.append('')

    if r.get('next_experiment'):
        L += ['## Recommended next experiment', '', r['next_experiment']['summary'], '']

    L += ['## How BioSense got here', '']
    L += [f'{s["step"]}. {s["text"]}' for s in r['narrative']]
    L.append('')

    L += ['## Main limitations', '']
    L += [f'- {x}' for x in r['limitations'][:8]]
    L.append('')

    sc = r['scorecard']
    L += ['## Capability scorecard', '',
          f'**{sc["passed"]} PASS / {sc["failed"]} FAIL**', '',
          '| Capability | Status |', '|---|---|']
    L += [f'| {x["capability"].replace("_", " ")} | {x["status"]} |' for x in sc['rows']]
    L += ['', f'*{sc["note"]}*', '']

    pv = r['privacy']
    L += ['## Privacy', '',
          f'- export policy: **{pv["export_policy"]}**',
          f'- private lineage: {"yes — " + ", ".join(pv["private_sources"]) if pv["has_private_lineage"] else "none"}',
          f'- safe to publish: **{"yes" if pv["safe_to_publish"] else "no"}**']
    if pv.get('refusal_reason'):
        L.append(f'- {pv["refusal_reason"]}')
    L.append('')
    return '\n'.join(L)


def _is_synthetic(result):
    return any(d['evidence_class'] == 'synthetic_fixture' for d in result['datasets']) or \
        (result['project']['simulator'] or {}).get('evidence_status') == 'synthetic_demonstration'


def write_tables(result, out_dir):
    """CSV tables for a reader who wants the numbers in a spreadsheet."""
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    written = []

    rows = []
    for a in result['analyses']:
        for s in a['statistics']:
            rows.append({'analysis_id': a['analysis_id'], 'readout': s['readout'],
                         'group_a': s.get('group_a'), 'group_b': s.get('group_b'),
                         'mean_a': s.get('mean_a'), 'mean_b': s.get('mean_b'),
                         'effect': s.get('effect'), 'effect_type': s.get('effect_type'),
                         'p_value': s.get('p_value'), 'q_value': s.get('q_value'),
                         'n_a': s.get('n_a'), 'n_b': s.get('n_b'), 'test': s.get('test')})
    if rows:
        written.append(_csv(d / 'analysis_statistics.csv', rows))

    rows = []
    for h in result['hypotheses']:
        for e in h['expected_effects']:
            rows.append({'hypothesis_id': h['hypothesis_id'], 'metric': e['metric'],
                         'unit': e['unit'], 'estimate_type': e['estimate_type'],
                         'baseline': (e.get('baseline') or {}).get('value'),
                         'candidate': (e.get('candidate') or {}).get('value'),
                         'absolute_change': e['absolute_change'],
                         'change_unit': e['change_unit'],
                         'relative_change_pct': e['relative_change_pct'],
                         'magnitude_estimated': e['magnitude_estimated'],
                         'withheld_reason': e['withheld_reason']})
    if rows:
        written.append(_csv(d / 'hypothesis_effects.csv', rows))

    rows = [{'parameter_id': c['parameter'], 'direction': c.get('direction'),
             'confidence': c.get('confidence'),
             'simulator_coverage': next(
                 (x['simulator_coverage'] for x in
                  result['simulator']['handoff']['applied'] +
                  result['simulator']['handoff']['skipped']
                  if x['parameter_id'] == c['parameter']), 'unknown'),
             'basis': (c.get('basis') or '')[:300]}
            for c in result['candidate_parameters']]
    if rows:
        written.append(_csv(d / 'candidate_parameters.csv', rows))

    rows = [{'capability': x['capability'], 'status': x['status'], 'detail': x['detail']}
            for x in result['scorecard']['rows']]
    written.append(_csv(d / 'scorecard.csv', rows))
    return written


def _csv(path, rows):
    with Path(path).open('w', newline='', encoding='utf-8') as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    return path


def write_detailed(result, out_dir, *, plans=(), analyses=()):
    """The audit package: everything the summary deliberately leaves out."""
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    K.write_json_atomic(d / 'hypotheses.json', result['hypotheses'])
    K.write_json_atomic(d / 'analyses.json', result['analyses'])
    K.write_json_atomic(d / 'simulator.json', result['simulator'])
    K.write_json_atomic(d / 'scorecard.json', result['scorecard'])
    K.write_json_atomic(d / 'narrative.json', result['narrative'])
    if plans:
        K.write_json_atomic(d / 'analysis_plans.json', list(plans))
    return d
