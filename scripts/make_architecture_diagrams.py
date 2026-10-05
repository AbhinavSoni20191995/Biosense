"""Repository-native architecture diagrams, light and dark.

    uv run --frozen python scripts/make_architecture_diagrams.py --out docs/assets

Four pictures, each making one claim:

  system       what talks to what, from a person's objective to a decision
  evidence     where evidence comes from and what provenance it keeps
  loop         the closed loop, and where it is allowed to stop
  capabilities which modalities run today and which are declared

Two files per diagram rather than one `currentColor` drawing, because GitHub
renders a README SVG inside an `<img>` where `currentColor` resolves to black
and the dark variant would be unreadable. The README picks with `<picture>`.

Every box carries a status: CURRENT, PHASE 1 or PLANNED. A diagram that shows a
planned capability the same way as a working one is a claim the code does not
support, so the legend is not decoration.
"""
from __future__ import annotations

import argparse
from pathlib import Path

LIGHT = {
    'bg': '#f4f8f6', 'ink': '#0d1a14', 'ink2': '#44574e', 'ink3': '#71847b',
    'line': '#c9d6cf', 'brand': '#169857', 'brand_soft': '#e4f2ea',
    'code': '#1f6fa8', 'code_soft': '#e2eef7', 'stop': '#a3291f', 'stop_soft': '#f7e4e2',
    'human': '#8a5a00', 'human_soft': '#f7eeda', 'panel': '#ffffff',
    'data': '#6b4ba8', 'data_soft': '#efe9f7', 'plan_soft': '#eef2f0',
}
DARK = {
    'bg': '#0d1317', 'ink': '#e9f1ed', 'ink2': '#a7b8b0', 'ink3': '#7c8d86',
    'line': '#2a373d', 'brand': '#3ddc84', 'brand_soft': '#112a1e',
    'code': '#4292c9', 'code_soft': '#10212e', 'stop': '#ef8178', 'stop_soft': '#2d1715',
    'human': '#e0b25c', 'human_soft': '#2b2210', 'panel': '#151c21',
    'data': '#9478cf', 'data_soft': '#1d1730', 'plan_soft': '#171e22',
}

FONT = 'Inter,system-ui,-apple-system,sans-serif'
MONO = 'ui-monospace,SFMono-Regular,Menlo,monospace'


def esc(s):
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def head(w, h, c, label):
    # sorted, not a set literal: set iteration order varies between processes
    # under hash randomisation, so an unsorted marker block makes the committed
    # SVG differ from a freshly generated one for no reason anybody can see
    cols = sorted({c['line'], c['brand'], c['code'], c['stop'], c['human'], c['data']})
    markers = ''.join(
        f'<marker id="a-{col.lstrip("#")}" viewBox="0 0 10 10" refX="9" refY="5" '
        f'markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
        f'<path d="M 0 1 L 9 5 L 0 9 z" fill="{col}"/></marker>' for col in cols)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" '
            f'height="{h}" role="img" font-family="{FONT}" aria-label="{esc(label)}">'
            f'<defs>{markers}</defs>'
            f'<rect width="{w}" height="{h}" rx="14" fill="{c["bg"]}"/>')


def box(x, y, w, h, title, sub, c, accent, soft, status=None, dash=False):
    out = [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="11" fill="{soft}" '
           f'stroke="{accent}" stroke-width="1.6"'
           + (' stroke-dasharray="5 4"' if dash else '') + '/>']
    lines = sub.split('|') if sub else []
    ty = y + (h / 2 - 6 * len(lines) + 4 if lines else h / 2 + 4)
    if status:
        out.append(f'<text x="{x + w - 9}" y="{y + 14}" text-anchor="end" fill="{accent}" '
                   f'font-size="8" font-family="{MONO}" font-weight="700" '
                   f'letter-spacing="0.9">{status}</text>')
        ty += 5
    out.append(f'<text x="{x + w / 2}" y="{ty}" text-anchor="middle" fill="{c["ink"]}" '
               f'font-size="13.5" font-weight="650">{esc(title)}</text>')
    for i, line in enumerate(lines):
        out.append(f'<text x="{x + w / 2}" y="{ty + 16 + i * 13}" text-anchor="middle" '
                   f'fill="{c["ink2"]}" font-size="11">{esc(line)}</text>')
    return ''.join(out)


