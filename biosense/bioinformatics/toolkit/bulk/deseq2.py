"""DESeq2 through the external-tool adapter.

The in-process bulk tool refuses raw counts and says they need dispersion
modelling. This is where they go.

Nothing here runs R in-process, and nothing in the base install depends on R.
The work splits into three parts on purpose:

* `write_inputs` turns a long-format counts table into the two TSVs DESeq2 wants;
* `parse_results` turns the R script's output back into statistics rows;
* `run` joins them around one call to the external adapter.

The first two are pure functions over files, so the handoff that is easy to get
silently wrong — which count column belongs to which sample's condition — is
tested offline, on every machine, with no R anywhere. Only the middle step needs
Rscript, and when it is absent the tool is not registered at all, so a plan
naming it is refused when it is planned rather than part-way through.

A mock record is never evidence. `run` refuses one outright: the mock exists to
exercise the contract, and a result built from it would carry fold changes that
no model produced.
"""
from __future__ import annotations

import csv
import math
import tempfile
from pathlib import Path

import numpy as np

from .... import contracts as K
from ... import external as EXT
from ...toolkit import statistics as S

NAME = 'external.deseq2'
VERSION = '1.0.0'
MODALITIES = ('bulk_rna',)
REQUIRED_METADATA = ('condition_column', 'control', 'treatments', 'sample_id_column')
ANALYSIS_TYPES = ('bulk_expression_comparison', 'count_level_differential_expression')

SCRIPT = Path(__file__).resolve().parents[2] / "r" / "deseq2.R"

FEATURE_COLUMNS = ('gene', 'feature', 'symbol', 'gene_symbol', 'transcript')
COUNT_COLUMNS = ('count', 'counts', 'raw_count', 'raw_counts', 'n_reads', 'reads')


def _pick(table, candidates, what):
    low = {c.lower(): c for c in table.columns}
    for c in candidates:
        if c in low:
            return low[c]
    raise K.ContractError(
        f'no {what} column found. Looked for {", ".join(candidates)}; the table has '
        f'{", ".join(table.columns)}. Name it in the plan rather than letting this guess.')


def _design(manifest, plan):
    design = manifest['experimental_design']
    comp = plan.get('comparison') or {}
    cond = comp.get('group_column') or design.get('condition_column')
    control = comp.get('control') or design.get('control')
    treatment = comp.get('treatment') or (design.get('treatments') or [None])[0]
    sample = design.get('sample_id_column')
    if not (cond and control and treatment):
        raise K.ContractError('DESeq2 needs a condition column, a control level and a treatment '
                              'level; they are not inferred')
    if not sample:
        raise K.ContractError(
            'DESeq2 needs sample_id_column: the counts matrix has one column per sample, so '
            'which rows belong to the same library has to be stated rather than guessed. '
            'Technical replicates summed into the wrong sample would change every dispersion '
            'estimate in the model.')
    return cond, control, treatment, sample


