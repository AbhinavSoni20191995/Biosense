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
    W, H = 980, 770
    s = [head(W, H, c,
              'A person states an objective. The orchestrator asks the literature and '
              'bioinformatics specialists for evidence. The bioinformatics agent discovers a '
              'public dataset or registers a private one and plans an analysis against a named '
              'uncertainty; deterministic tools then execute that plan over the data itself and '
              'return an AnalysisResult. Only those results, never the raw datasets, enter '
              'evidence synthesis, which produces candidate parameters; a validated decision '
              'reaches the simulator or the bioreactor, and measurements return to the '
              'orchestrator.')]

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

    # The datasets the agent found, and the plan it wrote. Neither is evidence on
    # its own: the plan says what to compute and against which uncertainty, the
    # datasets are what it is computed over, and only the result below is cited.
    s.append(box(300, 300, 176, 54, 'Public datasets', 'GEO · ENCODE · EBI|accession + checksum',
                 c, c['data'], c['data_soft'], 'PHASE 1'))
    s.append(box(504, 300, 176, 54, 'Private datasets', 'never served, never|a citation', c,
                 c['human'], c['human_soft'], 'PHASE 1'))
    s.append(box(708, 206, 216, 58, 'Analysis planner', 'needs a named|uncertainty', c,
                 c['code'], c['code_soft'], 'PHASE 1'))
    s.append(arrow(388, 264, 388, 300, c, 'fetches', color=c['data'], lx=382, ly=288,
                   anchor='end'))
    s.append(arrow(592, 264, 592, 300, c, 'registers', color=c['human'], lx=598, ly=288,
                   anchor='start'))
    s.append(arrow(662, 234, 708, 234, c, 'plans', color=c['code'], ly=228))

    # The join the earlier drawing got wrong: the data goes INTO the tools, and
    # what leaves is a computed result. A dataset never reaches synthesis itself.
    s.append(box(300, 396, 624, 58, 'Deterministic analysis',
                 'the plan, executed over the data · statistics · cytometry · bulk expression|'
                 'refuses a dataset whose metadata does not say what it is', c,
                 c['code'], c['code_soft'], 'PHASE 1'))
    s.append(arrow(388, 354, 388, 396, c, '', color=c['data']))
    s.append(arrow(592, 354, 592, 396, c, '', color=c['human']))
    s.append(arrow(816, 264, 816, 396, c, '', color=c['code']))

    s.append(box(300, 500, 380, 56, 'Evidence synthesis',
                 'every source keeps its class, its visibility|and its parent', c,
                 c['code'], c['code_soft'], 'PHASE 1'))
    s.append(arrow(490, 454, 490, 500, c, 'AnalysisResult — derived analysis', color=c['code'],
                   ly=478))
    s.append(elbow([(170, 264), (170, 528), (300, 528)], c, '', color=c['brand']))

    s.append(box(300, 592, 380, 52, 'Candidate parameters',
                 'suggestions in the loop’s lever vocabulary', c, c['code'], c['code_soft'],
                 'PHASE 1'))
    s.append(arrow(490, 556, 490, 592, c, '', color=c['code']))

    s.append(box(96, 678, 212, 50, 'Simulator', 'labelled simulation', c, c['code'],
                 c['code_soft'], 'CURRENT'))
    s.append(box(672, 678, 212, 50, 'Bioreactor', 'people run it', c, c['human'],
                 c['human_soft'], 'PLANNED', dash=True))
    s.append(box(344, 678, 292, 50, 'Decision envelope', 'refuses anything outside the verdict',
                 c, c['stop'], c['stop_soft'], 'CURRENT'))
    s.append(arrow(490, 644, 490, 678, c, 'validated', color=c['stop'], ly=664))
    s.append(arrow(344, 703, 308, 703, c, '', color=c['code']))
    s.append(arrow(636, 703, 672, 703, c, '', color=c['human']))

    s.append(elbow([(96, 703), (40, 703), (40, 135), (330, 135)], c,
                   'sensors · FACS · omics', color=c['brand'], lx=48, ly=612, anchor='start'))
    s.append(elbow([(884, 703), (944, 703), (944, 135), (650, 135)], c,
                   'measurements', color=c['human'], lx=936, ly=612, anchor='end'))

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
              'The closed loop. A person states the objective once and authorises the campaign '
              'once, by name, with an envelope and stop conditions. After that each iteration '
              'runs without a signature: an uncertainty is raised, evidence is collected and '
              'analysed, a candidate parameter is proposed, a validated decision reaches the '
              'simulator or the bioreactor, and the instruments feed their readings straight '
              'back to the orchestrator. Every round is checked against the authorised '
              'envelope; stepping outside it stops the loop and asks again.')]
    s.append(title(490, 28, c, 'ONE ITERATION'))

    nodes = [
        (30, 60, 'Objective', 'stated once · campaign|authorised once, by name',
         c['human'], c['human_soft']),
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
    s.append(box(798, 262, 152, 56, 'Bioreactor', 'setpoints applied', c, c['human'],
                 c['human_soft'], 'PLANNED', dash=True))
    s.append(arrow(682, 226, 682, 262, c, '', color=c['code']))
    s.append(arrow(874, 226, 874, 262, c, '', color=c['human']))
    # The bioreactor's own return path, dashed because the actuation half is not
    # built. The reading half is: the instrument modules are modelled already.
    s.append(elbow([(874, 318), (874, 340), (470, 340), (470, 318)], c, '',
                   color=c['human'], dash=True))

    # The return path is the instruments, not a person retyping numbers. The
    # stand-in already models them module by module, with their own noise, cost
    # and latency; that is what makes this a loop rather than a batch job.
    s.append(box(222, 262, 344, 56, 'Detectors feed back',
                 'density · viability · pH · DO · FACS · omics|'
                 'in-line, no one retypes a number', c, c['brand'], c['brand_soft']))
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
                     'The campaign is authorised once, by name. Every round is checked '
                     'against it, and an analysis never changes a parameter by itself.'))
    s.append('</svg>')
    return ''.join(s)


