"""Draw the loop diagram used in the README, in a light and a dark variant.

    uv run --frozen python scripts/make_readme_diagram.py --out docs/assets

One claim: the agent chooses, and separate code decides what it is allowed to
choose. Everything in the picture is there to make that boundary visible — the
reasoning side on the left, the deterministic side on the right, and the gate
between them that can refuse.

Two files rather than one `currentColor` drawing because GitHub renders a README
SVG inside an `<img>`, where `currentColor` resolves to black and the dark
variant would be unreadable. The README picks between them with `<picture>`.
"""
from __future__ import annotations

import argparse
from pathlib import Path

W, H = 940, 496

LIGHT = {
    'bg': '#f4f8f6', 'ink': '#0d1a14', 'ink2': '#44574e', 'ink3': '#71847b',
    'line': '#c9d6cf', 'brand': '#169857', 'brand_soft': '#e4f2ea',
    'code': '#1f6fa8', 'code_soft': '#e2eef7', 'stop': '#a3291f', 'stop_soft': '#f7e4e2',
    'human': '#8a5a00', 'human_soft': '#f7eeda', 'panel': '#ffffff',
}
DARK = {
    'bg': '#0d1317', 'ink': '#e9f1ed', 'ink2': '#a7b8b0', 'ink3': '#7c8d86',
    'line': '#2a373d', 'brand': '#3ddc84', 'brand_soft': '#112a1e',
    'code': '#4292c9', 'code_soft': '#10212e', 'stop': '#ef8178', 'stop_soft': '#2d1715',
    'human': '#e0b25c', 'human_soft': '#2b2210', 'panel': '#151c21',
}


def esc(s):
    return (s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))


def box(x, y, w, h, title, sub, c, accent, soft, dash=False):
    out = [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="11" fill="{soft}" '
           f'stroke="{accent}" stroke-width="1.6"'
           + (' stroke-dasharray="5 4"' if dash else '') + '/>']
    ty = y + (h / 2 - 7 if sub else h / 2 + 4)
    out.append(f'<text x="{x + w / 2}" y="{ty}" text-anchor="middle" fill="{c["ink"]}" '
               f'font-size="14" font-weight="650">{esc(title)}</text>')
    if sub:
        for i, line in enumerate(sub.split('|')):
            out.append(f'<text x="{x + w / 2}" y="{ty + 17 + i * 14}" text-anchor="middle" '
                       f'fill="{c["ink2"]}" font-size="11.5">{esc(line)}</text>')
    return ''.join(out)


def arrow(x1, y1, x2, y2, c, label='', color=None, dash=False, lx=None, ly=None,
          anchor='middle'):
    col = color or c['line']
    out = [f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{col}" stroke-width="1.8" '
           f'marker-end="url(#a-{col.lstrip("#")})"'
           + (' stroke-dasharray="5 4"' if dash else '') + '/>']
    if label:
        out.append(f'<text x="{lx if lx is not None else (x1 + x2) / 2}" '
                   f'y="{ly if ly is not None else (y1 + y2) / 2 - 7}" text-anchor="{anchor}" '
                   f'fill="{c["ink3"]}" font-size="11" font-family="ui-monospace,monospace">'
                   f'{esc(label)}</text>')
    return ''.join(out)


def elbow(pts, c, label='', color=None, lx=0, ly=0, anchor='middle'):
    col = color or c['line']
    d = ' '.join(f'{x},{y}' for x, y in pts)
    out = [f'<polyline points="{d}" fill="none" stroke="{col}" stroke-width="1.8" '
           f'marker-end="url(#a-{col.lstrip("#")})"/>']
    if label:
        out.append(f'<text x="{lx}" y="{ly}" text-anchor="{anchor}" fill="{c["ink3"]}" '
                   f'font-size="11" font-family="ui-monospace,monospace">{esc(label)}</text>')
    return ''.join(out)


