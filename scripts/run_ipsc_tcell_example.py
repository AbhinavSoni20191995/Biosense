"""Run the worked iPSC -> T-lineage example and write the reports kept in the repo.

    uv run --frozen python scripts/run_ipsc_tcell_example.py --out reports

Two loops, from the two halves of the question this example answers:

  wt     wild-type only: what the control alone reaches, and the best protocol
         the search finds for it;
  bach2  wild-type against an isogenic BACH2 knockout: what changes when a
         knocked-out line has to be expanded to the same target.

Both run against the synthetic stand-in. Nothing here is biological evidence,
nothing calls a model, and nothing needs network access.

The HTML and PDF it writes are committed, so a reader can see the reasoning
without running anything. Re-running overwrites them; the loop directories
themselves go under runs/ and are git-ignored.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from biosense import contracts as K            # noqa: E402
from biosense.production import engine as EN   # noqa: E402
from biosense.production import report as RP   # noqa: E402

EX = K.ROOT / 'examples' / 'ipsc_tcell'
LOOPS = (('wt', 'request.wt_d40.json',
          'Wild type only: iPSC to T lineage, day 40'),
         ('bach2', 'request.bach2_d40.json',
          'Wild type against an isogenic BACH2 knockout: iPSC to T lineage, day 40'))

INTRO = """
<h3>What this comparison answers</h3>
<p>The first loop asks what a wild-type iPSC-derived T-lineage process reaches on its own, and
what the best schedule the search can find for it looks like. The second asks the same of a line
carrying a BACH2 knockout that must still be expanded to the same target, alongside its own
isogenic control.</p>
<p>The result worth reading is not either arm's number. It is <b>whether one shared schedule can
serve both genotypes</b>. Where the search finds that it cannot, it says so explicitly and splits
that parameter per arm, and the split is listed below.</p>
<h3>What it does not answer</h3>
<p>The reactor is a synthetic stand-in whose per-arm behaviour was invented. It was built so that
the levers the curated BACH2 annotation suggests are the ones that pay off, which is what makes
the demonstration legible and also what makes it worthless as biology. The curated annotations
separate what a cited paper reports from a transfer to this cell type, and the cited BACH2 work
is in mouse and human peripheral or engineered T cells, not in an iPSC-derived differentiation.
No numeric value in either protocol is attributed to any publication, because no full text was
retrieved.</p>
"""


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--out', default='reports', help='where the committed reports are written')
    ap.add_argument('--runs', default='runs', help='where the loop directories go')
    ap.add_argument('--reports-only', action='store_true',
                    help='rewrite the reports from loop directories that already exist, '
                         'without running the loops again')
    a = ap.parse_args(argv)
    out, runs = Path(a.out), Path(a.runs)
    out.mkdir(parents=True, exist_ok=True)
    runs.mkdir(parents=True, exist_ok=True)
    truth = K.read_json(EX / 'standin_truth.synthetic.json')

    dirs, results = [], []
    for name, req_file, title in LOOPS:
        d = runs / f'example-ipsc-tcell-{name}'
        request = K.require_valid('production_request', K.read_json(EX / req_file))
        if a.reports_only:
            if not (d / 'loop_state.json').is_file():
                raise SystemExit(f'--reports-only needs an existing loop at {d}')
            print(f'\n=== {name}: reusing {d} ===', flush=True)
            dirs.append(d)
            results.append((name, title, K.read_json(d / 'ENGINE_SUMMARY.json')))
            html, pdf = RP.write_report(d, out / f'{name}.reasoning_report.html',
                                        out / f'{name}.reasoning_report.pdf',
                                        title=f'Reasoning report: {title}')
            print(f'  -> {html}' + (f' and {pdf}' if pdf else ''), flush=True)
            continue
        if d.exists():
            shutil.rmtree(d)
        print(f'\n=== {name}: {request["request_id"]} ===', flush=True)

        def show(e, _n=name):
            if e['kind'] == 'analysis':
                arms = ' | '.join(f'{x["arm_id"]} {x["observed"]:.2f}'
                                  f'{"MET" if x["status"] == "MET" else "   "}'
                                  for x in e['arms'])
                print(f'  it{e["iteration"]:<3} {e["verdict"]:<24} {arms}', flush=True)
            elif e['kind'] == 'revision' and e.get('findings'):
                for f in e['findings']:
                    if f not in getattr(show, 'seen', set()):
                        show.seen = getattr(show, 'seen', set()) | {f}
                        print(f'  FINDING: {f}', flush=True)
            elif e['kind'] in ('terminal', 'refusal', 'error'):
                print(f'  {e["kind"].upper()}: {e.get("type") or e.get("detail")}', flush=True)

        summary = EN.run_loop(request, d, standin='ipsc_tcell', truth=truth, on_event=show)
        dirs.append(d)
        results.append((name, title, summary))
        html, pdf = RP.write_report(d, out / f'{name}.reasoning_report.html',
                                    out / f'{name}.reasoning_report.pdf',
                                    title=f'Reasoning report: {title}')
        print(f'  -> {html}' + (f' and {pdf}' if pdf else ''), flush=True)

    html, pdf = RP.write_comparative_report(
        dirs, out / 'comparative.reasoning_report.html',
        out / 'comparative.reasoning_report.pdf',
        title='Wild type against a BACH2 knockout: iPSC to T lineage',
        intro=INTRO)
    print(f'\ncomparative -> {html}' + (f' and {pdf}' if pdf else ''), flush=True)

    print('\n=== summary ===')
    for name, _title, s in results:
        best = ', '.join(f'{k} {v["observed"]:.2f} ({"met" if v["status"] == "MET" else "not met"})'
                         for k, v in sorted((s.get('best') or {}).items()))
        print(f'  {name:<6} {s["terminal"]:<22} '
              f'{s.get("iterations_used")}/{s.get("max_iterations")} iterations  {best}')
    print('\nSynthetic stand-in throughout. Not biological evidence.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
