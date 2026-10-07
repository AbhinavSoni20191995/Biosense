"""Which programmes moved: pathway enrichment over a bulk comparison.

A genome-wide comparison answers "which genes moved"; a reader wants "which
programmes moved" — antigen presentation, lipid metabolism, the cell cycle.
This runs the same per-gene Welch comparison as the screening tool, then asks
of every pathway in a named, openly licensed library (Reactome, GO biological
process, or a person's own panel):

* **rank shift** — do the pathway's genes sit higher or lower in the ranking of
  all measured genes (signed −log10 p) than the rest? A Mann-Whitney test, so a
  programme that moves together modestly is found even when none of its genes
  reaches significance alone. This is the primary statistic, BH-corrected.
* **over-representation** — among genes that pass the q threshold in each
  direction, are the pathway's genes over-represented? A one-sided Fisher test,
  reported beside the rank shift.

The background is the genes this table measured, never the genome: a series
fetched as its 2000 most expressed genes can only say what those genes say,
and the result says so.

What it is not: GSEA with phenotype permutation, or a claim that a pathway is
active. The library, its version and checksum travel with the result.
"""
from __future__ import annotations

import math

import numpy as np
from scipy import stats as sps

from .... import contracts as K
from ... import genesets as GS
from ...toolkit import options as OPT
from ...toolkit import statistics as S
from . import basic_de as DE

NAME = 'bulk.pathway_enrichment'
VERSION = '1.0.0'
MODALITIES = ('bulk_rna', 'generic_table')
REQUIRED_METADATA = ('condition_column', 'control', 'treatments')
ANALYSIS_TYPES = ('pathway_enrichment',)
DEFAULTS = {'library': 'reactome', 'q': 0.05, 'min_size': 5, 'max_size': 500, 'top': 50}


def _score(row):
    p = row.get('p_value')
    if p is None or row.get('effect') is None:
        return None
    return math.copysign(-math.log10(max(p, 1e-300)), row['effect'])


def run(table, manifest, plan):
    opt = OPT.take(plan, DEFAULTS)
    sets, meta = GS.load(str(opt['library']))
    gene = DE.run(table, manifest, plan)
    rows = gene['statistics']
    scores = {}
    for r in rows:
        s = _score(r)
        if s is not None:
            scores[str(r['readout']).upper()] = s
    if len(scores) < 50:
        raise K.ContractError(f'{len(scores)} genes have a test result; ranking pathways needs '
                              f'a genome-wide table (at least 50 tested genes). Fetch the series '
                              f'with more genes (--max-genes), or ask about a named module with '
                              f'bulk.gene_set_score instead.')
    universe = np.array(list(scores))
    vals = np.array([scores[g] for g in universe])
    q_of = {str(r['readout']).upper(): r.get('q_value') for r in rows}
    up = {g for g, s in scores.items() if s > 0 and (q_of.get(g) or 1) <= opt['q']}
    down = {g for g, s in scores.items() if s < 0 and (q_of.get(g) or 1) <= opt['q']}
    n_all = len(universe)

    out, skipped = [], 0
    for name, s in sets.items():
        members = [g for g in s['genes'] if g in scores]
        if not opt['min_size'] <= len(members) <= opt['max_size']:
            skipped += 1
            continue
        inset = np.isin(universe, members)
        a, b = vals[inset], vals[~inset]
        u = sps.mannwhitneyu(a, b, alternative='two-sided')
        shift = float(u.statistic) / (len(a) * len(b))       # 0.5 = no shift
        ora = {}
        for label, hits in (('up', up), ('down', down)):
            k = len(hits & set(members))
            table2 = [[k, len(members) - k], [len(hits) - k, n_all - len(members) - len(hits) + k]]
            ora[label] = {'overlap': k, 'p_value': float(sps.fisher_exact(
                table2, alternative='greater')[1]) if hits else None}
        lead = sorted(members, key=lambda g: -abs(scores[g]))[:10]
        out.append({
            'readout': name, 'pathway_id': s.get('id'), 'test': 'Mann-Whitney U (rank shift)',
            'effect': round(shift - 0.5, 6), 'effect_type': 'rank_shift_from_0.5',
            'direction': ('increase' if shift > 0.5 else 'decrease' if shift < 0.5
                          else 'no_change'),
            'p_value': float(u.pvalue), 'q_value': None, 'n': len(members),
            'set_size_in_library': len(s['genes']),
            'over_representation': ora, 'leading_genes': lead,
            'summary': (f'{name}: its {len(members)} measured genes sit '
                        f'{"higher" if shift > 0.5 else "lower"} in {gene["comparison"]["treatment"]} '
                        f'than the rest (rank shift {shift - 0.5:+.3f}); '
                        f'{ora["up"]["overlap"]} in the up list, {ora["down"]["overlap"]} in the '
                        f'down list; led by {", ".join(lead[:5])}'),
            'note': f'{len(members)} of {len(s["genes"])} genes measured here'})
    if not out:
        raise K.ContractError(f'no pathway of {meta["library"]} has {opt["min_size"]}-'
                              f'{opt["max_size"]} genes among the {n_all} measured here; check '
                              f'the identifiers are symbols, or lower min_size')
    S.apply_fdr(out)
    out.sort(key=lambda r: (r['q_value'] if r['q_value'] is not None else 1, r['p_value']))
    shown = out[:int(opt['top'])]
    sig = sum(1 for r in out if (r['q_value'] or 1) <= 0.05)
    checks = [
        {'check': 'library', 'status': 'PASS',
         'detail': f'{meta["library"]} ({meta.get("label")}), {meta["sets"]} sets, '
                   f'retrieved {meta.get("retrieved_at")}, sha256 {meta.get("sha256", "")[:12]}…, '
                   f'licence: {meta.get("license")}'},
        {'check': 'background', 'status': 'PASS' if n_all >= 5000 else 'WARN',
         'detail': f'{n_all} measured genes are the background, not the genome'
                   + ('' if n_all >= 5000 else '; a restricted table limits which pathways can '
                                               'be seen at all')},
        {'check': 'pathways tested', 'status': 'PASS',
         'detail': f'{len(out)} tested ({skipped} outside {opt["min_size"]}-{opt["max_size"]} '
                   f'measured genes); {sig} at q ≤ 0.05; the {len(shown)} strongest reported'},
        {'check': 'method', 'status': 'WARN',
         'detail': 'rank shift of each pathway in the ranking of all measured genes (signed '
                   '-log10 p of a per-gene Welch test), with over-representation among genes at '
                   f'q ≤ {opt["q"]} beside it. Not phenotype-permutation GSEA.'},
    ] + [c for c in gene['quality_control']['checks'] if c['check'] == 'group sizes']
    comp = dict(gene['comparison'], library=meta, options=opt,
                genes_tested=n_all, genes_up=len(up), genes_down=len(down))
    return {
        'statistics': shown,
        'quality_control': {'checks': checks, 'passed': True},
        'comparison': comp,
        'software': S.SOFTWARE,
        'independent_units_min': gene['independent_units_min'],
        'replicate_n_min': gene['replicate_n_min'],
    }