# ── 4. bioinformatics capabilities ────────────────────────────────────────
def capabilities(c):
    W, H = 980, 510
    s = [head(W, H, c,
              'Which analysis modalities run today and which are declared but not implemented. '
              'Phase 1 runs generic statistics, processed flow cytometry and a screening bulk '
              'expression comparison. Phase 2 adds single-cell pseudobulk and peak overlap; '
              'single cell needs the optional singlecell extra, and without it the tool is not '
              'registered at all, so a plan is refused when it is planned rather than when it '
              'runs. Raw FCS and external R tools stay declared so a manifest written today '
              'stays valid, and calling one is refused.')]
    s.append(title(490, 30, c, 'WHAT RUNS, AND WHAT IS ONLY DECLARED'))

    rows = [
        ('Generic statistics', 't-test · Wilcoxon · Fisher|correlation · BH-FDR · CI',
         'PHASE 1', True),
        ('Flow cytometry / FACS', 'processed, gated population tables|frequency · intensity · '
                                  'viability', 'PHASE 1', True),
        ('Bulk RNA', 'screening comparison|of normalised expression', 'PHASE 1', True),
        ('Single cell RNA', 'pseudobulk by sample · signatures|h5ad, needs [singlecell] extra',
         'PHASE 2', True),
        ('ChIP-seq / ATAC-seq', 'peak overlap · peak-to-gene|reads called peaks, never calls them',
         'PHASE 2', True),
        ('Raw FCS / FlowSOM / UMAP', 'automated gating|high-dimensional cytometry', 'PLANNED',
         False),
        ('External R / DESeq2', 'count-level DE with shrinkage|needs Rscript + DESeq2',
         'NEEDS R', False),
    ]
    for i, (t, sub, status, live) in enumerate(rows):
        x = 30 + (i % 3) * 312
        y = 66 + (i // 3) * 118
        accent = c['brand'] if live else c['ink3']
        soft = c['brand_soft'] if live else c['plan_soft']
        s.append(box(x, y, 296, 90, t, sub, c, accent, soft, status, dash=not live))

    s.append(box(30, 408, 920, 66, 'External-tool adapter',
                 'DESeq2 · edgeR · Scanpy · MACS — the contract and a mock ship now; the base '
                 'install needs no R,|and every record must carry its command, versions, '
                 'checksums and environment', c, c['code'], c['code_soft'], 'PHASE 1'))
    s.append(legend(30, H - 10, c, [(c['brand'], 'runs today'), (c['ink3'], 'declared only')]))
    s.append('</svg>')
    return ''.join(s)


# ── 5. how a number keeps its provenance ──────────────────────────────────
def provenance(c):
    """Phase 2: the estimate type travels with each number, not with the object.

    Drawn because the single most dangerous thing this system could do is let a
    simulated figure and a measured one sit side by side looking alike.
    """
    W, H = 980, 530
    s = [head(W, H, c,
              'Every number carries how it was produced. A change computed from two measured '
              'values is DERIVED, because no instrument measured a difference; a comparison '
              'takes the weaker of its two inputs; and a prediction becomes checkable only '
              'where a residual compares it against a real measurement.')]
    s.append(title(490, 30, c, 'THE ESTIMATE TYPE TRAVELS WITH THE NUMBER'))
    s.append(caption(490, 48, c,
                     'not with the card, the report or the hypothesis holding it'))

    kinds = [
        ('MEASURED', 'an instrument read it|FACS, sensor, sequencer', c['human'],
         c['human_soft']),
        ('DERIVED', 'code computed it|from measured values', c['data'], c['data_soft']),
        ('SIMULATED', 'a model produced it|needs a modelled parameter', c['code'],
         c['code_soft']),
        ('PREDICTED', 'expected, not yet run|direction may stand alone', c['code'],
         c['code_soft']),
        ('TARGET', 'what was asked for|never evidence of anything', c['brand'],
         c['brand_soft']),
    ]
    for i, (t, sub, accent, soft) in enumerate(kinds):
        x = 30 + i * 186
        s.append(box(x, 76, 170, 70, t, sub, c, accent, soft, 'PHASE 2'))

    s.append(box(30, 176, 448, 80, 'Weakest link wins',
                 'combining a measured value with a simulated one gives SIMULATED;|'
                 'two measured values give DERIVED, because nothing measured the difference',
                 c, c['code'], c['code_soft'], 'PHASE 2'))
    s.append(box(502, 176, 448, 80, 'Points and percentages stay apart',
                 '42% to 68% is +26 percentage POINTS and +62% RELATIVE;|'
                 'one figure standing for both is a larger-sounding claim about another quantity',
                 c, c['code'], c['code_soft'], 'PHASE 2'))

    s.append(arrow(254, 256, 254, 292, c))
    s.append(arrow(726, 256, 726, 292, c))
    s.append(box(30, 292, 920, 70, 'Quantified hypothesis',
                 'one parameter, its current and candidate value, the expected effects with '
                 'their own types,|the evidence rows with their classes, and the limitations '
                 'that travel with it',
                 c, c['brand'], c['brand_soft'], 'PHASE 2'))

    s.append(arrow(490, 362, 490, 398, c, 'committed BEFORE the run', color=c['human'],
                   lx=500, ly=382, anchor='start'))
    s.append(box(30, 398, 920, 76, 'Prediction residual',
                 'measured minus predicted, hashed at commitment so a prediction edited after '
                 'the result is visible.|A stand-in run, an unreplicated reading or an '
                 'unmodelled parameter is recorded and never counted as model evidence.',
                 c, c['human'], c['human_soft'], 'PHASE 2'))

    s.append(legend(30, 500, c, [(c['human'], 'an instrument'),
                                 (c['data'], 'code computed it'),
                                 (c['code'], 'a model produced it'),
                                 (c['brand'], 'what was asked for')]))
    s.append('</svg>')
    return ''.join(s)


DIAGRAMS = {'system': system, 'evidence': evidence, 'loop': loop,
            'capabilities': capabilities, 'provenance': provenance}


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
