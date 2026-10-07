/* ──────────────────────────────────────────────────────────────
   Simulator mode.

   The server owns the model and the condition read; this file owns the
   drawing. The split matters: a number shown here was computed in
   `biosense/production/sim_mode.py` from an observation channel, and nothing
   in this file invents, smooths or rescales biology. What it does invent is
   where an aggregate sits in the vessel -- position is arrangement, not data,
   so it comes from a fixed hash rather than the model.

   One request per condition change (about a tenth of a second), then the
   timeline scrubs locally through the days that request returned.
   ────────────────────────────────────────────────────────────── */
'use strict';
const $ = (s, r) => (r || document).querySelector(s);
const el = (t, c, x) => { const n = document.createElement(t); if (c) n.className = c;
  if (x !== undefined) n.textContent = x; return n; };
const num = (v, nd) => (v === null || v === undefined || Number.isNaN(v)) ? '—'
  : (Math.abs(v) >= 1e6 ? v.toExponential(2) : (+v.toFixed(nd === undefined ? 2 : nd)).toString());

/* a stable pseudo-random from an integer: aggregate placement must not jitter
   when the day advances, or the eye reads movement as biology */
const hash = (i) => { let x = Math.sin(i * 127.1 + 311.7) * 43758.5453; return x - Math.floor(x); };

const state = {
  cfg: null, result: null, day: 0, playing: null,
  A: null, B: null, inflight: 0, seed: 7,
  handoff: null, projects: null, project: null, candidates: null,
};

/* theme — same key as the console, so the choice follows you across modes */
(function () {
  let saved = null; try { saved = localStorage.getItem('bs-theme'); } catch (_) {}
  if (saved) document.documentElement.setAttribute('data-theme', saved);
  $('#themeBtn').addEventListener('click', () => {
    const cur = document.documentElement.getAttribute('data-theme')
      || (matchMedia('(prefers-color-scheme:dark)').matches ? 'dark' : 'light');
    const next = cur === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    try { localStorage.setItem('bs-theme', next); } catch (_) {}
    if (state.result) draw();
  });
})();

function status(txt, cls) {
  $('#statusTxt').textContent = txt;
  $('#status').className = 'pill ' + (cls || 'idle') + (cls === 'run' ? ' live' : '');
}
function fail(msg) { const e = $('#err'); e.textContent = msg; e.classList.remove('hide'); }
function clearFail() { $('#err').classList.add('hide'); }

/* ── build the controls from the server's own knob list ──── */
async function boot() {
  try {
    state.cfg = await (await fetch('/api/sim/config')).json();
  } catch (_) { return fail('the simulator service did not answer'); }
  const cfg = state.cfg;
  $('#notModelled').textContent = 'Not modelled: ' + cfg.not_modelled.join('; ') + '.';

  const byStage = {};
  cfg.knobs.forEach(k => (byStage[k.stage] = byStage[k.stage] || []).push(k));
  const order = ['all', 'expansion', 'mesoderm', 'hemato', 'myeloid'];
  const label = { all: 'Every stage', expansion: 'Expansion', mesoderm: 'Mesoderm',
    hemato: 'Hemogenic', myeloid: 'Myeloid harvest' };
  const host = $('#knobs');
  order.filter(s => byStage[s]).forEach(s => {
    const g = el('div', 'stagegrp');
    g.append(Object.assign(el('span', 'lbl', label[s]), {}));
    byStage[s].forEach(k => g.append(knobRow(k)));
    host.append(g);
  });

  const days = $('#days');
  cfg.stages.forEach(s => days.append(knobRow({
    id: 'day_' + s.id, label: s.label, unit: 'days', min: s.min, max: s.max,
    step: 0.5, default: s.default_days, note: s.label + ' wants ' + s.wants + '.',
  })));

  const sel = $('#challenge');
  cfg.challenges.forEach(c => sel.append(Object.assign(el('option', null, c.label),
    { value: c.id })));
  sel.addEventListener('change', () => { noteFor(sel.value); run(); });
  noteFor('none');

  $('#presetControl').addEventListener('click', () => applyPreset('control'));
  $('#presetCandidate').addEventListener('click', () => applyPreset('candidate'));
  $('#resetBtn').addEventListener('click', () => {
    document.querySelectorAll('input[type=range][data-knob]').forEach(i => {
      i.value = i.dataset.default; i.dispatchEvent(new Event('input')); });
    sel.value = 'none'; noteFor('none'); run();
  });
  $('#reseedBtn').addEventListener('click', () => {
    state.seed = (state.seed * 7 + 13) % 100000; run(); });
  await candidates();
  await projects();
  $('#playBtn').addEventListener('click', play);
  $('#scrub').addEventListener('input', e => { stop(); state.day = +e.target.value; draw(); });
  $('#slotA').addEventListener('click', () => hold('A'));
  $('#slotB').addEventListener('click', () => hold('B'));
  $('#cmpBtn').addEventListener('click', compare);
  $('#exportBtn').addEventListener('click', exportSession);
  $('#carryBtn').addEventListener('click', carry);
  $('#useAsCandidate').addEventListener('click', useAsCandidate);
  ['genoGrowth', 'genoDiff'].forEach(id => $('#' + id).addEventListener('input', e => {
    $('#' + id + 'V').textContent = '×' + (+e.target.value).toFixed(2);
  }));
  $('#genoBtn').addEventListener('click', genotypeCompare);
  $('#genoKind').addEventListener('change', e => {
    const factor = e.target.value === 'factor';
    $('#genoStageWrap').hidden = !factor;
    const name = $('#genoLabel');
    if (factor && name.value === 'BACH2 KO') name.value = 'IL-34';
    if (!factor && name.value === 'IL-34') name.value = 'BACH2 KO';
  });
  const h = readHandoff();
  if (h) applyHandoff(h);
  run();
}

