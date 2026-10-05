"""Comparing processed flow-cytometry population tables between two conditions.

Phase 1 reads **gated, processed** tables: one row per sample, columns of
population frequencies, marker intensities and viability. Raw FCS parsing,
automated gating, FlowSOM and UMAP are Phase 2 and are not approximated here —
an FCS file can be described in a manifest and this tool refuses it.

Two things it does that a generic t-test does not:

* **It counts independent units, not rows.** If the manifest names a donor
  column, n is the number of donors, and the row says so. Four wells from one
  donor are one donor's worth of evidence.
* **It separates what improved from what it cost.** Identity and frequency
  readouts speak to the product; viability and stress readouts bound how far a
  parameter may be pushed. Mixing them is how "purity improved" gets reported
  about a culture that was dying.
"""
from __future__ import annotations

import numpy as np

from .... import contracts as K
from ...toolkit import statistics as S

NAME = 'cytometry.population_comparison'
VERSION = '1.0.0'
MODALITIES = ('flow_cytometry', 'cytometry_summary')
REQUIRED_METADATA = ('condition_column', 'control', 'treatments')
ANALYSIS_TYPES = ('population_comparison', 'marker_intensity_comparison', 'viability_comparison')

# Columns that describe the sample rather than measure it.
NON_READOUT = ('sample_id', 'replicate', 'donor', 'batch', 'timepoint', 'condition',
               'events_acquired', 'well', 'plate', 'tube')


def readouts_for(table, design, requested=()):
    if requested:
        missing = [c for c in requested if c not in table.columns]
        if missing:
            raise K.ContractError(f'readout(s) {", ".join(missing)} are not columns of this table')
        return list(requested)
    skip = {(design.get(k) or '').lower() for k in
            ('condition_column', 'replicate_column', 'batch_column', 'sample_id_column',
             'timepoint_column', 'donor_column')}
    skip |= set(NON_READOUT)
    return [c for c in table.columns if c in table.numeric and c.lower() not in skip]


def unit_column(design):
    """The column that identifies an independent experimental unit, if any.

    Donor outranks replicate: two wells from one person are one person's worth of
    evidence, however they are labelled.
    """
    for key, label in (('donor_column', 'donors'), ('replicate_column', 'replicates')):
        col = design.get(key)
        if col:
            return col, label
    return None, None


def aggregate_units(table, design, rows_idx, readout):
    """One value per independent unit, and what that unit is.

    Where the manifest names a donor, several wells from that donor are averaged
    into one observation before any test runs. Testing the wells instead inflates
    n by the number of times each person was sampled, which makes a p-value small
    for a reason that has nothing to do with biology. The aggregation is reported,
    not silent.
    """
    vals = table.numeric_column(readout)
    col, label = unit_column(design)
    if col is None:
        return (np.asarray([vals[i] for i in rows_idx], dtype=float),
                len(rows_idx),
                'n counted as rows: the manifest names no donor or replicate column, so these '
                'may not be independent observations')
    groups = {}
    for i in rows_idx:
        groups.setdefault(table.rows[i][col], []).append(vals[i])
    means = np.asarray([float(np.mean(v)) for v in groups.values()], dtype=float)
    pooled = any(len(v) > 1 for v in groups.values())
    note = f'n counted as {len(groups)} {label} ({col})'
    if pooled:
        note += (f'; {len(rows_idx)} rows averaged within each {label[:-1]} first, so repeated '
                 f'wells from one {label[:-1]} count once')
    return means, len(groups), note


def run(table, manifest, plan):
    """Compare the plan's treatment against its control. Returns a result fragment."""
    design = manifest['experimental_design']
    comp = plan.get('comparison') or {}
    cond = comp.get('group_column') or design.get('condition_column')
    control = comp.get('control') or design.get('control')
    treatment = comp.get('treatment') or (design.get('treatments') or [None])[0]
    if not (cond and control and treatment):
        raise K.ContractError(
            'a population comparison needs a condition column, a control level and a treatment '
            'level. Supply them in the manifest or the plan; they are not inferred.')

    a_idx, b_idx = table.where(cond, control), table.where(cond, treatment)
    if not a_idx or not b_idx:
        raise K.ContractError(
            f'column {cond!r} has no rows for '
            f'{control!r} ({len(a_idx)}) or {treatment!r} ({len(b_idx)}). '
            f'Values present: {", ".join(map(str, table.levels(cond)))}')

    readouts = readouts_for(table, design, comp.get('readouts') or ())
    if not readouts:
        raise K.ContractError('no numeric readout columns to compare after excluding the design '
                              'columns; name them explicitly in comparison.readouts')

    rows, n_a, n_b, indep = [], None, None, ''
    for r in readouts:
        a, n_a, note_a = aggregate_units(table, design, a_idx, r)
        b, n_b, note_b = aggregate_units(table, design, b_idx, r)
        indep = f'{note_a} for {control}; {note_b} for {treatment}'
        rows.append(S.compare_groups(
            r, a, b, group_a=control, group_b=treatment,
            paired=bool(comp.get('paired') or design.get('paired')),
            effect_type='difference', independence_note=indep))
    S.apply_fdr(rows)

    checks = [
        {'check': 'group sizes', 'status': 'PASS' if min(n_a, n_b) >= 3 else 'WARN',
         'detail': f'{control}: {n_a} independent unit(s); {treatment}: {n_b}. {indep}'},
        {'check': 'readout range',
         'status': 'PASS' if all(_in_percent_range(table.numeric_column(r)) or
                                 not _looks_percentage(r) for r in readouts) else 'WARN',
         'detail': 'columns named as percentages fall within 0-100'
                   if all(_in_percent_range(table.numeric_column(r)) or not _looks_percentage(r)
                          for r in readouts)
                   else 'a column named as a percentage holds values outside 0-100; check the units'},
        {'check': 'multiplicity', 'status': 'PASS',
         'detail': f'Benjamini-Hochberg applied across {len(rows)} readouts in this result'},
    ]
    if any(f['file_type'] == 'fcs' for f in manifest['files']):
        checks.append({'check': 'raw events', 'status': 'WARN',
                       'detail': 'this dataset also carries FCS files; they were not read. Only '
                                 'the processed table was analysed.'})
    return {
        'statistics': rows,
        'quality_control': {'checks': checks,
                            'passed': all(c['status'] != 'FAIL' for c in checks)},
        'comparison': {'group_column': cond, 'control': control, 'treatment': treatment,
                       'paired': bool(comp.get('paired') or design.get('paired')),
                       'readouts': readouts, 'independent_units': {control: n_a, treatment: n_b},
                       'independence_note': indep},
        'software': S.SOFTWARE,
        'independent_units_min': min(n_a, n_b),
        'replicate_n_min': min(len(a_idx), len(b_idx)),
    }


def _looks_percentage(name):
    n = name.lower()
    return 'pct' in n or 'percent' in n or n.endswith('_%')


def _in_percent_range(v):
    return bool(np.all((v >= 0) & (v <= 100)))
