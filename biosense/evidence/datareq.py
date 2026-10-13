"""What the run could not get, asked of the person who can.

An agent that cannot fetch a table writes a limitation, and a limitation is
usually where it ends: the run finishes a little thinner and nobody is told
what would have fixed it. Often the person who started the run has exactly the
missing thing — the processed counts a repository never published, the sample
sheet behind an accession, the mapping for an in-house array, a number from
their own bench book.

This turns each dead end into a request they can answer, and records the
answer with its provenance. Two rules hold it honest:

* a request says what was tried and why it failed, so the ask is checkable
  rather than a shrug, and what it would unblock, so it is worth answering;
* an answer in the person's own words is their report of their own process. It
  can settle a direction and update a hypothesis; a number in it enters a
  protocol only as an adapted value naming that report, never as a cited one.
  A dataset they register is the answer that can actually be analysed.

Nothing here fetches or re-runs anything. The next round reads the answers.
"""
from __future__ import annotations

import uuid

from .. import contracts as K

NEEDS = ('processed_table', 'sample_sheet', 'identifier_mapping', 'full_text',
         'measurement', 'access', 'other')
#: The words an agent actually reaches for, and the need each one names. A run
#: that could not fetch three series wrote `needs: "dataset"` and the request
#: was refused for the word — so the person who could have supplied the table
#: was asked for nothing, which is the one outcome this file exists to prevent.
#: The kind is a filing label, not a claim, and nothing downstream is weaker
#: for the agent having said "dataset".
NEEDS_ALSO = {
    'dataset': 'processed_table', 'datasets': 'processed_table', 'data': 'processed_table',
    'table': 'processed_table', 'counts': 'processed_table', 'count_table': 'processed_table',
    'count_matrix': 'processed_table', 'matrix': 'processed_table',
    'expression_table': 'processed_table', 'expression_matrix': 'processed_table',
    'processed_counts': 'processed_table', 'processed_data': 'processed_table',
    'metadata': 'sample_sheet', 'samples': 'sample_sheet', 'sample_metadata': 'sample_sheet',
    'sample_table': 'sample_sheet', 'design': 'sample_sheet',
    'annotation': 'identifier_mapping', 'mapping': 'identifier_mapping',
    'identifiers': 'identifier_mapping', 'ids': 'identifier_mapping',
    'gene_mapping': 'identifier_mapping', 'gene_ids': 'identifier_mapping',
    'paper': 'full_text', 'fulltext': 'full_text', 'pdf': 'full_text',
    'publication': 'full_text', 'article': 'full_text',
    'number': 'measurement', 'value': 'measurement', 'measurements': 'measurement',
    'permission': 'access', 'credentials': 'access', 'login': 'access', 'account': 'access',
}
STATUS = ('open', 'answered', 'declined')
NAME = 'data_requests.json'
MAX_ITEMS = 40
MAX_NOTE = 4000

NOTE = ('What this run could not get, and what each would unblock. An answer is either a '
        'dataset you register — which the next round can analyse — or your own words, which '
        'can settle a direction but never become a cited value. Answering changes nothing by '
        'itself: start the next round to use it.')

TEMPLATE = {
    'items': [
        {'request_id': 'R1', 'needs': 'processed_table',
         'what': 'A processed count matrix for GSE311159 (genes as rows, the 8 samples as '
                 'columns), on a linear or log2 scale.',
         'why': 'NCBI publishes no processed counts for this series (HTTP 404) and the '
                'supplementary directory holds only archives and raw files, so there is no '
                'table this run can register.',
         'shape': 'CSV or TSV, first column the gene symbol or Ensembl id, one column per '
                  'sample, with the sample names matching the series metadata.',
         'unblocks': 'the differential expression between the treated and control arms that '
                     'the decision-blocking uncertainty needs',
         'accession': 'GSE311159',
         'where_to_look': ['the authors\' supplementary files on the journal page',
                           'ArrayExpress or a mirror that reprocessed the series'],
         'status': 'open', 'answer': None},
        {'request_id': 'R2', 'needs': 'identifier_mapping',
         'what': 'A mapping from this series\' row ids (100287102, 653635, 102466751, …) to '
                 'gene symbols.',
         'why': 'The rows are the series\' own identifiers and none of the panel genes '
                '(PPARG, CSF1R, MRC1, …) resolve against them, so the panel cannot be scored.',
         'shape': 'two columns: the row id as it appears in the matrix, and the gene symbol.',
         'unblocks': 'scoring the macrophage identity panel on this series',
         'accession': None, 'where_to_look': ['the platform record (GPL…) for this series'],
         'status': 'open', 'answer': None},
    ],
}


def _text(v, where, *, minimum=1, required=True, limit=2000):
    s = ' '.join(str(v or '').split())
    if not s and not required:
        return None
    if len(s) < minimum:
        raise K.ContractError(f'{where} is too short (need at least {minimum} characters)')
    return s[:limit]