/* ── which process is being simulated ─────────────────────── */
/* The model is one project's model. A project that has no mechanistic model
   says so and lists its knobs read-only, rather than borrowing this project's
   sliders: an M-CSF control on a CAR-T process would invite a setpoint nobody
   can run, and a prediction for it would be invented outright. */
async function projects() {
  let d;
  try { d = await (await fetch('/api/projects')).json(); } catch (_) { return; }
  state.projects = d.projects;
  const sel = $('#project');
  const simModel = (state.cfg && state.cfg.model_id) || 'ipsc_monocyte_v1';
  d.projects.forEach(p => sel.append(Object.assign(
    el('option', null, p.name + (p.has_simulator ? '' : '  (no model)')),
    { value: p.project_id })));
  const mine = d.projects.find(p => (p.simulator || {}).model_id === simModel)
    || d.projects.find(p => p.has_simulator);
  if (mine) sel.value = mine.project_id;
  sel.addEventListener('change', () => showProject(sel.value));
  showProject(sel.value);
}

function showProject(id) {
  const p = (state.projects || []).find(x => x.project_id === id);
  if (!p) return;
  state.project = p;
  const simModel = (state.cfg && state.cfg.model_id) || 'ipsc_monocyte_v1';
  const servedByThisModel = (p.simulator || {}).model_id === simModel;

  $('#projNote').textContent = [p.biological_system.species,
    'from ' + p.biological_system.starting_cell, 'to ' + p.biological_system.target_cell,
    p.biological_system.culture_format].filter(Boolean).join(' · ');

  $('#modelled').hidden = !servedByThisModel;
  $('#noModel').hidden = servedByThisModel;
  if (servedByThisModel) { applyPreset('control'); return; }

  $('#noModelWhy').textContent = p.has_simulator
    ? 'This process has a model, but it is not the one this page runs. Its knobs are listed '
      + 'below; no trajectory is produced for them here.'
    : 'This process has no mechanistic model of its own. Its knobs are real design variables '
      + 'you can set in the lab. To see a derived protocol run anyway, open the run and press '
      + '"Simulate this protocol": its reactor setpoints run on the stand-in reactor model, '
      + 'labelled as such — the physics carry over, this cell type\u2019s biology does not.';
  const host = $('#noModelKnobs'); host.textContent = '';
  p.parameters.forEach(q => {
    const r = el('div', 'knob');
    const row = el('div', 'row');
    row.append(el('span', 'nm', q.label || q.parameter_id));
    const right = el('span');
    right.append(el('span', 'val', fmtRange(q)), document.createTextNode(' '),
      el('span', 'un', q.unit || ''));
    row.append(right);
    r.append(row);
    if (q.bound_origin) r.append(el('p', 'note', 'limits from '
      + q.bound_origin.source.replace(/_/g, ' ') + ': ' + q.bound_origin.basis));
    host.append(r);
  });
}

function fmtRange(q) {
  return (q.minimum == null ? '?' : q.minimum) + ' – ' + (q.maximum == null ? '?' : q.maximum);
}

/* Candidates come from hypotheses the system actually formed, never from a
   value invented for a button. If nothing has been proposed for this project
   there is no candidate to offer, and the control says so rather than the
   button quietly reproducing the control under a second name. */
async function candidates() {
  let d;
  try { d = await (await fetch('/api/hypotheses')).json(); } catch (_) { return; }
  state.candidates = {};
  (d.hypotheses || []).forEach(r => {
    const h = r.hypothesis, q = h.parameter;
    if (q.candidate_value == null) return;
    (state.candidates[h.project_id] = state.candidates[h.project_id] || []).push({
      parameter_id: q.parameter_id, value: q.candidate_value,
      label: q.label || q.parameter_id, from: h.hypothesis_id });
  });
}

/* Control is this project's recorded current process, not the model's optimum.
   That distinction is the whole point of a comparison: a control picked because
   it flatters the candidate is not a control. */
function applyPreset(which) {
  const p = state.project;
  if (!p) return;
  const byKnob = {}, byId = {};
  p.parameters.forEach(q => {
    byId[q.parameter_id] = q;
    if (q.simulator_mapping) byKnob[q.simulator_mapping] = q;
  });
  const props = (state.candidates || {})[p.project_id] || [];
  const want = {};
  if (which === 'candidate') {
    props.forEach(c => {
      const q = byId[c.parameter_id];
      if (q && q.simulator_mapping) want[q.simulator_mapping] = c.value;
    });
  }

  let changed = 0;
  document.querySelectorAll('input[type=range][data-knob]').forEach(i => {
    const k = i.dataset.knob;
    const q = byKnob[k];
    if (!q) return;
    const v = which === 'candidate' && k in want ? want[k] : q.default_value;
    if (v == null) return;
    if (+i.value !== +v) changed++;
    i.value = v; i.dispatchEvent(new Event('input'));
  });

  $('#presetControl').setAttribute('aria-pressed', String(which === 'control'));
  $('#presetCandidate').setAttribute('aria-pressed', String(which === 'candidate'));
  $('#presetCandidate').disabled = !props.length;
  $('#presetNote').textContent = which === 'control'
    ? (props.length ? "the process as recorded, not the model's optimum"
                    : "the process as recorded; nothing has been proposed for it yet")
    : props.map(c => c.label + ' → ' + c.value).join(', ')
      + ' (from ' + props[0].from + ')';
  if (changed) run();
}

