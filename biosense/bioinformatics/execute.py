"""Executing an AnalysisPlan and writing the AnalysisResult.

The division of labour this module holds:

    the plan says what question is being asked and against which uncertainty
    a registered tool computes the numbers
    this module records what was computed, over what, by what, and what it is not

Provenance is assembled here rather than by the tool, so every result carries the
same chain whichever tool ran: dataset ids, the sha256 of every input file, the
plan hash, the package hash and the git revision.

The lineage rule the whole design turns on: **a computed result is always
`derived_analysis`.** What it was computed FROM lives in `source_evidence_class`
and `source_visibility`. An analysis of a private FACS table is therefore a
derived analysis with a private source, never "a private dataset", and that
distinction survives into the interface and into any later synthesis.

Nothing here can change a protocol. The result carries candidate levers; the
orchestrator weighs them against everything else, and a revise_protocol decision
still has to pass the envelope.
"""
from __future__ import annotations

import time
from pathlib import Path

from .. import contracts as K
from ..data import manifest as MF
from ..data import registry as DREG
from ..data import tables as TB
from . import implications as IMP
from . import plan as PLAN
from . import registry as TREG


def execute(p, *, dirs=None, executed_by='bioinformatics_agent', analysis_id=None,
            external_record=None, out=None):
    """Run a validated plan. Returns the AnalysisResult."""
    K.require_valid('analysis_plan', p)
    t0 = time.time()
    manifests = [DREG.require(d, dirs=dirs) for d in p['dataset_ids']]
    PLAN.check_ready(p, manifests)
    spec = TREG.get(p['tool']['name'])

    if external_record is not None and external_record.get('mock'):
        raise K.ContractError(
            'refusing to build an AnalysisResult from a mock external execution. The adapter '
            'contract may be exercised with a mock; evidence may not be produced from one.')

    # Phase 1 analyses one dataset at a time. Combining two is a real design
    # question (batch, platform, normalisation) and refusing is better than
    # concatenating two tables and calling the result a comparison.
    if len(manifests) > 1:
        raise K.ContractError(
            'this version analyses one dataset per plan. Combining datasets needs batch handling '
            'and a shared normalisation that nothing here does; run them separately and compare '
            'the results.')
    m = manifests[0]
    readable = MF.readable_files(m)
    if not readable:
        raise K.ContractError(f'{m["dataset_id"]} has no readable table')
    f = readable[0]
    table = TB.read_table(MF.resolve_path(f['path']), f['file_type'])

    frag = spec.runner(table, m, p)
    rows = frag['statistics']

    src_class, src_vis = MF.combined_source(manifests)
    confidence, conf_reasons = IMP.confidence_for(
        rows, source_visibility=src_vis,
        replicate_n=frag.get('replicate_n_min'),
        independent_units=frag.get('independent_units_min'))
    levers, no_lever_reason = IMP.candidate_parameters(rows, m, confidence=confidence)

    findings, implications = _narrate(rows, m, frag, levers, no_lever_reason)

    lims = list(p.get('limitations') or [])
    lims.append(spec.notes)
    lims.append('One dataset, one comparison. A difference here is directional evidence about '
                'this experiment, not a demonstration that the same change helps your process.')
    if src_vis == 'private':
        lims.append('Computed from private user-provided data. It may support or contradict a '
                    'hypothesis and may inform a parameter decision, and it can never become a '
                    'literature citation or leave this machine.')
    if m.get('source') == 'user_upload':
        lims.append('BioSense did not generate these measurements and cannot verify how they '
                    'were produced, gated or normalised.')
    lims += [r for r in conf_reasons]

    result = {
        'schema_version': K.PRODUCTION_VERSION,
        'analysis_id': analysis_id or f'analysis-{p["plan_id"]}',
        'created_at': K.now_iso(),
        'plan_ref': p['plan_id'], 'loop_id': p.get('loop_id'), 'iteration': p.get('iteration'),
        'question': p['question'], 'uncertainty_ref': dict(p['uncertainty_ref']),

        'evidence_class': 'derived_analysis',
        'source_evidence_class': src_class,
        'source_visibility': src_vis,
        'parent_dataset_ids': [x['dataset_id'] for x in manifests],

        'datasets': [{
            'dataset_id': x['dataset_id'], 'title': x['title'], 'source': x['source'],
            'accession': x.get('accession'), 'visibility': x['visibility'],
            'evidence_class': x['evidence_class'],
            'checksums': [ff['checksum_sha256'] for ff in x['files']],
        } for x in manifests],

        'method': {
            'tool': spec.name, 'tool_version': spec.version, 'software': frag['software'],
            'analysis_type': p['analysis_type'],
            'parameters': dict(p['tool'].get('parameters') or {}),
            'external_execution': external_record,
        },
        'comparison': frag.get('comparison'),
        'quality_control': frag['quality_control'],
        'statistics': rows,
        'key_findings': findings,
        'process_implications': implications,
        'candidate_process_parameters': levers,
        'confidence': confidence,
        'decision_relevance': p['decision_relevance'],
        'citable': False,
        'provenance': {
            'executed_at': K.now_iso(), 'executed_by': executed_by,
            'code_sha256': K.code_sha256(), 'code_revision': K.code_revision(),
            'input_checksums': sorted({ff['checksum_sha256'] for x in manifests
                                       for ff in x['files']}),
            'plan_sha256': K.sha256_obj(p),
            'duration_s': round(time.time() - t0, 3),
        },
        'limitations': sorted(set(lims)),
    }
    K.require_valid('analysis_result', result)
    if out:
        K.write_json_atomic(Path(out), result, overwrite=False)
    return result


