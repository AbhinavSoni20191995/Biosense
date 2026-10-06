"""Figures rendered from the structured benchmark result.

Deterministic SVG, no screenshots, no raster. Every label comes from the active
ProjectProfile, so a CAR-T benchmark draws CAR-T parameters and readouts rather
than M-CSF and aggregate diameter. A figure for which the result holds no data
is not drawn at all — an empty axis implying a measurement is worse than a
missing picture.

Light and dark variants, because the README renders them with `<picture>` and a
`currentColor` drawing resolves to black inside GitHub's `<img>`.
"""
from __future__ import annotations

from pathlib import Path

from ..evidence import estimates as E

LIGHT = {'bg': '#f4f8f6', 'ink': '#0d1a14', 'ink2': '#44574e', 'ink3': '#71847b',
         'line': '#c9d6cf', 'brand': '#169857', 'brand_soft': '#e4f2ea',
         'code': '#1f6fa8', 'code_soft': '#e2eef7', 'stop': '#a3291f', 'stop_soft': '#f7e4e2',
         'human': '#8a5a00', 'human_soft': '#f7eeda', 'data': '#6b4ba8',
         'data_soft': '#efe9f7', 'flat': '#eef2f0'}
DARK = {'bg': '#0d1317', 'ink': '#e9f1ed', 'ink2': '#a7b8b0', 'ink3': '#7c8d86',
        'line': '#2a373d', 'brand': '#3ddc84', 'brand_soft': '#112a1e',
        'code': '#4292c9', 'code_soft': '#10212e', 'stop': '#ef8178', 'stop_soft': '#2d1715',
        'human': '#e0b25c', 'human_soft': '#2b2210', 'data': '#9478cf',
        'data_soft': '#1d1730', 'flat': '#171e22'}

FONT = 'Inter,system-ui,-apple-system,sans-serif'
MONO = 'ui-monospace,SFMono-Regular,Menlo,monospace'

# One colour per evidence class, so a reader learns the mapping once.
CLASS_COLOUR = {'published_literature': 'data', 'public_dataset': 'data',
                'private_user_dataset': 'human', 'expert_knowledge': 'human',
                'derived_analysis': 'brand', 'simulation': 'code',
                'real_measurement': 'brand', 'synthetic_fixture': 'ink3'}
TYPE_LABEL = {'measured': 'MEASURED', 'derived': 'DERIVED', 'simulated': 'SIMULATED',
              'predicted': 'PREDICTED', 'target': 'TARGET',
              'judgement': 'BEST GUESS'}


def esc(s):
    return str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def _svg(w, h, c, label, body):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" '
            f'height="{h}" role="img" font-family="{FONT}" aria-label="{esc(label)}">'
            f'<rect width="{w}" height="{h}" rx="14" fill="{c["bg"]}"/>{body}</svg>')


def _text(x, y, s, c, *, size=12, anchor='start', fill='ink', weight=400, mono=False):
    return (f'<text x="{x}" y="{y}" text-anchor="{anchor}" fill="{c[fill]}" font-size="{size}" '
            f'font-weight="{weight}"' + (f' font-family="{MONO}"' if mono else '') +
            f'>{esc(s)}</text>')


def _box(x, y, w, h, c, accent, soft, dash=False):
    return (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="{c[soft]}" '
            f'stroke="{c[accent]}" stroke-width="1.6"'
            + (' stroke-dasharray="5 4"' if dash else '') + '/>')


def _tag(x, y, s, c, fill):
    return (f'<rect x="{x}" y="{y - 9}" width="{len(s) * 5.6 + 12}" height="14" rx="7" '
            f'fill="{c[fill]}" opacity="0.16"/>'
            + _text(x + 6, y + 1.5, s, c, size=8.5, fill=fill, weight=700, mono=True))


