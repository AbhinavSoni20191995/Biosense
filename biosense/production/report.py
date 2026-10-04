"""Reasoning reports: why the loop did what it did, with references and rationales.

The loop already writes what it decided. This module writes **why**, in a form a
person can audit without reading JSON:

* every decision, with its reasoning, the alternatives it considered and why each
  was refused, the hypotheses it raised and the basis for each, the tool results
  it cited, the action set it was validated against, and - where an agent chose
  differently from the deterministic policy - both, side by side;
* every search move, what it was trying to fix, what it moved and by how much,
  what happened to each arm, and whether the move survived or was reverted;
* every quantity in the final protocol with its provenance, so a reader can see
  at a glance which numbers rest on a citation and which are design choices;
* a references section listing each cited source with its DOI, what it was used
  for, and whether its full text was ever retrieved;
* a limitations section that states what the run cannot support.

Two things this module will not do. It does not describe deterministic policy
output as a model's reasoning: every decision is labelled with its `authored_by`,
and a policy-authored reason is presented as the rule it is. And it does not
upgrade provenance: a design choice is shown as a design choice however
confident the surrounding prose sounds.

`comparative_report` puts two or more loops side by side, which is what answers a
question of the form "and what happens if the gene is knocked out instead".

PDF: `write_pdf` prints the HTML with headless Chromium when one is present, and
otherwise says so and leaves the HTML, rather than shipping a broken file.
"""
from __future__ import annotations

import html
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .. import contracts as K

PROV_LABEL = {
    'reported': 'Reported',
    'adapted': 'Adapted',
    'design_choice': 'Design choice',
    'gap': 'Gap (no value)',
}
PROV_NOTE = {
    'reported': 'The value equals a cited claim.',
    'adapted': 'Transferred or converted from cited claims, with a rationale.',
    'design_choice': 'Chosen without direct evidence. Needs human approval and carries no citation.',
    'gap': 'No value. Blocks a wet-lab run.',
}
AUTHOR_LABEL = {
    'policy': 'deterministic policy (not a model)',
    'orchestrator_agent': 'orchestrator agent (a reasoning model, validated against the envelope)',
}
OUTCOME_LABEL = {
    'better': 'kept - beat the best reading',
    'worse': 'reverted - fell below the best reading',
    'flat': 'direction abandoned - no material change',
    'conflict': 'reverted and split per arm - helped one arm, hurt another',
}


def _e(x):
    return html.escape('' if x is None else str(x), quote=True)


def _num(x, nd=3):
    """A number for display.

    Trailing zeros are stripped only when a decimal point is present. Stripping
    them unconditionally turns 10 into 1 and 500 into 5 whenever `nd` is 0, which
    is exactly the case axis ticks use.
    """
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return _e(x)
    if not math.isfinite(x):
        return _e(x)
    if abs(x) >= 1e6:
        return f'{x:.3e}'
    out = f'{x:,.{nd}f}'
    return out.rstrip('0').rstrip('.') if '.' in out else out


def _pct(x):
    return '' if x is None else f'{x * 100:+.1f}%'


# ── reading a loop directory ────────────────────────────────

def load_loop(loop_dir):
    """Everything the report needs from one loop directory.

    Files whose name contains 'truth' are never read: the stand-in's hidden
    answers are not part of what the loop knew, and a report that quoted them
    would be describing a different run than the one the agents saw.
    """
    d = Path(loop_dir)
    state = K.read_json(d / 'loop_state.json')
    out = {'dir': d, 'loop_id': state.get('loop_id', d.name), 'state': state,
           'handoff': None, 'search': None, 'summary': None,
           'decisions': [], 'stages': [], 'consults': [], 'bioinfo': []}
    for name, key in (('handoff.json', 'handoff'), ('search_state.json', 'search'),
                      ('ENGINE_SUMMARY.json', 'summary')):
        p = d / name
        if p.exists():
            out[key] = K.read_json(p)
    for p in sorted((d / 'decisions').glob('decision-*.json')):
        out['decisions'].append(K.read_json(p))
    for sd in sorted(x for x in d.iterdir() if x.is_dir() and x.name.startswith('it')):
        rec = {'stage': sd.name}
        for name, key in (('analysis.json', 'analysis'), ('protocol.approved.json', 'protocol'),
                          ('run.json', 'run'), ('bioinfo.json', 'bioinfo')):
            p = sd / name
            if p.exists():
                rec[key] = K.read_json(p)
        out['stages'].append(rec)
        if 'bioinfo' in rec:
            out['bioinfo'].append(rec['bioinfo'])
    for p in sorted((d / 'consults').glob('*.json')):
        c = K.read_json(p)
        existing = {x['consult_id']: i for i, x in enumerate(out['consults'])}
        if c['consult_id'] in existing and not c.get('answer'):
            continue
        if c['consult_id'] in existing:
            out['consults'][existing[c['consult_id']]] = c
        else:
            out['consults'].append(c)
    out['stages'].sort(key=lambda r: (r.get('protocol') or {}).get('iteration', 0))
    return out


def final_protocol(loop):
    """The last approved protocol, which is the best configuration the search held."""
    with_p = [s for s in loop['stages'] if s.get('protocol')]
    return with_p[-1]['protocol'] if with_p else None


def final_analysis(loop):
    with_a = [s for s in loop['stages'] if s.get('analysis')]
    return with_a[-1]['analysis'] if with_a else None


def iter_quantities(protocol):
    """(path, quantity) for every quantity in a protocol, including arm adjustments."""
    cs = protocol['culture_system']
    yield 'culture_system.working_volume', cs['working_volume']
    for name, q in sorted(cs['parameters'].items()):
        yield f'culture_system.parameters.{name}', q
    for st in protocol['stages']:
        yield f'{st["stage_id"]}.medium', st['medium']
        for s in st['steps']:
            if s.get('quantity'):
                label = s.get('factor') or s['action']
                yield f'{st["stage_id"]}.{s["step_id"]} ({label})', s['quantity']
    for a in protocol.get('arm_adjustments', []):
        if a.get('quantity'):
            yield f'arm_adjustment {a["adjustment_id"]} [{a["arm_id"]}] {a["step_id"]}', a['quantity']


def provenance_counts(protocol):
    counts = {}
    for _p, q in iter_quantities(protocol):
        counts[q['provenance']] = counts.get(q['provenance'], 0) + 1
    return counts


def references(loop):
    """Every source the loop actually used, with what it was used for."""
    refs = {}
    h = loop.get('handoff') or {}
    for src in h.get('source_manifest', []):
        refs[src['id']] = {'id': src['id'], 'citation': src.get('citation') or src['id'],
                           'doi': src.get('doi'), 'pmcid': src.get('pmcid'),
                           'url': src.get('url'),
                           'full_text_retrieved': src.get('full_text_retrieved'),
                           'used_for': set(), 'kind': 'literature'}
    for c in h.get('claims', []):
        sid = (c.get('evidence') or {}).get('source_id')
        if sid in refs:
            refs[sid]['used_for'].add(f'claim {c["id"]} ({c["parameter"]})')
    for bio in loop.get('bioinfo', []):
        for g in bio.get('genes', []):
            for eff in g.get('effects', []):
                key = f'annotation:{eff["source"]}'
                refs.setdefault(key, {'id': key, 'citation': eff['source'], 'doi': None,
                                      'pmcid': None, 'url': None, 'full_text_retrieved': None,
                                      'used_for': set(), 'kind': 'annotation',
                                      'confidence': eff['confidence']})
                refs[key]['used_for'].add(
                    f'{g["symbol"]} {eff["perturbation"]}: {eff["direction"]} '
                    f'{eff["readout_metric"]} [{eff["confidence"]}]')
    for r in refs.values():
        r['used_for'] = sorted(r['used_for'])
    return sorted(refs.values(), key=lambda r: (r['kind'], r['citation']))


# ── HTML pieces ─────────────────────────────────────────────

