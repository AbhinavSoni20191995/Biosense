"""Support for the AI analyst: look at the data, run what the tools cannot, say what it means.

The deterministic tools are right and literal: a plan that names `Treatment`
where the table says `treatment`, or a value column called `log2_cpm` where the
tool looks for `logcpm`, is refused — correctly — and a run used to end there
with "no plan". The analyst agent is the judgement around the tools: it reads
the data, writes the plan that fits it, repairs it from the refusal, and says
what the result means for this process. These three commands are what it uses.
None of them lets a model produce a statistic.

    inspect    a dataset's columns, groups, sizes and which tools can run on it,
               with the plan arguments that fit — so a plan is written against
               the data, not against a guess about it;
    script     an analysis the tools cannot express, written by the agent as a
               Python script and run here: the script, its inputs' checksums,
               its output and its exit code are kept, and the result is labelled
               AGENT-WRITTEN with a low confidence ceiling — never mistaken for
               a validated tool;
    interpret  the agent's reading of a result, checked: its confidence is
               capped by the result's own, by the dataset's evidence weight
               (species and route) and by how close the experiment is to this
               process, and each cap says why.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path

from .. import contracts as K
from ..data import manifest as MF
from ..data import registry as REG
from ..data import tables as TB

ORDER = {'low': 0, 'moderate': 1, 'high': 2}
LEVELS = ('low', 'moderate', 'high')
MATCH = ('same', 'related', 'distant')
SCRIPT_TIMEOUT_S = 300
MAX_LEVELS = 30


def _lower(a, b):
    return a if ORDER[a] <= ORDER[b] else b


# ── inspect ─────────────────────────────────────────────────────────────
def inspect(dataset_id):
    """What a dataset holds and which plans can run on it."""
    from . import registry as TREG
    m = REG.require(dataset_id)
    out = {'dataset_id': dataset_id, 'title': m.get('title'), 'modality': m.get('modality'),
           'organism': m.get('organism'), 'visibility': m.get('visibility'),
           'evidence_class': m.get('evidence_class'), 'accession': m.get('accession'),
           'experimental_design': m.get('experimental_design'),
           'notes': m.get('notes'), 'limitations': m.get('limitations')}
    files = MF.readable_files(m)
    if files:
        t = TB.read_table(MF.resolve_path(files[0]['path']), files[0]['file_type'])
        cats = {}
        for c in t.columns:
            if c in t.numeric:
                continue
            levels = t.levels(c)
            if len(levels) <= MAX_LEVELS:
                cats[c] = {lv: sum(1 for r in t.rows if r[c] == lv) for lv in levels}
            else:
                cats[c] = f'{len(levels)} distinct values (an identifier or a feature column)'
        out.update(rows=t.n, columns=t.columns, numeric_columns=sorted(t.numeric),
                   categorical_columns=cats)
    tools = []
    for spec in TREG.TOOLS.values():
        if m.get('modality') not in spec.modalities:
            continue
        tools.append({'tool': spec.name, 'analysis_types': list(spec.analysis_types),
                      'needs': list(spec.required_metadata), 'notes': spec.notes[:300]})
    out['tools_that_accept_this_modality'] = tools
    d = m.get('experimental_design') or {}
    if d.get('condition_column') and d.get('control') and tools:
        tr = (d.get('treatments') or [None])[0]
        out['suggested_plan_arguments'] = (
            f'--dataset-ids {dataset_id} --tool {tools[0]["tool"]} --analysis-type '
            f'{tools[0]["analysis_types"][0]} --group-column "{d["condition_column"]}" '
            f'--control "{d["control"]}"' + (f' --treatment-level "{tr}"' if tr else ''))
    ceiling = _ceiling_from(m)
    if ceiling:
        out['evidence_ceiling'] = ceiling
    return out


def _ceiling_from(m):
    hit = re.search(r'confidence ceiling for this species and route: (low|moderate|high)',
                    m.get('notes') or '')
    return hit.group(1) if hit else None


# ── script ──────────────────────────────────────────────────────────────
def _sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def run_script(script, *, dataset_ids, question, out_dir, analysis_id=None,
               timeout=SCRIPT_TIMEOUT_S, executed_by='analyst_agent'):
    """Run an agent-written analysis script and keep everything about it.

    The script is called as `python <script> --data <table> [...] --out <dir>`
    and must write `<dir>/result.json`: {"method": "...", "findings": ["..."],
    "statistics": [{"name", "value", ...}]}. Numbers come from that file, which
    the script computed; nothing here or in the agent's reply is retyped.
    """
    script = Path(script).resolve()
    if not script.is_file() or script.suffix != '.py':
        raise K.ContractError(f'{script} is not a Python script')
    out = Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    inputs, data_args, visibilities, ceilings = [], [], set(), []
    for did in dataset_ids:
        m = REG.require(did)
        files = MF.readable_files(m) or m['files']
        if not files:
            raise K.ContractError(f'{did} has no file to read')
        path = MF.resolve_path(files[0]['path'])
        data_args += ['--data', str(path)]
        inputs.append({'dataset_id': did, 'title': m.get('title'), 'source': m.get('source'),
                       'accession': m.get('accession'), 'visibility': m.get('visibility'),
                       'evidence_class': m.get('evidence_class'), 'path_checksum': _sha(path)})
        visibilities.add(m.get('visibility'))
        if _ceiling_from(m):
            ceilings.append(_ceiling_from(m))
    started = time.time()
    try:
        proc = subprocess.run([sys.executable, str(script), *data_args, '--out', str(out)],
                              cwd=str(out), capture_output=True, text=True, timeout=timeout)
        code, so, se = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as e:
        code, so, se = -1, (e.stdout or ''), f'timed out after {timeout} s'
    result_path = out / 'result.json'
    result, problem = None, None
    if code != 0:
        problem = f'the script exited {code}: {(se or so or "")[-600:]}'
    elif not result_path.is_file():
        problem = 'the script finished without writing result.json'
    else:
        try:
            result = json.loads(result_path.read_text())
            if not isinstance(result, dict) or not isinstance(result.get('findings'), list):
                raise ValueError('result.json needs a "findings" list')
        except ValueError as e:
            problem, result = f'result.json is not usable: {e}', None
    confidence = 'low'
    for c in ceilings:
        confidence = _lower(confidence, c)
    doc = {
        'schema_version': K.PRODUCTION_VERSION,
        'analysis_id': analysis_id or f'agent-{hashlib.sha1(str(started).encode()).hexdigest()[:10]}',
        'created_at': K.now_iso(), 'method_kind': 'agent_written', 'question': question,
        'executed_by': executed_by, 'succeeded': problem is None, 'problem': problem,
        'script': {'path': str(script), 'sha256': _sha(script), 'text': script.read_text()[:20000]},
        'inputs': inputs,
        'source_visibility': 'private' if 'private' in visibilities else 'public',
        'method': (result or {}).get('method'),
        'findings': [str(x)[:500] for x in (result or {}).get('findings') or []][:20],
        'statistics': ((result or {}).get('statistics') or [])[:200],
        'confidence': confidence, 'citable': False,
        'stdout_tail': (so or '')[-2000:], 'stderr_tail': (se or '')[-2000:],
        'exit_code': code, 'seconds': round(time.time() - started, 1),
        'limitations': [
            'AGENT-WRITTEN ANALYSIS: an AI wrote this script for this question. It ran here '
            'and its numbers are what the script computed, but the method has not been '
            'validated the way the registered tools are; its confidence is capped at low.',
            'Read the script (kept above) before relying on it.'],
    }
    K.require_valid('agent_analysis', doc)
    K.write_json_atomic(out / 'agent_analysis.json', doc)
    return doc


# ── interpret ───────────────────────────────────────────────────────────
TEMPLATE = {
    'analysis_ref': '<path to analysis_result.json or agent_analysis.json>',
    'question': 'Does retinoic acid raise GATA6 in macrophages?',
    'what_it_shows': 'GATA6 is higher with ATRA (log2 difference 1.9, q 0.01, n 3 vs 3).',
    'meaning_for_process': 'Supports adding a retinoid at maturation to push peritoneal identity.',
    'transfer': {'species': 'Homo sapiens', 'cells': 'related', 'stage': 'same',
                 'treatment': 'same', 'notes': 'monocyte-derived, not iPSC-derived'},
    'confidence': 'moderate',
    'confidence_reason': 'one public experiment, adequate replicates, related cells',
    'confirm_with': 'the same comparison in an iPSC-macrophage series, or qPCR here',
    'recommendation': 'Carry ATRA 100 nM at maturation as a candidate lever, direction up.',
    'caveats': ['bulk RNA; protein not measured'],
}


def interpret(draft, *, result=None, manifests=None):
    """Check an interpretation and cap its confidence. Returns the record."""
    if not isinstance(draft, dict):
        raise K.ContractError('an interpretation is an object')
    for key in ('analysis_ref', 'what_it_shows', 'meaning_for_process', 'confidence',
                'confidence_reason', 'confirm_with'):
        if not str(draft.get(key) or '').strip():
            raise K.ContractError(f'an interpretation needs {key}')
    claimed = draft['confidence']
    if claimed not in LEVELS:
        raise K.ContractError(f'confidence must be one of {LEVELS}')
    tr = dict(draft.get('transfer') or {})
    for k in ('cells', 'stage', 'treatment'):
        if tr.get(k) not in MATCH:
            raise K.ContractError(f'transfer.{k} must be one of {MATCH}: how close the '
                                  f'experiment\'s {k} is to this process')
    caps = []
    level = claimed
    if result is not None:
        rc = result.get('confidence')
        if rc in LEVELS and ORDER[rc] < ORDER[level]:
            level = rc
            caps.append(f'the result itself is {rc} confidence')
        if result.get('method_kind') == 'agent_written' and level != 'low':
            level = 'low'
            caps.append('an agent-written analysis carries low confidence at most')
    for m in manifests or []:
        c = _ceiling_from(m)
        if c and ORDER[c] < ORDER[level]:
            level = c
            caps.append(f'{m["dataset_id"]} ({m.get("organism")}) carries a {c} evidence '
                        f'ceiling for its species and processing route')
    worst = max((tr[k] for k in ('cells', 'stage', 'treatment')), key=MATCH.index)
    tcap = {'same': 'high', 'related': 'moderate', 'distant': 'low'}[worst]
    if ORDER[tcap] < ORDER[level]:
        level = tcap
        caps.append(f'the experiment\'s {"/".join(k for k in ("cells", "stage", "treatment") if tr[k] == worst)} '
                    f'is {worst} to this process')
    doc = {
        'schema_version': K.PRODUCTION_VERSION, 'created_at': K.now_iso(),
        'analysis_ref': str(draft['analysis_ref']), 'question': draft.get('question'),
        'what_it_shows': str(draft['what_it_shows'])[:1500],
        'meaning_for_process': str(draft['meaning_for_process'])[:1500],
        'transfer': {'species': tr.get('species'), 'cells': tr['cells'], 'stage': tr['stage'],
                     'treatment': tr['treatment'], 'notes': tr.get('notes')},
        'confidence': level, 'confidence_claimed': claimed, 'confidence_capped_by': caps,
        'confidence_reason': str(draft['confidence_reason'])[:800],
        'confirm_with': str(draft['confirm_with'])[:800],
        'recommendation': (str(draft.get('recommendation') or '')[:800] or None),
        'caveats': [str(c)[:300] for c in draft.get('caveats') or []][:10],
        'evidence_status': 'interpretation',
        'note': ('An AI analyst\'s reading of a computed result. The numbers are the '
                 'result\'s; the meaning is a judgement, with its confidence capped by the '
                 'evidence it rests on.'),
    }
    K.require_valid('analysis_interpretation', doc)
    return doc


def interpret_file(draft_path, out):
    draft = K.read_json(draft_path)
    ref = Path(str(draft.get('analysis_ref') or ''))
    result, manifests = None, []
    if ref.is_file():
        result = K.read_json(ref)
        ids = ([d.get('dataset_id') for d in result.get('datasets') or []]
               or [d.get('dataset_id') for d in result.get('inputs') or []])
        for did in ids:
            m = REG.load(did) if did else None
            if m:
                manifests.append(m)
    else:
        raise K.ContractError(f'analysis_ref {ref} is not a file: point it at the result '
                              f'this interprets')
    doc = interpret(draft, result=result, manifests=manifests)
    K.write_json_atomic(out, doc)
    return doc
