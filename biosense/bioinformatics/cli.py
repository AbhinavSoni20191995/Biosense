"""Bioinformatics CLI for the orchestrator and the bioinformatics agent.

    .venv/bin/python -m biosense.bioinformatics.cli annotate --genes EXH1 --perturbation knockout \
        --cell-type "CAR-T cell" --protocol runs/loop1/it1/protocol.approved.json --out runs/loop1/it1/bioinfo.json

Deterministic and offline. A gene with no annotation returns found=false plus the
public queries to run; `live-lookup` performs them only when you ask for it.
"""
import argparse
import json
import sys
from pathlib import Path

from .. import contracts as K
from . import knowledge as KB
from . import tools as BT


def _print(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def cmd_annotate(a):
    report = BT.annotate(a.genes, perturbation=a.perturbation, cell_type=a.cell_type,
                         knowledge_sets=a.knowledge_set, loop_id=a.loop_id, iteration=a.iteration,
                         question=a.question, asked_by=a.asked_by)
    out = {'report_id': report['report_id'],
           'genes': [{'symbol': g['symbol'], 'found': g['found'], 'status': g['status'],
                      'effects': [{k: e[k] for k in ('affected_process', 'direction', 'readout_metric',
                                                     'confidence')} for e in g['effects']],
                      'live_lookup_plan': [p['source'] for p in (g['live_lookup_plan'] or [])] or None}
                     for g in report['genes']],
           'lever_suggestions': report['lever_suggestions'],
           'untestable_here': [u['assay'] for u in report['untestable_here']],
           'limitations': report['limitations']}
    if a.protocol:
        protocol = K.read_json(a.protocol)
        cross = BT.cross_check(report, protocol)
        report['protocol_cross_check'] = cross
        out['protocol_cross_check'] = cross
    if a.out:
        p = Path(a.out)
        K.write_json_atomic(p, report)
        K.write_text_atomic(p.with_suffix('.md'), BT.render_report(report))
        out['out'] = str(p)
        out['report'] = str(p.with_suffix('.md'))
    _print(out)
    return 0 if any(g['found'] for g in report['genes']) else 2


def cmd_live_lookup(a):
    if not a.i_have_network_permission:
        _print({'refused': 'live lookups touch the network. Pass --i-have-network-permission only when the '
                           'request sets bioinformatics.live_lookups true.'})
        return 1
    res = BT.live_lookup(a.gene, sources=a.source or None, timeout=a.timeout)
    if a.out:
        K.write_json_atomic(a.out, res)
        res = {**res, 'out': a.out}
    _print({k: v for k, v in res.items() if k != 'results'} |
           {'results': [{'source': r['source'], 'url': r['url'], 'bytes': len(json.dumps(r['payload']))}
                        for r in res.get('results', [])]})
    return 0


def cmd_plan(a):
    _print({'symbol': a.gene, 'live_lookup_plan': KB.live_lookup_plan(a.gene),
            'note': 'Run these yourself, or with live-lookup when the request permits network access. '
                    'Curate the result into bioinfo_knowledge/ before a decision relies on it.'})
    return 0


def cmd_sets(a):
    sets = KB.load_sets(a.knowledge_set)
    _print({'knowledge_sets': KB.set_manifest(sets),
            'genes': sorted({g for s in sets for g in s['genes']})})
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog='python -m biosense.bioinformatics.cli')
    sub = ap.add_subparsers(dest='command', required=True)
    p = sub.add_parser('annotate', help='what the loaded knowledge sets say about a gene perturbation')
    p.add_argument('--genes', nargs='+', required=True)
    p.add_argument('--perturbation', default='knockout')
    p.add_argument('--cell-type')
    p.add_argument('--knowledge-set', nargs='*')
    p.add_argument('--protocol', help='cross-check the implicated levers against what this protocol already does per arm')
    p.add_argument('--loop-id'); p.add_argument('--iteration', type=int)
    p.add_argument('--question'); p.add_argument('--asked-by')
    p.add_argument('--out'); p.set_defaults(fn=cmd_annotate)
    p = sub.add_parser('plan', help='the public queries that would annotate a gene')
    p.add_argument('--gene', required=True); p.set_defaults(fn=cmd_plan)
    p = sub.add_parser('live-lookup', help='fetch public annotations (network; off by default)')
    p.add_argument('--gene', required=True); p.add_argument('--source', nargs='*')
    p.add_argument('--timeout', type=int, default=20); p.add_argument('--out')
    p.add_argument('--i-have-network-permission', action='store_true')
    p.set_defaults(fn=cmd_live_lookup)
    p = sub.add_parser('sets', help='list loaded knowledge sets and their genes')
    p.add_argument('--knowledge-set', nargs='*'); p.set_defaults(fn=cmd_sets)
    a = ap.parse_args(argv)
    try:
        return a.fn(a)
    except (K.ContractError, ValueError, KeyError, FileNotFoundError) as e:
        _print({'error': type(e).__name__, 'message': str(e)})
        return 1


if __name__ == '__main__':
    sys.exit(main())