CSS = """
:root{--bg:#fbfbfa;--fg:#1a1a18;--mut:#5c5c56;--line:#dcdcd6;--card:#fff;
--ok:#1f6f43;--bad:#9b2226;--warn:#8a5a00;--info:#1b4e7a;--accent:#30343f;
--design:#8a5a00;--reported:#1f6f43;--adapted:#1b4e7a;--gap:#9b2226;}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
--bg:#16171a;--fg:#e9e9e4;--mut:#a3a39c;--line:#2e3034;--card:#1d1f23;
--ok:#6cc48d;--bad:#f08a8a;--warn:#e0b25c;--info:#78b4e0;--accent:#c9ccd4;
--design:#e0b25c;--reported:#6cc48d;--adapted:#78b4e0;--gap:#f08a8a;}}
:root[data-theme="dark"]{--bg:#16171a;--fg:#e9e9e4;--mut:#a3a39c;--line:#2e3034;
--card:#1d1f23;--ok:#6cc48d;--bad:#f08a8a;--warn:#e0b25c;--info:#78b4e0;
--accent:#c9ccd4;--design:#e0b25c;--reported:#6cc48d;--adapted:#78b4e0;--gap:#f08a8a;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.6 ui-sans-serif,system-ui,
-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;}
.wrap{max-width:62rem;margin:0 auto;padding:2.5rem 16px 5rem}
h1{font-size:1.9rem;line-height:1.25;margin:0 0 .3rem}
h2{font-size:1.3rem;margin:2.6rem 0 .8rem;padding-bottom:.35rem;border-bottom:2px solid var(--line)}
h3{font-size:1.05rem;margin:1.6rem 0 .5rem}
h4{font-size:.95rem;margin:1.1rem 0 .35rem;color:var(--mut)}
p,li{margin:.5rem 0}
.sub{color:var(--mut);margin:0 0 1.2rem}
.banner{border:1px solid var(--warn);border-left:5px solid var(--warn);background:var(--card);
padding:.9rem 1.1rem;border-radius:6px;margin:1.2rem 0}
.banner b{color:var(--warn)}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:1rem 1.2rem;margin:.9rem 0}
.card.dec{border-left:4px solid var(--info)}
.card.rev{border-left:4px solid var(--design)}
table{width:100%;border-collapse:collapse;margin:.7rem 0;font-size:.9rem}
th,td{text-align:left;padding:.45rem .55rem;border-bottom:1px solid var(--line);
vertical-align:top}
th{font-weight:600;color:var(--mut);font-size:.8rem;text-transform:uppercase;letter-spacing:.03em}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
code,.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.86em}
.tag{display:inline-block;padding:.08rem .45rem;border-radius:4px;font-size:.74rem;
font-weight:600;border:1px solid currentColor;white-space:nowrap}
.t-reported{color:var(--reported)}.t-adapted{color:var(--adapted)}
.t-design_choice{color:var(--design)}.t-gap{color:var(--gap)}
.ok{color:var(--ok);font-weight:600}.bad{color:var(--bad);font-weight:600}
.warn{color:var(--warn);font-weight:600}.mut{color:var(--mut)}
.kv{display:grid;grid-template-columns:minmax(9rem,auto) 1fr;gap:.2rem .9rem;font-size:.9rem}
.kv dt{color:var(--mut)}.kv dd{margin:0}
ul.tight{margin:.3rem 0;padding-left:1.2rem}
ul.tight li{margin:.2rem 0}
.why{border-left:3px solid var(--line);padding-left:.9rem;margin:.6rem 0;color:var(--fg)}
blockquote{margin:.6rem 0;padding:.6rem .9rem;background:var(--bg);border:1px solid var(--line);
border-radius:6px;font-size:.92rem}
.toc{columns:2;column-gap:2rem;font-size:.92rem}
@media (max-width:40rem){.toc{columns:1}.kv{grid-template-columns:1fr}}
.foot{margin-top:3rem;padding-top:1rem;border-top:1px solid var(--line);
color:var(--mut);font-size:.85rem}

/* ── charts ──────────────────────────────────────────────
   Inline SVG with no script, so a chart reads the same in the HTML and in the
   printed PDF. Series colours are validated for CVD separation and contrast
   against both surfaces; a third arm is the last that gets its own hue, and
   beyond that the chart falls back to small multiples. */
:root{--s1:#1f6fa8;--s2:#a8475f;--s3:#6b6b00;
--good:#1f6f43;--bad:#9b2226;--neutral:#78787a;}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
--s1:#4292c9;--s2:#cf6b83;--s3:#a89040;
--good:#6cc48d;--bad:#f08a8a;--neutral:#8e8e92;}}
:root[data-theme="dark"]{--s1:#4292c9;--s2:#cf6b83;--s3:#a89040;
--good:#6cc48d;--bad:#f08a8a;--neutral:#8e8e92;}
figure{margin:1.1rem 0}
figcaption{font-size:.82rem;color:var(--mut);margin-top:.5rem;line-height:1.5}
svg.chart{width:100%;height:auto;display:block;overflow:visible}
svg.chart text{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
font-size:9.5px;fill:var(--mut)}
svg.chart text.lbl{font-weight:600;font-size:10px}
svg.chart .grid{stroke:var(--line);stroke-width:1}
svg.chart .ax{stroke:var(--line);stroke-width:1}
svg.chart .rule{stroke:var(--mut);stroke-width:1.4;stroke-dasharray:5 4;fill:none}
svg.chart .ln{fill:none;stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
svg.chart .bar{stroke:var(--card);stroke-width:2}
.legend{display:flex;gap:13px;flex-wrap:wrap;font-size:.8rem;margin:.1rem 0 .5rem;
color:var(--mut)}
.legend span{display:inline-flex;align-items:center;gap:6px}
.sw{width:11px;height:11px;border-radius:3px;flex:none;display:inline-block}
.smul{display:grid;grid-template-columns:repeat(auto-fit,minmax(16rem,1fr));gap:1.1rem}
@media (max-width:40rem){.smul{grid-template-columns:1fr}}
@media print{body{background:#fff;color:#000}.card{break-inside:avoid}
h2{break-after:avoid}table{break-inside:auto}tr{break-inside:avoid}
figure{break-inside:avoid}}
"""


# ── charts ──────────────────────────────────────────────────
#
# Every chart here is inline SVG built in Python, with no script, because the
# report is read as a printed PDF as often as a page and a chart that needs
# JavaScript is a blank box there. Colours come from CSS variables so the same
# markup is correct in light and dark mode.

SERIES_VARS = ('var(--s1)', 'var(--s2)', 'var(--s3)')
OUTCOME_STYLE = {
    'better': ('var(--good)', 'kept — beat the arm’s best'),
    'worse': ('var(--bad)', 'reverted — fell below it'),
    'conflict': ('var(--warn)', 'split per arm — helped one, hurt another'),
    'flat': ('var(--neutral)', 'no material change — direction abandoned'),
}
PROV_COLOR = {'reported': 'var(--reported)', 'adapted': 'var(--adapted)',
              'design_choice': 'var(--design)', 'gap': 'var(--gap)'}


def _ticks(lo, hi, n=4):
    """Round-ish tick values spanning [lo, hi]."""
    if hi <= lo:
        hi = lo + 1
    raw = (hi - lo) / n
    mag = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1
    step = next((m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw), 10 * mag)
    start = math.floor(lo / step) * step
    out, v = [], start
    while v <= hi + step * 0.5:
        if v >= lo - step * 0.5:
            out.append(round(v, 10))
        v += step
    return out


def arm_order(loop, present=None):
    """Arms in a stable order: the wild-type control first, then alphabetical.

    Colour must follow the entity, not its position in a list. Ordering by
    `sorted()` alone makes WT the first series in a loop that has only WT and the
    second in a loop that also has BACH2_KO, so the same arm changes colour
    between two panels of the same comparison.
    """
    p = final_protocol(loop) or {}
    declared = p.get('genotype_arms') or []
    wild = [a['arm_id'] for a in declared if a.get('genotype') == 'wild_type']
    rest = sorted(a['arm_id'] for a in declared if a.get('genotype') != 'wild_type')
    order = wild + rest
    if present is not None:
        order = [a for a in order if a in present] + sorted(set(present) - set(order))
    return order


def _arm_colors(arms):
    """arm_id -> colour, assigned once so every chart agrees."""
    return {a: SERIES_VARS[i % len(SERIES_VARS)] for i, a in enumerate(arms)}


def _fig(svg, caption, legend_html=''):
    return f'<figure>{legend_html}{svg}<figcaption>{caption}</figcaption></figure>'


def _legend(items):
    """items: [(colour, label)]. Identity is never colour alone, so this always ships."""
    return ('<div class="legend">' + ''.join(
        f'<span><i class="sw" style="background:{c}"></i>{_e(l)}</span>' for c, l in items)
        + '</div>')


