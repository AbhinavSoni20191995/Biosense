"""Reading BED, narrowPeak and broadPeak, and the conservative things to do with them.

Stdlib only — these are columnar text and need no extra.

What this layer deliberately does NOT do is call peaks. BioSense orchestrates
established tools around a decision; it does not replace MACS, nf-core or a
genome browser. A FASTQ-to-peaks pipeline is out of scope and stays out.

What it does: read a peak set, overlap two of them, annotate peaks against gene
anchors, and summarise a differential-peak table. Each answers a question a
process decision might actually turn on.
"""
from __future__ import annotations

from pathlib import Path

from ... import contracts as K

MAX_PEAKS = 2_000_000
# narrowPeak and broadPeak are BED6+ with fixed extra columns.
NARROW = ('chrom', 'start', 'end', 'name', 'score', 'strand',
          'signal_value', 'p_value', 'q_value', 'summit')
BROAD = NARROW[:9]


def read_peaks(path, file_type=None):
    """A peak set as a list of dicts. Refuses a malformed interval rather than skipping it."""
    p = Path(path)
    if not p.is_file():
        raise K.ContractError(f'{p} is not a file')
    ft = (file_type or p.suffix.lstrip('.')).lower()
    if ft not in ('bed', 'narrowpeak', 'broadpeak'):
        raise K.ContractError(
            f'{p.name}: this reader handles bed, narrowPeak and broadPeak. '
            f'Calling peaks from signal is outside what BioSense does: run MACS or an '
            f'established pipeline and register the result.')
    cols = NARROW if ft == 'narrowpeak' else BROAD if ft == 'broadpeak' else None
    out = []
    with p.open(encoding='utf-8') as fh:
        for n, line in enumerate(fh, 1):
            line = line.rstrip('\n')
            if not line or line.startswith(('#', 'track', 'browser')):
                continue
            f = line.split('\t')
            if len(f) < 3:
                raise K.ContractError(f'{p.name}:{n} has fewer than three columns')
            try:
                start, end = int(f[1]), int(f[2])
            except ValueError:
                raise K.ContractError(f'{p.name}:{n} has a non-integer interval: {f[1]!r}-{f[2]!r}')
            if end < start:
                raise K.ContractError(f'{p.name}:{n} ends before it starts ({start} > {end})')
            rec = {'chrom': f[0], 'start': start, 'end': end,
                   'name': f[3] if len(f) > 3 else None,
                   'score': _num(f[4]) if len(f) > 4 else None,
                   'strand': f[5] if len(f) > 5 else None}
            if cols:
                for i, key in enumerate(cols[6:], start=6):
                    rec[key] = _num(f[i]) if len(f) > i else None
            out.append(rec)
            if len(out) > MAX_PEAKS:
                raise K.ContractError(f'{p.name} has more than {MAX_PEAKS} peaks')
    if not out:
        raise K.ContractError(f'{p.name} contains no peaks')
    return out


def _num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def describe(path, file_type=None):
    """What a DatasetManifest records about a peak set."""
    peaks = read_peaks(path, file_type)
    widths = [p['end'] - p['start'] for p in peaks]
    chroms = {}
    for p in peaks:
        chroms[p['chrom']] = chroms.get(p['chrom'], 0) + 1
    widths_sorted = sorted(widths)
    return {
        'n_peaks': len(peaks),
        'chromosomes': sorted(chroms),
        'peaks_per_chromosome': chroms,
        'total_bp': sum(widths),
        'median_width_bp': widths_sorted[len(widths_sorted) // 2],
        'min_width_bp': widths_sorted[0], 'max_width_bp': widths_sorted[-1],
        'has_scores': any(p.get('score') is not None for p in peaks),
        'has_qvalues': any(p.get('q_value') is not None for p in peaks),
        'note': 'A peak set describes where signal was called, by whatever tool called it. '
                'BioSense did not call these peaks and cannot verify the calling parameters.',
    }


def _by_chrom(peaks):
    out = {}
    for p in peaks:
        out.setdefault(p['chrom'], []).append((p['start'], p['end'], p))
    for v in out.values():
        v.sort()
    return out


def overlap(a, b, *, min_bp=1):
    """Peaks of *a* that overlap *b* by at least `min_bp`, and the counts.

    Reported both ways round, because "80% of A overlaps B" and "20% of B
    overlaps A" are both true and describe very different peak sets.
    """
    bb = _by_chrom(b)
    hits, pairs = [], []
    for p in a:
        for start, end, q in bb.get(p['chrom'], ()):
            if start >= p['end']:
                break
            ov = min(p['end'], end) - max(p['start'], start)
            if ov >= min_bp:
                hits.append(p)
                pairs.append({'a': p, 'b': q, 'overlap_bp': ov})
                break
    a_ids = {id(x) for x in hits}
    b_hit = {id(x['b']) for x in pairs}
    return {
        'n_a': len(a), 'n_b': len(b), 'n_overlapping': len(hits),
        'frac_a_overlapping': round(len(hits) / len(a), 4) if a else None,
        'frac_b_overlapping': round(len(b_hit) / len(b), 4) if b else None,
        'pairs': pairs,
        'a_only': [p for p in a if id(p) not in a_ids],
        'note': 'Both directions are reported: the fraction of A covered and the fraction of B '
                'covered answer different questions and are rarely the same number.',
    }


def annotate_to_genes(peaks, anchors, *, promoter_bp=2000):
    """Assign each peak to the nearest gene anchor, and say how far away it is.

    `anchors` is [{gene, chrom, tss, strand}]. Distance is signed relative to
    the TSS so a reader can tell upstream from downstream. A peak within
    `promoter_bp` is called promoter-proximal; everything else is reported with
    its distance rather than labelled an enhancer, which is a claim this data
    cannot support on its own.
    """
    by_chrom = {}
    for a in anchors:
        by_chrom.setdefault(a['chrom'], []).append(a)
    out = []
    for p in peaks:
        mid = (p['start'] + p['end']) // 2
        best, best_d = None, None
        for a in by_chrom.get(p['chrom'], ()):
            d = mid - int(a['tss'])
            if a.get('strand') == '-':
                d = -d
            if best_d is None or abs(d) < abs(best_d):
                best, best_d = a, d
        out.append({
            'peak': {k: p[k] for k in ('chrom', 'start', 'end', 'name')},
            'gene': best['gene'] if best else None,
            'distance_to_tss_bp': best_d,
            'class': ('promoter_proximal' if best_d is not None and abs(best_d) <= promoter_bp
                      else 'distal' if best_d is not None else 'unassigned'),
        })
    assigned = [x for x in out if x['gene']]
    return {
        'annotations': out,
        'n_peaks': len(peaks), 'n_assigned': len(assigned),
        'n_promoter_proximal': sum(1 for x in out if x['class'] == 'promoter_proximal'),
        'n_distal': sum(1 for x in out if x['class'] == 'distal'),
        'note': 'Distal peaks are reported with their distance, not called enhancers. '
                'Regulatory function is not something a coordinate overlap establishes.',
    }
