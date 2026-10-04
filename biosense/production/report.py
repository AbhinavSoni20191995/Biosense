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
import os
import shutil
import subprocess
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
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return _e(x)
    return f'{x:,.{nd}f}'.rstrip('0').rstrip('.') if abs(x) < 1e6 else f'{x:.3e}'


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
@media print{body{background:#fff;color:#000}.card{break-inside:avoid}
h2{break-after:avoid}table{break-inside:auto}tr{break-inside:avoid}}
"""


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

def build_comparative_report(loop_dirs, title='Comparative reasoning report', intro=None):
    """Two or more loops side by side: the question that asks 'and what if instead'."""
    loops = [load_loop(d) for d in loop_dirs]
    body = [f'<h1>{_e(title)}</h1>']
    body.append(f'<p class="sub">{len(loops)} loops compared &middot; '
                + ' &middot; '.join(f'<code>{_e(l["loop_id"])}</code>' for l in loops) + '</p>')
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
        body.append(f'<div class="card"><h3><code>{_e(l["loop_id"])}</code></h3>'
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

def _chromium():
    for name in ('chromium', 'chromium-browser', 'google-chrome', 'chrome'):
        p = shutil.which(name)
        if p:
            return p
    for p in ('/opt/pw-browsers/chromium', '/usr/bin/chromium'):
        if Path(p).is_file() and os.access(p, os.X_OK):
            return p
    # Playwright's bundled builds live in versioned directories.
    root = Path(os.environ.get('PLAYWRIGHT_BROWSERS_PATH', '/opt/pw-browsers'))
    if root.is_dir():
        for cand in sorted(root.glob('chromium*/chrome-linux/chrome')):
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


def write_comparative_report(loop_dirs, out_html, out_pdf=None, title=None, intro=None):
    out_html = Path(out_html)
    out_html.parent.mkdir(parents=True, exist_ok=True)
    out_html.write_text(
        build_comparative_report(loop_dirs, title or 'Comparative reasoning report', intro),
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