def chart_trajectory(loop, width=680, height=250, colors=None):
    """The headline: the request's own target metric per arm, per iteration."""
    s = loop.get('search') or {}
    obs = s.get('observations') or []
    if len(obs) < 2:
        return ''
    present = {a for o in obs for a in o['arms']}
    arms = arm_order(loop, present)
    if not arms:
        return ''
    colors = colors or _arm_colors(arms)
    target = None
    a = final_analysis(loop)
    if a and a['arms']:
        target = a['arms'][0]['target'].get('required')
    m = {'t': 12, 'r': 74, 'b': 32, 'l': 50}
    iw, ih = width - m['l'] - m['r'], height - m['t'] - m['b']
    xs = [o['iteration'] for o in obs]
    x0, x1 = min(xs), max(max(xs), min(xs) + 1)
    vals = [v for o in obs for v in o['arms'].values() if isinstance(v, (int, float))]
    if target:
        vals.append(target)
    ymax = max(vals) * 1.12 if vals else 1
    X = lambda v: m['l'] + (v - x0) / (x1 - x0) * iw
    Y = lambda v: m['t'] + ih - (v / ymax) * ih if ymax else m['t'] + ih

    p = [f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
         f'aria-label="Target metric per iteration for {_e(", ".join(arms))}">']
    for t in _ticks(0, ymax):
        y = Y(t)
        p.append(f'<line class="grid" x1="{m["l"]}" x2="{m["l"] + iw}" y1="{y:.1f}" y2="{y:.1f}"/>')
        p.append(f'<text x="{m["l"] - 7}" y="{y + 3:.1f}" text-anchor="end">{_num(t, 0)}</text>')
    step = max(1, math.ceil((x1 - x0) / 9))
    for v in range(int(x0), int(x1) + 1, step):
        p.append(f'<text x="{X(v):.1f}" y="{height - m["b"] + 14}" '
                 f'text-anchor="middle">{v}</text>')
    p.append(f'<text x="{m["l"] + iw / 2:.1f}" y="{height - 3}" text-anchor="middle">'
             f'iteration</text>')
    if target:
        p.append(f'<line class="rule" x1="{m["l"]}" x2="{m["l"] + iw}" '
                 f'y1="{Y(target):.1f}" y2="{Y(target):.1f}"/>')
        p.append(f'<text x="{m["l"] + 4}" y="{Y(target) - 6:.1f}">target {_num(target, 0)}</text>')
    dots = len(obs) <= 12
    for i, arm in enumerate(arms):
        col = colors[arm]
        pts = [(X(o['iteration']), Y(o['arms'][arm])) for o in obs
               if isinstance(o['arms'].get(arm), (int, float))]
        if not pts:
            continue
        d = ' '.join(('L' if j else 'M') + f'{x:.1f} {y:.1f}' for j, (x, y) in enumerate(pts))
        p.append(f'<path class="ln" stroke="{col}" d="{d}"/>')
        if dots:
            for x, y in pts:
                p.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{col}" '
                         f'stroke="var(--card)" stroke-width="2"/>')
        lx, ly = pts[-1]
        p.append(f'<text class="lbl" x="{lx + 7:.1f}" y="{ly + (13 if i % 2 else -6):.1f}" '
                 f'style="fill:{col}">{_e(arm)}</text>')
    p.append('</svg>')
    return _fig(''.join(p),
                'One point per stand-in run. The optimiser moves one lever per arm per iteration '
                'and reverts any move that does not beat that arm’s best reading, which is why a '
                'line can step back and then recover. The dashed rule is the target the request '
                'asked for.',
                _legend([(colors[a], a) for a in arms]))


def chart_arms_vs_target(analysis, width=680, colors=None):
    """Where each arm finished against what was asked for."""
    if not analysis or not analysis.get('arms'):
        return ''
    rows = [(a['arm_id'], a['target']['observed_mean'], a['target']['required'],
             a['target']['status']) for a in analysis['arms']]
    colors = colors or _arm_colors([r[0] for r in rows])
    req = rows[0][2]
    hi = max([r[1] for r in rows] + [req]) * 1.15
    # Top margin leaves room for the target label above the rule; at 10 it sat on
    # the viewBox edge and was clipped wherever the figure is cropped.
    bh, gap, m = 22, 14, {'t': 24, 'r': 78, 'b': 26, 'l': 92}
    height = m['t'] + m['b'] + len(rows) * (bh + gap)
    iw = width - m['l'] - m['r']
    X = lambda v: m['l'] + (v / hi) * iw
    p = [f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
         f'aria-label="Final value per arm against the target">']
    for t in _ticks(0, hi):
        p.append(f'<line class="grid" x1="{X(t):.1f}" x2="{X(t):.1f}" y1="{m["t"]}" '
                 f'y2="{height - m["b"]}"/>')
        p.append(f'<text x="{X(t):.1f}" y="{height - m["b"] + 14}" text-anchor="middle">'
                 f'{_num(t, 0)}</text>')
    for i, (arm, got, need, status) in enumerate(rows):
        y = m['t'] + i * (bh + gap)
        col = colors.get(arm, SERIES_VARS[i % len(SERIES_VARS)])
        p.append(f'<rect class="bar" x="{m["l"]}" y="{y}" width="{max(X(got) - m["l"], 1):.1f}" '
                 f'height="{bh}" rx="3" fill="{col}"/>')
        p.append(f'<text class="lbl" x="{m["l"] - 8}" y="{y + bh / 2 + 3.5:.0f}" '
                 f'text-anchor="end">{_e(arm)}</text>')
        mark = '✓ met' if status == 'MET' else '✗ not met'
        p.append(f'<text x="{X(got) + 7:.1f}" y="{y + bh / 2 + 3.5:.0f}" '
                 f'style="fill:{"var(--ok)" if status == "MET" else "var(--bad)"}">'
                 f'{_num(got)} {mark}</text>')
    p.append(f'<line class="rule" x1="{X(req):.1f}" x2="{X(req):.1f}" y1="{m["t"] - 4}" '
             f'y2="{height - m["b"]}"/>')
    p.append(f'<text x="{X(req):.1f}" y="{m["t"] - 9}" text-anchor="middle">'
             f'target {_num(req, 0)}</text>')
    p.append('</svg>')
    metric = analysis['arms'][0]['target']['metric']
    unit = analysis['arms'][0]['target'].get('unit', '')
    return _fig(''.join(p), f'Final {_e(metric)} per arm, in {_e(unit)}, against the required '
                            f'value. Bars are the best configuration each arm ended on.')


def chart_outcomes(loop, width=680):
    """What happened to every move the search made: the accept/reject record."""
    hist = ((loop.get('search') or {}).get('scored')) or []
    if not hist:
        return ''
    counts = {}
    for h in hist:
        counts[h['outcome']] = counts.get(h['outcome'], 0) + 1
    order = [k for k in ('better', 'worse', 'conflict', 'flat') if counts.get(k)]
    total = sum(counts[k] for k in order)
    if not total:
        return ''
    height, m = 56, {'t': 8, 'r': 8, 'b': 22, 'l': 8}
    iw = width - m['l'] - m['r']
    p = [f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
         f'aria-label="Outcome of every search move">']
    x = m['l']
    for k in order:
        w = counts[k] / total * iw
        col = OUTCOME_STYLE[k][0]
        p.append(f'<rect class="bar" x="{x:.1f}" y="{m["t"]}" width="{max(w, 1):.1f}" '
                 f'height="26" rx="3" fill="{col}"/>')
        if w > 34:
            p.append(f'<text class="lbl" x="{x + w / 2:.1f}" y="{m["t"] + 17}" '
                     f'text-anchor="middle" style="fill:var(--card)">{counts[k]}</text>')
        x += w
    p.append(f'<text x="{m["l"]}" y="{height - 6}">{total} moves scored</text>')
    p.append('</svg>')
    return _fig(''.join(p),
                'Every move the search made, by what the next run said about it. A reverted move '
                'is not a failure of the search — it is the search refusing to keep a change that '
                'did not pay, which is what stops it drifting away from a good configuration.',
                _legend([(OUTCOME_STYLE[k][0], f'{OUTCOME_STYLE[k][1]} ({counts[k]})')
                         for k in order]))


def chart_levers(loop, width=680, max_levers=14):
    """Which parameters the search explored, and how far from their starting value.

    Every lever is drawn on one dimensionless axis — value divided by that
    lever's own baseline — because the levers carry different units and a shared
    absolute axis would be meaningless. 1.0 is where the protocol started.
    """
    levers = ((loop.get('search') or {}).get('levers')) or {}
    # One factor appears in several stages, so the factor name alone does not
    # identify a lever. Resolve each step back to its stage.
    fp = final_protocol(loop) or {}
    stage_of = {st_step['step_id']: st['stage_id']
                for st in fp.get('stages', []) for st_step in st['steps']}
    rows = []
    for key, rec in levers.items():
        tried = [t['value'] for t in (rec.get('tried') or []) if isinstance(t.get('value'),
                                                                           (int, float))]
        base = rec.get('baseline')
        if not tried or not isinstance(base, (int, float)) or base == 0:
            continue
        best = (rec.get('best') or {}).get('value')
        stage = stage_of.get(rec.get('step_id'))
        rows.append({'label': f'{rec.get("factor")}'
                              + (f' · {stage}' if stage else '')
                              + (f' [{rec["arm_id"]}]' if rec.get('arm_id') else ''),
                     'step': rec.get('step_id'), 'ratios': [t / base for t in tried],
                     'best': (best / base) if isinstance(best, (int, float)) else None,
                     'unit': rec.get('unit'), 'base': base})
    if not rows:
        return ''
    rows.sort(key=lambda r: -len(r['ratios']))
    more = max(0, len(rows) - max_levers)
    rows = rows[:max_levers]
    lo = min([min(r['ratios']) for r in rows] + [1.0])
    hi = max([max(r['ratios']) for r in rows] + [1.0])
    lo, hi = min(lo, 0.9) * 0.95, max(hi, 1.1) * 1.05
    rh, m = 20, {'t': 22, 'r': 54, 'b': 26, 'l': 232}
    height = m['t'] + m['b'] + len(rows) * rh
    iw = width - m['l'] - m['r']
    X = lambda v: m['l'] + (v - lo) / (hi - lo) * iw
    p = [f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
         f'aria-label="Values tried per lever, relative to its starting value">']
    for t in _ticks(lo, hi):
        if t <= 0:
            continue
        p.append(f'<line class="grid" x1="{X(t):.1f}" x2="{X(t):.1f}" y1="{m["t"] - 6}" '
                 f'y2="{height - m["b"]}"/>')
        p.append(f'<text x="{X(t):.1f}" y="{height - m["b"] + 14}" text-anchor="middle">'
                 f'{_num(t, 2)}×</text>')
    p.append(f'<line class="rule" x1="{X(1.0):.1f}" x2="{X(1.0):.1f}" y1="{m["t"] - 6}" '
             f'y2="{height - m["b"]}"/>')
    p.append(f'<text x="{X(1.0):.1f}" y="{m["t"] - 10}" text-anchor="middle">as designed</text>')
    for i, r in enumerate(rows):
        y = m['t'] + i * rh + rh / 2
        p.append(f'<line class="ax" x1="{m["l"]}" x2="{m["l"] + iw}" y1="{y:.1f}" y2="{y:.1f}" '
                 f'opacity="0.45"/>')
        p.append(f'<text x="{m["l"] - 8}" y="{y + 3.5:.1f}" text-anchor="end">'
                 f'{_e(r["label"])}</text>')
        for v in r['ratios']:
            p.append(f'<circle cx="{X(v):.1f}" cy="{y:.1f}" r="3.4" fill="var(--mut)" '
                     f'opacity="0.55"/>')
        if r['best'] is not None:
            p.append(f'<circle cx="{X(r["best"]):.1f}" cy="{y:.1f}" r="5.4" fill="var(--good)" '
                     f'stroke="var(--card)" stroke-width="2"/>')
            p.append(f'<text x="{m["l"] + iw + 6}" y="{y + 3.5:.1f}" style="fill:var(--good)">'
                     f'{_num(r["best"] * r["base"])}</text>')
    p.append('</svg>')
    cap = ('Each row is one lever, with every value the search tried on it, as a multiple of what '
           'the designed protocol started with. A shared absolute axis would be meaningless '
           'because the levers carry different units. The filled marker is the value that gave '
           'that lever its best reading, printed in its own units on the right.')
    if more:
        cap += f' {more} further lever(s) with fewer attempts are not drawn; the table below has all of them.'
    return _fig(''.join(p), cap,
                _legend([('var(--mut)', 'a value tried'), ('var(--good)', 'best value found')]))


