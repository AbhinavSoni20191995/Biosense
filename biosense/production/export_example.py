"""Turn a finished real run into a small example folder fit for the repository.

A run directory is not something to commit: it holds downloaded full texts,
raw event logs, model transcripts and, sometimes, results computed from a
person's private data. An example is the part a reader needs — what was asked,
what the agents found and concluded, the protocol, its gaps and limitations —
and nothing that the project's rules keep out of version control.

    python -m biosense.production.export_example --run runs/ai-20261007-ab12cd \\
        --name macrophage-retinoid --out examples/real

What is copied (when present): the request; the protocol summary (JSON and
Markdown); every quantified hypothesis; design choices; the literature,
bioinformatics and analyst running notes; analysis plans, results and
interpretations computed from PUBLIC data; the landscape summary; the
simulation summaries. What is never copied: `sources/` and any full text,
`events.jsonl`, caches, drafts, the run's state record, and any analysis whose
source is private. A synthetic demo run is refused: it is already in the
repository as the worked demonstration, and labelling it "real" would mislead.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

from .. import contracts as K

NAME_RE = re.compile(r'^[a-z0-9][a-z0-9-]{2,63}$')
TOP = ('discovery_request.json', 'protocol_summary.json', 'protocol_summary.md',
       'design_choices.json', 'research_context.json', 'landscape_summary.md',
       'simulation.json')
GLOBS = ('quantified_hypothesis*.json', 'analysis_plan*.json', 'analysis_result*.json',
         'interpretation*.json', 'analyst/analysis_plan*.json', 'analyst/analysis_result*.json',
         'analyst/interpretation*.json', 'analyst/*/agent_analysis.json',
         'bioinformatics/insights.md', 'bioinformatics/gene_info.json',
         'literature/insights.md', 'literature/*/insights.md', 'literature/merged/discover.md')
NEVER = ('sources', 'cache', 'events.jsonl', 'app_run.json', '.draft.json', 'transcript')
MAX_FILE = 2 * 1024 * 1024


def _private(doc):
    return isinstance(doc, dict) and (doc.get('source_visibility') == 'private'
                                      or any((d or {}).get('visibility') == 'private'
                                             for d in doc.get('datasets') or doc.get('inputs') or []))


def export(run_dir, *, name, out_root, overwrite=False):
    run = Path(run_dir)
    if not (run / 'discovery_request.json').is_file():
        raise K.ContractError(f'{run} is not a discovery run directory (no discovery_request.json)')
    if not NAME_RE.match(name or ''):
        raise K.ContractError('name the example in lower-case letters, digits and hyphens')
    req = K.read_json(run / 'discovery_request.json')
    if req.get('runtime_mode') == 'synthetic_demo':
        raise K.ContractError('this is a synthetic demo run; a real example must come from a run '
                              'with a real AI runtime')
    dest = Path(out_root) / name
    if dest.exists():
        if not overwrite:
            raise K.ContractError(f'{dest} exists; pass --overwrite to replace it')
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    copied, withheld = [], []
    candidates = [run / f for f in TOP] + [p for g in GLOBS for p in sorted(run.glob(g))]
    for src in candidates:
        if not src.is_file():
            continue
        rel = src.relative_to(run)
        if any(n in str(rel) for n in NEVER):
            continue
        if src.stat().st_size > MAX_FILE:
            withheld.append(f'{rel} (larger than 2 MB)')
            continue
        if src.suffix == '.json':
            try:
                doc = K.read_json(src)
            except (OSError, ValueError):
                withheld.append(f'{rel} (unreadable)')
                continue
            if _private(doc):
                withheld.append(f'{rel} (computed from private data)')
                continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        copied.append(str(rel))
    (dest / 'README.md').write_text(_readme(name, req, run, dest, copied, withheld),
                                    encoding='utf-8')
    return {'example': str(dest), 'copied': copied, 'withheld': withheld}


def _readme(name, req, run, dest, copied, withheld):
    proto = None
    if (dest / 'protocol_summary.json').is_file():
        proto = K.read_json(dest / 'protocol_summary.json')
    hyps = [K.read_json(dest / f) for f in copied if Path(f).name.startswith('quantified_hypothesis')]
    L = [f'# Real example: {name}', '',
         '> A BioSense run with a real AI runtime, exported with '
         '`python -m biosense.production.export_example`. Proposed, not approved: nothing here '
         'has been run in a bioreactor unless a section below says so.', '',
         f'**Objective.** {req.get("objective")}', '',
         f'**Project.** `{req.get("project_id")}` · **Runtime.** `{req.get("runtime_mode")}` · '
         f'**Effort.** {req.get("effort") or "standard"} · **Run.** `{run.name}`', '',
         '<!-- Add screenshots beside this file (e.g. timeline.png) and reference them here: -->',
         '<!-- ![Protocol timeline](timeline.png) -->', '']
    if hyps:
        L += ['## Hypotheses', '']
        for h in hyps:
            L.append(f'- **{h.get("status", "proposed")}, {h.get("confidence") or "?"} '
                     f'confidence** — {h.get("statement")}')
        L.append('')
    if proto:
        c = proto.get('summary_counts') or {}
        L += ['## Protocol', '',
              f'See [protocol_summary.md](protocol_summary.md). '
              + (f'{c.get("parameters_changed")} of {c.get("parameters_total")} parameters change; '
                 if c else '')
              + f'{len(proto.get("gaps") or [])} gap(s) block the wet lab.', '']
        lims = proto.get('limitations') or []
        if lims:
            L += ['## Limitations', ''] + [f'- {x}' for x in lims[:12]] + ['']
    L += ['## Files', ''] + [f'- [{f}]({f})' for f in copied] + ['']
    if withheld:
        L += ['## Withheld', '', 'Kept out of the repository on purpose:', ''] + \
             [f'- {w}' for w in withheld] + ['']
    L += ['## How this was produced', '',
          '1. In the web app, AI Discovery, with the objective above and a real runtime.',
          '2. `python -m biosense.production.export_example --run <run dir> --name '
          f'{name} --out examples/real`',
          '3. Screenshots added by hand; this README edited for context.', '']
    return '\n'.join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--run', required=True, help='the run directory (runs/ai-…)')
    ap.add_argument('--name', required=True, help='lower-case-with-hyphens')
    ap.add_argument('--out', default='examples/real')
    ap.add_argument('--overwrite', action='store_true')
    a = ap.parse_args(argv)
    try:
        r = export(a.run, name=a.name, out_root=a.out, overwrite=a.overwrite)
    except K.ContractError as e:
        print(json.dumps({'refused': True, 'reason': str(e)}))
        return 1
    print(json.dumps(r, indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
