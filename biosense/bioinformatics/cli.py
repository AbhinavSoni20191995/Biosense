"""Bioinformatics CLI for the orchestrator and the bioinformatics agent.

Two families of command. The older one answers "what does an annotation say about
this gene"; the newer one answers "what data exists, what analysis would reduce
this uncertainty, and what did it find".

    datasets register|list|search|show     describe and find datasets
    analyse plan|run                       plan against a named uncertainty, execute it
    tools                                  what can run, and what is only declared


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
        # Validate the object that is actually written, not the one that existed
        # before the cross-check was attached. An artifact on disk that fails its
        # own schema is a contract that only looks enforced.
        K.require_valid('bioinformatics_report', report)
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


# ── datasets ──────────────────────────────────────────────────────────────

def cmd_datasets_register(a):
    from ..data import ingest as ING
    design = {}
    for key, val in (('condition_column', a.condition_column), ('control', a.control),
                     ('replicate_column', a.replicate_column), ('batch_column', a.batch_column),
                     ('sample_id_column', a.sample_id_column), ('donor_column', a.donor_column),
                     ('timepoint_column', a.timepoint_column)):
        if val:
            design[key] = val
    if a.treatment:
        design['treatments'] = list(a.treatment)
    m, path = ING.ingest_local(
        a.file, dataset_id=a.dataset_id, title=a.title, modality=a.modality,
        organism=a.organism, cell_type=a.cell_type, perturbation=a.perturbation,
        experimental_design=design, description=a.description, registered_by=a.registered_by,
        visibility=a.visibility, overwrite=a.overwrite)
    _print({'dataset_id': m['dataset_id'], 'manifest': str(path),
            'visibility': m['visibility'], 'evidence_class': m['evidence_class'],
            'modality': m['modality'], 'citable': m['citable'],
            'checksums': [f['checksum_sha256'] for f in m['files']],
            'sample_count': (m['sample_metadata'] or {}).get('sample_count'),
            'missing_metadata': m['missing_metadata'],
            'note': ('Private. Stored under the private data root, never served over HTTP, never '
                     'committed, and never a literature citation.'
                     if m['visibility'] == 'private' else 'Public.')})
    return 0


def cmd_datasets_list(a):
    from ..data import registry as DREG
    rows = [DREG.summary(m) for m in DREG.list_datasets(include_private=not a.public_only)]
    _print({'datasets': rows, 'count': len(rows),
            'private_included': not a.public_only})
    return 0


def cmd_datasets_show(a):
    from ..data import registry as DREG
    m = DREG.require(a.dataset_id)
    _print({'manifest': m, 'lineage': DREG.lineage_of(a.dataset_id)})
    return 0


def cmd_datasets_search(a):
    from ..data import sources
    src = sources.get(a.source)
    res = src.search(a.query, organism=a.organism, modality=a.modality,
                     cell_type=a.cell_type, perturbation=a.perturbation, limit=a.limit,
                     live=a.live, i_have_network_permission=a.i_have_network_permission)
    _print(res.to_dict())
    return 0


# ── analysis ──────────────────────────────────────────────────────────────

def cmd_analyse_plan(a):
    from . import plan as PLAN
    if not a.hypothesis and not a.evidence_gap:
        raise K.ContractError(
            'name what this analysis resolves: --evidence-gap <id> for a gap you are recording '
            'now (with --uncertainty saying what is unknown), or --hypothesis <id> for one from '
            'an analysis report. --uncertainty alone is the words without the reference.')
    unc = ({'kind': 'hypothesis', 'ref': a.hypothesis, 'statement': a.uncertainty,
            'raised_by': a.created_by, 'source_artifact': a.analysis_report}
           if a.hypothesis else
           PLAN.evidence_gap(a.evidence_gap, a.uncertainty, raised_by=a.created_by)
           if a.evidence_gap else None)
    report = K.read_json(a.analysis_report) if a.analysis_report else None
    p = PLAN.plan(
        plan_id=a.plan_id, question=a.question, uncertainty_ref=unc,
        why_requested=a.why, dataset_ids=a.dataset_ids, analysis_type=a.analysis_type,
        tool=a.tool, decision_relevance=a.decision_relevance,
        parameters_that_may_change=a.parameters or [], created_by=a.created_by,
        comparison=({'group_column': a.group_column, 'control': a.control,
                     'treatment': a.treatment_level, 'paired': a.paired, 'covariates': [],
                     'readouts': a.readouts or []} if a.group_column or a.control else None),
        loop_id=a.loop_id, iteration=a.iteration, analysis_report=report,
        recorded_gaps=[a.evidence_gap] if a.evidence_gap else ())
    if a.out:
        K.write_json_atomic(a.out, p)
    _print({**p, 'out': a.out})
    return 0


def cmd_analyse_run(a):
    from . import execute as EX
    p = K.read_json(a.plan)
    r = EX.execute(p, executed_by=a.executed_by, analysis_id=a.analysis_id, out=a.out)
    if a.out:
        K.write_text_atomic(Path(a.out).with_suffix('.md'), EX.render(r))
    _print({'analysis_id': r['analysis_id'], 'evidence_class': r['evidence_class'],
            'source_evidence_class': r['source_evidence_class'],
            'source_visibility': r['source_visibility'], 'confidence': r['confidence'],
            'statistics': r['statistics'], 'key_findings': r['key_findings'],
            'process_implications': r['process_implications'],
            'candidate_process_parameters': r['candidate_process_parameters'],
            'limitations': r['limitations'], 'citable': r['citable'],
            'out': a.out,
            'note': 'Candidates only. This result cannot change a protocol; the orchestrator '
                    'decides, and a revision still has to pass the envelope.'})
    return 0


def cmd_tools(a):
    from . import external as EXT
    from . import registry as TREG
    d = TREG.describe()
    d['external_adapters'] = EXT.describe()
    _print(d)
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

    # ── datasets ──────────────────────────────────────────────────────────
    ds = sub.add_parser('datasets', help='register, list and search datasets').add_subparsers(
        dest='datasets_command', required=True)
    p = ds.add_parser('register', help='register a local table as a dataset (private by default)')
    p.add_argument('--file', required=True)
    p.add_argument('--dataset-id', required=True)
    p.add_argument('--title', required=True)
    p.add_argument('--modality')
    p.add_argument('--organism', default='Homo sapiens')
    p.add_argument('--cell-type'); p.add_argument('--perturbation')
    p.add_argument('--condition-column'); p.add_argument('--control')
    p.add_argument('--treatment', nargs='*')
    p.add_argument('--replicate-column'); p.add_argument('--batch-column')
    p.add_argument('--sample-id-column'); p.add_argument('--donor-column')
    p.add_argument('--timepoint-column')
    p.add_argument('--description'); p.add_argument('--registered-by')
    p.add_argument('--visibility', default='private', choices=('private', 'public'))
    p.add_argument('--overwrite', action='store_true')
    p.set_defaults(fn=cmd_datasets_register)
    p = ds.add_parser('list', help='registered datasets')
    p.add_argument('--public-only', action='store_true')
    p.set_defaults(fn=cmd_datasets_list)
    p = ds.add_parser('show', help='one dataset manifest and its lineage')
    p.add_argument('--dataset-id', required=True); p.set_defaults(fn=cmd_datasets_show)
    p = ds.add_parser('search', help='find candidate datasets (offline by default)')
    p.add_argument('--query', required=True)
    p.add_argument('--source', default='geo')
    p.add_argument('--organism'); p.add_argument('--modality')
    p.add_argument('--cell-type'); p.add_argument('--perturbation')
    p.add_argument('--limit', type=int, default=10)
    p.add_argument('--live', action='store_true')
    p.add_argument('--i-have-network-permission', action='store_true')
    p.set_defaults(fn=cmd_datasets_search)

    # ── analysis ──────────────────────────────────────────────────────────
    an = sub.add_parser('analyse', help='plan and run an analysis').add_subparsers(
        dest='analyse_command', required=True)
    p = an.add_parser('plan', help='write an AnalysisPlan against a named uncertainty')
    p.add_argument('--plan-id', required=True)
    p.add_argument('--question', required=True)
    p.add_argument('--hypothesis', help='hypothesis_id from an analysis report')
    p.add_argument('--evidence-gap', help='identifier for a recorded evidence gap')
    p.add_argument('--uncertainty', required=True, help='what is not known, in words')
    p.add_argument('--analysis-report', help='the report the hypothesis comes from')
    p.add_argument('--why', required=True, help='why ask now rather than revise or stop')
    p.add_argument('--dataset-ids', nargs='+', required=True)
    p.add_argument('--analysis-type', required=True)
    p.add_argument('--tool', required=True)
    p.add_argument('--decision-relevance', required=True)
    p.add_argument('--parameters', nargs='*')
    p.add_argument('--group-column'); p.add_argument('--control')
    p.add_argument('--treatment-level'); p.add_argument('--paired', action='store_true')
    p.add_argument('--readouts', nargs='*')
    p.add_argument('--created-by', default='bioinformatics_agent')
    p.add_argument('--loop-id'); p.add_argument('--iteration', type=int)
    p.add_argument('--out'); p.set_defaults(fn=cmd_analyse_plan)
    p = an.add_parser('run', help='execute a plan and write the AnalysisResult')
    p.add_argument('--plan', required=True)
    p.add_argument('--analysis-id'); p.add_argument('--executed-by', default='bioinformatics_agent')
    p.add_argument('--out'); p.set_defaults(fn=cmd_analyse_run)

    p = sub.add_parser('tools', help='the analysis tool registry and external adapters')
    p.set_defaults(fn=cmd_tools)
    a = ap.parse_args(argv)
    try:
        return a.fn(a)
    except (K.ContractError, ValueError, KeyError, FileNotFoundError) as e:
        _print({'error': type(e).__name__, 'message': str(e)})
        return 1


if __name__ == '__main__':
    sys.exit(main())