def chart_provenance(protocol, width=680):
    """How much of the protocol rests on a citation, and how much on a choice."""
    if not protocol:
        return ''
    counts = provenance_counts(protocol)
    order = [k for k in ('reported', 'adapted', 'design_choice', 'gap') if counts.get(k)]
    total = sum(counts[k] for k in order)
    if not total:
        return ''
    height, m = 56, {'t': 8, 'r': 8, 'b': 22, 'l': 8}
    iw = width - m['l'] - m['r']
    p = [f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
         f'aria-label="Provenance of every quantity in the final protocol">']
    x = m['l']
    for k in order:
        w = counts[k] / total * iw
        p.append(f'<rect class="bar" x="{x:.1f}" y="{m["t"]}" width="{max(w, 1):.1f}" '
                 f'height="26" rx="3" fill="{PROV_COLOR[k]}"/>')
        if w > 34:
            p.append(f'<text class="lbl" x="{x + w / 2:.1f}" y="{m["t"] + 17}" '
                     f'text-anchor="middle" style="fill:var(--card)">{counts[k]}</text>')
        x += w
    p.append(f'<text x="{m["l"]}" y="{height - 6}">{total} quantities</text>')
    p.append('</svg>')
    return _fig(''.join(p),
                'Every quantity in the final protocol by where its value came from. A bar that is '
                'entirely design choice means nothing in the recipe is attributable to a '
                'publication, which is a statement about the evidence available, not about the '
                'recipe being wrong.',
                _legend([(PROV_COLOR[k], f'{PROV_LABEL[k]} ({counts[k]})') for k in order]))


def chart_small_multiples(loops, width=330, height=190):
    """One trajectory panel per loop, on a shared y scale so they can be compared."""
    panels, allv = [], []
    for l in loops:
        obs = ((l.get('search') or {}).get('observations')) or []
        allv += [v for o in obs for v in o['arms'].values() if isinstance(v, (int, float))]
    if not allv:
        return ''
    target = None
    for l in loops:
        a = final_analysis(l)
        if a and a['arms']:
            target = a['arms'][0]['target'].get('required')
            break
    ymax = max(allv + ([target] if target else [])) * 1.12
    # One colour map across every panel, so an arm keeps its colour throughout.
    union = []
    for l in loops:
        for a in arm_order(l, {x for o in ((l.get('search') or {}).get('observations')) or []
                               for x in o['arms']}):
            if a not in union:
                union.append(a)
    colors = _arm_colors(union)
    for l in loops:
        obs = ((l.get('search') or {}).get('observations')) or []
        if len(obs) < 2:
            continue
        arms = arm_order(l, {a for o in obs for a in o['arms']})
        m = {'t': 10, 'r': 58, 'b': 28, 'l': 42}
        iw, ih = width - m['l'] - m['r'], height - m['t'] - m['b']
        xs = [o['iteration'] for o in obs]
        x0, x1 = min(xs), max(max(xs), min(xs) + 1)
        X = lambda v: m['l'] + (v - x0) / (x1 - x0) * iw
        Y = lambda v: m['t'] + ih - (v / ymax) * ih
        p = [f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
             f'aria-label="Trajectory for {_e(l["dir"].name)}">']
        for t in _ticks(0, ymax, 3):
            p.append(f'<line class="grid" x1="{m["l"]}" x2="{m["l"] + iw}" y1="{Y(t):.1f}" '
                     f'y2="{Y(t):.1f}"/>')
            p.append(f'<text x="{m["l"] - 6}" y="{Y(t) + 3:.1f}" text-anchor="end">'
                     f'{_num(t, 0)}</text>')
        if target:
            p.append(f'<line class="rule" x1="{m["l"]}" x2="{m["l"] + iw}" y1="{Y(target):.1f}" '
                     f'y2="{Y(target):.1f}"/>')
        for i, arm in enumerate(arms):
            col = colors[arm]
            pts = [(X(o['iteration']), Y(o['arms'][arm])) for o in obs
                   if isinstance(o['arms'].get(arm), (int, float))]
            if not pts:
                continue
            d = ' '.join(('L' if j else 'M') + f'{x:.1f} {y:.1f}'
                         for j, (x, y) in enumerate(pts))
            p.append(f'<path class="ln" stroke="{col}" d="{d}"/>')
            lx, ly = pts[-1]
            p.append(f'<text class="lbl" x="{lx + 6:.1f}" y="{ly + (12 if i % 2 else -5):.1f}" '
                     f'style="fill:{col}">{_e(arm)}</text>')
        p.append(f'<text x="{m["l"] + iw / 2:.1f}" y="{height - 3}" text-anchor="middle">'
                 f'{len(obs) - 1} revisions</text>')
        p.append('</svg>')
        panels.append(f'<div><h4>{_e(l["dir"].name)}</h4>{"".join(p)}</div>')
    if not panels:
        return ''
    return _fig(f'<div class="smul">{"".join(panels)}</div>',
                'The same axis in both panels, so the panels can be compared directly. Separate '
                'panels rather than one overlay because the loops ran different arms; a single '
                'plot would imply they share a run.')


def chart_iterations(loops, width=680):
    """How long each loop took to get where it got."""
    rows = []
    for l in loops:
        s = l.get('summary') or {}
        used = s.get('iterations_used')
        if used is None:
            continue
        rows.append((l['dir'].name, used, s.get('max_iterations') or used,
                     s.get('terminal')))
    if not rows:
        return ''
    hi = max(r[2] for r in rows)
    bh, gap, m = 22, 14, {'t': 10, 'r': 150, 'b': 26, 'l': 180}
    height = m['t'] + m['b'] + len(rows) * (bh + gap)
    iw = width - m['l'] - m['r']
    X = lambda v: m['l'] + (v / hi) * iw
    p = [f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
         f'aria-label="Iterations used by each loop">']
    for t in _ticks(0, hi):
        p.append(f'<line class="grid" x1="{X(t):.1f}" x2="{X(t):.1f}" y1="{m["t"]}" '
                 f'y2="{height - m["b"]}"/>')
        p.append(f'<text x="{X(t):.1f}" y="{height - m["b"] + 14}" text-anchor="middle">'
                 f'{_num(t, 0)}</text>')
    for i, (name, used, budget, terminal) in enumerate(rows):
        y = m['t'] + i * (bh + gap)
        p.append(f'<rect x="{m["l"]}" y="{y}" width="{max(X(budget) - m["l"], 1):.1f}" '
                 f'height="{bh}" rx="3" fill="var(--line)"/>')
        p.append(f'<rect class="bar" x="{m["l"]}" y="{y}" '
                 f'width="{max(X(used) - m["l"], 1):.1f}" height="{bh}" rx="3" '
                 f'fill="{SERIES_VARS[i % len(SERIES_VARS)]}"/>')
        p.append(f'<text class="lbl" x="{m["l"] - 8}" y="{y + bh / 2 + 3.5:.0f}" '
                 f'text-anchor="end">{_e(name)}</text>')
        p.append(f'<text x="{m["l"] + iw + 6}" y="{y + bh / 2 + 3.5:.0f}">'
                 f'{used} of {budget} · {_e(terminal)}</text>')
    p.append(f'<text x="{m["l"] + iw / 2:.1f}" y="{height - 3}" text-anchor="middle">'
             f'iterations spent</text>')
    p.append('</svg>')
    return _fig(''.join(p),
                'Iterations each loop spent, against the budget its request allowed. The pale bar '
                'is the budget. Only a protocol revision spends an iteration; gathering evidence '
                'and closing QC do not.')