def write_inputs(table, manifest, plan, out_dir):
    """Write counts.tsv and coldata.tsv, in one agreed sample order.

    Returns the two paths and the order. The order is returned rather than left
    implicit because it is the thing the R script checks before it models
    anything: if the count columns and the coldata rows ever disagree, every
    sample is labelled with another sample's condition.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cond, control, treatment, sample = _design(manifest, plan)
    for col in (cond, sample):
        if col not in table.columns:
            raise K.ContractError(f'{col!r} is not a column in the table. Present: '
                                  f'{", ".join(table.columns)}')

    feature = _pick(table, FEATURE_COLUMNS, 'feature')
    count = _pick(table, COUNT_COLUMNS, 'raw count')
    vals = table.numeric_column(count)

    # DESeq2 models counts. Anything already normalised breaks that assumption
    # silently: the dispersion estimates would be fitted to numbers whose
    # variance structure has already been changed.
    if not np.all(np.isclose(vals, np.round(vals))) or np.any(vals < 0):
        raise K.ContractError(
            f'column {count!r} is not raw integer counts. DESeq2 models counts and estimates '
            f'dispersion from them, so normalised or log-transformed values would be fitted by a '
            f'model whose assumptions they already break. Pass raw counts, or use '
            f'bulk.expression_comparison for a screening comparison of normalised expression.')

    sample_cond = {}
    for r in table.rows:
        s, c = str(r[sample]), str(r[cond])
        if sample_cond.setdefault(s, c) != c:
            raise K.ContractError(
                f'sample {s!r} appears under more than one condition; a sample is one library '
                f'and cannot be in both arms of the contrast')
    samples = [s for s in sample_cond if sample_cond[s] in (control, treatment)]
    if not samples:
        raise K.ContractError(f'no sample is in {control!r} or {treatment!r}')
    n_a = sum(1 for s in samples if sample_cond[s] == control)
    n_b = len(samples) - n_a
    if n_a < 2 or n_b < 2:
        raise K.ContractError(
            f'DESeq2 needs at least two samples per group; found {n_a} {control!r} and '
            f'{n_b} {treatment!r}. Sequencing deeper does not help: dispersion is estimated '
            f'across replicate libraries, and with one there is nothing to estimate it from.')

    features = table.levels(feature)
    matrix = {g: dict.fromkeys(samples, 0) for g in features}
    seen = set()
    for i, r in enumerate(table.rows):
        s = str(r[sample])
        if s not in matrix[str(r[feature])]:
            continue
        key = (str(r[feature]), s)
        if key in seen:
            raise K.ContractError(
                f'feature {key[0]!r} appears twice for sample {key[1]!r}. Summing them here would '
                f'invent a library; collapse technical replicates before ingest and record that '
                f'you did.')
        seen.add(key)
        matrix[str(r[feature])][s] = int(round(vals[i]))

    counts_p, coldata_p = out_dir / 'counts.tsv', out_dir / 'coldata.tsv'
    with counts_p.open('w', newline='') as fh:
        w = csv.writer(fh, delimiter='\t', lineterminator='\n')
        w.writerow(['feature'] + samples)
        for g in features:
            w.writerow([g] + [matrix[g][s] for s in samples])
    with coldata_p.open('w', newline='') as fh:
        w = csv.writer(fh, delimiter='\t', lineterminator='\n')
        w.writerow(['sample', cond])
        for s in samples:
            w.writerow([s, sample_cond[s]])
    return {'counts': counts_p, 'coldata': coldata_p, 'samples': samples,
            'feature_column': feature, 'count_column': count,
            'condition': cond, 'control': control, 'treatment': treatment,
            'n_control': n_a, 'n_treatment': n_b, 'n_features': len(features)}


def _num(x):
    if x is None or x == '' or x == 'NA' or x == 'NaN':
        return None
    v = float(x)
    return None if math.isnan(v) else v


def parse_results(path, *, control, treatment):
    """Read the R script's table into statistics rows.

    A feature DESeq2 filtered out comes back with q NA. It is dropped rather than
    given q=1: independent filtering removed it from the multiple-testing
    correction, so calling it a tested-and-not-significant result would both
    overstate how many features were tested and understate every other q.
    """
    rows, filtered = [], 0
    with Path(path).open(newline='') as fh:
        rdr = csv.DictReader(fh, delimiter='\t')
        need = {'feature', 'log2_fold_change', 'p_value', 'q_value'}
        missing = need - set(rdr.fieldnames or ())
        if missing:
            raise K.ContractError(f'DESeq2 output is missing {", ".join(sorted(missing))}; '
                                  f'got {", ".join(rdr.fieldnames or [])}')
        for r in rdr:
            q, p, lfc = _num(r['q_value']), _num(r['p_value']), _num(r['log2_fold_change'])
            if q is None or lfc is None:
                filtered += 1
                continue
            rows.append({
                'readout': r['feature'],
                'group_a': control, 'group_b': treatment,
                'effect': lfc, 'effect_type': 'log2_fold_change',
                'effect_ci_low': lfc - 1.96 * (_num(r.get('lfc_se')) or 0.0),
                'effect_ci_high': lfc + 1.96 * (_num(r.get('lfc_se')) or 0.0),
                'p_value': p, 'q_value': q,
                'test': 'DESeq2 Wald test, BH-FDR',
                'base_mean': _num(r.get('base_mean')),
                'shrinkage': r.get('shrinkage') or 'unknown',
            })
    if not rows:
        raise K.ContractError('DESeq2 returned no feature with an adjusted p-value')
    return rows, filtered


def run(table, manifest, plan, *, work_dir=None, i_have_execution_permission=True):
    """Run DESeq2 over the dataset and return an analysis result.

    Only reached when the adapter is available, because the tool is not
    registered otherwise.
    """
    work = Path(work_dir or tempfile.mkdtemp(prefix='biosense-deseq2-'))
    io = write_inputs(table, manifest, plan, work)
    out = work / 'deseq2_results.tsv'
    argv = ['Rscript', str(SCRIPT), str(io['counts']), str(io['coldata']),
            io['condition'], io['control'], io['treatment'], str(out)]
    rec = EXT.run_external(
        'deseq2', argv, inputs=[io['counts'], io['coldata']], outputs=[out],
        parameters={'condition': io['condition'], 'control': io['control'],
                    'treatment': io['treatment'], 'design': f'~ {io["condition"]}',
                    'script_sha256': K.sha256_file(SCRIPT)},
        i_have_execution_permission=i_have_execution_permission)
    if rec.get('mock'):
        raise K.ContractError(
            'refusing to build a result from a mock DESeq2 record. The mock exists to exercise '
            'the contract; every fold change in the table it would produce came from no model.')
    if rec['exit_status'] != 0:
        raise K.ContractError(f'DESeq2 exited {rec["exit_status"]}: '
                              f'{rec["stderr_tail"][-600:] or rec["stdout_tail"][-600:]}')

    rows, filtered = parse_results(out, control=io['control'], treatment=io['treatment'])
    n = min(io['n_control'], io['n_treatment'])
    checks = [
        {'check': 'input', 'status': 'PASS',
         'detail': f'raw counts from {io["count_column"]!r}; {io["n_features"]} features across '
                   f'{len(io["samples"])} samples'},
        {'check': 'sample mapping', 'status': 'PASS',
         'detail': 'counts columns and coldata rows were written in one order and the R script '
                   'refuses to model them if they disagree'},
        {'check': 'group sizes', 'status': 'PASS' if n >= 3 else 'WARN',
         'detail': f'{io["control"]}: {io["n_control"]}; {io["treatment"]}: {io["n_treatment"]}'},
        {'check': 'independent filtering', 'status': 'PASS',
         'detail': f'{filtered} feature(s) returned no adjusted p-value and were dropped rather '
                   f'than counted as tested and not significant'},
        {'check': 'shrinkage', 'status': 'PASS',
         'detail': f'log2 fold changes: {rows[0]["shrinkage"]} shrinkage'},
        {'check': 'external execution', 'status': 'PASS',
         'detail': f'{rec["tool"]} {rec["tool_version"]} as a subprocess, '
                   f'exit {rec["exit_status"]} in {rec["duration_s"]}s'},
    ]
    return {
        'statistics': rows,
        'quality_control': {'checks': checks, 'passed': True},
        'comparison': {'group_column': io['condition'], 'control': io['control'],
                       'treatment': io['treatment'], 'paired': False,
                       'feature_column': io['feature_column'],
                       'value_column': io['count_column'],
                       'features_tested': len(rows), 'features_filtered': filtered,
                       'samples': io['samples']},
        'software': f'DESeq2 {rec["tool_version"]} via Rscript (external-tool adapter)',
        'external_execution': rec,
        'independent_units_min': n,
        'replicate_n_min': n,
    }