/* Any manual move makes the condition custom: it is no longer either preset,
   and leaving a preset highlighted would misdescribe what is on screen. */
function markCustom() {
  $('#presetControl').setAttribute('aria-pressed', 'false');
  $('#presetCandidate').setAttribute('aria-pressed', 'false');
  $('#presetNote').textContent = 'custom';
}

/* A candidate carried over from a discovery run. Read once and cleared, so a
   reload does not silently re-apply a setpoint the person has since moved. */
function readHandoff() {
  let raw = null;
  try { raw = localStorage.getItem('bs-sim-candidate'); } catch (_) { return null; }
  if (!raw) return null;
  try { localStorage.removeItem('bs-sim-candidate'); } catch (_) {}
  try { return JSON.parse(raw); } catch (_) { return null; }
}

/* A whole derived protocol. On the project's own model its setpoints load as
   the candidate. A project with no model of its own is run on this reactor as
   a STAND-IN: the physical setpoints (agitation, oxygen, feeding, seed
   density) mean the same thing in any stirred suspension culture, so they
   carry over by parameter; the target cell's biology does not, and the page
   says so every time — the harvest and identity readouts stay this model's. */
function applyProtocolHandoff(h) {
  const box = $('#handoff');
  box.hidden = false; box.textContent = '';
  const simModel = (state.cfg && state.cfg.model_id) || 'ipsc_monocyte_v1';
  const own = (state.projects || []).find(p => p.project_id === h.project_id);
  const ownServed = own && (own.simulator || {}).model_id === simModel;
  const model = ownServed ? own : (state.projects || []).find(
    p => (p.simulator || {}).model_id === simModel) || (state.projects || []).find(p => p.has_simulator);
  if (!model) { box.append(el('div', 'warnc', 'No reactor model is installed here.')); return; }
  const sel = $('#project');
  sel.value = model.project_id; showProject(model.project_id);
  const byId = {};
  (model.parameters || []).forEach(q => { if (q.simulator_mapping) byId[q.parameter_id] = q; });
  const used = h.values.filter(v => byId[v.parameter_id]);
  const unused = h.values.filter(v => !byId[v.parameter_id]);
  const t = el('div'); t.style.cssText = 'font-weight:600;color:var(--ink)';
  t.textContent = `Simulating the protocol from ${h.from_run ? 'run ' + h.from_run : 'a discovery run'}`
    + ` — ${h.project_name || h.project_id}`;
  box.append(t);
  if (!ownServed) {
    box.append(el('div', 'warnc', `STAND-IN MODEL. ${h.project_name || h.project_id} has no model `
      + `of its own, so its reactor setpoints run on the ${model.name} model. Growth, shear, `
      + `oxygen, feeding and waste carry over by physics; the differentiation biology of `
      + `${h.project_name || 'this process'} is NOT modelled, so the harvest and identity `
      + `readouts below are this model's cell type, not yours. Read the trends, not the numbers.`));
  }
  box.append(el('div', null, 'Candidate setpoints: ' + (used.length
    ? used.map(v => `${v.label || v.parameter_id} ${v.value}${v.unit ? ' ' + v.unit : ''}`
      + (v.provenance && v.provenance !== 'reported' ? ` [${v.provenance === 'design_choice' ? 'D' : v.provenance}]` : '')).join(' · ')
    : 'none of them maps onto this model')));
  if (unused.length) box.append(el('div', 'dim', 'Not represented in this model: '
    + unused.map(v => v.label || v.parameter_id).join(', ')));
  if ((h.gaps || []).length) box.append(el('div', 'dim', 'Gaps in the protocol (left at the '
    + 'control value): ' + h.gaps.join(', ')));
  box.append(el('div', 'dim', 'Control is the model\u2019s recorded process. Nothing was '
    + 'approved by loading this, and no simulated number is evidence.'));
  state.candidates = state.candidates || {};
  state.candidates[model.project_id] = used.map(v => ({ parameter_id: v.parameter_id,
    value: v.value, label: v.label || v.parameter_id, from: h.from_run || 'protocol' }));
  state.handoff = h;
  applyPreset('candidate');
}