def _page(title, body, description=''):
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(title)}</title>
<meta name="description" content="{_e(description)}">
<style>{CSS}</style></head>
<body><div class="wrap">{body}</div></body></html>"""


def _prov_tag(p):
    return f'<span class="tag t-{_e(p)}">{_e(PROV_LABEL.get(p, p))}</span>'


def _arm_table(analysis):
    rows = []
    for a in analysis['arms']:
        t = a['target']
        cls = 'ok' if t['status'] == 'MET' else 'bad'
        qc_fail = [c['id'] for c in a['qc']['criteria'] if c['status'] == 'FAIL']
        qc_open = [c['id'] for c in a['qc']['criteria']
                   if c['status'] in ('NOT_TESTED', 'SPEC_MISSING', 'MARGINAL')]
        rows.append(
            f'<tr><td><b>{_e(a["arm_id"])}</b></td>'
            f'<td class="n">{_num(t["observed_mean"])}</td>'
            f'<td class="n">{_num(t["required"])}</td>'
            f'<td><span class="{cls}">{_e(t["status"])}</span></td>'
            f'<td>{"<span class=bad>" + _e(", ".join(qc_fail)) + "</span>" if qc_fail else "<span class=ok>all pass</span>"}'
            f'{("<br><span class=warn>open: " + _e(", ".join(qc_open)) + "</span>") if qc_open else ""}</td></tr>')
    return ('<table><thead><tr><th>Arm</th><th class="n">Observed</th><th class="n">Required</th>'
            '<th>Target</th><th>QC</th></tr></thead><tbody>' + ''.join(rows) + '</tbody></table>')


def _decision_card(d, index):
    author = AUTHOR_LABEL.get(d.get('authored_by'), d.get('authored_by'))
    bits = [f'<div class="card dec"><h3>{index}. {_e(d["type"])} '
            f'<span class="mut">&mdash; {_e(d["decision_id"])}, iteration {d["iteration"]}</span></h3>']
    bits.append('<dl class="kv">')
    bits.append(f'<dt>Authored by</dt><dd>{_e(author)}</dd>')
    bits.append(f'<dt>Status</dt><dd>{_e(d.get("status"))}</dd>')
    bits.append(f'<dt>Spends an iteration</dt><dd>{"yes" if d.get("advances_iteration") else "no"}</dd>')
    bits.append(f'<dt>Routed to</dt><dd>{_e(d.get("route_to"))}</dd>')
    if d.get('allowed_actions'):
        bits.append(f'<dt>Validated against</dt><dd class="mono">'
                    f'{_e(", ".join(d["allowed_actions"]))}</dd>')
    bits.append('</dl>')

    if d.get('reasoning'):
        label = ('Reasoning as written by the deterministic policy, which is a description of a rule'
                 if d.get('authored_by') == 'policy' else 'Reasoning as written by the agent')
        bits.append(f'<h4>{label}</h4><div class="why">{_e(d["reasoning"])}</div>')
    if d.get('reason'):
        bits.append(f'<h4>Stated reason</h4><div class="why">{_e(d["reason"])}</div>')

    adv = d.get('policy_advice')
    if adv and adv.get('type') and d.get('authored_by') == 'orchestrator_agent':
        same = adv['type'] == d['type']
        bits.append(f'<h4>What the deterministic policy would have done</h4>'
                    f'<p>{_e(adv["type"])} &mdash; '
                    f'{"the agent agreed" if same else "<b>the agent chose differently</b>"}.'
                    f'<br><span class="mut">{_e(adv.get("reason"))}</span></p>')

    if d.get('considered'):
        rows = ''.join(f'<tr><td class="mono">{_e(c.get("type"))}</td>'
                       f'<td>{_e(c.get("why_not"))}</td></tr>' for c in d['considered'])
        bits.append('<h4>Alternatives considered, and why not</h4>'
                    '<table><thead><tr><th>Action</th><th>Why it was not chosen</th></tr></thead>'
                    f'<tbody>{rows}</tbody></table>')

    if d.get('hypotheses'):
        items = []
        for h in d['hypotheses']:
            basis = ''.join(f'<li>{_e(b)}</li>' for b in h.get('basis', []))
            items.append(f'<li><b>{_e(h.get("hypothesis_id"))}</b> {_e(h.get("statement"))}'
                         f'<br><span class="mut">Basis:</span><ul class="tight">{basis}</ul>'
                         + (f'<span class="mut">Would be tested by: '
                            f'{_e(h.get("would_be_tested_by"))}</span>'
                            if h.get('would_be_tested_by') else '') + '</li>')
        bits.append(f'<h4>Hypotheses raised, each with its basis</h4>'
                    f'<ul class="tight">{"".join(items)}</ul>')

    if d.get('tool_calls'):
        rows = ''.join(f'<tr><td class="mono">{_e(t.get("tool"))}</td><td>{_e(t.get("purpose"))}</td>'
                       f'<td>{_e(t.get("summary"))}</td></tr>' for t in d['tool_calls'])
        bits.append('<h4>Tool results cited</h4><table><thead><tr><th>Tool</th><th>Purpose</th>'
                    f'<th>Result</th></tr></thead><tbody>{rows}</tbody></table>')

    if d.get('operator_actions'):
        bits.append('<h4>Actions handed to a person</h4><ul class="tight>'
                    + ''.join(f'<li>{_e(a)}</li>' for a in d['operator_actions']) + '</ul>')

    b = d.get('revision_brief')
    if b:
        failed = ''.join(f'<li>{_e(f["criterion"])} <b>{_e(f["status"])}</b> &mdash; observed '
                         f'{_num(f.get("observed"))}, required {_e(f.get("required"))}</li>'
                         for f in b.get('failed_criteria', []))
        keep = ''.join(f'<li>{_e(k)}</li>' for k in b.get('keep_fixed', []))
        lev = ''.join(f'<tr><td>{_e(l["parameter"])}</td><td>{_e(l.get("direction_hint"))}</td>'
                      f'<td class="mono">{_e(l.get("step_id"))}</td>'
                      f'<td>{_e(l.get("basis"))}</td></tr>' for l in b.get('levers', [])[:14])
        bits.append(f'<h4>Revision brief {_e(b.get("brief_id"))}</h4>'
                    f'<blockquote>{_e(b.get("why_it_did_not_work"))}</blockquote>'
                    + (f'<p><b>Failed criteria</b></p><ul class="tight">{failed}</ul>' if failed else '')
                    + (f'<p><b>Must stay fixed</b></p><ul class="tight">{keep}</ul>' if keep else '')
                    + (f'<p><b>Levers offered</b> (first 14)</p><table><thead><tr><th>Parameter</th>'
                       f'<th>Direction</th><th>Step</th><th>Basis</th></tr></thead>'
                       f'<tbody>{lev}</tbody></table>' if lev else ''))
    bits.append('</div>')
    return ''.join(bits)


def _search_section(loop):
    s = loop.get('search')
    if not s:
        return ('<p class="mut">No search state was recorded for this loop, so the move-by-move '
                'history is not available.</p>')
    out = []
    if s.get('findings'):
        out.append('<h3>What the search established</h3><ul class="tight">'
                   + ''.join(f'<li>{_e(f)}</li>' for f in s['findings']) + '</ul>')
    obs = s.get('observations') or []
    if obs:
        arms = sorted({a for o in obs for a in o['arms']})
        head = ''.join(f'<th class="n">{_e(a)}</th>' for a in arms)
        rows = ''.join('<tr><td class="n">' + str(o['iteration']) + '</td>'
                       + ''.join(f'<td class="n">{_num(o["arms"].get(a))}</td>' for a in arms)
                       + '</tr>' for o in obs)
        out.append('<h3>Reading per iteration</h3><table><thead><tr><th class="n">Iteration</th>'
                   f'{head}</tr></thead><tbody>{rows}</tbody></table>')
    best = s.get('best') or {}
    if best:
        rows = ''.join(f'<tr><td>{_e(a)}</td><td class="n">{_num(v.get("metric"))}</td>'
                       f'<td class="n">{_e(v.get("iteration"))}</td></tr>'
                       for a, v in sorted(best.items()))
        out.append('<h3>Best reading per arm</h3><table><thead><tr><th>Arm</th>'
                   '<th class="n">Best</th><th class="n">At iteration</th></tr></thead>'
                   f'<tbody>{rows}</tbody></table>')
    levers = s.get('levers') or {}
    if levers:
        rows = []
        for key, rec in sorted(levers.items()):
            tried = ', '.join(_num(t['value']) for t in rec.get('tried', []))
            ex = rec.get('exhausted') or []
            exl = ', '.join({1: 'up', -1: 'down'}.get(e, str(e)) for e in ex) or '&mdash;'
            rows.append(f'<tr><td>{_e(rec.get("factor"))}</td>'
                        f'<td>{_e(rec.get("arm_id") or "all arms")}</td>'
                        f'<td class="mono">{_e(rec.get("step_id"))}</td>'
                        f'<td>{_e(rec.get("kind"))}</td>'
                        f'<td class="mono">{tried}</td><td>{exl}</td>'
                        f'<td class="n">{_num((rec.get("best") or {}).get("value"))}</td></tr>')
        out.append('<h3>Every lever the search touched</h3>'
                   '<table><thead><tr><th>Parameter</th><th>Scope</th><th>Step</th><th>Kind</th>'
                   '<th>Values tried</th><th>Exhausted</th><th class="n">Best</th></tr></thead>'
                   f'<tbody>{"".join(rows)}</tbody></table>')
    return ''.join(out)


def _moves_section(loop):
    """The move-by-move narrative, reconstructed from each protocol's own change log."""
    rows = []
    for st in loop['stages']:
        p = st.get('protocol')
        if not p or not p.get('changes_from_parent'):
            continue
        for ch in p['changes_from_parent']:
            rows.append(f'<tr><td class="n">{_e(p["iteration"])}</td>'
                        f'<td class="mono">{_e(ch["step_id"])}</td>'
                        f'<td>{_e(ch["change"])}</td>'
                        f'<td class="mono">{_e(", ".join(ch.get("addresses") or []))}</td></tr>')
    if not rows:
        return '<p class="mut">The loop never revised the protocol.</p>'
    return ('<table><thead><tr><th class="n">Into iteration</th><th>Step</th><th>Change</th>'
            '<th>Addresses</th></tr></thead><tbody>' + ''.join(rows) + '</tbody></table>')


