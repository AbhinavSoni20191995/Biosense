/* ──────────────────────────────────────────────────────────────
   Wild type against an engineered line: growth curves, side by side.

   Small multiples, one per channel, both lines on the same axes in every
   panel so the eye compares heights directly. The numbers come from the
   server's reactor run; this file only draws them. Colours are the series
   tokens, so both themes read the same.
   ────────────────────────────────────────────────────────────── */
'use strict';
const BSCurves = (() => {
  const NS = 'http://www.w3.org/2000/svg';
  const PANELS = [
    ['vcd_e6_per_ml', 'Viable cell density', '1e6/mL'],
    ['viability_pct', 'Viability', '%'],
    ['harvest_cum_e6_per_ml', 'Harvested, cumulative', '1e6/mL'],
  ];
  const mk = (t, a, txt) => {
    const n = document.createElementNS(NS, t);
    Object.entries(a || {}).forEach(([k, v]) => n.setAttribute(k, v));
    if (txt != null) n.textContent = txt;
    return n;
  };
  const fmt = v => (v == null ? '—' : Math.abs(v) >= 100 ? v.toFixed(0)
    : Math.abs(v) >= 10 ? v.toFixed(1) : v.toFixed(2));

  function panel(days, a, b, label, unit, names) {
    const W = 300, H = 170, L = 40, R = 10, T = 22, B = 26;
    const vals = a.concat(b).filter(v => typeof v === 'number');
    const lo = Math.min(0, ...vals), hi = Math.max(...vals, 1e-9) * 1.08;
    const d0 = days[0] || 0, d1 = days[days.length - 1] || 1;
    const X = d => L + (d - d0) / ((d1 - d0) || 1) * (W - L - R);
    const Y = v => T + (1 - (v - lo) / ((hi - lo) || 1)) * (H - T - B);
    const svg = mk('svg', { viewBox: `0 0 ${W} ${H}`, class: 'bc-panel', role: 'img',
      'aria-label': `${label}: ${names[0]} and ${names[1]} over ${Math.round(d1)} days` });
    svg.append(mk('text', { x: L, y: 14, class: 'bc-title' }, `${label} (${unit})`));
    [lo, (lo + hi) / 2, hi].forEach(v => {
      svg.append(mk('line', { x1: L, x2: W - R, y1: Y(v), y2: Y(v), class: 'bc-grid' }));
      svg.append(mk('text', { x: L - 4, y: Y(v) + 3, class: 'bc-tick', 'text-anchor': 'end' }, fmt(v)));
    });
    [d0, (d0 + d1) / 2, d1].forEach(d => svg.append(mk('text',
      { x: X(d), y: H - 8, class: 'bc-tick', 'text-anchor': 'middle' }, `d${Math.round(d)}`)));
    [[a, 'bc-a'], [b, 'bc-b']].forEach(([series, cls]) => {
      const pts = series.map((v, i) => (typeof v === 'number' ? `${X(days[i])},${Y(v)}` : null))
        .filter(Boolean).join(' ');
      svg.append(mk('polyline', { points: pts, class: 'bc-line ' + cls }));
    });
    return svg;
  }

  /* curves: {days, wild_type: {channel: [..]}, edited: {channel: [..]}} */
  function render(host, curves, opts) {
    host.textContent = '';
    if (!curves || !(curves.days || []).length) return;
    const names = [(opts && opts.aLabel) || 'wild type', (opts && opts.bLabel) || 'edited'];
    const legend = document.createElement('div'); legend.className = 'bc-legend';
    names.forEach((n, i) => {
      const s = document.createElement('span');
      const sw = document.createElement('i'); sw.className = i ? 'bc-sw bc-b' : 'bc-sw bc-a';
      s.append(sw, document.createTextNode(n)); legend.append(s);
    });
    const grid = document.createElement('div'); grid.className = 'bc-grid-wrap';
    PANELS.forEach(([key, label, unit]) => {
      const a = (curves.wild_type || {})[key] || [], b = (curves.edited || {})[key] || [];
      if (!a.length && !b.length) return;
      grid.append(panel(curves.days, a, b, label, unit, names));
    });
    host.append(legend, grid);
  }

  /* The few numbers a reader asks for, from the server's computed deltas. */
  function table(host, deltas, names) {
    host.textContent = '';
    const rows = [
      ['harvest_per_input_ipsc', 'Harvest per input iPSC'],
      ['peak_vcd_e6_per_ml', 'Peak density (1e6/mL)'],
      ['final_viability_pct', 'Final viability (%)'],
      ['cumulative_differentiation_efficiency', 'Differentiation efficiency'],
    ];
    const head = document.createElement('tr');
    ['', names[0], names[1], 'difference'].forEach(t => {
      const th = document.createElement('th'); th.textContent = t; head.append(th);
    });
    host.append(head);
    rows.forEach(([k, label]) => {
      const d = (deltas || {})[k]; if (!d) return;
      const tr = document.createElement('tr');
      [label, fmt(d.a), fmt(d.b), (d.delta > 0 ? '+' : '') + fmt(d.delta)
        + (d.pct != null ? ` (${d.pct > 0 ? '+' : ''}${d.pct}%)` : '')].forEach(t => {
        const td = document.createElement('td'); td.textContent = t; tr.append(td);
      });
      host.append(tr);
    });
  }

  return { render, table };
})();