function applyHandoff(h) {
  if (h && h.kind === 'protocol') return applyProtocolHandoff(h);
  if (!h || !h.parameter_id) return;
  const box = $('#handoff');
  box.hidden = false;
  box.textContent = '';
  const t = el('div');
  t.style.cssText = 'font-weight:600;color:var(--ink)';
  t.textContent = `Carried from a discovery run: ${h.label || h.parameter_id} `
    + `${h.control != null ? h.control : '?'} → ${h.candidate != null ? h.candidate : '?'}`
    + `${h.unit ? ' ' + h.unit : ''}`;
  box.append(t);
  if (h.reason) box.append(el('div', null, h.reason));
  if (h.confidence) box.append(el('div', 'dim', 'confidence ' + h.confidence));
  box.append(el('div', 'dim',
    'Control and candidate are loaded below. Nothing was approved by loading it.'));
  /* The run proposed this for a project; honour that rather than applying a
     setpoint to whichever process happened to be selected. */
  if (h.project_id && state.projects) {
    const sel = $('#project');
    if (Array.from(sel.options).some(o => o.value === h.project_id)) {
      sel.value = h.project_id;
      showProject(h.project_id);
    }
  }
  state.handoff = h;
  const p = state.project;
  if (!p) return;
  const q = (p.parameters || []).find(x => x.parameter_id === h.parameter_id);
  if (!q || !q.simulator_mapping) {
    box.append(el('div', 'warnc',
      'This project\u2019s model has no term for that parameter, so the sliders below cannot '
      + 'represent it and no prediction is produced for it.'));
    return;
  }
  (state.candidates = state.candidates || {});
  state.candidates[p.project_id] = [{
    parameter_id: h.parameter_id, value: h.candidate,
    label: h.label || h.parameter_id, from: h.from_run || 'discovery run',
  }];
  applyPreset('candidate');
}

/* A condition the person built by hand, offered back to the loop as a candidate.
   Recorded as a design choice with that origin: a setpoint somebody liked in a
   sandbox is not evidence, and it is certainly not an approved protocol. */
function useAsCandidate() {
  const c = condition();
  const payload = {
    origin: 'user_design_choice',
    created_at: new Date().toISOString(),
    project_id: state.project ? state.project.project_id : null,
    setpoints: c.setpoints,
    challenge: c.challenge,
    note: 'Chosen by a person in the simulator sandbox. It is a design choice, not evidence, '
          + 'and it is not an approved protocol: a wet-lab run still needs a named human '
          + 'approver.',
  };
  try { localStorage.setItem('bs-user-candidate', JSON.stringify(payload)); } catch (_) {}
  const box = $('#handoff');
  box.hidden = false; box.textContent = '';
  const t = el('div'); t.style.cssText = 'font-weight:600;color:var(--ink)';
  t.textContent = 'Saved as a candidate — origin: user design choice';
  box.append(t, el('div', null, payload.note));
  const go = el('a', 'btn', 'Take it to AI discovery');
  go.href = 'console.html';
  box.append(go);
}

function noteFor(id) {
  const c = state.cfg.challenges.find(c => c.id === id);
  $('#chNote').textContent = c ? c.note : '';
}

function knobRow(k) {
  const w = el('div', 'knob');
  const row = el('div', 'row');
  row.append(el('span', 'nm', k.label));
  const val = el('span', 'val'); val.textContent = (+k.default).toString();
  const un = el('span', 'un', k.unit);
  const right = el('span'); right.append(val, document.createTextNode(' '), un);
  row.append(right);
  const sl = Object.assign(document.createElement('input'), {
    type: 'range', min: k.min, max: k.max, step: k.step, value: k.default,
  });
  sl.dataset.knob = k.id; sl.dataset.default = k.default;
  sl.setAttribute('aria-label', k.label + ' in ' + k.unit);
  sl.addEventListener('input', () => { val.textContent = (+sl.value).toString(); });
  sl.addEventListener('change', () => { markCustom(); run(); });
  w.append(row, sl);
  if (k.note) {
    w.append(el('p', 'note', k.note));
    row.style.cursor = 'pointer';
    row.addEventListener('click', () => w.classList.toggle('open'));
  }
  return w;
}

function condition() {
  const setpoints = {}, stage_days = {};
  document.querySelectorAll('input[type=range][data-knob]').forEach(i => {
    const id = i.dataset.knob;
    if (id.startsWith('day_')) stage_days[id.slice(4)] = +i.value;
    else setpoints[id] = +i.value;
  });
  return { setpoints, stage_days, challenge: $('#challenge').value, seed: state.seed };
}