def arrow(x1, y1, x2, y2, c, label='', color=None, dash=False, lx=None, ly=None,
          anchor='middle'):
    col = color or c['line']
    out = [f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{col}" stroke-width="1.7" '
           f'marker-end="url(#a-{col.lstrip("#")})"'
           + (' stroke-dasharray="5 4"' if dash else '') + '/>']
    if label:
        out.append(f'<text x="{lx if lx is not None else (x1 + x2) / 2}" '
                   f'y="{ly if ly is not None else (y1 + y2) / 2 - 7}" text-anchor="{anchor}" '
                   f'fill="{c["ink3"]}" font-size="10.5" font-family="{MONO}">{esc(label)}</text>')
    return ''.join(out)


def elbow(pts, c, label='', color=None, lx=0, ly=0, anchor='middle', dash=False):
    col = color or c['line']
    d = ' '.join(f'{x},{y}' for x, y in pts)
    out = [f'<polyline points="{d}" fill="none" stroke="{col}" stroke-width="1.7" '
           f'marker-end="url(#a-{col.lstrip("#")})"'
           + (' stroke-dasharray="5 4"' if dash else '') + '/>']
    for i, line in enumerate((label or '').split('|')):
        if line:
            out.append(f'<text x="{lx}" y="{ly + i * 13}" text-anchor="{anchor}" '
                       f'fill="{c["ink3"]}" font-size="10.5" font-family="{MONO}">'
                       f'{esc(line)}</text>')
    return ''.join(out)


def legend(x, y, c, items):
    out = []
    for i, (col, text) in enumerate(items):
        xx = x + i * 150
        out.append(f'<rect x="{xx}" y="{y - 8}" width="10" height="10" rx="3" fill="{col}"/>')
        out.append(f'<text x="{xx + 15}" y="{y + 1}" fill="{c["ink3"]}" font-size="10" '
                   f'font-family="{MONO}">{esc(text)}</text>')
    return ''.join(out)


def caption(x, y, c, text, anchor='middle', size=11):
    return (f'<text x="{x}" y="{y}" text-anchor="{anchor}" fill="{c["ink3"]}" '
            f'font-size="{size}">{esc(text)}</text>')


def title(x, y, c, text, color=None):
    return (f'<text x="{x}" y="{y}" text-anchor="middle" fill="{color or c["brand"]}" '
            f'font-size="11" font-weight="700" letter-spacing="1.5" '
            f'font-family="{MONO}">{text}</text>')