def _protocol_section(p):
    rows = []
    for path, q in iter_quantities(p):
        rows.append(f'<tr><td class="mono">{_e(path)}</td>'
                    f'<td class="n">{_num(q["value"]) if isinstance(q["value"], (int, float)) else _e(q["value"])}</td>'
                    f'<td>{_e(q["unit"])}</td><td>{_prov_tag(q["provenance"])}</td>'
                    f'<td class="mono">{_e(", ".join(q.get("claim_ids") or [])) or "&mdash;"}</td>'
                    f'<td>{_e(q.get("rationale"))}</td></tr>')
    counts = provenance_counts(p)
    summary = ' &middot; '.join(f'{_prov_tag(k)} {v}' for k, v in sorted(counts.items()))
    appr = p.get('approval') or {}
    head = (f'<dl class="kv"><dt>Protocol</dt><dd class="mono">{_e(p["protocol_id"])}</dd>'
            f'<dt>Title</dt><dd>{_e(p["title"])}</dd>'
            f'<dt>Day 0 is</dt><dd>{_e(p["day_origin"])}</dd>'
            f'<dt>Approved by</dt><dd>{_e(appr.get("approved_by")) or "not approved"}</dd>'
            f'<dt>Quantities</dt><dd>{summary}</dd></dl>')
    adj = p.get('arm_adjustments') or []
    adj_html = ''
    if adj:
        arows = ''.join(
            f'<tr><td>{_e(a["arm_id"])}</td><td class="mono">{_e(a["step_id"])}</td>'
            f'<td class="n">{_num((a.get("quantity") or {}).get("value")) if a.get("quantity") else _e(a.get("day_shift"))}</td>'
            f'<td>{_e((a.get("quantity") or {}).get("unit") or ("day shift" if a.get("day_shift") is not None else ""))}</td>'
            f'<td>{_e(a["rationale"])}</td></tr>' for a in adj)
        adj_html = ('<h3>Where the arms differ</h3><p>These are the per-arm deviations the search '
                    'established. A parameter listed here could not be satisfied by one shared '
                    'value.</p><table><thead><tr><th>Arm</th><th>Step</th><th class="n">Value</th>'
                    f'<th>Unit</th><th>Why</th></tr></thead><tbody>{arows}</tbody></table>')
    return (head + adj_html + '<h3>Every quantity, with its provenance</h3>'
            '<table><thead><tr><th>Path</th><th class="n">Value</th><th>Unit</th>'
            '<th>Provenance</th><th>Claims</th><th>Rationale</th></tr></thead><tbody>'
            + ''.join(rows) + '</tbody></table>')


def _references_section(refs):
    if not refs:
        return '<p class="mut">The loop cited no source.</p>'
    lit = [r for r in refs if r['kind'] == 'literature']
    ann = [r for r in refs if r['kind'] == 'annotation']
    out = []
    if lit:
        items = []
        for r in lit:
            link = (f' <a href="{_e(r["url"])}">{_e(r["doi"] or r["url"])}</a>'
                    if r.get('url') else '')
            ft = ('<span class="warn">full text not retrieved</span>'
                  if r.get('full_text_retrieved') is False else
                  '<span class="ok">full text retrieved</span>'
                  if r.get('full_text_retrieved') else '<span class="mut">unknown</span>')
            used = ''.join(f'<li>{_e(u)}</li>' for u in r['used_for'])
            items.append(f'<li><p>{_e(r["citation"])}{link}'
                         + (f' &middot; PMC {_e(r["pmcid"])}' if r.get('pmcid') else '')
                         + f'<br>{ft}</p>'
                         + (f'<p class="mut">Used for:</p><ul class="tight">{used}</ul>'
                            if used else '') + '</li>')
        out.append(f'<h3>Publications</h3><ol class="tight">{"".join(items)}</ol>')
    if ann:
        items = []
        for r in ann:
            used = ''.join(f'<li>{_e(u)}</li>' for u in r['used_for'])
            items.append(f'<li><p>{_e(r["citation"])}</p>'
                         + (f'<ul class="tight">{used}</ul>' if used else '') + '</li>')
        out.append('<h3>Annotation sources</h3><p class="mut">Each annotation carries its own '
                   'confidence. <code>local_annotation</code> restates what a cited paper reports; '
                   '<code>inference</code> is a transfer to this cell type or readout that no cited '
                   'paper measures; <code>synthetic_fixture</code> is invented for software testing.'
                   f'</p><ol class="tight">{"".join(items)}</ol>')
    return ''.join(out)


def _limitations(loop, protocol, analysis):
    lim = ['The reactor was a synthetic stand-in, so no number in this report is a measurement of '
           'any real cell population. The stand-in is phenomenological and is not a validated '
           'digital twin.']
    if loop['decisions'] and all(d.get('authored_by') == 'policy' for d in loop['decisions']):
        lim.append('Every decision here was authored by the deterministic policy. The reasoning '
                   'shown is a description of a rule in the code, not a model\'s reasoning. No LLM '
                   'ran in this loop.')
    counts = provenance_counts(protocol) if protocol else {}
    if counts.get('design_choice') and not counts.get('reported'):
        lim.append(f'Not one quantity in the final protocol is attributed to a publication: all '
                   f'{counts["design_choice"]} of them are design choices. The citations support '
                   f'the direction of a lever and nothing numeric.')
    if protocol:
        reps = protocol['measurement_plan'].get('replicates_per_arm')
        if reps and reps < 2:
            lim.append(f'{reps} replicate per arm. Every between-arm difference is directional only, '
                       f'and the search was climbing a surface it could not distinguish from noise '
                       f'below its own step size.')
    if analysis:
        for a in analysis['arms']:
            open_qc = [c['id'] for c in a['qc']['criteria'] if c['status'] == 'SPEC_MISSING']
            if open_qc:
                lim.append(f'{a["arm_id"]}: QC limits are unset for {", ".join(open_qc)}, so those '
                           f'criteria were not evaluated rather than passed.')
    lim.append('A loop that reaches a target has not produced a validated or released process. '
               'Confirmation runs, a replicate design and human QA sign-off are all outside it.')
    for x in (protocol or {}).get('limitations', []):
        lim.append(x)
    return '<ul class="tight">' + ''.join(f'<li>{_e(x)}</li>' for x in lim) + '</ul>'


# ── single-loop report ──────────────────────────────────────