/* ── wild type against an engineered line ─────────────────── */
async function genotypeCompare() {
  const b = $('#genoBtn'); b.disabled = true; b.textContent = 'running both…';
  const kind = $('#genoKind').value;
  const label = ($('#genoLabel').value || '').trim() || (kind === 'factor' ? 'added factor' : 'edited line');
  const body = Object.assign(condition(), { genotype: { label, kind,
    stage: kind === 'factor' ? $('#genoStage').value : null,
    growth_ratio: +$('#genoGrowth').value, diff_ratio: +$('#genoDiff').value } });
  try {
    const r = await fetch('/api/sim/genotype', { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || `the server answered ${r.status}`);
    const names = [d.control_label || 'wild type',
      d.genotype.kind === 'factor' ? 'with ' + d.genotype.label : d.genotype.label];
    $('#genoVerdict').textContent = 'ASSUMED EFFECT · ' + d.verdict;
    BSCurves.render($('#genoCurves'), d.curves, { aLabel: names[0], bLabel: names[1] });
    BSCurves.table($('#genoTab'), d.deltas, names);
    $('#genoNote').textContent = d.note;
  } catch (e) {
    $('#genoVerdict').textContent = e.message;
  } finally { b.disabled = false; b.textContent = 'Compare against the control'; }
}

/* ── run one condition ───────────────────────────────────── */
let pending = null;
async function run() {
  clearFail();
  const body = condition();
  if (state.inflight) { pending = body; return; }
  state.inflight++; status('running', 'run');
  try {
    const r = await fetch('/api/sim/run', { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || 'the simulator refused this condition');
    state.result = j;
    state.day = Math.min(state.day, j.frames.length - 1);
    bands(); draw(); outcome();
    status(j.frames.length + ' readings', 'ok');
  } catch (e) { fail(e.message); status('refused', 'bad'); }
  finally {
    state.inflight--;
    if (pending) { const p = pending; pending = null; if (JSON.stringify(p) !== JSON.stringify(body)) run(); }
  }
}

/* ── timeline ────────────────────────────────────────────── */
const STAGE_TINT = { expansion: 'var(--s1)', mesoderm: 'var(--s3)', hemato: 'var(--sage)',
  myeloid: 'var(--brand)' };

function bands() {
  const f = state.result.frames, host = $('#bands');
  host.textContent = '';
  const counts = {};
  f.forEach(x => counts[x.stage] = (counts[x.stage] || 0) + 1);
  Object.keys(counts).forEach(s => {
    const i = el('i'); i.style.flex = counts[s];
    i.style.background = 'color-mix(in srgb,' + (STAGE_TINT[s] || 'var(--ink-3)') + ' 60%,transparent)';
    i.title = s + ': ' + counts[s] + ' days';
    host.append(i);
  });
  const sc = $('#scrub');
  sc.max = f.length - 1; sc.value = state.day;
  $('#tlEnd').textContent = 'day ' + num(f[f.length - 1].day, 0);
  $('#tlNote').textContent = 'scrub the day · ' + state.result.total_days
    + ' days total · seed ' + state.result.seed;
}

function play() {
  if (state.playing) return stop();
  $('#playBtn').textContent = '❚❚ pause';
  state.playing = setInterval(() => {
    const n = state.result.frames.length;
    state.day = (state.day + 1) % n;
    $('#scrub').value = state.day; draw();
  }, 340);
}
function stop() {
  if (state.playing) clearInterval(state.playing);
  state.playing = null; $('#playBtn').textContent = '▶ play';
}

/* ── the vessel ──────────────────────────────────────────── */
const STATE_COL = { good: 'var(--ok)', strained: 'var(--warn)', failing: 'var(--bad)' };

function draw() {
  const f = state.result.frames[state.day];
  if (!f) return;
  $('#dayLbl').textContent = 'day ' + num(f.day, 1);
  $('#stgLbl').textContent = f.stage.replace('_', ' ');
  drawSide(f); drawTop(f); instruments(f);
}

/* How many aggregates to draw, and how big. Both are read off the imaging
   channels; the cap is there because 40 circles is already a field and more
   only costs frame time. */
function field(f) {
  const n = Math.max(3, Math.min(44, Math.round(6 * Math.log10(Math.max(f.agg_count_per_ml, 10)))));
  const d = f.agg_diameter_mean_um || 60;
  const r = Math.max(3, Math.min(26, d / 18));      // um -> px, one scale everywhere
  return { n, r, over: f.agg_frac_over_300um || 0, sd: (f.agg_diameter_sd_um || 0) / 18 };
}

function drawSide(f) {
  const { n, r, over, sd } = field(f);
  const st = f.read.state, col = STATE_COL[st];
  // liquid tint: the condition score drives it, so a culture going wrong reads
  // at a glance before any number is parsed
  const good = f.read.score / 100;
  const s = [];
  s.push(`<defs>
    <linearGradient id="liq" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="${col}" stop-opacity="${0.1 + 0.1 * (1 - good)}"/>
      <stop offset="100%" stop-color="${col}" stop-opacity="${0.22 + 0.2 * (1 - good)}"/>
    </linearGradient>
    <clipPath id="inside"><path d="M96 86 h248 v232 a40 40 0 0 1 -40 40 h-168 a40 40 0 0 1 -40 -40 z"/></clipPath>
  </defs>`);
  // vessel body
  s.push(`<path d="M96 86 h248 v232 a40 40 0 0 1 -40 40 h-168 a40 40 0 0 1 -40 -40 z"
    fill="var(--glass-2)" stroke="var(--line)" stroke-width="2"/>`);
  // headplate
  s.push(`<rect x="86" y="68" width="268" height="20" rx="7" fill="var(--glass-2)"
    stroke="var(--line)" stroke-width="2"/>`);
  // liquid
  s.push(`<g clip-path="url(#inside)">`);
  s.push(`<rect x="96" y="128" width="248" height="232" fill="url(#liq)"/>`);
  // aggregates
  for (let i = 0; i < n; i++) {
    const x = 110 + hash(i * 3) * 220;
    const y = 146 + hash(i * 3 + 1) * 196;
    const rr = Math.max(2.5, r + (hash(i * 3 + 2) - 0.5) * 2 * sd);
    const big = hash(i * 7 + 5) < over;              // this one is over 300 um
    s.push(`<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${rr.toFixed(1)}"
      fill="color-mix(in srgb, var(--brand) ${(38 + 34 * good).toFixed(0)}%, transparent)"
      stroke="color-mix(in srgb, var(--brand) 70%, transparent)" stroke-width="1"/>`);
    if (big) {
      // hypoxic core: drawn only where agg_frac_over_300um says cores exist
      s.push(`<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${(rr * 0.52).toFixed(1)}"
        fill="color-mix(in srgb, var(--ink) 58%, transparent)"/>`);
    }
  }
  // debris, scaled by LDH: released intracellular enzyme, so lysis
  const debris = Math.min(34, Math.max(0, Math.round((f.ldh_u_per_l - 140) / 26)));
  for (let i = 0; i < debris; i++) {
    s.push(`<circle cx="${(104 + hash(i * 11 + 2) * 232).toFixed(1)}"
      cy="${(140 + hash(i * 11 + 3) * 206).toFixed(1)}" r="1.7"
      fill="var(--bad)" opacity=".55"/>`);
  }
  s.push(`</g>`);
  // impeller shaft and blades
  s.push(`<rect x="216" y="60" width="8" height="250" rx="3" fill="var(--ink-3)" opacity=".6"/>`);
  const spin = (state.day * 41) % 360;
  s.push(`<g transform="translate(220 306) rotate(${spin})">
    <ellipse cx="0" cy="0" rx="48" ry="9" fill="var(--ink-3)" opacity=".55"/>
    <ellipse cx="0" cy="0" rx="9" ry="7" fill="var(--ink-3)" opacity=".8"/></g>`);
  s.push(`<text x="220" y="392" text-anchor="middle" font-size="11.5"
    font-family="var(--mono)" fill="var(--ink-3)">${num(f.agitation_rpm, 0)} rpm · ${
      num(f.oxygen_uptake_rate, 2)} mmol/L/h OUR</text>`);
  // sparge
  for (let i = 0; i < 7; i++) {
    s.push(`<circle cx="${(176 + i * 15)}" cy="${(344 - ((state.day * 7 + i * 13) % 60) * 2.4).toFixed(1)}"
      r="2.4" fill="var(--s1)" opacity=".4"/>`);
  }
  // probes, labelled: this is the instrumented part, so name it
  const probes = [['pH', f.ph, 3], ['DO', f.do_measured, 2], ['capacitance', f.capacitance_pf_cm, 1]];
  probes.forEach((p, i) => {
    const y = 136 + i * 30;
    s.push(`<line x1="96" y1="${y}" x2="74" y2="${y}" stroke="var(--ink-3)" stroke-width="2"/>`);
    s.push(`<circle cx="96" cy="${y}" r="3.2" fill="var(--ink-3)"/>`);
    s.push(`<text x="68" y="${y - 3}" text-anchor="end" font-size="10"
      font-family="var(--mono)" fill="var(--ink-3)">${p[0]}</text>`);
    s.push(`<text x="68" y="${y + 11}" text-anchor="end" font-size="12.5"
      font-family="var(--mono)" font-weight="600" fill="var(--ink-2)">${num(p[1], p[2])}</text>`);
  });
  // harvest stream — only drawn when the harvest channel is actually releasing
  const rel = f.harvest_cells_e6_per_ml_day || 0;
  s.push(`<path d="M344 300 h44 v64" fill="none" stroke="var(--line)" stroke-width="2"/>`);
  if (rel > 0.001) {
    const k = Math.min(10, Math.max(1, Math.round(rel * 9)));
    for (let i = 0; i < k; i++) {
      s.push(`<circle cx="388" cy="${(312 + ((state.day * 9 + i * 17) % 52)).toFixed(1)}"
        r="2.6" fill="var(--brand)" opacity=".8"/>`);
    }
    s.push(`<text x="400" y="384" font-size="10.5" font-family="var(--mono)"
      fill="var(--brand)" text-anchor="end">harvest</text>`);
  } else {
    s.push(`<text x="400" y="384" font-size="10.5" font-family="var(--mono)"
      fill="var(--ink-3)" text-anchor="end">no release</text>`);
  }
  s.push(`<text x="220" y="412" text-anchor="middle" font-size="12"
    font-family="var(--mono)" font-weight="600"
    fill="${col}">${st} · ${num(f.read.score, 0)}/100</text>`);
  $('#side').innerHTML = s.join('');
}

function drawTop(f) {
  const { n, r, over, sd } = field(f);
  const col = STATE_COL[f.read.state];
  const s = [];
  s.push(`<defs><clipPath id="bowl"><circle cx="140" cy="210" r="116"/></clipPath></defs>`);
  s.push(`<circle cx="140" cy="210" r="124" fill="var(--glass-2)" stroke="var(--line)" stroke-width="2"/>`);
  s.push(`<circle cx="140" cy="210" r="116" fill="color-mix(in srgb,${col} 14%,transparent)"/>`);
  s.push(`<g clip-path="url(#bowl)">`);
  for (let i = 0; i < n; i++) {
    const a = hash(i * 5) * Math.PI * 2, rad = Math.sqrt(hash(i * 5 + 1)) * 104;
    const x = 140 + Math.cos(a) * rad, y = 210 + Math.sin(a) * rad;
    const rr = Math.max(2.5, r * 0.85 + (hash(i * 5 + 2) - 0.5) * 2 * sd);
    s.push(`<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${rr.toFixed(1)}"
      fill="color-mix(in srgb, var(--brand) 46%, transparent)"
      stroke="color-mix(in srgb, var(--brand) 70%, transparent)" stroke-width="1"/>`);
    if (hash(i * 9 + 4) < over) {
      s.push(`<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${(rr * 0.52).toFixed(1)}"
        fill="color-mix(in srgb, var(--ink) 58%, transparent)"/>`);
    }
  }
  s.push(`</g>`);
  // impeller, three blades, turning with the day so the view reads as live
  const spin = (state.day * 41) % 360;
  s.push(`<g transform="translate(140 210) rotate(${spin})">`);
  for (let b = 0; b < 3; b++) {
    s.push(`<g transform="rotate(${b * 120})"><rect x="0" y="-7" width="74" height="14" rx="6"
      fill="var(--ink-3)" opacity=".5"/></g>`);
  }
  s.push(`<circle r="11" fill="var(--ink-3)" opacity=".8"/></g>`);
  s.push(`<text x="140" y="58" text-anchor="middle" font-size="11"
    font-family="var(--mono)" fill="var(--ink-3)">TOP VIEW</text>`);
  s.push(`<text x="140" y="356" text-anchor="middle" font-size="12"
    font-family="var(--mono)" fill="var(--ink-2)">${num(f.agg_diameter_mean_um, 0)} µm mean · ${
      num(over * 100, 0)}% over 300 µm</text>`);
  s.push(`<text x="140" y="374" text-anchor="middle" font-size="11.5"
    font-family="var(--mono)" fill="var(--ink-3)">${num(f.agg_count_per_ml, 0)} aggregates/mL</text>`);
  $('#top').innerHTML = s.join('');
}

/* ── instruments panel ───────────────────────────────────── */
const CHANS = [
  ['vcd_e6_per_ml', 'Viable cell density', '1e6/mL', 2],
  ['viability_pct', 'Viability', '%', 1],
  ['glucose_mM', 'Glucose', 'mM', 1],
  ['lactate_mM', 'Lactate', 'mM', 1],
  ['ammonia_mM', 'Ammonia', 'mM', 2],
  ['ldh_u_per_l', 'LDH', 'U/L', 0],
  ['oxygen_uptake_rate', 'Oxygen uptake', 'mmol/L/h', 2],
  ['harvest_cum_e6_per_ml', 'Harvested, cumulative', '1e6/mL', 2],
  ['imp_frac_monocyte_cluster', 'In monocyte cluster', 'fraction', 3],
  ['cd14_pct', 'CD14 (cytometer)', '%', 1],
];

function instruments(f) {
  const box = $('#stateBox');
  box.className = 'state ' + f.read.state;
  $('#stateTxt').textContent = f.read.state;
  $('#stateSub').textContent = 'condition score ' + num(f.read.score, 0) + ' / 100';

  const host = $('#chans'); host.textContent = '';
  const flagged = {}; f.read.flags.forEach(x => flagged[x.channel] = x.severity);
  CHANS.forEach(([k, label, unit, nd]) => {
    if (f[k] === undefined || f[k] === null) return;
    const row = el('div', 'chan' + (flagged[k] ? ' ' + flagged[k] : ''));
    row.append(el('span', 'k', label));
    row.append(sparkline(k));
    row.append(el('span', 'v', num(f[k], nd) + ' ' + unit));
    host.append(row);
  });

  const fl = $('#flagList'); fl.textContent = '';
  if (!f.read.flags.length) fl.append(el('li', 'ok', 'No channel is outside its threshold.'));
  f.read.flags.forEach(x => fl.append(el('li', x.severity, x.detail)));
}

/* a 72x20 sparkline over the whole run, with the current day marked. Built as
   SVG rather than canvas so it survives a print to PDF. */
function sparkline(key) {
  const f = state.result.frames, vals = f.map(x => x[key]).filter(v => v !== null && v !== undefined);
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('class', 'spark'); svg.setAttribute('viewBox', '0 0 72 20');
  if (vals.length < 2) return svg;
  const lo = Math.min(...vals), hi = Math.max(...vals), span = (hi - lo) || 1;
  const pts = f.map((x, i) => {
    const v = x[key]; if (v === null || v === undefined) return null;
    return [(i / (f.length - 1)) * 70 + 1, 18 - ((v - lo) / span) * 16];
  }).filter(Boolean);
  const path = document.createElementNS(svg.namespaceURI, 'polyline');
  path.setAttribute('points', pts.map(p => p[0].toFixed(1) + ',' + p[1].toFixed(1)).join(' '));
  path.setAttribute('fill', 'none'); path.setAttribute('stroke', 'var(--s1)');
  path.setAttribute('stroke-width', '1.4');
  svg.append(path);
  const cur = pts[Math.min(state.day, pts.length - 1)];
  const dot = document.createElementNS(svg.namespaceURI, 'circle');
  dot.setAttribute('cx', cur[0]); dot.setAttribute('cy', cur[1]); dot.setAttribute('r', '2.2');
  dot.setAttribute('fill', 'var(--brand)');
  svg.append(dot);
  return svg;
}

/* ── outcome ─────────────────────────────────────────────── */
const OUT = [
  ['harvest_per_input_ipsc', 'monocytes per input iPSC', 2],
  ['harvest_total_e6_per_ml', 'harvested 1e6/mL', 2],
  ['cumulative_differentiation_efficiency', 'cumulative differentiation efficiency', 4],
  ['final_viability_pct', 'final viability %', 1],
  ['peak_vcd_e6_per_ml', 'peak VCD 1e6/mL', 2],
  ['mean_score', 'mean condition score', 0],
];

function outcome() {
  const sig = state.result.signature, host = $('#outNums');
  host.textContent = '';
  OUT.forEach(([k, label, nd]) => {
    const w = el('div', 'num');
    w.append(el('div', 'n', num(sig[k], nd)), el('div', 'k', label));
    host.append(w);
  });
  const d = state.result;
  $('#outNote').textContent = d.note + (d.clamped.length
    ? ' ' + d.clamped.length + ' value(s) were clamped to the model\'s valid range.' : '')
    + ' Days strained: ' + sig.days_strained + '; failing: ' + sig.days_failing + '.';
}

/* ── A/B ─────────────────────────────────────────────────── */
function hold(slot) {
  if (!state.result) return;
  const c = condition(); c.label = slot;
  state[slot] = c;
  $('#slot' + slot).textContent = slot + ' held ✓';
  $('#cmpBtn').disabled = !(state.A && state.B);
}

async function compare() {
  clearFail(); status('comparing', 'run');
  try {
    const r = await fetch('/api/sim/compare', { method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ conditions: [state.A, state.B] }) });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || 'the comparison was refused');
    renderCompare(j); status('compared', 'ok');
  } catch (e) { fail(e.message); status('refused', 'bad'); }
}

