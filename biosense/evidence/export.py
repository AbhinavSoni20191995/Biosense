"""The per-hypothesis export: everything behind one claim, in one directory.

A hypothesis is the unit someone argues about. "Raise M-CSF from 25 to 50 ng/mL"
is what gets taken to a meeting, questioned, and either run or dropped, and the
question it has to survive is always the same one: *where did that number come
from?* Answering it should not mean reading a whole loop directory.

So the workspace is organised around one hypothesis and holds what a reader
needs to check it: the claim and its parameter, the evidence rows with their
classes, each number with its estimate type, the prose with the facts it was
validated against, the datasets by reference, any residuals once the loop has
closed, and the limitations. It copies no raw data — a dataset appears as its
id, checksum and visibility, which is what makes the export checkable without
making it a second, uncontrolled copy of somebody's experiment.

Two rules it inherits rather than reinvents:

* **public_safe refuses; it does not anonymise.** The same privacy check the
  benchmark export uses, for the same reason: a frequency from an unpublished
  experiment is still that experiment's result, and removing the name does not
  change that. A refused export writes nothing at all.

* **A private workspace is written under the private root**, which is outside
  the published tree and git-ignored, so it cannot be committed by accident.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from .. import contracts as K
from ..benchmark import privacy as PRIV
from ..data import roots as DR
from . import hypothesis as HY
from . import narrative as NR
from . import residual as RS

PUBLIC_DIR = K.ROOT / 'exports'


def _as_result(h, *, datasets=(), expert_knowledge=()):
    """The shape privacy.lineage() reads, built from one hypothesis."""
    return {'hypotheses': [h], 'datasets': list(datasets), 'analyses': [],
            'expert_knowledge': list(expert_knowledge)}


def workspace_dir(hypothesis_id, policy, *, out=None):
    """Where this workspace belongs. Private ones never land in the repository."""
    if out:
        return Path(out)
    if policy == 'private':
        return DR.private_root() / 'exports' / hypothesis_id
    return PUBLIC_DIR / hypothesis_id


def plan(h, *, policy='public_safe', datasets=(), expert_knowledge=(), residuals=()):
    """Decide what may be exported, before anything is written.

    Separate from `write` on purpose: a caller can ask whether an export is
    permitted without creating a directory that then has to be cleaned up, and a
    refusal is a value to show someone rather than an exception to catch.
    """
    priv = PRIV.check(_as_result(h, datasets=datasets, expert_knowledge=expert_knowledge),
                      policy, what='hypothesis export')
    return {
        'hypothesis_id': h['hypothesis_id'],
        'policy': policy,
        'privacy': priv,
        'may_write': policy == 'private' or priv['safe_to_publish'],
        'contents': _contents(h, datasets, residuals),
    }


def _contents(h, datasets, residuals):
    items = ['hypothesis.json', 'evidence.md', 'narrative.md', 'numbers.json',
             'datasets.json', 'limitations.md', 'MANIFEST.json']
    if residuals:
        items.insert(-1, 'residuals.json')
    return items


def _dataset_reference(d):
    """A dataset as a reference, never as a copy.

    Enough to find and verify the data, nothing of the data itself. The checksum
    is the point: it lets a reader confirm they are looking at the same file
    without the export becoming a second copy of it.
    """
    return {'dataset_id': d.get('dataset_id'), 'title': d.get('title'),
            'modality': d.get('modality'), 'visibility': d.get('visibility'),
            'sha256': d.get('sha256') or (d.get('files') or [{}])[0].get('sha256'),
            'accession': d.get('accession'), 'n_rows': d.get('n_rows'),
            'note': 'reference only; the export never copies dataset contents'}


def _numbers(h, residuals):
    """Every number in the export with the estimate type that governs it.

    Flattened deliberately. The estimate type travels with each number rather
    than with the object holding it, and a reader checking one figure should not
    have to know which nested structure it came from to find out what kind of
    number it is.
    """
    rows = []
    for e in h['expected_effects']:
        rows.append({'where': 'expected_effect', 'metric': e['metric'], 'unit': e['unit'],
                     'estimate_type': e['estimate_type'],
                     'magnitude_estimated': e['magnitude_estimated'],
                     'withheld_reason': e.get('withheld_reason'),
                     'absolute_change': e.get('absolute_change'),
                     'change_unit': e.get('change_unit'),
                     'relative_change_pct': e.get('relative_change_pct'),
                     'relative_withheld_reason': e.get('relative_withheld_reason'),
                     'baseline': e['baseline'], 'candidate': e['candidate']})
    p = h['parameter']
    for key in ('current_value', 'candidate_value'):
        if p.get(key) is not None:
            rows.append({'where': f'parameter.{key}', 'metric': p['parameter_id'],
                         'unit': p.get('unit'), 'estimate_type': 'design_choice'
                         if key == 'candidate_value' else 'measured',
                         'value': p[key]})
    for r in residuals:
        rows.append({'where': 'residual', 'metric': r['readout'], 'unit': r['unit'],
                     'estimate_type': r['residual']['estimate_type'],
                     'predicted': r['predicted']['value'], 'measured': r['measured']['value'],
                     'absolute_change': r['residual']['absolute_change'],
                     'change_unit': r['residual']['change_unit'],
                     'counts_as_model_evidence': r['counts_as_model_evidence']})
    return rows


def _narrative(h):
    """Prose, and the facts it was checked against.

    Both are written. Prose alone would be a claim; the fact set is what makes it
    an auditable rendering of structured values rather than a summary somebody
    has to take on trust.
    """
    facts = NR.facts_from_hypothesis(h)
    text = NR.say_hypothesis(h)
    problems = NR.validate(text, facts)
    if problems:
        raise K.ContractError(
            f'refusing to export prose that does not match its facts: {"; ".join(problems[:4])}')
    return text, facts


def write(h, out_dir=None, *, policy='public_safe', datasets=(), expert_knowledge=(),
          residuals=(), overwrite=True):
    """Write the workspace, or refuse and write nothing."""
    pl = plan(h, policy=policy, datasets=datasets, expert_knowledge=expert_knowledge,
              residuals=residuals)
    if not pl['may_write']:
        raise K.ContractError(pl['privacy']['refusal_reason'])

    d = Path(out_dir) if out_dir else workspace_dir(h['hypothesis_id'], policy)
    if d.exists() and overwrite:
        shutil.rmtree(d)
    d.mkdir(parents=True, exist_ok=True)

    text, facts = _narrative(h)
    written = {}

    def put(name, content):
        p = d / name
        p.write_text(content if isinstance(content, str) else
                     json.dumps(content, indent=2, ensure_ascii=False, default=str) + '\n',
                     encoding='utf-8')
        written[name] = K.sha256_file(p)

    put('hypothesis.json', h)
    put('evidence.md', _evidence_markdown(h))
    put('narrative.md', _narrative_markdown(h, text, facts))
    put('numbers.json', _numbers(h, residuals))
    put('datasets.json', [_dataset_reference(x) for x in datasets])
    put('limitations.md', _limitations_markdown(h, residuals, pl))
    if residuals:
        put('residuals.json', {'residuals': list(residuals),
                               'summary': RS.summarise(residuals)})

    manifest = {
        'schema_version': '0.1', 'hypothesis_id': h['hypothesis_id'],
        'statement': h['statement'], 'project_id': h['project_id'],
        'parameter_id': h['parameter']['parameter_id'],
        'confidence': h['confidence'],
        'simulator_coverage': h['parameter'].get('simulator_coverage'),
        'created_at': K.now_iso(), 'biosense_version': K.BIOSENSE_VERSION,
        'privacy': pl['privacy'], 'files': written,
        'contains_dataset_contents': False,
        'note': 'Datasets appear by reference and checksum. This export never copies data.',
    }
    (d / 'MANIFEST.json').write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False, default=str) + '\n', encoding='utf-8')
    return {'workspace': str(d), 'files': sorted(written) + ['MANIFEST.json'],
            'privacy': pl['privacy']}


def _evidence_markdown(h):
    rows = HY.evidence_table(h)
    out = [f'# Evidence for {h["hypothesis_id"]}', '', f'> {h["statement"]}', '',
           '| class | stance | strength | summary | reference |',
           '| --- | --- | --- | --- | --- |']
    for r in rows:
        out.append(f'| {r.get("evidence_class", "")} | {r.get("stance", "")} | '
                   f'{r.get("strength", "")} | {str(r.get("summary", "")).replace("|", "/")} | '
                   f'{r.get("ref") or ""} |')
    out += ['', f'Confidence: **{h["confidence"]}**', '']
    for why in h.get('confidence_basis') or []:
        out.append(f'- {why}')
    return '\n'.join(out) + '\n'


def _narrative_markdown(h, text, facts):
    out = [f'# {h["hypothesis_id"]} in plain language', '', text, '',
           '## The facts this was checked against', '',
           'Every number above matches one of these. The prose is a rendering of '
           'these values, not a separate account of them.', '',
           '| fact | value | unit | estimate type |', '| --- | --- | --- | --- |']
    for f in facts:
        out.append(f'| {f.fact_id} | {f.value} | {f.unit or ""} | {f.estimate_type or ""} |')
    return '\n'.join(out) + '\n'


def _limitations_markdown(h, residuals, pl):
    out = [f'# What {h["hypothesis_id"]} does not establish', '']
    for lim in h['limitations']:
        out.append(f'- {lim}')
    cov = h['parameter'].get('simulator_coverage')
    if cov and cov != 'modelled':
        out.append(f'- the simulator coverage for this parameter is `{cov}`, so no predicted '
                   f'effect was produced for it')
    if not residuals:
        out += ['', '## The loop has not closed', '',
                'No prediction here has been checked against a measurement. Everything above is '
                'what the system expects, not what happened.']
    else:
        s = RS.summarise(residuals)
        out += ['', '## Against measurement', '', s['statement'], '']
        out += [f'- {x}' for x in s['limitations']]
    if pl['privacy']['has_private_lineage']:
        out += ['', '## Private lineage', '',
                'This hypothesis rests on private sources: ' +
                ', '.join(pl['privacy']['private_sources']) + '.']
    return '\n'.join(out) + '\n'