def build(draft, *, run_id, project_id=None, created_by=None):
    """A checked DataRequests document from an agent's draft."""
    if not isinstance(draft, dict):
        raise K.ContractError('a data request draft is a JSON object; see '
                              '`python -m biosense.evidence.cli template data-request`')
    rows, seen = [], set()
    for i, it in enumerate(draft.get('items') or []):
        w = f'items[{i}]'
        rid = str(it.get('request_id') or f'R{i + 1}').strip()
        if rid in seen:
            raise K.ContractError(f'{w}: request_id {rid!r} is used twice')
        seen.add(rid)
        needs = str(it.get('needs') or 'other').strip().lower().replace(' ', '_') \
            .replace('-', '_')
        needs = NEEDS_ALSO.get(needs, needs)
        if needs not in NEEDS:
            raise K.ContractError(f'{w}.needs is {it.get("needs")!r}; use one of '
                                  f'{", ".join(NEEDS)}. If none fits, use "other" and say '
                                  f'what it is in "what" — the kind is a filing label, and no '
                                  f'request is worth losing over it.')
        rows.append({
            'request_id': rid, 'needs': needs,
            'what': _text(it.get('what'), f'{w}.what', minimum=10),
            'why': _text(it.get('why'), f'{w}.why', minimum=10),
            'shape': _text(it.get('shape'), f'{w}.shape', required=False),
            'unblocks': _text(it.get('unblocks'), f'{w}.unblocks', minimum=5),
            'accession': _text(it.get('accession'), f'{w}.accession', required=False, limit=64),
            'where_to_look': [_text(x, f'{w}.where_to_look', minimum=1)
                              for x in it.get('where_to_look') or []][:8],
            'status': 'open', 'answer': None,
        })
    if not rows:
        raise K.ContractError('a data request names at least one thing the run could not get; '
                              'if nothing is missing, do not write one')
    if len(rows) > MAX_ITEMS:
        raise K.ContractError(f'{len(rows)} requests; the limit is {MAX_ITEMS}. Ask for what '
                              f'would change the answer, not for everything that was missing')
    doc = {'kind': 'data_requests', 'schema_version': K.PRODUCTION_VERSION,
           'project_id': project_id, 'run_id': run_id, 'created_at': K.now_iso(),
           'created_by': created_by, 'items': rows, 'note': NOTE}
    K.require_valid('data_requests', doc)
    return doc


def load(run_dir):
    """The requests this run wrote, or None."""
    from pathlib import Path
    try:
        doc = K.read_json(Path(run_dir) / NAME)
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get('kind') != 'data_requests':
        return None
    return doc


def answer(run_dir, request_id, *, dataset_id=None, note=None, url=None, provided_by=None):
    """Record what a person handed over, against one request.

    An answer with neither a dataset nor words is not an answer; declining is
    its own state, so a request nobody can satisfy stops being asked without
    pretending it was met.
    """
    from pathlib import Path
    doc = load(run_dir)
    if doc is None:
        raise K.ContractError('this run asked for nothing')
    item = next((i for i in doc['items'] if i['request_id'] == str(request_id)), None)
    if item is None:
        raise K.ContractError(f'no request {request_id!r} in this run')
    dataset_id = (dataset_id or '').strip() or None
    note = _text(note, 'note', required=False, limit=MAX_NOTE)
    url = (url or '').strip() or None
    if not (dataset_id or note or url):
        raise K.ContractError('give a dataset id, a link, or what you know in your own words')
    item['answer'] = {'dataset_id': dataset_id, 'note': note, 'url': url,
                      'provided_by': str(provided_by or 'a person')[:80], 'at': K.now_iso()}
    item['status'] = 'answered'
    K.require_valid('data_requests', doc)
    K.write_json_atomic(Path(run_dir) / NAME, doc)
    return item


def decline(run_dir, request_id, *, note=None, provided_by=None):
    """Say this one cannot be supplied, so the next round stops waiting on it."""
    from pathlib import Path
    doc = load(run_dir)
    if doc is None:
        raise K.ContractError('this run asked for nothing')
    item = next((i for i in doc['items'] if i['request_id'] == str(request_id)), None)
    if item is None:
        raise K.ContractError(f'no request {request_id!r} in this run')
    item['status'] = 'declined'
    item['answer'] = {'dataset_id': None,
                      'note': _text(note, 'note', required=False, limit=MAX_NOTE),
                      'url': None, 'provided_by': str(provided_by or 'a person')[:80],
                      'at': K.now_iso()}
    K.require_valid('data_requests', doc)
    K.write_json_atomic(Path(run_dir) / NAME, doc)
    return item


def handover(doc):
    """What a follow-up round should carry: the datasets, and the words, as text.

    The words are quoted as the person's report of their own process, which is
    the same standing a follow-up's results already have.
    """
    doc = doc or {}
    datasets, lines = [], []
    for it in doc.get('items') or []:
        a = it.get('answer') or {}
        if it.get('status') == 'answered' and a.get('dataset_id'):
            datasets.append(a['dataset_id'])
        if a.get('note'):
            lines.append(f'{it["request_id"]} ({it["what"][:80]}): {a["note"]}'
                         + (f' [{a["url"]}]' if a.get('url') else ''))
        elif a.get('url') and it.get('status') == 'answered':
            lines.append(f'{it["request_id"]}: see {a["url"]}')
        if it.get('status') == 'declined':
            lines.append(f'{it["request_id"]} cannot be supplied'
                         + (f': {a["note"]}' if a.get('note') else '')
                         + ' — do not ask for it again; work without it and say so.')
    return {'dataset_ids': list(dict.fromkeys(datasets)),
            'results': '\n'.join(lines) or None,
            'open': sum(1 for i in doc.get('items') or [] if i.get('status') == 'open'),
            'answered': sum(1 for i in doc.get('items') or [] if i.get('status') == 'answered')}
