"""Protein or analyte abundance between two conditions, from a processed table.

What it reads: a table someone else already quantified. A MaxQuant, DIA-NN or
Spectronaut export reduced to protein x sample, an Olink NPX table, or a
secretome / cytokine panel (Luminex, MSD) of concentrations. Two shapes are
accepted, and which one was read is stated in the QC:

* **long** — one row per protein and sample: a feature column (protein,
  protein_id, gene, analyte, uniprot, assay ...), the sample column, the
  condition column and a value column;
* **wide** — one row per sample, the condition and sample columns, and one
  numeric column per analyte (the usual cytokine-panel layout). Every numeric
  column that is not a design column is an analyte; the plan's readouts may
  restrict them.

What it does: decides whether the values are already on a log scale (NPX, log2
LFQ) or linear (intensities, concentrations), log2-transforms linear values
once, and compares the two conditions per protein with Welch's t-test, BH-FDR
across the proteins tested. The effect is a difference in mean log2 abundance.

What it is **not**:

* not a replacement for limma, MSstats or DEqMS — no moderated variance, no
  peptide- or precursor-level model, no peptide-to-protein rollup;
* **no imputation.** A missing value stays missing. A protein seen in one
  condition and absent from the other is listed as such, without a fold change,
  because any number for "absent" would be invented;
* no normalisation beyond whatever the depositor applied. The tool cannot tell
  whether a table was normalised and does not try;
* not a reader of raw spectra or of a panel's standard curves.
"""
from __future__ import annotations

import re
from collections import Counter

import numpy as np

from .... import contracts as K
from ....data import tables as TB
from ...toolkit import options as OPT
from ...toolkit import statistics as S
from ..bulk import basic_de as DE

NAME = 'proteomics.abundance_comparison'
VERSION = '1.0.0'
MODALITIES = ('proteomics', 'secretome', 'generic_table')
REQUIRED_METADATA = ('condition_column', 'control', 'treatments')
ANALYSIS_TYPES = ('protein_abundance_comparison',)

DEFAULTS = {'scale': 'auto', 'min_valid': 2}
SCALES = ('auto', 'log', 'linear')

FEATURE_COLUMNS = ('protein', 'protein_id', 'protein_ids', 'protein_group', 'protein.group',
                   'protein_groups', 'majority_protein_ids', 'uniprot', 'uniprot_id',
                   'accession', 'gene', 'gene_name', 'gene_names', 'genes', 'analyte', 'assay',
                   'olinkid', 'feature')
VALUE_COLUMNS = ('log2_abundance', 'log2_intensity', 'log2_lfq', 'lfq_log2', 'npx',
                 'log2_quantity', 'abundance', 'lfq_intensity', 'lfq', 'intensity',
                 'quantity', 'concentration', 'value')
SAMPLE_COLUMNS = ('sample_id', 'sample', 'sampleid')
# Columns that describe a sample rather than measure it, in a wide table.
NON_ANALYTE = ('sample_id', 'sample', 'sampleid', 'replicate', 'donor', 'batch', 'timepoint',
               'time', 'condition', 'well', 'plate', 'run', 'dilution', 'tube')
DESIGN_KEYS = ('condition_column', 'replicate_column', 'batch_column', 'sample_id_column',
               'timepoint_column', 'donor_column')

# A log2 abundance above this would be 2**64 on the linear scale; nothing
# measured is that large, so a value above it is read as linear.
LOG_MAX = 64.0
LOG_HINTS = ('log', 'npx')
MAX_FEATURES = 10_000
# Below/above-range markers written by panel software. Read as missing, counted.
CENSORED = re.compile(r'^\s*(<|>|oor|out of range|below|above|blq|alq|nd\b|n\.d\.)'
                      r'|lod|lloq|uloq', re.I)