# ── 1. workflow ──────────────────────────────────────────────────────────
def workflow(result, c):
    steps = [('Objective', result['objective'][:40], 'human', 'human_soft'),
             ('Uncertainty', result['uncertainties'][0]['ref'], 'brand', 'brand_soft'),
             ('Evidence', f'{len(result["datasets"])} dataset(s)', 'data', 'data_soft'),
             ('Analysis', (result['analyses'][0]['tool'].split('.')[-1]
                           if result['analyses'] else 'none'), 'code', 'code_soft'),
             ('Hypothesis', result['selected_hypothesis'] or 'none', 'brand', 'brand_soft'),
             ('Candidate', ', '.join(sorted({c_['parameter']
                                             for c_ in result['candidate_parameters']}))[:28],
              'code', 'code_soft'),
             ('Simulator', result['simulator']['prediction'], 'code', 'code_soft'),
             ('Next experiment', 'proposed' if result['next_experiment'] else 'none',
              'human', 'human_soft')]
    W, H = 980, 190
    b = [_text(W / 2, 28, 'WHAT BIOSENSE DID', c, size=11, anchor='middle', fill='brand',
               weight=700, mono=True)]
    x, bw, gap = 20, 108, 14
    for i, (title, sub, accent, soft) in enumerate(steps):
        b.append(_box(x, 56, bw, 72, c, accent, soft))
        b.append(_text(x + bw / 2, 84, title, c, size=11.5, anchor='middle', weight=650))
        b.append(_text(x + bw / 2, 101, sub[:17], c, size=9, anchor='middle', fill='ink2'))
        if i < len(steps) - 1:
            b.append(f'<line x1="{x + bw}" y1="92" x2="{x + bw + gap}" y2="92" '
                     f'stroke="{c["line"]}" stroke-width="1.6"/>')
        x += bw + gap
    b.append(_text(W / 2, 160, 'An analysis never changes a protocol. The orchestrator decides, '
                               'and the envelope can refuse.', c, size=11, anchor='middle',
                   fill='ink3'))
    return _svg(W, H, c, 'The benchmark workflow: objective, uncertainty, evidence, analysis, '
                         'hypothesis, candidate parameter, simulator, next experiment.',
                ''.join(b))


# ── 2. evidence summary ──────────────────────────────────────────────────
def evidence_summary(result, c):
    counts = result['evidence_classes'] or {}
    order = ['published_literature', 'public_dataset', 'synthetic_fixture',
             'private_user_dataset', 'expert_knowledge', 'derived_analysis',
             'simulation', 'real_measurement']
    rows = [(k, counts.get(k, 0)) for k in order]
    W, H = 640, 60 + len(rows) * 30 + 40
    b = [_text(20, 30, 'EVIDENCE CONSIDERED', c, size=11, fill='brand', weight=700, mono=True)]
    top = max([v for _, v in rows] + [1])
    y = 58
    for name, n in rows:
        col = CLASS_COLOUR.get(name, 'ink3')
        b.append(_text(20, y + 11, name.replace('_', ' '), c, size=11.5,
                       fill='ink' if n else 'ink3'))
        bw = 0 if not n else max(14, (n / top) * 230)
        b.append(f'<rect x="250" y="{y}" width="{bw}" height="16" rx="5" fill="{c[col]}" '
                 f'opacity="{0.9 if n else 0.25}"/>')
        b.append(_text(250 + bw + 8, y + 12, str(n) if n else 'none', c, size=11,
                       fill='ink2' if n else 'ink3', mono=True))
        y += 30
    b.append(_text(20, y + 18, 'Classes are never collapsed into one score.', c, size=10.5,
                   fill='ink3'))
    return _svg(W, H, c, 'Counts of evidence by class: literature, public data, private data, '
                         'expert knowledge, derived analysis, simulation and real measurements.',
                ''.join(b))


# ── 3 & 4. effect bars ───────────────────────────────────────────────────
def _clip(s, n):
    """Truncate on a word boundary. "...while mainta" reads as a rendering bug."""
    s = str(s)
    if len(s) <= n:
        return s
    cut = s[:n].rsplit(' ', 1)[0]
    return cut + '…'


def _effect_chart(effects, title, c, subtitle=None):
    shown = [e for e in effects if e['magnitude_estimated']][:7]
    if not shown:
        return None
    W = 900
    H = 86 + len(shown) * 40 + 34
    b = [_text(20, 30, title, c, size=11, fill='brand', weight=700, mono=True)]
    if subtitle:
        b.append(_text(20, 48, _clip(subtitle, 110), c, size=10.5, fill='ink3'))
    y = 70
    span = max(abs(e['relative_change_pct'] or 0) for e in shown) or 1
    mid = 470
    for e in shown:
        label = e.get('label') or e['metric'].replace('_', ' ')
        b.append(_text(20, y + 14, label[:34], c, size=11.5))
        a = (e.get('baseline') or {}).get('value')
        bv = (e.get('candidate') or {}).get('value')
        unit = '%' if E.is_percent(e['unit']) else ''
        b.append(_text(240, y + 14, f'{a:.4g}{unit} → {bv:.4g}{unit}', c, size=11,
                       fill='ink2', mono=True))
        rel = e['relative_change_pct'] or 0
        w = min(150, abs(rel) / span * 150)
        col = 'brand' if e.get('favourable') else ('stop' if e.get('favourable') is False
                                                   else 'ink3')
        x0 = mid if rel >= 0 else mid - w
        b.append(f'<rect x="{x0}" y="{y + 3}" width="{max(w, 2)}" height="16" rx="4" '
                 f'fill="{c[col]}" opacity=".85"/>')
        chg = (f'{e["absolute_change"]:+.4g} pp' if e['change_unit'] == E.PP
               else f'{e["absolute_change"]:+.4g}')
        rels = f' ({rel:+.3g}%)' if e['relative_change_pct'] is not None else ''
        # The change text and the provenance tag used to overlap at the same x;
        # a label nobody can read is worse than no label.
        b.append(_text(mid + 162, y + 14, chg + rels, c, size=11, fill='ink2', mono=True,
                       anchor='start'))
        b.append(_tag(W - 118, y + 14, TYPE_LABEL[e['estimate_type']], c,
                      'code' if e['estimate_type'] in ('simulated', 'predicted') else 'brand'))
        y += 40
    b.append(f'<line x1="{mid}" y1="62" x2="{mid}" y2="{y - 6}" stroke="{c["line"]}" '
             f'stroke-width="1.2" stroke-dasharray="3 4"/>')
    b.append(_text(20, y + 20, 'Every number carries the label it earned. Simulated values are '
                               'not measurements.', c, size=10.5, fill='ink3'))
    return _svg(W, H, c, f'{title}: baseline and candidate per outcome, with the absolute and '
                         f'relative change and the provenance label of each number.', ''.join(b))