# ── 1. system architecture ────────────────────────────────────────────────
def system(c):
    W, H = 980, 640
    s = [head(W, H, c,
              'A person states an objective. The orchestrator asks the literature and '
              'bioinformatics specialists for evidence, the bioinformatics agent plans an '
              'analysis against a named uncertainty and deterministic tools execute it, the '
              'evidence is synthesised into candidate parameters, and only a validated decision '
              'reaches the simulator or the bioreactor. Measurements return to the orchestrator.')]

    s.append(box(390, 24, 200, 48, 'Person', 'objective · constraints', c,
                 c['human'], c['human_soft'], 'CURRENT'))
    s.append(box(330, 104, 320, 62, 'Orchestrator',
                 'what evidence is missing to make|the next production decision?', c,
                 c['brand'], c['brand_soft'], 'CURRENT'))
    s.append(arrow(490, 72, 490, 104, c, '', color=c['human']))

    s.append(box(62, 206, 216, 58, 'Literature agent', 'cited claims, with|source and quote', c,
                 c['brand'], c['brand_soft'], 'CURRENT'))
    s.append(box(318, 206, 344, 58, 'Bioinformatics agent',
                 'annotation · dataset discovery|analysis planning', c,
                 c['brand'], c['brand_soft'], 'PHASE 1'))
    s.append(arrow(330, 166, 200, 206, c, '', color=c['brand']))
    s.append(arrow(490, 166, 490, 206, c, '', color=c['brand']))

    s.append(box(300, 300, 176, 54, 'Public datasets', 'GEO · ENCODE · EBI|accession + checksum',
                 c, c['data'], c['data_soft'], 'PHASE 1'))
    s.append(box(504, 300, 176, 54, 'Private datasets', 'never served, never|a citation', c,
                 c['human'], c['human_soft'], 'PHASE 1'))
    s.append(box(708, 206, 216, 58, 'Analysis planner', 'needs a named|uncertainty', c,
                 c['code'], c['code_soft'], 'PHASE 1'))
    s.append(box(708, 300, 216, 54, 'Deterministic tools',
                 'statistics · cytometry|bulk expression', c, c['code'], c['code_soft'],
                 'PHASE 1'))
    s.append(arrow(388, 264, 388, 300, c, '', color=c['data']))
    s.append(arrow(592, 264, 592, 300, c, '', color=c['human']))
    s.append(arrow(662, 234, 708, 234, c, 'plans', color=c['code'], ly=228))
    s.append(arrow(816, 264, 816, 300, c, '', color=c['code']))

    s.append(box(300, 396, 380, 56, 'Evidence synthesis',
                 'every source keeps its class, its visibility|and its parent', c,
                 c['code'], c['code_soft'], 'PHASE 1'))
    s.append(elbow([(170, 264), (170, 424), (300, 424)], c, '', color=c['brand']))
    s.append(elbow([(816, 354), (816, 424), (680, 424)], c, '', color=c['code']))
    s.append(arrow(388, 354, 420, 396, c, '', color=c['data']))
    s.append(arrow(592, 354, 560, 396, c, '', color=c['human']))

    s.append(box(300, 480, 380, 52, 'Candidate parameters',
                 'suggestions in the loop’s lever vocabulary', c, c['code'], c['code_soft'],
                 'PHASE 1'))
    s.append(arrow(490, 452, 490, 480, c, '', color=c['code']))

    s.append(box(96, 566, 212, 50, 'Simulator', 'labelled simulation', c, c['code'],
                 c['code_soft'], 'CURRENT'))
    s.append(box(672, 566, 212, 50, 'Bioreactor', 'people run it', c, c['human'],
                 c['human_soft'], 'PLANNED', dash=True))
    s.append(box(344, 566, 292, 50, 'Decision envelope', 'refuses anything outside the verdict',
                 c, c['stop'], c['stop_soft'], 'CURRENT'))
    s.append(arrow(490, 532, 490, 566, c, 'validated', color=c['stop'], ly=552))
    s.append(arrow(344, 591, 308, 591, c, '', color=c['code']))
    s.append(arrow(636, 591, 672, 591, c, '', color=c['human']))

    s.append(elbow([(96, 591), (40, 591), (40, 135), (330, 135)], c,
                   'sensors · FACS · omics', color=c['brand'], lx=48, ly=400, anchor='start'))
    s.append(elbow([(884, 591), (944, 591), (944, 135), (650, 135)], c,
                   'measurements', color=c['human'], lx=936, ly=400, anchor='end'))

    s.append(legend(62, H - 14, c, [(c['brand'], 'reasoning agent'), (c['code'], 'deterministic'),
                                    (c['data'], 'public data'), (c['human'], 'person / private'),
                                    (c['stop'], 'refusal')]))
    s.append('</svg>')
    return ''.join(s)