def run(table, manifest, plan):
    opt = OPT.take(plan, DEFAULTS)
    scale_opt = str(opt['scale']).strip().lower()
    if scale_opt not in SCALES:
        raise K.ContractError(f'option scale={opt["scale"]!r}; it is one of {", ".join(SCALES)}')
    min_valid = opt['min_valid']
    if isinstance(min_valid, bool) or not isinstance(min_valid, int) or min_valid < 1:
        raise K.ContractError(f'option min_valid={min_valid!r}; it is a whole number of values '
                              f'per group, at least 1')
    need = max(min_valid, S.MIN_N)

    design = manifest['experimental_design']
    comp = plan.get('comparison') or {}
    cond = comp.get('group_column') or design.get('condition_column')
    control = comp.get('control') or design.get('control')
    treatment = comp.get('treatment') or (design.get('treatments') or [None])[0]
    if not (cond and control and treatment):
        raise K.ContractError('an abundance comparison needs a condition column, a control level '
                              'and a treatment level; they are not inferred')
    if control == treatment:
        raise K.ContractError(f'control and treatment are both {control!r}')
    if cond not in table.columns:
        raise K.ContractError(f'the condition column {cond!r} is not in the table; it has '
                              f'{", ".join(table.columns)}')
    present = table.levels(cond)
    absent = [x for x in (control, treatment) if x not in present]
    if absent:
        raise K.ContractError(f'condition level(s) {", ".join(map(repr, absent))} are not in '
                              f'column {cond!r}. Values present: {", ".join(map(str, present))}')
    sample_col = _sample_column(table, design)

    feature = (DE._named(table, comp.get('feature_column'))
               or _find(table, FEATURE_COLUMNS))
    if feature:
        shape = _read_long(table, comp, cond, control, treatment, sample_col, feature)
    elif comp.get('value_column'):
        raise K.ContractError(
            f'the plan names value column {comp["value_column"]!r}, which means a long table, '
            f'but no feature column was found (looked for {", ".join(FEATURE_COLUMNS)}). Name '
            f'it with --feature-column.')
    else:
        shape = _read_wide(table, design, comp, cond, control, treatment, sample_col)

    data, samples = shape['data'], shape['samples']
    groups = (control, treatment)
    n_samples = {g: len(samples[g]) for g in groups}
    if min(n_samples.values()) < need:
        raise K.ContractError(
            f'{control}: {n_samples[control]} sample(s), {treatment}: {n_samples[treatment]}. '
            f'Each group needs at least {need} samples to test anything (min_valid {min_valid}, '
            f'and never fewer than {S.MIN_N}).')
    if len(data) > MAX_FEATURES:
        raise K.ContractError(f'{len(data)} proteins is beyond what this in-process tool is for '
                              f'(at most {MAX_FEATURES}); run limma or MSstats through the '
                              f'external-tool adapter and register the result')

    scale, why, transformed = _decide_scale(
        [v for per in data.values() for g in groups for v in per[g]], scale_opt, shape['hint'])
    non_positive = 0
    if scale == 'linear':
        for per in data.values():
            for g in groups:
                non_positive += sum(1 for v in per[g] if v <= 0)
                per[g] = [float(np.log2(v)) for v in per[g] if v > 0]

    rows, one_side, not_testable = [], [], []
    for p, per in data.items():
        a, b = per[control], per[treatment]
        if len(a) >= need and len(b) >= need:
            rows.append(S.compare_groups(
                p, a, b, group_a=control, group_b=treatment, effect_type='difference',
                independence_note=(f'{len(a)} of {n_samples[control]} {control} and {len(b)} of '
                                   f'{n_samples[treatment]} {treatment} samples had a value; '
                                   f'missing values were not imputed')))
        elif (len(a) >= need and not b) or (len(b) >= need and not a):
            seen, unseen = (control, treatment) if a else (treatment, control)
            one_side.append({'protein': p, 'detected_in': seen, 'absent_from': unseen,
                             'n_values': {control: len(a), treatment: len(b)},
                             'n_samples': dict(n_samples),
                             'note': f'no value in any {unseen} sample: not tested and given no '
                                     f'fold change, because a value for "absent" would be '
                                     f'invented. Absence here may be below detection rather '
                                     f'than not expressed.'})
        else:
            not_testable.append(p)
    if not rows:
        raise K.ContractError(
            f'no protein has at least {need} values in both {control} and {treatment} '
            f'({len(data)} proteins; {len(one_side)} detected in one condition only, '
            f'{len(not_testable)} with too few values). Nothing is imputed to make one testable.')
    S.apply_fdr(rows)
    for r in rows:
        r['effect_type'] = 'difference_in_log2_abundance'

    units, unit_check = _units(table, design, cond, sample_col, groups, n_samples)
    checks = [{'check': 'input shape', 'status': 'PASS', 'detail': shape['describe']}]
    scale_detail = f'{scale}: {why}. ' + (
        f'Values were log2-transformed once; {non_positive} zero or negative value(s) were read '
        f'as missing, not as log2(0).' if transformed else 'Values were used as they are; no '
        'transform was applied.')
    zeros_on_log = (sum(1 for per in data.values() for g in groups for v in per[g] if v == 0)
                    if scale == 'log' else 0)
    if zeros_on_log:
        scale_detail += (f' {zeros_on_log} value(s) are exactly 0; some exports write 0 for '
                         f'"not quantified", and if this one does they were read as values.')
    checks.append({'check': 'scale', 'status': 'WARN' if zeros_on_log else 'PASS',
                   'detail': scale_detail})
    if shape['unreadable']:
        n, ex = shape['unreadable']
        checks.append({'check': 'non-numeric values', 'status': 'WARN',
                       'detail': f'{n} cell(s) were not numbers (e.g. {", ".join(map(repr, ex))}) '
                                 f'and were read as missing, not as zero or as a detection limit'})
    if shape.get('missing_readouts'):
        checks.append({'check': 'requested readouts', 'status': 'WARN',
                       'detail': 'not in the table: ' + ', '.join(shape['missing_readouts'])})
    checks += [
        {'check': 'proteins tested', 'status': 'PASS',
         'detail': f'{len(rows)} of {len(data)} tested: each needs at least {need} values in '
                   f'both groups (min_valid {min_valid})'},
        {'check': 'not testable', 'status': 'PASS' if not not_testable else 'WARN',
         'detail': f'{len(not_testable)} protein(s) had too few values in a group to test; '
                   f'they were not imputed and are not in the statistics'
                   + (f' (e.g. {", ".join(not_testable[:5])})' if not_testable else '')},
        {'check': 'detected in one condition only', 'status': 'PASS' if not one_side else 'WARN',
         'detail': f'{len(one_side)} protein(s) have at least {need} values in one condition and '
                   f'none in the other; listed in comparison.detected_in_one_condition_only, '
                   f'with counts and no fold change'
                   + (f' ({", ".join(x["protein"] for x in one_side[:5])})' if one_side else '')},
        {'check': 'group sizes', 'status': 'PASS' if min(n_samples.values()) >= 3 else 'WARN',
         'detail': f'{control}: {n_samples[control]} sample(s); {treatment}: '
                   f'{n_samples[treatment]}'},
    ]
    if unit_check:
        checks.append(unit_check)
    checks.append({
        'check': 'method', 'status': 'WARN',
        'detail': 'Welch\'s t-test per protein on log2 abundance, Benjamini-Hochberg across the '
                  'proteins tested. No imputation, no moderated variance, no peptide rollup, and '
                  'no normalisation beyond the depositor\'s: this tool does not normalise and '
                  'cannot tell whether the input was. A screening comparison, not a limma or '
                  'MSstats result.'})

    comparison = {
        'group_column': cond, 'control': control, 'treatment': treatment, 'paired': False,
        'input_shape': shape['shape'], 'sample_column': sample_col,
        'samples': dict(n_samples),
        'scale': {'decided': scale, 'reason': why, 'option': scale_opt,
                  'transform': 'log2' if transformed else 'none',
                  'non_positive_read_as_missing': non_positive},
        'min_valid': min_valid, 'min_values_per_group': need,
        'proteins_total': len(data), 'proteins_tested': len(rows),
        'proteins_not_testable': len(not_testable),
        'detected_in_one_condition_only': one_side,
        'imputation': 'none',
    }
    if shape['shape'] == 'long':
        comparison.update(feature_column=feature, value_column=shape['value'])
    else:
        comparison['analytes'] = list(data)
    return {
        'statistics': rows,
        'quality_control': {'checks': checks,
                            'passed': all(c['status'] != 'FAIL' for c in checks)},
        'comparison': comparison,
        'software': S.SOFTWARE,
        'independent_units_min': units,
        'replicate_n_min': min(n_samples.values()),
    }


