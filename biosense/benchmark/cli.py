"""Benchmark CLI.

    python -m biosense.benchmark.cli list
    python -m biosense.benchmark.cli run      --config benchmarks/configs/<id>.json
    python -m biosense.benchmark.cli validate --benchmark benchmarks/public/<id>/benchmark.json
    python -m biosense.benchmark.cli export   --benchmark <path> --out <dir>

`run` executes the configuration, writes the bundle, and **refuses to write a
public bundle whose results depend on private lineage**. A private benchmark is
written outside the repository, under the private data root, and the command
says where.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .. import contracts as K
from ..data import roots as DR
from . import figures as FG
from . import privacy as PV
from . import report as RP
from . import runner as BR

PUBLIC_DIR = K.ROOT / 'benchmarks' / 'public'
CONFIG_DIR = K.ROOT / 'benchmarks' / 'configs'


def _print(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def bundle_dir(result, *, out=None):
    """Where a bundle belongs: public ones in the repository, private ones never."""
    if out:
        return Path(out)
    if result['privacy']['export_policy'] == 'private':
        return DR.private_root() / 'benchmarks' / result['benchmark_id']
    return PUBLIC_DIR / result['benchmark_id']


def write_bundle(result, out_dir):
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    BR.write(result, d)
    (d / 'summary.md').write_text(RP.summary_markdown(result), encoding='utf-8')
    figs = FG.write_all(result, d / 'figures')
    tables = RP.write_tables(result, d / 'tables')
    RP.write_detailed(result, d / 'detailed')
    return {'bundle': str(d), 'figures': [str(p.relative_to(d)) for p in figs],
            'tables': [str(p.relative_to(d)) for p in tables]}


def cmd_list(a):
    configs = sorted(CONFIG_DIR.glob('*.json')) if CONFIG_DIR.is_dir() else []
    out = []
    for p in configs:
        try:
            c = K.read_json(p)
            out.append({'benchmark_id': c['benchmark_id'], 'title': c['title'],
                        'project_id': c['project_id'], 'mode': c['mode'],
                        'export_policy': c['export_policy'], 'config': str(p.relative_to(K.ROOT))})
        except (ValueError, KeyError):
            continue
    built = sorted(p.parent.name for p in PUBLIC_DIR.glob('*/benchmark.json')) \
        if PUBLIC_DIR.is_dir() else []
    _print({'configs': out, 'built_public_benchmarks': built})
    return 0


def cmd_run(a):
    config = BR.load_config(a.config)
    result = BR.run(config, private_root=a.private_root)
    d = bundle_dir(result, out=a.out)

    if config['export_policy'] == 'public_safe' and not result['privacy']['safe_to_publish']:
        _print({'refused': True, 'benchmark_id': result['benchmark_id'],
                'reason': result['privacy']['refusal_reason'],
                'note': 'Nothing was written. Re-run against public or fixture data, or set '
                        'export_policy to "private".'})
        return 2

    info = write_bundle(result, d)
    findings = []
    if config['export_policy'] == 'public_safe':
        findings = PV.validate_bundle(d, result,
                                      private_ids=[x['dataset_id'] for x in result['datasets']
                                                   if x['visibility'] == 'private'])
    sc = result['scorecard']
    _print({'benchmark_id': result['benchmark_id'], **info,
            'scorecard': f'{sc["passed"]} PASS / {sc["failed"]} FAIL',
            'failed_rows': [r['capability'] for r in sc['rows'] if r['status'] == 'FAIL'],
            'export_policy': config['export_policy'],
            'safe_to_publish': result['privacy']['safe_to_publish'],
            'bundle_scan': findings or 'clean',
            'warnings': result['warnings'],
            'note': ('Private bundle, written outside the repository.'
                     if config['export_policy'] == 'private'
                     else 'Public bundle. Review summary.md before publishing.')})
    return 0 if sc['failed'] == 0 and not findings else 1


def cmd_validate(a):
    result = K.read_json(a.benchmark)
    problems = validate(result, Path(a.benchmark).parent)
    _print({'benchmark_id': result.get('benchmark_id'), 'problems': problems,
            'valid': not problems})
    return 0 if not problems else 1


def validate(result, bundle_dir_):
    """Every check the brief asks for, run over a built bundle."""
    problems = list(K.schema_errors('benchmark_result', result))
    d = Path(bundle_dir_)

    # referenced artifacts exist
    for rel in ('summary.md', 'benchmark.json', 'provenance/execution.json'):
        if not (d / rel).is_file():
            problems.append(f'missing artifact: {rel}')

    # ids resolve
    plan_refs = {a['plan_ref'] for a in result['analyses']}
    analysis_ids = {a['analysis_id'] for a in result['analyses']}
    for h in result['hypotheses']:
        for e in h['evidence']:
            if e['evidence_class'] == 'derived_analysis' and e.get('ref') \
                    and e['ref'] not in analysis_ids:
                problems.append(f'hypothesis evidence references unknown analysis {e["ref"]!r}')
    if result['selected_hypothesis'] and result['selected_hypothesis'] not in {
            h['hypothesis_id'] for h in result['hypotheses']}:
        problems.append('selected_hypothesis does not resolve')
    if result['analyses'] and not plan_refs:
        problems.append('an analysis carries no plan reference')

    # tool versions
    for a in result['analyses']:
        if not a.get('tool_version'):
            problems.append(f'{a["analysis_id"]} records no tool version')
        if not a['provenance'].get('input_checksums'):
            problems.append(f'{a["analysis_id"]} records no input checksums')

    # simulator identity and coverage honesty
    sim = result['simulator']
    if sim.get('effects') and not (sim.get('model') or {}).get('model_id'):
        problems.append('simulated effects carry no model identity')
    declared = {c['parameter'] for c in result['candidate_parameters']}
    accounted = {r['parameter_id'] for r in sim['handoff']['applied']} | \
                {r['parameter_id'] for r in sim['handoff']['skipped']}
    missing = declared - accounted
    if missing:
        problems.append(f'candidate parameter(s) neither applied nor explained: '
                        f'{", ".join(sorted(missing))}')
    for row in sim['handoff']['skipped']:
        if not row.get('reason'):
            problems.append(f'{row["parameter_id"]} is unmodelled with no stated reason')

    # every number has provenance
    for h in result['hypotheses']:
        for e in h['expected_effects']:
            if e['magnitude_estimated'] and not e.get('estimate_type'):
                problems.append(f'{e["metric"]} carries a magnitude with no estimate_type')
            if not e['magnitude_estimated'] and not e.get('withheld_reason'):
                problems.append(f'{e["metric"]} withholds a magnitude with no reason')
    for e in sim.get('effects') or []:
        if e['estimate_type'] != 'simulated':
            problems.append(f'simulator effect {e["metric"]} is not labelled simulated')

    # narrative invents nothing
    for s in result['narrative']:
        if not s['ok']:
            problems.append(f'narrative step {s["step"]} failed validation: '
                            f'{"; ".join(s["problems"])}')

    # privacy
    pv = result['privacy']
    if pv['export_policy'] == 'public_safe':
        if pv['has_private_lineage'] and pv['safe_to_publish']:
            problems.append('a public bundle claims to be safe while carrying private lineage')
        findings = PV.validate_bundle(d, result)
        problems += [f'{f["what"]} in {f["where"]}' for f in findings]

    # scorecard honesty
    sc = result['scorecard']
    if 'capability' not in sc['note'].lower() or 'biological truth' not in sc['note'].lower():
        problems.append('the scorecard does not say it measures capability rather than biology')
    return problems


def cmd_export(a):
    result = K.read_json(a.benchmark)
    d = bundle_dir(result, out=a.out)
    info = write_bundle(result, d)
    _print({**info, 'safe_to_publish': result['privacy']['safe_to_publish']})
    return 0


def cmd_export_hypothesis(a):
    """Export one hypothesis and everything needed to check it.

    --dry-run answers whether the export is permitted without creating anything,
    so a refusal can be shown to someone rather than cleaned up afterwards.
    """
    from ..evidence import export as HX
    result = K.read_json(a.benchmark)
    wanted = [h for h in (result.get('hypotheses') or [])
              if a.hypothesis in (None, h['hypothesis_id'])]
    if not wanted:
        raise K.ContractError(
            f'no hypothesis {a.hypothesis!r} in {a.benchmark}; it holds: '
            + ', '.join(h['hypothesis_id'] for h in (result.get('hypotheses') or [])))
    out = []
    for h in wanted:
        kw = dict(policy=a.policy, datasets=result.get('datasets') or [],
                  residuals=result.get('residuals') or [])
        if a.dry_run:
            out.append(HX.plan(h, **kw))
        else:
            d = Path(a.out) / h['hypothesis_id'] if a.out else None
            out.append(HX.write(h, d, **kw))
    _print(out if len(out) > 1 else out[0])
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog='python -m biosense.benchmark.cli',
                                 description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest='command', required=True)
    p = sub.add_parser('list', help='configurations and built benchmarks')
    p.set_defaults(fn=cmd_list)
    p = sub.add_parser('run', help='run a benchmark configuration and write its bundle')
    p.add_argument('--config', required=True)
    p.add_argument('--out', help='override the bundle directory')
    p.add_argument('--private-root', help='where private expert knowledge lives')
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser('validate', help='check a built benchmark bundle')
    p.add_argument('--benchmark', required=True)
    p.set_defaults(fn=cmd_validate)
    p = sub.add_parser('export', help='re-render a bundle from benchmark.json')
    p.add_argument('--benchmark', required=True)
    p.add_argument('--out')
    p.set_defaults(fn=cmd_export)
    p = sub.add_parser('export-hypothesis',
                       help='export one hypothesis and the evidence behind it')
    p.add_argument('--benchmark', required=True)
    p.add_argument('--hypothesis', help='hypothesis id; default every one in the benchmark')
    p.add_argument('--policy', choices=('public_safe', 'private'), default='public_safe')
    p.add_argument('--out', help='parent directory; each hypothesis gets its own folder')
    p.add_argument('--dry-run', action='store_true',
                   help='report whether the export is permitted, writing nothing')
    p.set_defaults(fn=cmd_export_hypothesis)
    a = ap.parse_args(argv)
    try:
        return a.fn(a)
    except (K.ContractError, ValueError, KeyError, FileNotFoundError) as e:
        _print({'error': type(e).__name__, 'message': str(e)})
        return 1


if __name__ == '__main__':
    sys.exit(main())