# ── 2. data and evidence architecture ─────────────────────────────────────
def evidence(c):
    W, H = 980, 560
    s = [head(W, H, c,
              'Six classes of evidence feed one layer. Each keeps what it is, where it came '
              'from and who may see it as separate facts, so an analysis over private data is a '
              'derived analysis with a private source rather than a private object.')]
    s.append(title(490, 30, c, 'EVERY SOURCE KEEPS THREE FACTS APART'))
    s.append(caption(490, 48, c,
                     'what it IS  ·  what it came FROM  ·  who may SEE it'))

    cols = [
        ('Published literature', 'source · paragraph|verbatim quote', c['data'], c['data_soft'],
         'CURRENT'),
        ('Public datasets', 'accession|checksum', c['data'], c['data_soft'], 'PHASE 1'),
        ('Private datasets', 'checksum|never published', c['human'], c['human_soft'], 'PHASE 1'),
        ('Simulator', 'labelled|simulation', c['code'], c['code_soft'], 'CURRENT'),
        ('Bioreactor + FACS', 'real|measurement', c['human'], c['human_soft'], 'PLANNED'),
    ]
    for i, (t, sub, accent, soft, status) in enumerate(cols):
        x = 30 + i * 186
        s.append(box(x, 78, 170, 66, t, sub, c, accent, soft, status,
                     dash=(status == 'PLANNED')))
        s.append(arrow(x + 85, 144, x + 85, 196, c, '', color=accent))

    s.append(box(30, 196, 920, 72, 'Evidence layer',
                 'evidence_class  ·  source_evidence_class  ·  source_visibility  ·  '
                 'parent_dataset_id  ·  checksums', c, c['code'], c['code_soft'], 'PHASE 1'))

    s.append(box(30, 306, 448, 96, 'Derived analysis',
                 'a computed result is ALWAYS derived_analysis;|what it was computed from stays '
                 'in|source_evidence_class and source_visibility', c, c['brand'],
                 c['brand_soft'], 'PHASE 1'))
    s.append(box(502, 306, 448, 96, 'The citation firewall',
                 'a literature claim requires a source, a paragraph|and a verbatim quote. A '
                 'measurement has none,|so no dataset can become one.', c, c['stop'],
                 c['stop_soft'], 'CURRENT'))
    s.append(arrow(254, 268, 254, 306, c, '', color=c['brand']))
    s.append(arrow(726, 268, 726, 306, c, '', color=c['stop']))

    s.append(box(240, 436, 500, 70, 'Candidate process parameter',
                 'parameter · direction · arm_scope · basis · confidence|'
                 'every contributing class still named and visible', c, c['code'],
                 c['code_soft'], 'PHASE 1'))
    s.append(arrow(254, 402, 380, 436, c, '', color=c['brand']))
    s.append(caption(490, 528, c,
                     'Classes are never collapsed into one score. A recommendation shows a row '
                     'per source.'))
    s.append('</svg>')
    return ''.join(s)


# ── 3. the closed loop ────────────────────────────────────────────────────
def loop(c):
    W, H = 980, 470
    s = [head(W, H, c,
              'The closed loop: an objective raises an uncertainty, evidence is collected and '
              'analysed, a candidate parameter is proposed, a validated decision reaches the '
              'simulator or the bioreactor, measurements return, and the loop either optimises '
              'again or finishes.')]
    s.append(title(490, 28, c, 'ONE ITERATION'))

    nodes = [
        (30, 60, 'Objective', 'stated once', c['human'], c['human_soft']),
        (222, 60, 'Uncertainty', 'hypothesis or gap', c['brand'], c['brand_soft']),
        (414, 60, 'Evidence', 'literature · data', c['brand'], c['brand_soft']),
        (606, 60, 'Analysis', 'deterministic', c['code'], c['code_soft']),
        (798, 60, 'Candidate', 'a lever to test', c['code'], c['code_soft']),
    ]
    for x, y, t, sub, accent, soft in nodes:
        s.append(box(x, y, 152, 56, t, sub, c, accent, soft))
    for i in range(4):
        s.append(arrow(nodes[i][0] + 152, 88, nodes[i + 1][0], 88, c, '',
                       color=nodes[i + 1][4]))

    s.append(box(606, 170, 344, 56, 'Decision envelope',
                 'refuses anything the verdict does not permit', c, c['stop'], c['stop_soft'],
                 'CURRENT'))
    s.append(arrow(874, 116, 874, 170, c, '', color=c['stop']))

    s.append(box(606, 262, 152, 56, 'Simulator', 'simulation', c, c['code'], c['code_soft']))
    s.append(box(798, 262, 152, 56, 'Bioreactor', 'people run it', c, c['human'],
                 c['human_soft'], dash=True))
    s.append(arrow(682, 226, 682, 262, c, '', color=c['code']))
    s.append(arrow(874, 226, 874, 262, c, '', color=c['human']))

    s.append(box(222, 262, 344, 56, 'Measurements',
                 'density · viability · pH · DO · FACS · omics', c, c['brand'], c['brand_soft']))
    s.append(arrow(606, 290, 566, 290, c, '', color=c['brand']))

    s.append(box(222, 364, 344, 60, 'Objective met?',
                 'yes → finish and report|no → name the dominant uncertainty, iterate', c,
                 c['code'], c['code_soft']))
    s.append(arrow(394, 318, 394, 364, c, '', color=c['brand']))
    # routed below the Objective box rather than through it: the return goes to
    # the uncertainty, not back to restating the objective
    s.append(elbow([(222, 394), (186, 394), (186, 146), (298, 146), (298, 116)], c,
                   'optimise against the|dominant uncertainty', color=c['brand'],
                   lx=194, ly=212, anchor='start'))
    s.append(box(606, 364, 344, 60, 'Finish', 'what was learned, and what was not settled', c,
                 c['brand'], c['brand_soft']))
    s.append(arrow(566, 394, 606, 394, c, '', color=c['brand']))

    s.append(caption(490, H - 14, c,
                     'An analysis never changes a parameter. It produces evidence; the '
                     'orchestrator decides, and the envelope can refuse.'))
    s.append('</svg>')
    return ''.join(s)