# ── reading the two shapes ────────────────────────────────────────────────

def _find(table, candidates):
    low = {c.lower(): c for c in table.columns}
    return next((low[c] for c in candidates if c in low), None)


def _sample_column(table, design):
    named = design.get('sample_id_column')
    if named:
        if named not in table.columns:
            raise K.ContractError(f'the sample column {named!r} is not in the table; it has '
                                  f'{", ".join(table.columns)}')
        return named
    found = _find(table, SAMPLE_COLUMNS)
    if not found:
        raise K.ContractError(
            f'an abundance comparison counts samples, so it needs a sample column: name it as '
            f'experimental_design.sample_id_column (looked for {", ".join(SAMPLE_COLUMNS)}; the '
            f'table has {", ".join(table.columns)})')
    return found


def _cell(raw, unreadable):
    """A number, or None for a missing cell. A non-numeric cell is counted, never zeroed."""
    v = TB._number(raw)
    if v is None and (raw or '').strip().lower() not in TB.MISSING:
        unreadable.append(raw)
    return v


def _read_long(table, comp, cond, control, treatment, sample_col, feature):
    value = (DE._named(table, comp.get('value_column'))
             or DE._pick(table, VALUE_COLUMNS, 'abundance value'))
    wanted = _wanted(comp)
    data, samples, group_of, seen, dups, unreadable = {}, {control: set(), treatment: set()}, \
        {}, set(), [], []
    found = set()
    for r in table.rows:
        g = r[cond]
        if g not in (control, treatment):
            continue
        s, p = r[sample_col], r[feature]
        if group_of.setdefault(s, g) != g:
            raise K.ContractError(f'sample {s!r} appears under both {group_of[s]!r} and {g!r} in '
                                  f'column {cond!r}; one sample belongs to one condition')
        samples[g].add(s)
        if not p:
            continue
        if wanted is not None:
            key = wanted.get(p.upper())
            if key is None:
                continue
            found.add(key)
        if (p, s) in seen:
            dups.append((p, s))
            continue
        seen.add((p, s))
        per = data.setdefault(p, {control: [], treatment: []})
        v = _cell(r[value], unreadable)
        if v is not None:
            per[g].append(v)
    if dups:
        ex = ', '.join(f'{p}/{s}' for p, s in dups[:5])
        raise K.ContractError(
            f'{len(dups)} (protein, sample) pair(s) appear more than once (e.g. {ex}). Counting '
            f'rows would count a sample twice. Reduce the table to one value per protein and '
            f'sample first; a peptide- or precursor-level table needs a protein rollup, which '
            f'this tool does not do.')
    if not data:
        raise K.ContractError(f'no protein in column {feature!r} has a row in {control!r} or '
                              f'{treatment!r}' + (' among the requested readouts'
                                                  if wanted is not None else ''))
    return {'shape': 'long', 'data': data, 'samples': samples, 'value': value,
            'hint': value,
            'unreadable': (len(unreadable), unreadable[:3]) if unreadable else None,
            'missing_readouts': [w for w in (wanted or {}).values() if w not in found],
            'describe': f'long: one row per protein and sample; protein column {feature!r}, '
                        f'sample column {sample_col!r}, value column {value!r}'}