def hypothesis_effect(result, c):
    if not result['hypotheses']:
        return None
    h = result['hypotheses'][0]
    return _effect_chart(h['expected_effects'], 'EXPECTED EFFECT OF THE CANDIDATE', c,
                         subtitle=h['statement'])


def simulator_comparison(result, c):
    sim = result['simulator']
    if not sim.get('effects'):
        return None
    changed = sim['candidate']['changed']
    sub = ' · '.join(f'{x["label"]} {x["from"]:g} → {x["to"]:g} {x["unit"]}' for x in changed)
    return _effect_chart(sim['effects'], 'CONTROL vs CANDIDATE — SIMULATED', c, subtitle=sub)


# ── 5. parameter change and coverage ─────────────────────────────────────
def parameter_change(result, c):
    sim = result['simulator']
    applied = sim['handoff']['applied']
    skipped = sim['handoff']['skipped']
    if not (applied or skipped):
        return None
    W = 820
    H = 92 + (len(applied) + len(skipped)) * 44 + 30
    b = [_text(20, 30, 'CANDIDATE PARAMETERS AND SIMULATOR COVERAGE', c, size=11, fill='brand',
               weight=700, mono=True),
         _text(20, 50, f'project {result["project"]["project_id"]} '
                       f'v{result["project"]["version"]} · model '
                       f'{result["project"]["simulator"].get("model_id") or "none"}',
               c, size=10.5, fill='ink3')]
    y = 72
    changed = {x['parameter_id']: x for x in (sim.get('candidate') or {}).get('changed', [])}
    for row in applied:
        b.append(_box(20, y, W - 40, 36, c, 'code', 'code_soft'))
        b.append(_text(34, y + 23, row['label'], c, size=12, weight=650))
        ch = changed.get(row['parameter_id'])
        if ch:
            b.append(_text(200, y + 23, f'{ch["from"]:g} → {ch["to"]:g} {ch["unit"]}', c,
                           size=11.5, fill='ink2', mono=True))
        b.append(_text(430, y + 23, f'→ model knob "{row["knob"]}"', c, size=11, fill='ink3',
                       mono=True))
        b.append(_tag(W - 110, y + 23, 'MODELLED', c, 'code'))
        y += 44
    for row in skipped:
        b.append(_box(20, y, W - 40, 36, c, 'ink3', 'flat', dash=True))
        b.append(_text(34, y + 23, row['label'], c, size=12, weight=650, fill='ink2'))
        b.append(_text(200, y + 23, row['reason'][:58], c, size=10, fill='ink3'))
        b.append(_tag(W - 128, y + 23, 'NOT MODELLED', c, 'ink3'))
        y += 44
    b.append(_text(20, y + 18, 'A parameter the model cannot predict is shown and labelled, '
                               'never dropped and never predicted anyway.', c, size=10.5,
                   fill='ink3'))
    return _svg(W, H, c, 'Each candidate parameter, whether the project simulator models it, '
                         'and the knob it maps to.', ''.join(b))


FIGURES = {'workflow': workflow, 'evidence_summary': evidence_summary,
           'hypothesis_effect': hypothesis_effect, 'simulator_comparison': simulator_comparison,
           'parameter_change': parameter_change}


def write_all(result, out_dir):
    """Render every figure the result supports. Returns the paths written."""
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    written = []
    for name, fn in FIGURES.items():
        for suffix, colours in (('light', LIGHT), ('dark', DARK)):
            svg = fn(result, colours)
            if svg is None:
                continue
            p = d / f'{name}-{suffix}.svg'
            p.write_text(svg, encoding='utf-8')
            written.append(p)
    return written