def build(c):
    markers = ''.join(
        f'<marker id="a-{col.lstrip("#")}" viewBox="0 0 10 10" refX="9" refY="5" '
        f'markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
        f'<path d="M 0 1 L 9 5 L 0 9 z" fill="{col}"/></marker>'
        for col in {c['line'], c['brand'], c['code'], c['stop'], c['human']})

    s = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" '
         f'height="{H}" role="img" font-family="Inter,system-ui,-apple-system,sans-serif" '
         f'aria-label="The agent proposes a decision; deterministic code decides which '
         f'decisions are allowed and refuses the rest. A wet-lab run additionally needs a '
         f'named human approver.">',
         f'<defs>{markers}</defs>',
         f'<rect width="{W}" height="{H}" rx="14" fill="{c["bg"]}"/>']

    # column headings
    s.append(f'<text x="212" y="34" text-anchor="middle" fill="{c["brand"]}" font-size="11.5" '
             f'font-weight="700" letter-spacing="1.6" font-family="ui-monospace,monospace">'
             f'REASONS</text>')
    s.append(f'<text x="212" y="50" text-anchor="middle" fill="{c["ink3"]}" font-size="11">'
             f'language models</text>')
    s.append(f'<text x="703" y="34" text-anchor="middle" fill="{c["code"]}" font-size="11.5" '
             f'font-weight="700" letter-spacing="1.6" font-family="ui-monospace,monospace">'
             f'DECIDES WHAT IS ALLOWED</text>')
    s.append(f'<text x="703" y="50" text-anchor="middle" fill="{c["ink3"]}" font-size="11">'
             f'deterministic code · no model</text>')
    s.append(f'<line x1="470" y1="24" x2="470" y2="286" stroke="{c["line"]}" '
             f'stroke-width="1.4" stroke-dasharray="3 6"/>')

    # ── left: the reasoning side ──────────────────────────
    s.append(box(44, 70, 150, 58, 'Person', 'aim · constraints', c, c['human'],
                 c['human_soft']))
    s.append(box(44, 168, 150, 64, 'Literature', 'Europe PMC|cited claims', c, c['brand'],
                 c['brand_soft']))
    s.append(box(44, 254, 150, 64, 'Bioinformatics', 'what a gene|annotation says', c,
                 c['brand'], c['brand_soft']))
    s.append(box(248, 150, 178, 104, 'Orchestrator', 'reads the analysis|forms a hypothesis|'
                                                     'proposes ONE action', c, c['brand'],
                 c['brand_soft']))

    s.append(elbow([(194, 99), (222, 99), (222, 176), (248, 176)], c,
                   'states the aim once', color=c['human'], lx=228, ly=92, anchor='start'))
    s.append(arrow(194, 200, 248, 190, c, '', color=c['brand']))
    s.append(arrow(194, 286, 248, 236, c, '', color=c['brand']))

    # ── the gate ───────────────────────────────────────────
    s.append(box(540, 150, 196, 104, 'Decision envelope', 'allowed_actions|validate_decision|'
                                                          'refuses the rest', c, c['code'],
                 c['code_soft']))
    s.append(arrow(426, 192, 540, 192, c, 'proposes', color=c['brand'], lx=483, ly=184))
    s.append(arrow(540, 228, 426, 228, c, 'refuses', color=c['stop'], lx=483, ly=246))

    # ── right: what the envelope permits ───────────────────
    s.append(box(778, 96, 128, 50, 'Revise', 'costs 1 iteration', c, c['code'], c['code_soft']))
    s.append(box(778, 160, 128, 50, 'Gather', 'costs none', c, c['code'], c['code_soft']))
    s.append(box(778, 224, 128, 50, 'Stop / escalate', '', c, c['code'], c['code_soft']))
    for y0, y1 in ((176, 121), (192, 185), (210, 249)):
        s.append(arrow(736, y0, 778, y1, c, '', color=c['code']))

    # ── the bottom lane: what actually runs ────────────────
    y = 356
    s.append(box(44, y, 168, 62, 'Protocol', 'every value carries|its provenance', c,
                 c['brand'], c['brand_soft']))
    s.append(box(246, y, 168, 62, 'Human approval', 'named person|required for wet lab', c,
                 c['human'], c['human_soft']))
    s.append(box(448, y, 168, 62, 'Bioreactor', 'people, or a labelled|synthetic stand-in', c,
                 c['human'], c['human_soft'], dash=True))
    s.append(box(650, y, 168, 62, 'Analysis', 'target · QC · verdict|computed, not written', c,
                 c['code'], c['code_soft']))

    s.append(elbow([(337, 254), (337, 330), (128, 330), (128, y)], c, 'revise →',
                   color=c['code'], lx=240, ly=324, anchor='middle'))
    s.append(arrow(212, y + 31, 246, y + 31, c, ''))
    s.append(arrow(414, y + 31, 448, y + 31, c, ''))
    s.append(arrow(616, y + 31, 650, y + 31, c, ''))
    s.append(elbow([(818, y + 31), (880, y + 31), (880, 300), (337, 300), (337, 254)], c,
                   'verdict feeds the next decision', color=c['code'], lx=620, ly=293,
                   anchor='middle'))

    s.append(f'<text x="470" y="{H - 20}" text-anchor="middle" fill="{c["ink3"]}" '
             f'font-size="11.5">A wet-lab run always needs a named human approver, whatever '
             f'the autonomy mode asks for.</text>')
    s.append('</svg>')
    return ''.join(s)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--out', default='docs/assets')
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for name, colors in (('loop-light.svg', LIGHT), ('loop-dark.svg', DARK)):
        (out / name).write_text(build(colors), encoding='utf-8')
        print(f'wrote {out / name}')


if __name__ == '__main__':
    raise SystemExit(main())
