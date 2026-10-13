"""A GEO ChIP-seq or ATAC-seq series as a registered peak-set dataset.

A run identified the series that answers "are the alveolar lineage gates
open?" (RXRa/b knockout alveolar macrophages) and could not use it: the series
holds `.narrowPeak.gz` files and no processed counts, and nothing brought a
peak set in. `chromatin.peak_overlap` existed; peak-set ingestion did not.

This is that route. Samples are chosen exactly as for an expression series
(any field, `title_label`, a pattern over a field, several control values);
each chosen sample's own peak file is downloaded and checked; and each group
becomes one peak set for the comparison:

* one sample — its own peak set, as the depositors called it;
* several — combined only when asked: `consensus` keeps peaks found in at
  least half of the replicates, `union` keeps every peak. Either is BioSense's
  combination, not the depositors' own peak set, and is stated as one.

A series whose peaks sit in the series directory rather than per sample is
read with an explicit map, condition -> file name, never by guessing which
file belongs to which condition from its name.

Nothing here calls peaks. A series that holds only signal tracks or reads is
refused with what it holds.
"""
from __future__ import annotations

import math
import urllib.error
from pathlib import Path

from ... import contracts as K
from .. import roots
from ..readers import peaks as PK
from . import geo_fetch as GF

MODALITY_FOR = {'atac-seq': 'atac_seq', 'chip-seq': 'chip_seq', 'cut&run': 'chip_seq',
                'cut&tag': 'chip_seq', 'dnase-hypersensitivity': 'atac_seq'}
REPLICATES = ('consensus', 'union')


def _peak_urls(sample, pick=None):
    urls = [u for u in sample.get('supplementary_files') or []
            if PK.peak_type(u.rsplit('/', 1)[-1])]
    if pick:
        urls = [u for u in urls if pick in u.rsplit('/', 1)[-1]]
    if len(urls) > 1:
        narrow = [u for u in urls if PK.peak_type(u.rsplit('/', 1)[-1]) == 'narrowPeak']
        urls = narrow if len(narrow) == 1 else urls
    return urls


def _modality(info, modality):
    if modality:
        if modality not in ('atac_seq', 'chip_seq'):
            raise K.ContractError('modality must be atac_seq or chip_seq')
        return modality
    found = {MODALITY_FOR.get((s or '').strip().lower()) for s in info['library_strategies']}
    found.discard(None)
    if len(found) == 1:
        return found.pop()
    raise K.ContractError(
        f'{info["accession"]} lists library strategies '
        f'{", ".join(info["library_strategies"]) or "none"}; say which this is with '
        f'--modality atac_seq or chip_seq')


def _download(url, dest, timeout):
    text = GF._text(GF._get(url, timeout))
    dest.write_text(text, encoding='utf-8')
    PK.read_peaks(dest, PK.peak_type(url.rsplit('/', 1)[-1]) or 'bed')    # refuse a bad file now
    return dest