function renderCompare(j) {
  $('#cmpCard').classList.remove('hide');
  const slots = $('#cmpSlots'); slots.textContent = '';
  [['a', j.a], ['b', j.b]].forEach(([k, d]) => {
    const w = el('div', 'slot ' + k);
    w.append(el('div', 'ttl', k.toUpperCase() + ' · ' + d.challenge_label));
    j.changed.forEach(c => {
      const row = el('div', 'chan');
      row.append(el('span', 'k', c.label));
      row.append(el('span', 'v', num(d.setpoints[c.knob], 2) + ' ' + c.unit));
      w.append(row);
    });
    if (!j.changed.length) w.append(el('p', 'caveat', 'No setpoint differs.'));
    slots.append(w);
  });
  const t = $('#cmpTab');
  t.innerHTML = '<thead><tr><th>Outcome</th><th>A</th><th>B</th><th>Δ</th><th>%</th></tr></thead>';
  const tb = document.createElement('tbody');
  Object.entries(j.deltas).forEach(([k, v]) => {
    const tr = document.createElement('tr');
    const name = (OUT.find(o => o[0] === k) || [, k.replace(/_/g, ' ')])[1];
    tr.append(el('td', null, name));
    [v.a, v.b, v.delta].forEach(x => tr.append(el('td', 'n', num(x, 3))));
    const pct = el('td', 'n', v.pct === null ? '—' : num(v.pct, 1) + '%');
    if (v.pct === null && v.pct_withheld) pct.title = v.pct_withheld;
    tr.append(pct);
    tb.append(tr);
  });
  t.append(tb);
  $('#cmpVerdict').textContent = j.verdict;
  $('#cmpNote').textContent = j.note;
  $('#cmpCard').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

/* ── carry into a loop run ───────────────────────────────── */
let brief = null;
async function carry() {
  clearFail();
  try {
    const r = await fetch('/api/sim/brief', { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(condition()) });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || 'refused');
    brief = j;
    $('#briefCard').classList.remove('hide');
    $('#briefCaveat').textContent = j.caveat;
    $('#briefPrompt').textContent = j.prompt;
    try { localStorage.setItem('bs-sim-prompt', j.prompt); } catch (_) {}
    $('#briefCard').scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (e) { fail(e.message); }
}

$('#copyBtn').addEventListener('click', async () => {
  if (!brief) return;
  try { await navigator.clipboard.writeText(brief.prompt); $('#copyBtn').textContent = 'copied ✓'; }
  catch (_) { fail('the browser refused clipboard access; select the text instead'); }
});
$('#dlBrief').addEventListener('click', () => brief && download(
  'biosense-sim-brief.json', brief));

/* ── export the session ──────────────────────────────────── */
function exportSession() {
  if (!state.result) return;
  download('biosense-sim-session.json', {
    exported_at: new Date().toISOString(),
    what_this_is: 'A simulator-mode session against the BioSense synthetic iPSC -> '
      + 'monocyte stand-in. Not a measurement, and not evidence for any protocol value.',
    evidence_status: state.result.evidence_status,
    bioreactor_source: state.result.bioreactor_source,
    thresholds_used_for_the_condition_read: state.cfg.limits,
    not_modelled: state.cfg.not_modelled,
    current: state.result,
    held_a: state.A, held_b: state.B,
  });
}

function download(name, obj) {
  const b = new Blob([JSON.stringify(obj, null, 2)], { type: 'application/json' });
  const a = Object.assign(document.createElement('a'),
    { href: URL.createObjectURL(b), download: name });
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 4000);
}

boot();
