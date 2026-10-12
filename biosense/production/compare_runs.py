"""Two runs of the same question, side by side.

A run of this system is a language model reading papers. Ask it the same thing
twice and it will not write the same file twice, and until now there was no way
to find out how much it moved — which left a person with one answer and no idea
whether it was the answer or one of several the run could have produced.

So: two runs, compared by code, on what each of them concluded.

The comparison is in two halves, and keeping them apart is the point.

**What went in.** Project, objective, runtime, model, effort, datasets, the
add-ons that were on. Two runs with different inputs did not measure
variability — they measured the inputs. Every input difference found is
reported first, because it is the explanation for anything that follows.

**What came out.** The levers each run proposed and which way, the values its
protocol recommends, the route it chose, how close it expects the product to
be. Per lever: both agreed, they disagreed, or only one of them raised it.

What this never does is pick a winner. Nothing here knows which run is right —
agreement is evidence that the pipeline is stable, not that it is correct, and
two runs can agree on something no experiment supports. The verdict words say
how much the two moved, and nothing about the biology.
"""
from __future__ import annotations

import json
from pathlib import Path

from .. import contracts as K

# Below this a difference between two recommended values is reporting noise
# (a rounded dose, a float printed back), not two different recommendations.
SAME_VALUE_REL = 1e-9
# The inputs that have to match before two runs are comparable at all. Each is
# (key, how to read it, what it means when it differs).
VERDICTS = ('same_inputs_same_answer', 'same_inputs_different_answer',
            'different_inputs', 'not_comparable')
VERDICT_TEXT = {
    'same_inputs_same_answer':
        'The two runs were asked the same thing and proposed the same levers in the same '
        'directions. That is the pipeline being stable; it is not evidence that either '
        'answer is biologically right.',
    'same_inputs_different_answer':
        'The two runs were asked the same thing and did not agree. The differences below are '
        'what varies between runs of this question — treat anything only one run found as '
        'unconfirmed until a round at the bench settles it.',
    'different_inputs':
        'These runs were not asked the same thing. The input differences listed first explain '
        'the rest, and nothing below measures run-to-run variability until they match.',
    'not_comparable':
        'At least one of these runs produced no protocol, so there is nothing to compare.',
}


def _read(path):
    try:
        return K.read_json(path)
    except (OSError, ValueError):
        return None