def fetch_peaks(acc, *, condition_key=None, control, treatments, keep=None, replicates=None,
                pick=None, peak_files=None, modality=None, dataset_id=None, title=None,
                cell_type=None, registered_by='bioinformatics_agent',
                i_have_network_permission=False, timeout=90, out_root=None, register=True,
                overwrite=True):
    """Fetch, check, combine where asked, and register a peak-set comparison."""
    from .. import ingest as ING
    GF._need(i_have_network_permission)
    acc = GF.check_accession(acc)
    if not treatments:
        raise K.ContractError('name at least one treatment value to compare with the control')
    if replicates not in (None,) + REPLICATES:
        raise K.ContractError(f'--replicates is one of {", ".join(REPLICATES)}')
    info = GF.samples(acc, i_have_network_permission=True, timeout=timeout)
    mod = _modality(info, modality)
    ctl = GF.controls_of(control)
    wanted = [ctl[0]] + list(treatments)
    did = dataset_id or f'{acc.lower()}_peaks'
    base = Path(out_root or roots.cache_root()) / 'geo' / did
    base.mkdir(parents=True, exist_ok=True)
    notes, members, by_group = [], {}, {}

    if peak_files:
        # The series directory, with the person's or agent's own map.
        unknown = [g for g in peak_files if g not in wanted]
        if unknown or set(wanted) - set(peak_files):
            raise K.ContractError(f'--peak-file must map exactly {", ".join(wanted)} to a file; '
                                  f'got {", ".join(peak_files)}')
        try:
            listing = GF._text(GF._get(f'{GF.series_url(acc)}/suppl/', timeout))
        except urllib.error.HTTPError:
            listing = ''
        held = GF.listing_names(listing)
        for g, name in peak_files.items():
            if name not in held:
                raise K.ContractError(f'{name} is not in {acc}\'s series directory (it holds '
                                      f'{", ".join(held[:10]) or "nothing readable"})')
            if not PK.peak_type(name):
                raise K.ContractError(f'{name} is not a peak file (bed, narrowPeak, broadPeak)')
            dest = base / f'group{wanted.index(g)}_{GF._slug(g)}.{PK.peak_type(name)}'
            by_group[g] = _download(f'{GF.series_url(acc)}/suppl/{name}', dest, timeout)
            members[g] = [name]
        notes.append('Each condition\'s peak set was named explicitly from the series '
                     'directory (' + '; '.join(f'{g} = {n}' for g, n in peak_files.items())
                     + '), not inferred from file names.')
    else:
        if not condition_key:
            raise K.ContractError('give --condition-key to choose samples, or --peak-file to '
                                  'map series-level files to conditions')
        rows = info['samples']
        chosen = GF.select_samples(rows, condition_key=condition_key, control=ctl,
                                   treatments=treatments, keep=keep)
        for gsm, g in chosen:
            members.setdefault(g, []).append(gsm)
        present = sorted({GF._value_of(s, condition_key) for s in rows
                          if GF._value_of(s, condition_key) is not None})
        empty = [g for g in wanted if not members.get(g)]
        if empty:
            raise K.ContractError(
                f'no sample has {condition_key!r} = {", ".join(map(repr, empty))}'
                + (f' (values: {", ".join(present)})' if present else
                   f' (no sample has a field {condition_key!r})'))
        if len(chosen) > GF.MAX_PER_SAMPLE:
            raise K.ContractError(f'{len(chosen)} samples match, more than the '
                                  f'{GF.MAX_PER_SAMPLE} peak files fetched for one comparison; '
                                  f'narrow with --keep')
        by_gsm = {s['gsm']: s for s in rows}
        urls = {gsm: _peak_urls(by_gsm[gsm], pick) for gsm, _ in chosen}
        bad = {gsm: u for gsm, u in urls.items() if len(u) != 1}
        if bad:
            gsm, u = next(iter(bad.items()))
            raise K.ContractError(
                f'{len(bad)} chosen sample(s) do not carry exactly one peak file '
                f'(e.g. {gsm}: {", ".join(x.rsplit("/", 1)[-1] for x in u) or "none"}). '
                f'What the samples hold: {GF.sample_files_summary([by_gsm[g] for g in bad])}. '
                + ('Pick one with --pick <substring of the file name>.' if any(bad.values())
                   else 'A series with only signal tracks or reads has no peak set to read: '
                        'peaks would have to be called, which BioSense does not do.'))
        several = {g: m for g, m in members.items() if len(m) > 1}
        if several and not replicates:
            g, m = next(iter(several.items()))
            raise K.ContractError(
                f'{g!r} has {len(m)} replicate peak sets ({", ".join(m[:6])}). Say how to '
                f'combine them: --replicates consensus (peaks in at least half the replicates) '
                f'or --replicates union; or narrow to one sample per condition with --keep.')
        sets = {}
        for gsm, g in chosen:
            name = urls[gsm][0].rsplit('/', 1)[-1]
            dest = base / f'{gsm}.{PK.peak_type(name)}'
            _download(urls[gsm][0], dest, timeout)
            sets.setdefault(g, []).append(dest)
        for g in wanted:
            paths = sets[g]
            if len(paths) == 1:
                by_group[g] = paths[0]
                continue
            need = math.ceil(len(paths) / 2) if replicates == 'consensus' else 1
            merged = PK.consensus([PK.read_peaks(p) for p in paths], need)
            by_group[g] = PK.write_bed(
                base / f'group{wanted.index(g)}_{GF._slug(g)}.{replicates}.bed', merged)
            notes.append(f'{g}: {len(merged)} peaks found in at least {need} of {len(paths)} '
                         f'replicate peak sets ({", ".join(members[g])}), merged by BioSense '
                         f'({replicates}); not the depositors\' own peak set, and its signal and '
                         f'q-values are not carried.')
        kinds = {g: (PK.peak_type(str(by_group[g])) or 'bed') for g in wanted}
        if len(set(kinds.values())) > 1:
            notes.append('The conditions\' peak sets are of different kinds ('
                         + ', '.join(f'{g}: {k}' for g, k in kinds.items())
                         + '): width and q-value comparisons between them are not like for '
                           'like.')
        notes += [c['statement'] for c in GF.confounded_fields(
            by_gsm, [(0, gsm, g) for gsm, g in chosen], condition_key, keep or {}) or []]
    if ctl[1:]:
        notes.append(f'The control pools {", ".join(ctl)}, labelled {ctl[0]!r}.')

    m = None
    paths = [by_group[g] for g in wanted]
    if register:
        m, _ = ING.ingest_peaks(
            paths[0], extra_files=paths[1:], dataset_id=did,
            title=title or f'{acc}: {info["series"].get("title") or "GEO series"} '
                           f'(peaks: {", ".join(treatments)} vs {ctl[0]})',
            modality=mod, organism=(info['organisms'] or ['unspecified'])[0],
            cell_type=cell_type, source='geo', accession=acc,
            source_url=f'https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc={acc}',
            experimental_design={'condition_column': 'condition', 'control': ctl[0],
                                 'treatments': list(treatments), 'replicate_column': None},
            peak_files={g: str(by_group[g]) for g in wanted},
            replicates={g: members.get(g, []) for g in wanted},
            description=(f'Peak sets from {acc}: one per condition '
                         f'({", ".join(f"{g}: {len(members.get(g, []))} sample(s)" for g in wanted)})'
                         + (f', replicates combined by {replicates}' if replicates else '')
                         + '.'),
            registered_by=registered_by, extra_limitations=notes, overwrite=overwrite)
    return {'dataset_id': did, 'accession': acc, 'modality': mod,
            'peak_files': {g: str(by_group[g]) for g in wanted},
            'samples_per_group': {g: len(members.get(g, [])) for g in wanted},
            'replicates': replicates, 'limitations_added': notes, 'registered': bool(m),
            'retrieved_at': K.now_iso(),
            'next': (f'analyse plan --dataset-ids {did} --analysis-type peak_overlap_comparison '
                     f'--tool chromatin.peak_overlap --control "{ctl[0]}" --treatment-level '
                     f'"{treatments[0]}" ...'),
            'note': ('Peak sets another lab called, from its own experiment: where signal was '
                     'called by its pipeline and thresholds, to be weighed for this process, not '
                     'a measurement of it.')}
