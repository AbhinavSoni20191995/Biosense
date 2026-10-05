"""Chromatin comparison from processed peak files.

Processed peaks only: BED, narrowPeak, broadPeak and differential-peak tables.
BioSense orchestrates established tools around a decision rather than replacing
them, so calling peaks from signal is out of scope and the reader says so.

The comparison answers a question a process decision can turn on — which peaks a
condition gains or loses, and which genes those peaks sit near — and reports
both directions of overlap, because "80% of A is in B" and "20% of B is in A"
are both true and describe different peak sets.
"""
from __future__ import annotations

from .... import contracts as K
from ....data.readers import peaks as PK
from ...toolkit import statistics as S

NAME = 'chromatin.peak_overlap'
VERSION = '1.0.0'
MODALITIES = ('chip_seq', 'atac_seq')
REQUIRED_METADATA = ('condition_column', 'control', 'treatments')
ANALYSIS_TYPES = ('peak_overlap_comparison',)


def _peak_files(manifest):
    from ....data import manifest as MF
    out = {}
    for f in manifest['files']:
        if f['file_type'] in ('bed', 'narrowPeak', 'broadPeak'):
            out[f.get('role') or f['path']] = (MF.resolve_path(f['path']), f['file_type'])
    return out


def run(table, manifest, plan):
    design = manifest['experimental_design']
    comp = plan.get('comparison') or {}
    control = comp.get('control') or design.get('control')
    treatment = comp.get('treatment') or (design.get('treatments') or [None])[0]
    if not (control and treatment):
        raise K.ContractError('a peak comparison needs a control and a treatment condition')

    detail = manifest.get('modality_detail') or {}
    mapping = detail.get('peak_files') or {}
    files = _peak_files(manifest)
    a_key, b_key = mapping.get(control), mapping.get(treatment)
    if not (a_key and b_key):
        raise K.ContractError(
            f'the manifest does not say which peak file belongs to {control!r} and which to '
            f'{treatment!r}. Record it in modality_detail.peak_files; guessing from filenames '
            f'would silently compare the wrong pair.')
    from ....data import manifest as MF
    a_path, a_ft = MF.resolve_path(a_key), 'narrowPeak' if a_key.endswith('narrowPeak') else 'bed'
    b_path, b_ft = MF.resolve_path(b_key), 'narrowPeak' if b_key.endswith('narrowPeak') else 'bed'

    a = PK.read_peaks(a_path, a_ft)
    b = PK.read_peaks(b_path, b_ft)
    ov = PK.overlap(a, b)

    rows = [
        S.fisher_counts('peaks_shared', a_pos=ov['n_overlapping'], a_n=ov['n_a'],
                        b_pos=len(b) - (ov['n_b'] - int((ov['frac_b_overlapping'] or 0) * ov['n_b'])),
                        b_n=ov['n_b'], group_a=control, group_b=treatment),
    ]
    widths_a = sorted(p['end'] - p['start'] for p in a)
    widths_b = sorted(p['end'] - p['start'] for p in b)
    rows.append(S.compare_groups('peak_width_bp', widths_a, widths_b,
                                 group_a=control, group_b=treatment,
                                 independence_note='peaks are not independent observations of a '
                                                   'condition; this describes the peak sets, not '
                                                   'a biological difference between samples'))
    scores_a = [p['q_value'] for p in a if p.get('q_value') is not None]
    scores_b = [p['q_value'] for p in b if p.get('q_value') is not None]
    if len(scores_a) > 2 and len(scores_b) > 2:
        rows.append(S.compare_groups('peak_qvalue', scores_a, scores_b, group_a=control,
                                     group_b=treatment,
                                     independence_note='describes the called peak sets'))
    S.apply_fdr(rows)

    annotation = None
    anchors = detail.get('gene_anchors')
    if anchors:
        from ....data import tables as TB
        t = TB.read_table(MF.resolve_path(anchors), 'tsv')
        anchor_rows = [{'gene': r['gene'], 'chrom': r['chrom'], 'tss': int(r['tss']),
                        'strand': r.get('strand')} for r in t.rows]
        annotation = PK.annotate_to_genes(ov['a_only'], anchor_rows)

    checks = [
        {'check': 'peak provenance', 'status': 'WARN',
         'detail': 'BioSense did not call these peaks and cannot verify the calling parameters. '
                   'A difference between peak sets may reflect calling thresholds rather than '
                   'chromatin.'},
        {'check': 'replication', 'status': 'WARN',
         'detail': 'this compares two peak SETS, not replicated samples. It describes the data, '
                   'and does not establish a difference between conditions.'},
        {'check': 'overlap direction', 'status': 'PASS',
         'detail': f'{ov["frac_a_overlapping"]:.0%} of {control} peaks overlap {treatment}; '
                   f'{ov["frac_b_overlapping"]:.0%} the other way round'},
    ]
    return {
        'statistics': rows,
        'quality_control': {'checks': checks, 'passed': True},
        'comparison': {'control': control, 'treatment': treatment,
                       'n_control_peaks': ov['n_a'], 'n_treatment_peaks': ov['n_b'],
                       'n_overlapping': ov['n_overlapping'],
                       'frac_control_overlapping': ov['frac_a_overlapping'],
                       'frac_treatment_overlapping': ov['frac_b_overlapping'],
                       'control_only': len(ov['a_only']),
                       'annotation': ({'n_assigned': annotation['n_assigned'],
                                       'n_promoter_proximal': annotation['n_promoter_proximal'],
                                       'n_distal': annotation['n_distal'],
                                       'genes': sorted({x['gene'] for x in
                                                        annotation['annotations'] if x['gene']})[:40]}
                                      if annotation else None)},
        'software': S.SOFTWARE,
        'independent_units_min': 1,
        'replicate_n_min': 1,
    }