def build_report(loop_dir, title=None):
    """The reasoning report for one loop, as an HTML string."""
    loop = load_loop(loop_dir)
    state, p, a = loop['state'], final_protocol(loop), final_analysis(loop)
    summary = loop.get('summary') or {}
    gates = state.get('autonomy') or {}
    title = title or f'Reasoning report: {loop["loop_id"]}'
    terminal = summary.get('terminal') or (state['iterations'][-1]['decision_type']
                                           if state.get('iterations') else 'unknown')
    arm_colors = _arm_colors(arm_order(loop, {x['arm_id'] for x in (a or {}).get('arms', [])}))
    authors = sorted({d.get('authored_by') for d in loop['decisions']})
    body = [f'<h1>{_e(title)}</h1>']
    body.append(f'<p class="sub">Loop <code>{_e(loop["loop_id"])}</code> &middot; request '
                f'<code>{_e(state.get("request_id"))}</code> &middot; '
                f'{len(loop["decisions"])} decisions &middot; '
                f'{summary.get("iterations_used", state.get("iterations") and len(state["iterations"]))}'
                f' of {state.get("max_iterations")} iterations &middot; ended in '
                f'<b>{_e(terminal)}</b></p>')
    body.append('<div class="banner"><b>What this report is.</b> It records why this loop made each '
                'choice, with the references and rationales behind them. The bioreactor was a '
                '<b>synthetic stand-in</b>, so nothing here is biological evidence, and the '
                'decisions were authored by '
                + _e(' and '.join(AUTHOR_LABEL.get(x, str(x)) for x in authors if x))
                + '. Read a recovered parameter as evidence that the search works, not as a '
                  'finding about any gene or any cell.</div>')

    body.append('<h2>Contents</h2><div class="toc"><ul class="tight">'
                '<li><a href="#question">The question and the envelope</a></li>'
                '<li><a href="#outcome">Outcome</a></li>'
                '<li><a href="#search">How the search moved</a></li>'
                '<li><a href="#decisions">Every decision, with its reasoning</a></li>'
                '<li><a href="#protocol">The protocol it ended on</a></li>'
                '<li><a href="#references">References</a></li>'
                '<li><a href="#limits">Limitations</a></li></ul></div>')

    body.append('<h2 id="question">The question and the envelope</h2>')
    body.append(f'<blockquote>{_e(state.get("question") or summary.get("question"))}</blockquote>')
    body.append('<dl class="kv">')
    if summary.get('target'):
        body.append(f'<dt>Target</dt><dd>{_e(summary["target"])}</dd>')
    body.append(f'<dt>Autonomy</dt><dd>{_e(gates.get("mode"))}</dd>'
                f'<dt>Reactor</dt><dd>{_e(gates.get("bioreactor_source"))}</dd>'
                f'<dt>Stand-in</dt><dd class="mono">{_e(summary.get("standin"))}</dd>'
                f'<dt>Iteration budget</dt><dd>{_e(state.get("max_iterations"))}</dd></dl>')
    if gates.get('safety_overrides'):
        body.append('<div class="banner"><b>A safety rule overrode what was asked.</b> '
                    + ' '.join(_e(x) for x in gates['safety_overrides']) + '</div>')

    body.append('<h2 id="outcome">Outcome</h2>')
    if a:
        body.append(f'<p>Final verdict <b>{_e(a["verdict"]["status"])}</b>: '
                    f'{_e(a["verdict"]["summary"])}</p>')
        body.append(chart_arms_vs_target(a, colors=arm_colors))
        body.append(_arm_table(a))
    if summary.get('best'):
        rows = ''.join(f'<tr><td>{_e(k)}</td><td class="n">{_num(v.get("observed"))}</td>'
                       f'<td class="n">{_e(v.get("iteration"))}</td>'
                       f'<td class="mono">{_e(v.get("protocol_id"))}</td></tr>'
                       for k, v in sorted(summary['best'].items()))
        body.append('<h3>Best reading reached per arm</h3><table><thead><tr><th>Arm</th>'
                    '<th class="n">Best</th><th class="n">Iteration</th><th>Protocol</th>'
                    f'</tr></thead><tbody>{rows}</tbody></table>')
    if summary.get('refusals'):
        body.append('<h3>Refusals</h3><p>Each of these is the system declining to do something, '
                    'which is the envelope working rather than a failure to debug away.</p>'
                    '<ul class="tight">' + ''.join(
                        f'<li><code>{_e(r.get("step"))}</code>: {_e(r.get("error") or r.get("status"))}</li>'
                        for r in summary['refusals']) + '</ul>')

    body.append('<h2 id="search">How the search moved</h2>')
    body.append('<p>The optimiser is a bounded coordinate search with backtracking. It moves one '
                'lever per arm per iteration, scores it against that arm\'s best reading so far, '
                'and reverts anything that does not beat it. It has no model of the process and '
                'cannot see interactions between levers.</p>')
    body.append(chart_trajectory(loop, colors=arm_colors))
    body.append(chart_outcomes(loop))
    body.append(chart_levers(loop))
    body.append(_search_section(loop))
    body.append('<h3>Change log, iteration by iteration</h3>')
    body.append(_moves_section(loop))

    body.append('<h2 id="decisions">Every decision, with its reasoning</h2>')
    if not loop['decisions']:
        body.append('<p class="mut">No decision was committed.</p>')
    for i, d in enumerate(loop['decisions'], 1):
        body.append(_decision_card(d, i))

    if loop['consults']:
        body.append('<h3>Questions put to a person</h3>')
        for c in loop['consults']:
            ans = c.get('answer') or {}
            body.append(f'<div class="card"><h4>{_e(c["consult_id"])} &middot; '
                        f'{"blocking" if c.get("blocking") else "non-blocking"} &middot; '
                        f'{_e(c.get("status"))}</h4>'
                        f'<p><b>{_e(c.get("question"))}</b></p>'
                        f'<p class="mut">{_e(c.get("why_it_matters"))}</p>'
                        + (f'<p><b>Answer</b> ({_e(ans.get("answered_by"))}, evidence status '
                           f'<code>{_e(ans.get("evidence_status"))}</code>): '
                           f'{_e(ans.get("content"))}</p>' if ans else
                           f'<p class="warn">Unanswered. If nobody answers: '
                           f'{_e(c.get("if_unanswered"))}</p>')
                        + '</div>')

    body.append('<h2 id="protocol">The protocol it ended on</h2>')
    if p:
        body.append('<p>This is the configuration the search held at the end, which under its own '
                    'accept/reject rule is the best it found. Read the provenance column before '
                    'reading any number: a design choice is a value the loop chose, not a value '
                    'anyone measured.</p>')
        body.append(chart_provenance(p))
        body.append(_protocol_section(p))
    else:
        body.append('<p class="mut">No protocol was approved.</p>')

    body.append('<h2 id="references">References</h2>')
    body.append(_references_section(references(loop)))

    body.append('<h2 id="limits">Limitations</h2>')
    body.append(_limitations(loop, p, a))

    body.append(f'<div class="foot">Generated by <code>biosense.production.report</code> from the '
                f'JSON in <code>{_e(loop["dir"].name)}</code>. Nothing in this report was written '
                f'by hand, and nothing in it is clinical or manufacturing guidance.</div>')
    return _page(title, ''.join(body),
                 f'Why the BioSense loop {loop["loop_id"]} made each choice, with references, '
                 f'rationales and the provenance of every protocol quantity.')


# ── comparative report ──────────────────────────────────────

