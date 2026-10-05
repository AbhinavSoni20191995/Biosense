"""Regenerate the README's benchmark section from benchmark.json.

    uv run --frozen python scripts/update_benchmark_readme.py

Only the block between the two markers is rewritten; the rest of the README is
left exactly as it was. Benchmark numbers are never typed into the README by
hand, because a hand-copied number is a number that goes stale silently and a
reader has no way to tell.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from biosense import contracts as K              # noqa: E402
from biosense.evidence import estimates as E     # noqa: E402

START = '<!-- BENCHMARK:START -->'
END = '<!-- BENCHMARK:END -->'
DEFAULT = K.ROOT / 'benchmarks' / 'public' / 'macrophage_mcsf_demo' / 'benchmark.json'


def section(result, bundle_rel):
    r = result
    sim = r['simulator']
    h = r['hypotheses'][0] if r['hypotheses'] else None
    sc = r['scorecard']
    L = [START, '',
         '## A worked demonstration',
         '',
         f'> **SYNTHETIC DEMONSTRATION.** Every input is an invented fixture committed to this',
         f'> repository and the simulator is a mechanistic stand-in. No number below is a',
         f'> measurement of any real cell.',
         '',
         f'**{r["title"]}** — project `{r["project"]["project_id"]}` '
         f'v{r["project"]["version"]}, run offline with no model API and no network.',
         '',
         '```bash',
         'uv run --frozen python -m biosense.benchmark.cli run \\',
         '  --config benchmarks/configs/macrophage_mcsf_demo.json',
         '```',
         '',
         '<picture>',
         f'  <source media="(prefers-color-scheme: dark)" srcset="{bundle_rel}/figures/workflow-dark.svg">',
         f'  <img alt="The benchmark workflow: objective, uncertainty, evidence, analysis, '
         f'hypothesis, candidate parameter, simulator, next experiment." '
         f'src="{bundle_rel}/figures/workflow-light.svg" width="100%">',
         '</picture>',
         '']

    L += [f'BioSense started from the objective *"{r["objective"]}"*, identified the unresolved '
          f'question `{r["uncertainties"][0]["ref"]}`, and planned an analysis against it.', '']

    if r['analyses']:
        a = r['analyses'][0]
        L += [f'It ran `{a["tool"]}` v{a["tool_version"]} over '
              f'`{r["datasets"][0]["dataset_id"]}`:', '']
        for f in a['key_findings'][:2]:
            L.append(f'- {f["finding"]} — *{f["basis"]}*')
        L.append('')

    if h:
        par = h['parameter']
        L += ['### The hypothesis it formed', '', h['statement'], '',
              '| Outcome | Baseline → Candidate | Change | Provenance |', '|---|---|---|---|']
        for e in h['expected_effects']:
            label = e.get('label') or e['metric'].replace('_', ' ')
            tag = e['estimate_type'].upper()
            if not e['magnitude_estimated']:
                L.append(f'| {label} | direction {e["direction"]} | '
                         f'magnitude not yet estimated | {tag} |')
                continue
            a_ = (e.get('baseline') or {}).get('value')
            b_ = (e.get('candidate') or {}).get('value')
            unit = '%' if E.is_percent(e['unit']) else f' {e["unit"]}'
            chg = (f'{e["absolute_change"]:+.4g} pp' if e['change_unit'] == E.PP
                   else f'{e["absolute_change"]:+.4g}{unit}')
            rel = (f' ({e["relative_change_pct"]:+.4g}%)'
                   if e['relative_change_pct'] is not None else '')
            L.append(f'| {label} | {a_:.4g}{unit} → {b_:.4g}{unit} | {chg}{rel} | {tag} |')
        L += ['', f'**Confidence: {h["confidence"]}.** '
                  f'{" ".join(h["confidence_basis"])}', '']

    L += ['### Simulator coverage', '',
          'Every candidate parameter is accounted for. A parameter the model cannot predict '
          'is labelled, never dropped and never predicted anyway.', '',
          '<picture>',
          f'  <source media="(prefers-color-scheme: dark)" srcset="{bundle_rel}/figures/parameter_change-dark.svg">',
          f'  <img alt="Candidate parameters and simulator coverage: M-CSF maps to a model knob '
          f'and is modelled; temperature is a real design variable the model has no term for." '
          f'src="{bundle_rel}/figures/parameter_change-light.svg" width="100%">',
          '</picture>', '']

    if sim.get('effects'):
        L += ['<picture>',
              f'  <source media="(prefers-color-scheme: dark)" srcset="{bundle_rel}/figures/simulator_comparison-dark.svg">',
              f'  <img alt="Control versus candidate in the project simulator, with every number '
              f'labelled SIMULATED." src="{bundle_rel}/figures/simulator_comparison-light.svg" '
              f'width="100%">',
              '</picture>', '']

    if r.get('next_experiment'):
        L += [f'**Next experiment.** {r["next_experiment"]["summary"]}', '']

    L += [f'**Capability scorecard: {sc["passed"]} PASS / {sc["failed"]} FAIL.** '
          f'{sc["note"]}', '',
          f'Full bundle — report, figures, tables, provenance and the audit package → '
          f'[`{bundle_rel}/`]({bundle_rel}/) · '
          f'how benchmarks work → [docs/BENCHMARKING.md](docs/BENCHMARKING.md)', '',
          END]
    return '\n'.join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--benchmark', default=str(DEFAULT))
    ap.add_argument('--readme', default=str(K.ROOT / 'README.md'))
    ap.add_argument('--check', action='store_true',
                    help='exit non-zero if the README is out of date, changing nothing')
    a = ap.parse_args(argv)

    result = K.read_json(a.benchmark)
    bundle_rel = str(Path(a.benchmark).parent.relative_to(K.ROOT))
    new = section(result, bundle_rel)

    readme = Path(a.readme)
    text = readme.read_text()
    if START not in text or END not in text:
        print(f'{readme} has no {START} / {END} markers; add them where the section belongs',
              file=sys.stderr)
        return 2
    before, rest = text.split(START, 1)
    _, after = rest.split(END, 1)
    updated = before + new + after
    if a.check:
        if updated != text:
            print('the README benchmark section is out of date; run this script', file=sys.stderr)
            return 1
        print('README benchmark section is current')
        return 0
    if updated != text:
        readme.write_text(updated)
        print(f'updated the benchmark section of {readme}')
    else:
        print('no change needed')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