def _read_wide(table, design, comp, cond, control, treatment, sample_col):
    skip = {(design.get(k) or '').lower() for k in DESIGN_KEYS} | set(NON_ANALYTE)
    skip |= {cond.lower(), sample_col.lower()}
    requested = [str(x).strip() for x in comp.get('readouts') or [] if str(x).strip()]
    if requested:
        missing = [c for c in requested if c not in table.columns]
        if missing:
            raise K.ContractError(f'readout(s) {", ".join(missing)} are not columns of this '
                                  f'table; it has {", ".join(table.columns)}')
        analytes = requested
    else:
        analytes = [c for c in table.columns if c.lower() not in skip and _analyte_like(table, c)]
    if not analytes:
        raise K.ContractError(
            f'no analyte columns: this is not a long table (no feature column among '
            f'{", ".join(FEATURE_COLUMNS)}) and, as one row per sample, no numeric column is left '
            f'after the design columns. Columns: {", ".join(table.columns)}')
    idx = [i for i, r in enumerate(table.rows) if r[cond] in (control, treatment)]
    counts = Counter(table.rows[i][sample_col] for i in idx)
    dup = sorted(s for s, k in counts.items() if k > 1)
    if dup:
        raise K.ContractError(
            f'sample(s) {", ".join(dup[:5])} appear on more than one row. A one-row-per-sample '
            f'table holds each sample once; average technical repeats yourself, or register the '
            f'table in long form with a feature column.')
    samples = {control: set(), treatment: set()}
    data, unreadable = {}, []
    for a in analytes:
        per = data.setdefault(a, {control: [], treatment: []})
        for i in idx:
            r = table.rows[i]
            v = _cell(r[a], unreadable)
            if v is not None:
                per[r[cond]].append(v)
    for i in idx:
        samples[table.rows[i][cond]].add(table.rows[i][sample_col])
    no_numbers = [a for a, per in data.items() if not (per[control] or per[treatment])]
    if no_numbers and len(no_numbers) == len(data):
        raise K.ContractError(f'none of {", ".join(no_numbers)} holds a number in these samples')
    shown = ', '.join(analytes[:10]) + (f' and {len(analytes) - 10} more'
                                        if len(analytes) > 10 else '')
    hint = ' '.join(analytes) if all(any(h in a.lower() for h in LOG_HINTS)
                                     for a in analytes) else ''
    return {'shape': 'wide', 'data': data, 'samples': samples, 'hint': hint,
            'unreadable': (len(unreadable), unreadable[:3]) if unreadable else None,
            'describe': f'wide: one row per sample (sample column {sample_col!r}), '
                        f'{len(analytes)} analyte column(s): {shown}'
                        + ('; named in the plan' if requested else
                           '; every numeric column that is not a design column')}