def build_comparative_report(loop_dirs, title='Comparative reasoning report', intro=None,
                             report_links=None):
    """Two or more loops side by side: the question that asks 'and what if instead'.

    `report_links` maps a loop directory name to the href of its own full report,
    so a reader who wants the decision-by-decision record can reach it. Without
    it the comparison stands alone.
    """
    loops = [load_loop(d) for d in loop_dirs]
    links = dict(report_links or {})
    body = [f'<h1>{_e(title)}</h1>']
    body.append(f'<p class="sub">{len(loops)} loops compared &middot; '
                + ' &middot; '.join(f'<code>{_e(l["loop_id"])}</code>' for l in loops) + '</p>')
    if links:
        body.append('<p class="sub">Full decision-by-decision report for each: '
                    + ' &middot; '.join(
                        f'<a href="{_e(links[l["dir"].name])}">{_e(l["dir"].name)}</a>'
                        for l in loops if l['dir'].name in links) + '</p>')
    body.append('<div class="banner"><b>Synthetic stand-in reactor.</b> Every number below is the '
                'output of a phenomenological stand-in whose per-arm behaviour was invented for '
                'software testing. The comparison demonstrates that the loop can find that two '
                'genotypes need different conditions. It is not evidence that they do.</div>')
    if intro:
        body.append(f'<div class="card">{intro}</div>')

    # Outcome comparison
    body.append('<h2>Outcomes side by side</h2>')
    rows = []
    for l in loops:
        a, p = final_analysis(l), final_protocol(l)
        s = l.get('summary') or {}
        best = s.get('best') or {}
        arms = ', '.join(f'{k} {_num(v.get("observed"))} '
                         f'({"met" if v.get("status") == "MET" else "not met"})'
                         for k, v in sorted(best.items()))
        rows.append(f'<tr><td><code>{_e(l["loop_id"])}</code></td>'
                    f'<td>{_e(a["verdict"]["status"] if a else "&mdash;")}</td>'
                    f'<td>{_e(s.get("terminal"))}</td>'
                    f'<td class="n">{_e(s.get("iterations_used"))}/{_e(s.get("max_iterations"))}</td>'
                    f'<td>{arms}</td>'
                    f'<td class="n">{len(l["decisions"])}</td></tr>')
    body.append('<table><thead><tr><th>Loop</th><th>Verdict</th><th>Ended in</th>'
                '<th class="n">Iterations</th><th>Best per arm</th><th class="n">Decisions</th>'
                f'</tr></thead><tbody>{"".join(rows)}</tbody></table>')
    body.append(chart_iterations(loops))
    body.append('<h3>How each search moved</h3>')
    body.append(chart_small_multiples(loops))

    # What each search established
    body.append('<h2>What each search established</h2>')
    for l in loops:
        s = l.get('search') or {}
        f = s.get('findings') or []
        body.append(f'<div class="card"><h3><code>{_e(l["loop_id"])}</code></h3>'
                    + ('<ul class="tight">' + ''.join(f'<li>{_e(x)}</li>' for x in f) + '</ul>'
                       if f else '<p class="mut">The search recorded no cross-arm finding.</p>')
                    + '</div>')

    # Per-arm final conditions, which is the comparison that matters
    body.append('<h2>Where the arms ended up</h2>')
    body.append('<p>The rows below are the per-arm deviations each loop established. A parameter '
                'that appears here is one the search could not satisfy with a single shared value '
                'across genotypes.</p>')
    for l in loops:
        p = final_protocol(l)
        adj = (p or {}).get('arm_adjustments') or []
        if not adj:
            body.append(f'<div class="card"><h3><code>{_e(l["loop_id"])}</code></h3>'
                        '<p class="mut">No per-arm deviation: one shared schedule served every '
                        'arm in this loop.</p></div>')
            continue
        steps = {s['step_id']: (st, s) for st in p['stages'] for s in st['steps']}
        rows = []
        for a in adj:
            st, s = steps.get(a['step_id'], (None, None))
            factor = (s or {}).get('factor') or (s or {}).get('action') or a['step_id']
            stage = (st or {}).get('stage_id', '')
            base = ((s or {}).get('quantity') or {}).get('value')
            if a.get('quantity'):
                val, unit = a['quantity'].get('value'), a['quantity'].get('unit')
            else:
                val, unit = a.get('day_shift'), 'day shift'
            rows.append(f'<tr><td>{_e(a["arm_id"])}</td><td><b>{_e(factor)}</b></td>'
                        f'<td class="mut">{_e(stage)}</td>'
                        f'<td class="n">{_num(base)}</td><td class="n">{_num(val)}</td>'
                        f'<td>{_e(unit)}</td></tr>')
        link = (f' <a href="{_e(links[l["dir"].name])}">full report</a>'
                if l['dir'].name in links else '')
        body.append(f'<div class="card"><h3><code>{_e(l["loop_id"])}</code>{link}</h3>'
                    '<table><thead><tr><th>Arm</th><th>Parameter</th><th>Stage</th>'
                    '<th class="n">Shared value</th><th class="n">This arm</th><th>Unit</th>'
                    f'</tr></thead><tbody>{"".join(rows)}</tbody></table>'
                    '<p class="mut">"Shared value" is what the base protocol runs for every other '
                    'arm. A row here is a parameter the search could not satisfy with one value '
                    'across genotypes.</p></div>')

    # Provenance comparison
    body.append('<h2>Evidence standing of each final protocol</h2>')
    rows = []
    for l in loops:
        p = final_protocol(l)
        c = provenance_counts(p) if p else {}
        rows.append(f'<tr><td><code>{_e(l["loop_id"])}</code></td>'
                    + ''.join(f'<td class="n">{c.get(k, 0)}</td>'
                              for k in ('reported', 'adapted', 'design_choice', 'gap'))
                    + '</tr>')
    body.append('<table><thead><tr><th>Loop</th><th class="n">Reported</th><th class="n">Adapted</th>'
                '<th class="n">Design choice</th><th class="n">Gap</th></tr></thead>'
                f'<tbody>{"".join(rows)}</tbody></table>')
    for l in loops:
        fp = final_protocol(l)
        if fp:
            body.append(f'<h4>{_e(l["dir"].name)}</h4>' + chart_provenance(fp))
    body.append('<p class="mut">'
                + ' '.join(f'<b>{_e(PROV_LABEL[k])}:</b> {_e(PROV_NOTE[k])}'
                           for k in ('reported', 'adapted', 'design_choice', 'gap'))
                + '</p>')

    # Shared references
    body.append('<h2>References used across these loops</h2>')
    merged = {}
    for l in loops:
        for r in references(l):
            if r['id'] in merged:
                merged[r['id']]['used_for'] = sorted(set(merged[r['id']]['used_for'])
                                                     | set(r['used_for']))
            else:
                merged[r['id']] = dict(r)
    body.append(_references_section(sorted(merged.values(),
                                           key=lambda r: (r['kind'], r['citation']))))

    body.append('<h2>Limitations of the comparison</h2>')
    body.append('<ul class="tight">'
                '<li>The arms were compared inside a synthetic stand-in whose per-arm knobs were '
                'invented. The stand-in was built so that the levers the annotation suggests are '
                'the ones that pay off, which is what makes the demonstration legible and also '
                'what makes it worthless as biology.</li>'
                '<li>One replicate per arm. Every difference is directional only.</li>'
                '<li>The curated gene annotations distinguish what a cited paper reports from a '
                'transfer to this cell type. Only the former rests on a publication, and the '
                'cited work is in peripheral or engineered T cells, not in an iPSC-derived '
                'differentiation.</li>'
                '<li>A parameter the search split per arm is a statement about this stand-in\'s '
                'response surface, not about the genotype.</li></ul>')
    body.append('<div class="foot">Generated by <code>biosense.production.report</code>. '
                'Not clinical or manufacturing guidance.</div>')
    return _page(title, ''.join(body),
                 'Two BioSense optimisation loops compared: outcomes, the per-arm conditions each '
                 'search established, evidence standing and references.')


# ── output ──────────────────────────────────────────────────

# Where a Chromium-family browser normally lives, per platform. Any of them can
# print the report; the PDF is identical whichever one does it.
_BROWSER_PATHS = {
    'darwin': (
        '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
        '/Applications/Chromium.app/Contents/MacOS/Chromium',
        '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
        '/Applications/Brave Browser.app/Contents/MacOS/Brave Browser',
    ),
    'win32': (
        r'C:\Program Files\Google\Chrome\Application\chrome.exe',
        r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
        r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
        r'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
    ),
    'linux': (
        '/opt/pw-browsers/chromium',
        '/usr/bin/chromium',
        '/usr/bin/chromium-browser',
        '/usr/bin/google-chrome',
        '/snap/bin/chromium',
    ),
}


def _chromium():
    """A Chromium-family browser that can print to PDF, or None.

    Looked up on PATH first, then in the platform's usual install locations,
    because on Windows and macOS a browser is normally installed somewhere PATH
    does not reach. BIOSENSE_CHROME overrides everything, for a browser kept
    somewhere unusual.
    """
    override = os.environ.get('BIOSENSE_CHROME')
    if override and Path(override).is_file():
        return override
    for name in ('chromium', 'chromium-browser', 'google-chrome', 'chrome',
                 'google-chrome-stable', 'msedge'):
        p = shutil.which(name)
        if p:
            return p
    for p in _BROWSER_PATHS.get(sys.platform, _BROWSER_PATHS['linux']):
        if Path(p).is_file() and os.access(p, os.X_OK):
            return p
    # Playwright's bundled builds live in versioned directories.
    root = Path(os.environ.get('PLAYWRIGHT_BROWSERS_PATH', '/opt/pw-browsers'))
    if root.is_dir():
        for pattern in ('chromium*/chrome-linux/chrome', 'chromium*/chrome-win/chrome.exe',
                        'chromium*/chrome-mac/Chromium.app/Contents/MacOS/Chromium'):
            for cand in sorted(root.glob(pattern)):
                if os.access(cand, os.X_OK):
                    return str(cand)
    return None


def write_pdf(html_path, pdf_path):
    """Print the HTML to PDF with headless Chromium.

    Returns the path on success, or None with the reason printed. It never writes
    a partial or placeholder PDF: a missing browser means no file, not a broken
    one.
    """
    exe = _chromium()
    html_path, pdf_path = Path(html_path), Path(pdf_path)
    if not exe:
        print(f'no Chromium found, so {pdf_path.name} was not written; the HTML at '
              f'{html_path} is complete and prints to PDF from any browser')
        return None
    with tempfile.TemporaryDirectory() as tmp:
        cmd = [exe, '--headless', '--disable-gpu', '--no-sandbox',
               f'--user-data-dir={tmp}', '--no-pdf-header-footer',
               '--virtual-time-budget=4000',
               f'--print-to-pdf={pdf_path}', html_path.resolve().as_uri()]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=180)
        except (OSError, subprocess.SubprocessError) as e:
            print(f'Chromium could not print {html_path.name}: {e}')
            return None
    if not pdf_path.exists() or pdf_path.stat().st_size < 1000:
        print(f'Chromium exited {r.returncode} without a usable PDF: '
              f'{r.stderr.decode()[-400:]}')
        if pdf_path.exists():
            pdf_path.unlink()
        return None
    return pdf_path


def write_report(loop_dir, out_html, out_pdf=None, title=None):
    out_html = Path(out_html)
    out_html.parent.mkdir(parents=True, exist_ok=True)
    out_html.write_text(build_report(loop_dir, title), encoding='utf-8')
    pdf = write_pdf(out_html, out_pdf) if out_pdf else None
    return out_html, pdf


def write_comparative_report(loop_dirs, out_html, out_pdf=None, title=None, intro=None,
                             report_links=None):
    out_html = Path(out_html)
    out_html.parent.mkdir(parents=True, exist_ok=True)
    out_html.write_text(
        build_comparative_report(loop_dirs, title or 'Comparative reasoning report', intro,
                                 report_links),
        encoding='utf-8')
    pdf = write_pdf(out_html, out_pdf) if out_pdf else None
    return out_html, pdf


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description='Write the reasoning report for a loop.')
    ap.add_argument('--loop-dir', action='append', required=True,
                    help='loop directory; repeat for a comparative report')
    ap.add_argument('--out', required=True, help='output .html path')
    ap.add_argument('--pdf', default=None, help='also print a PDF here')
    ap.add_argument('--title', default=None)
    a = ap.parse_args(argv)
    if len(a.loop_dir) == 1:
        h, p = write_report(a.loop_dir[0], a.out, a.pdf, a.title)
    else:
        h, p = write_comparative_report(a.loop_dir, a.out, a.pdf, a.title)
    print(json.dumps({'html': str(h), 'pdf': str(p) if p else None}, indent=2))


if __name__ == '__main__':
    main()