def facts(run_dir):
    """Everything about one run that a comparison looks at. Nothing derived."""
    d = Path(run_dir)
    req = _read(d / 'discovery_request.json') or {}
    state = _read(d / 'app_run.json') or {}
    protocol = _read(d / 'protocol_summary.json')
    route = _read(d / 'route_selection.json')
    fid = _read(d / 'cell_fidelity.json')
    base = _read(d / 'baseline_protocol.json')
    session = state.get('session') or {}
    ctx = req.get('research_context') or {}

    levers, values = {}, {}
    for row in (protocol or {}).get('hypothesis_ledger') or []:
        lev = row.get('lever') or {}
        pid = lev.get('parameter_id') or row.get('parameter_id')
        if not pid:
            continue
        levers[pid] = {
            'parameter_id': pid, 'label': lev.get('label') or pid,
            'direction': lev.get('direction'), 'value': lev.get('candidate_value'),
            'unit': lev.get('unit'), 'stage': lev.get('stage'),
            'hypothesis_id': row.get('hypothesis_id'),
            'adopted': bool(row.get('adopted')), 'status': row.get('status'),
            'confidence': row.get('confidence'),
        }
    for st in (protocol or {}).get('stages') or []:
        for p in st.get('parameters') or []:
            values[(st['stage_id'], p['parameter_id'])] = {
                'stage_id': st['stage_id'], 'parameter_id': p['parameter_id'],
                'label': p.get('label'), 'unit': p.get('unit'),
                'value': p.get('recommended_value'), 'provenance': p.get('provenance'),
                'changed': bool(p.get('changed')),
            }
    return {
        'run_dir': d.name,
        'run_id': state.get('run_id'),
        'title': state.get('title'),
        'status': state.get('status'),
        'started_at': state.get('started_at'),
        'inputs': {
            'project_id': req.get('project_id'),
            'objective': (req.get('objective') or '').strip(),
            'runtime_mode': req.get('runtime_mode'),
            'model': session.get('llm_model') or state.get('model'),
            'engine': session.get('harness') or state.get('engine'),
            'effort': req.get('effort'),
            'mode': req.get('mode'),
            'purpose': req.get('purpose'),
            'dataset_ids': sorted(req.get('dataset_ids') or []),
            'public_data': bool(req.get('public_data')),
            'constraints': sorted(x for x in (req.get('constraints') or []) if x),
            'context_strictness': ctx.get('strictness'),
            'continued_from': ((req.get('continued_from') or {}).get('run_id')),
        },
        'has_protocol': protocol is not None,
        'levers': levers,
        'values': values,
        'route': ((route or {}).get('chosen') or {}).get('route_id'),
        'route_label': ((route or {}).get('chosen') or {}).get('label'),
        'need_verdict': ((route or {}).get('need_assessment') or {}).get('verdict'),
        'fidelity': ((fid or {}).get('overall') or {}).get('expected_match'),
        'baseline_factors': sorted({f['name'] for s in (base or {}).get('stages') or []
                                    for f in s.get('factors') or []}),
        'gaps': len((protocol or {}).get('gaps') or []),
        'limitations': len((protocol or {}).get('limitations') or []),
        'hypotheses': len((protocol or {}).get('hypothesis_ledger') or []),
    }


# What an input difference means for the comparison, in the words a person
# needs to act on it. Only emitted for the ones actually found to differ.
INPUT_ADVICE = {
    'project_id': 'Different projects: different stages, parameters and bounds, so the '
                  'protocols are not comparable line by line.',
    'objective': 'The objective text differs. The whole run is steered by it, so this is the '
                 'first thing to make identical before comparing anything else.',
    'runtime_mode': 'One run was synthetic and the other used real agents, or they used '
                    'different runtimes. Only compare runs of the same runtime.',
    'model': 'A different model answered. Pin BIOSENSE_AGENT_MODEL to one value for both runs.',
    'engine': 'A different agent harness ran them.',
    'effort': 'Different effort budgets: the deeper run searched more and had more to work '
              'from, so finding more is expected rather than informative.',
    'mode': 'Different modes — one of them worked out the route before the parameters, the '
            'other did not.',
    'purpose': 'The runs had different purposes, so they were not trying to produce the '
               'same thing.',
    'dataset_ids': 'They read different datasets.',
    'public_data': 'One could search public databases and the other could not.',
    'constraints': 'Different process constraints were in force.',
    'context_strictness': 'Different research-context strictness: one accepted evidence from '
                          'further away than the other.',
    'continued_from': 'One of these continues an earlier run, so it started from that run\'s '
                      'artifacts rather than from nothing.',
}
# True of every pair of real-AI runs, however carefully the inputs are matched.
ALWAYS = ('These runs are language models reading papers. Identical inputs do not produce '
          'identical output, so a difference below is the ordinary variability of the method '
          'unless an input differs too — which is why it is measured here rather than assumed.',
          'A literature search run on two different days sees a different corpus. Where that '
          'matters, run both from the same literature cache directory.')


def _same_number(a, b):
    if a is None or b is None:
        return a is None and b is None
    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
        return a == b
    if a == b:
        return True
    scale = max(abs(a), abs(b)) or 1.0
    return abs(a - b) / scale <= SAME_VALUE_REL