def _narrate(rows, m, frag, levers, no_lever_reason):
    """Findings and process implications, each tied to the number behind it.

    Wording is constrained on purpose. A finding states what moved and by how
    much; an implication states what that *could* mean and what else could
    explain it. Neither says a parameter should change.
    """
    signif = [r for r in rows if r.get('q_value') is not None and r['q_value'] < 0.05]
    signif.sort(key=lambda r: abs(r.get('effect') or 0), reverse=True)
    comp = frag.get('comparison') or {}
    a, b = comp.get('control'), comp.get('treatment')

    findings = []
    for r in signif[:8]:
        unit = '' if r['effect_type'].startswith('difference_in_log2') else ''
        findings.append({
            'finding': (f'{r["readout"]}: {r["mean_a"]:.4g} in {a} vs {r["mean_b"]:.4g} in {b} '
                        f'({"+" if (r["effect"] or 0) > 0 else ""}{r["effect"]:.4g}{unit})'),
            'basis': (f'{r["test"]}, p={r["p_value"]:.3g}, BH-q={r["q_value"]:.3g}, '
                      f'n={r["n_a"]} vs {r["n_b"]}'
                      + (f', 95% CI [{r["ci_low"]:.3g}, {r["ci_high"]:.3g}]'
                         if r.get('ci_low') is not None else '')),
        })
    if not findings:
        findings.append({
            'finding': f'No readout differs between {a} and {b} beyond what multiplicity '
                       f'correction tolerates.',
            'basis': f'{len(rows)} readouts tested, smallest BH-q = '
                     f'{min((r["q_value"] for r in rows if r.get("q_value") is not None), default=float("nan")):.3g}',
        })

    alternatives = [
        'batch or handling differences between the groups, if they were not processed together',
        'gating or compensation differences, which this analysis cannot see from a processed table',
        'a difference in cell type, stage or culture format between this dataset and the process '
        'being optimised',
    ]
    if not (m.get('experimental_design') or {}).get('donor_column'):
        alternatives.append('non-independent replicates: the manifest names no donor column, so '
                            'the observations may come from fewer biological units than n suggests')

    implications = []
    if levers:
        lv = levers[0]
        implications.append({
            'implication': (f'The measured difference is consistent with {lv["parameter"]} '
                            f'affecting the readouts that moved. It identifies {lv["parameter"]} '
                            f'as worth testing in the process; it does not establish that '
                            f'changing it will help, or by how much.'),
            'rests_on': lv['basis'],
            'alternative_explanations': alternatives,
        })
    elif no_lever_reason:
        implications.append({
            'implication': 'No process parameter is implicated by this result.',
            'rests_on': no_lever_reason,
            'alternative_explanations': alternatives,
        })
    harms = [r for r in signif if IMP.readout_kind(r['readout'])[0] in ('viability', 'stress')]
    for r in harms:
        implications.append({
            'implication': f'{r["readout"]} also moved ({r["effect"]:+.4g}), which constrains how '
                           f'far the condition can be pushed regardless of any gain.',
            'rests_on': f'{r["test"]}, BH-q={r["q_value"]:.3g}',
            'alternative_explanations': alternatives,
        })
    return findings, implications


def render(result):
    """Markdown for the loop record and the orchestrator."""
    r = result
    L = [f'# Analysis {r["analysis_id"]}', '',
         f'**Question.** {r["question"]}', '',
         f'**Uncertainty it addresses.** `{r["uncertainty_ref"]["ref"]}` '
         f'({r["uncertainty_ref"]["kind"]}): {r["uncertainty_ref"]["statement"]}', '',
         f'**Evidence class.** {r["evidence_class"]} · source: {r["source_evidence_class"]} · '
         f'visibility: {r["source_visibility"]} · confidence: {r["confidence"]}', '',
         '**Datasets.**', '']
    for d in r['datasets']:
        L.append(f'- `{d["dataset_id"]}` — {d["title"]} ({d["source"]}'
                 + (f', {d["accession"]}' if d.get('accession') else '')
                 + f', {d["visibility"].upper()})')
    L += ['', f'**Method.** {r["method"]["tool"]} v{r["method"]["tool_version"]} · '
              f'{r["method"]["software"]}', '']
    L += ['| Readout | ' + (r['comparison'] or {}).get('control', 'A') + ' | '
          + (r['comparison'] or {}).get('treatment', 'B') + ' | Effect | p | BH-q |',
          '|---|---|---|---|---|---|']
    for s in r['statistics'][:25]:
        L.append(f'| {s["readout"]} | {_n(s["mean_a"])} | {_n(s["mean_b"])} | {_n(s["effect"])} | '
                 f'{_n(s["p_value"])} | {_n(s["q_value"])} |')
    L += ['', '## Findings', '']
    L += [f'- {f["finding"]}  \n  _{f["basis"]}_' for f in r['key_findings']]
    L += ['', '## Process implications', '']
    for i in r['process_implications']:
        L.append(f'- {i["implication"]}  \n  _Rests on: {i["rests_on"]}_')
    L += ['', '## Candidate process parameters', '']
    if r['candidate_process_parameters']:
        L += ['| Parameter | Direction | Confidence | Basis |', '|---|---|---|---|']
        L += [f'| `{c["parameter"]}` | {c["direction"]} | {c["confidence"]} | {c["basis"]} |'
              for c in r['candidate_process_parameters']]
        L += ['', 'Candidates only. This result cannot change a protocol: the orchestrator weighs '
                  'it with every other source, and a revision still has to pass the envelope.', '']
    else:
        L += ['None implicated by this result.', '']
    L += ['## Limitations', ''] + [f'- {x}' for x in r['limitations']] + ['']
    return '\n'.join(L)


def _n(v, nd=4):
    return '—' if v is None else f'{v:.{nd}g}'