def _analyte_like(table, col):
    """Numbers, blanks, and panel range markers ('<LOD', 'OOR <') only."""
    if col in table.numeric:
        return True
    cells = [(r[col] or '').strip() for r in table.rows]
    nums = [c for c in cells if TB._number(c) is not None]
    other = [c for c in cells if TB._number(c) is None and c.lower() not in TB.MISSING]
    return bool(nums) and all(CENSORED.search(c) for c in other)


def _wanted(comp):
    req = [str(x).strip() for x in comp.get('readouts') or [] if str(x).strip()]
    return {x.upper(): x for x in req} if req else None


# ── scale ─────────────────────────────────────────────────────────────────

def _decide_scale(values, option, hint):
    """('log' | 'linear', why, transformed?). Refuses rather than guessing, and
    never log-transforms values that are already logs."""
    v = np.asarray(values, dtype=float)
    if v.size == 0:
        raise K.ContractError('the table holds no numeric abundance values for these conditions')
    lo, hi = float(v.min()), float(v.max())
    neg = lo < 0
    rng = f'values run from {lo:.4g} to {hi:.4g}'
    if option == 'log':
        if hi > LOG_MAX:
            raise K.ContractError(
                f'scale=log was named, but {rng}; a log2 abundance above {LOG_MAX:g} is not '
                f'plausible. These look linear: use scale=linear or scale=auto.')
        return 'log', f'named in the plan (scale=log); {rng}', False
    if option == 'linear':
        if neg and hi <= LOG_MAX:
            raise K.ContractError(
                f'scale=linear was named, but {rng}: negative values within a log2 range look '
                f'already logged (NPX, log2 LFQ), and log-transforming them again would be '
                f'wrong. Use scale=log or scale=auto.')
        return 'linear', f'named in the plan (scale=linear); {rng}', True
    if neg and hi > LOG_MAX:
        raise K.ContractError(
            f'{rng}: negative values say log, the maximum says linear (background-subtracted '
            f'concentrations?). Name the scale with --option scale=log or scale=linear.')
    says_log = bool(hint) and any(h in hint.lower() for h in LOG_HINTS)
    if says_log and hi > LOG_MAX:
        raise K.ContractError(
            f'the column name ({hint[:60]!r}) says log, but {rng}; a log2 abundance above '
            f'{LOG_MAX:g} is not plausible. Name the scale with --option scale=log or '
            f'scale=linear.')
    if neg:
        return 'log', (f'{rng}; negative values cannot be intensities or concentrations, so '
                       f'these were read as already log (NPX, log2 LFQ)'), False
    if hi > LOG_MAX:
        return 'linear', (f'{rng}; a log2 abundance above {LOG_MAX:g} is not plausible, so '
                          f'these were read as linear intensities or concentrations'), True
    if says_log:
        return 'log', (f'{rng}, non-negative and within a log2 range, and the column name '
                       f'({hint[:60]!r}) says log'), False
    raise K.ContractError(
        f'{rng}: non-negative and small, which fits log2 abundances and low concentrations '
        f'alike. Name the scale with --option scale=log or --option scale=linear rather than '
        f'letting this guess.')


# ── independent units ─────────────────────────────────────────────────────

def _units(table, design, cond, sample_col, groups, n_samples):
    """Donors per group when the manifest names a donor column; else samples."""
    col = design.get('donor_column')
    if not col or col not in table.columns:
        return min(n_samples.values()), None
    donors = {g: set() for g in groups}
    for r in table.rows:
        if r[cond] in donors:
            donors[r[cond]].add(r[col])
    n = {g: len(donors[g]) for g in groups}
    repeated = any(n[g] < n_samples[g] for g in groups)
    detail = f'donors ({col}): {groups[0]}: {n[groups[0]]}; {groups[1]}: {n[groups[1]]}'
    if repeated:
        detail += ('. Some donors gave more than one sample, and each sample was tested as its '
                   'own observation, so n overstates the independent evidence')
    return min(n.values()), {'check': 'independent units', 'status': 'WARN' if repeated
                             else 'PASS', 'detail': detail}