def compare(a_dir, b_dir):
    """The comparison document for two run directories."""
    A, B = facts(a_dir), facts(b_dir)
    if A['run_dir'] == B['run_dir']:
        raise K.ContractError('a run compared with itself says nothing; name two runs')

    inputs = []
    for key, advice in INPUT_ADVICE.items():
        av, bv = A['inputs'].get(key), B['inputs'].get(key)
        if av != bv:
            inputs.append({'field': key, 'a': av, 'b': bv, 'means': advice})

    levers = []
    for pid in sorted(set(A['levers']) | set(B['levers'])):
        la, lb = A['levers'].get(pid), B['levers'].get(pid)
        if la and lb:
            same_dir = la['direction'] == lb['direction']
            same_val = _same_number(la['value'], lb['value'])
            agree = ('agreed' if same_dir and same_val else
                     'same_direction' if same_dir else 'opposed')
        else:
            agree = 'only_a' if la else 'only_b'
        levers.append({'parameter_id': pid,
                       'label': (la or lb)['label'], 'unit': (la or lb).get('unit'),
                       'agreement': agree, 'a': la, 'b': lb})

    values = []
    for key in sorted(set(A['values']) | set(B['values'])):
        va, vb = A['values'].get(key), B['values'].get(key)
        if not (va and vb):
            values.append({'stage_id': key[0], 'parameter_id': key[1],
                           'label': (va or vb).get('label'), 'unit': (va or vb).get('unit'),
                           'agreement': 'only_a' if va else 'only_b', 'a': va, 'b': vb})
            continue
        same = _same_number(va['value'], vb['value'])
        values.append({'stage_id': key[0], 'parameter_id': key[1], 'label': va.get('label'),
                       'unit': va.get('unit'),
                       'agreement': 'agreed' if same else 'differs', 'a': va, 'b': vb})

    shared = [l for l in levers if l['agreement'] not in ('only_a', 'only_b')]
    agreed = [l for l in shared if l['agreement'] == 'agreed']
    changed_a = {k for k, v in A['values'].items() if v['changed']}
    changed_b = {k for k, v in B['values'].items() if v['changed']}

    if not (A['has_protocol'] and B['has_protocol']):
        verdict = 'not_comparable'
    elif inputs:
        verdict = 'different_inputs'
    elif (len(shared) == len(levers) and len(agreed) == len(levers)
          and all(v['agreement'] == 'agreed' for v in values)):
        verdict = 'same_inputs_same_answer'
    else:
        verdict = 'same_inputs_different_answer'

    # What to change so a rerun is comparable. Only what was actually found,
    # plus the two things that are always true of this method.
    todo = [i['means'] for i in inputs]
    if verdict == 'same_inputs_different_answer':
        only = [l for l in levers if l['agreement'] in ('only_a', 'only_b')]
        if only:
            todo.append(f'{len(only)} lever(s) were raised by one run and not the other. A '
                        f'lever one run of two found is a candidate, not a finding: put it in '
                        f'the round plan as an arm rather than in the protocol.')
        opposed = [l for l in levers if l['agreement'] == 'opposed']
        if opposed:
            todo.append(f'{len(opposed)} lever(s) were pushed in opposite directions by the '
                        f'two runs. Read both hypotheses\' evidence: this usually means the '
                        f'sources disagree and neither run said so.')
        if any(v['agreement'] == 'differs' for v in values):
            todo.append('Some recommended doses differ. Where the dose matters, the round plan '
                        'should test the range the two runs fall in rather than either '
                        'number — picking one of them is picking a run, not a value.')
    todo += list(ALWAYS)

    return {
        'kind': 'run_comparison', 'schema_version': K.PRODUCTION_VERSION,
        'created_at': K.now_iso(),
        'a': {k: A[k] for k in ('run_dir', 'run_id', 'title', 'status', 'started_at',
                                'inputs', 'has_protocol', 'route', 'route_label',
                                'need_verdict', 'fidelity', 'gaps', 'limitations',
                                'hypotheses', 'baseline_factors')},
        'b': {k: B[k] for k in ('run_dir', 'run_id', 'title', 'status', 'started_at',
                                'inputs', 'has_protocol', 'route', 'route_label',
                                'need_verdict', 'fidelity', 'gaps', 'limitations',
                                'hypotheses', 'baseline_factors')},
        'input_differences': inputs,
        'levers': levers,
        'values': values,
        'summary': {
            'levers_total': len(levers),
            'levers_in_both': len(shared),
            'levers_agreed': len(agreed),
            'levers_opposed': sum(1 for l in levers if l['agreement'] == 'opposed'),
            'levers_only_one': sum(1 for l in levers
                                   if l['agreement'] in ('only_a', 'only_b')),
            'values_compared': sum(1 for v in values if v['agreement'] in ('agreed', 'differs')),
            'values_differing': sum(1 for v in values if v['agreement'] == 'differs'),
            'changed_in_both': len(changed_a & changed_b),
            'route_agreed': (A['route'] == B['route']) if (A['route'] or B['route']) else None,
            'fidelity_agreed': ((A['fidelity'] == B['fidelity'])
                                if (A['fidelity'] or B['fidelity']) else None),
        },
        'verdict': verdict,
        'verdict_text': VERDICT_TEXT[verdict],
        'to_make_them_comparable': todo,
        'note': 'Agreement between two runs says the pipeline is stable on this question. It '
                'is not evidence that either answer is biologically right, and neither run is '
                'called better than the other here.',
    }