# ── 4. bioinformatics capabilities ────────────────────────────────────────
def capabilities(c):
    W, H = 980, 420
    s = [head(W, H, c,
              'Which analysis modalities run today and which are declared but not implemented. '
              'Phase 1 runs generic statistics, processed flow cytometry and a screening bulk '
              'expression comparison; single cell, ChIP, ATAC, raw FCS and external R tools are '
              'declared so a manifest written today stays valid, and calling one is refused.')]
    s.append(title(490, 30, c, 'WHAT RUNS, AND WHAT IS ONLY DECLARED'))

    rows = [
        ('Generic statistics', 't-test · Wilcoxon · Fisher|correlation · BH-FDR · CI',
         'PHASE 1', True),
        ('Flow cytometry / FACS', 'processed, gated population tables|frequency · intensity · '
                                  'viability', 'PHASE 1', True),
        ('Bulk RNA', 'screening comparison|of normalised expression', 'PHASE 1', True),
        ('Single cell RNA', 'pseudobulk · cell state|needs h5ad + Scanpy', 'PLANNED', False),
        ('ChIP-seq / ATAC-seq', 'peak overlap · annotation|peak-to-gene', 'PLANNED', False),
        ('Raw FCS / FlowSOM / UMAP', 'automated gating|high-dimensional cytometry', 'PLANNED',
         False),
    ]
    for i, (t, sub, status, live) in enumerate(rows):
        x = 30 + (i % 3) * 312
        y = 66 + (i // 3) * 118
        accent = c['brand'] if live else c['ink3']
        soft = c['brand_soft'] if live else c['plan_soft']
        s.append(box(x, y, 296, 90, t, sub, c, accent, soft, status, dash=not live))

    s.append(box(30, 316, 920, 66, 'External-tool adapter',
                 'DESeq2 · edgeR · Scanpy · MACS — the contract and a mock ship now; the base '
                 'install needs no R,|and every record must carry its command, versions, '
                 'checksums and environment', c, c['code'], c['code_soft'], 'PHASE 1'))
    s.append(legend(30, H - 10, c, [(c['brand'], 'runs today'), (c['ink3'], 'declared only')]))
    s.append('</svg>')
    return ''.join(s)


DIAGRAMS = {'system': system, 'evidence': evidence, 'loop': loop, 'capabilities': capabilities}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--out', default='docs/assets')
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for name, fn in DIAGRAMS.items():
        for suffix, colors in (('light', LIGHT), ('dark', DARK)):
            p = out / f'arch-{name}-{suffix}.svg'
            p.write_text(fn(colors), encoding='utf-8')
            print(f'wrote {p}')


if __name__ == '__main__':
    raise SystemExit(main())