AGREE_WORD = {'agreed': 'agreed', 'same_direction': 'same direction, different dose',
              'opposed': 'OPPOSED', 'only_a': 'only run A', 'only_b': 'only run B',
              'differs': 'differs'}


def markdown(c):
    """The comparison as a page a person can paste into a lab notebook."""
    s = c['summary']
    L = [f'# Two runs compared: {c["a"]["run_dir"]} and {c["b"]["run_dir"]}', '',
         c['verdict_text'], '', c['note'], '']
    if c['input_differences']:
        L += ['## What was different going in', '']
        for d in c['input_differences']:
            L.append(f'- **{d["field"]}** — A: `{d["a"]}` · B: `{d["b"]}`. {d["means"]}')
        L.append('')
    L += ['## Levers', '',
          f'{s["levers_agreed"]} of {s["levers_total"]} agreed; {s["levers_opposed"]} opposed; '
          f'{s["levers_only_one"]} raised by one run only.', '']
    for l in c['levers']:
        a, b = l['a'] or {}, l['b'] or {}
        L.append(f'- **{l["label"]}** — {AGREE_WORD[l["agreement"]]}; '
                 f'A: {a.get("direction") or "—"} {a.get("value") if a else "—"} · '
                 f'B: {b.get("direction") or "—"} {b.get("value") if b else "—"}')
    L += ['', '## Recommended values', '',
          f'{s["values_differing"]} of {s["values_compared"]} differ.', '']
    for v in c['values']:
        if v['agreement'] == 'agreed':
            continue
        a, b = v['a'] or {}, v['b'] or {}
        L.append(f'- **{v["label"] or v["parameter_id"]}** ({v["stage_id"]}) — '
                 f'{AGREE_WORD[v["agreement"]]}; A: {a.get("value", "—")} · '
                 f'B: {b.get("value", "—")}')
    L += ['', '## To make a rerun comparable', '']
    L += [f'- {t}' for t in c['to_make_them_comparable']]
    return '\n'.join(L) + '\n'


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog='python -m biosense.production.compare_runs',
                                 description=__doc__.splitlines()[0])
    ap.add_argument('a'); ap.add_argument('b')
    ap.add_argument('--markdown', action='store_true')
    ap.add_argument('--out')
    a = ap.parse_args(argv)
    try:
        doc = compare(a.a, a.b)
    except K.ContractError as e:
        print(json.dumps({'refused': True, 'reason': str(e)}, indent=2))
        return 2
    if a.out:
        K.write_json_atomic(a.out, doc)
    print(markdown(doc) if a.markdown else json.dumps(doc, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
