'use strict';
/* The discovery workflow: pick a project, state an objective, choose a runtime,
   watch it work, read what it found.
 *
 * Two rules this file exists to hold, and they are the reason it is long.
 *
 * 1. Nothing is rendered from prose. Every card is built from a structured
 *    payload the backend validated against a contract. If a field is absent the
 *    card says so; it never fills a gap with a plausible sentence.
 * 2. Every badge can explain itself. The glossary is fetched once and attached
 *    to each badge as a popover, because a reader who does not know whether a
 *    number is MEASURED or SIMULATED is reading decoration, and nobody goes to
 *    a repository to find out.
 */

const $ = s => document.querySelector(s);
const $$ = s => Array.from(document.querySelectorAll(s));
const el = (t, c, x) => { const n = document.createElement(t); if (c) n.className = c;
  if (x != null) n.textContent = x; return n; };
const num = (v, nd = 3) => (typeof v === 'number' && isFinite(v))
  ? (Math.abs(v) >= 1e6 ? v.toExponential(2) : (+v.toFixed(nd)).toString()) : '—';

const state = {
  glossary: null, runtime: null, projects: [], project: null, datasets: [],
  run: null, es: null, result: null, benchmark: null,
  candidates: {}, projectParams: [],
};

/* ── glossary popover ───────────────────────────────────────────────────
   One element, moved and refilled, rather than one per badge: a page with
   forty hypotheses would otherwise carry forty hidden panels. */
let glossEl = null;
function glossNode() {
  if (!glossEl) { glossEl = el('div', 'gloss'); glossEl.setAttribute('role', 'tooltip');
    document.body.append(glossEl); }
  return glossEl;
}
function showGloss(target, group, term) {
  const d = state.glossary && state.glossary.groups[group] &&
    state.glossary.groups[group].terms[term];
  if (!d) return;
  const g = glossNode();
  g.textContent = '';
  g.append(Object.assign(el('h5', null, state.glossary.groups[group].title), {}),
    el('p', 's', d.short || d.label || term));
  if (d.long) g.append(el('p', 'l', d.long));
  g.classList.add('on');
  const r = target.getBoundingClientRect(), gr = g.getBoundingClientRect();
  let left = r.left, top = r.bottom + 8;
  if (left + gr.width > innerWidth - 12) left = Math.max(12, innerWidth - gr.width - 12);
  if (top + gr.height > innerHeight - 12) top = Math.max(12, r.top - gr.height - 8);
  g.style.left = left + 'px'; g.style.top = top + 'px';
}
function hideGloss() { if (glossEl) glossEl.classList.remove('on'); }

/* A badge that knows what it means. Falls back to a plain span when the
   glossary has no entry, rather than offering an explanation that is not there. */
function term(group, key, text, cls) {
  const label = (state.glossary && state.glossary.groups[group] &&
    state.glossary.groups[group].terms[key] &&
    state.glossary.groups[group].terms[key].label) || key;
  const known = !!(state.glossary && state.glossary.groups[group] &&
    state.glossary.groups[group].terms[key]);
  if (!known) return el('span', cls || '', text || label);
  const b = el('button', (cls || 'term-plain') + ' term', text || label);
  b.type = 'button';
  b.setAttribute('aria-label', `${text || label}: what this means`);
  const show = () => showGloss(b, group, key);
  b.addEventListener('mouseenter', show);
  b.addEventListener('focus', show);
  b.addEventListener('click', e => { e.preventDefault(); show(); });
  b.addEventListener('mouseleave', hideGloss);
  b.addEventListener('blur', hideGloss);
  return b;
}
document.addEventListener('keydown', e => { if (e.key === 'Escape') hideGloss(); });
addEventListener('scroll', hideGloss, { passive: true });

/* ── boot ───────────────────────────────────────────────────────────────── */
async function get(path) {
  const r = await fetch(path);
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw Object.assign(new Error(d.error || `${path} answered ${r.status}`), d);
  return d;
}
async function post(path, body) {
  const r = await fetch(path, { method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}) });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw Object.assign(new Error(d.error || `${path} answered ${r.status}`), d);
  return d;
}

async function boot() {
  try { state.glossary = await get('/api/glossary'); } catch (_) { /* badges degrade */ }
  await Promise.all([loadRuntime(), loadProjects(), loadDatasets(), loadBenchmarks()]);
  wire();
  renderStages(null);
}

async function loadRuntime() {
  try { state.runtime = await get('/api/runtime?probe=1'); }
  catch (_) { return; }
  renderRuntimePicker();
  renderIdentity();
}

function renderIdentity() {
  const box = $('#identity'); if (!box) return;
  const id = state.runtime && state.runtime.identity;
  box.textContent = '';
  if (!id) return;
  if (id.authenticated) {
    box.append(el('span', 'mono dim', id.display), ' ');
    const out = el('button', 'btn', 'Sign out');
    out.addEventListener('click', async () => {
      await post('/api/auth/logout'); location.reload();
    });
    box.append(out);
  } else {
    const inBtn = el('button', 'btn', 'Sign in');
    inBtn.addEventListener('click', () => $('#signinCard').hidden = !$('#signinCard').hidden);
    box.append(inBtn);
  }
  const note = $('#identityNote');
  if (note) note.textContent = id.note || '';
}

function renderRuntimePicker() {
  const host = $('#runtimes'); if (!host || !state.runtime) return;
  host.textContent = '';
  const avail = state.runtime.availability || {};
  state.runtime.modes.forEach(m => {
    const a = avail[m.mode] || {};
    const usable = m.offered && (a.ok !== false);
    const lab = el('label', 'rt-opt' + (usable ? '' : ' off'));
    const input = el('input');
    input.type = 'radio'; input.name = 'runtime'; input.value = m.mode;
    input.disabled = !usable;
    if (m.mode === state.runtime.default_mode && usable) input.checked = true;
    lab.append(input, el('div', 't', m.label), el('div', 'b', m.blurb));
    if (!usable) {
      const why = el('div', 'why', a.headline || 'Not offered by this deployment.');
      if (a.next_step) { why.append(document.createElement('br'));
        why.append(el('code', null, a.next_step)); }
      lab.append(why);
    }
    lab.addEventListener('change', updateBadge);
    host.append(lab);
  });
  if (!host.querySelector('input:checked')) {
    const first = host.querySelector('input:not(:disabled)');
    if (first) first.checked = true;
  }
  updateBadge();
}

function chosenRuntime() {
  const r = $('#runtimes input:checked');
  return r ? r.value : (state.runtime && state.runtime.default_mode) || 'synthetic_demo';
}

function updateBadge() {
  const mode = chosenRuntime();
  const host = $('#runtimeBadge'); if (!host) return;
  host.textContent = '';
  host.className = 'rt-badge ' + mode;
  host.append(el('span', 'dot'));
  const t = term('runtime_mode', mode);
  host.append(t);
}

async function loadProjects() {
  let d;
  try { d = await get('/api/workspace/projects'); } catch (_) { return; }
  /* Your own projects first, then templates; a project with a model ahead of one
     without. The default matters: landing on an unrelated project produces a run
     that refuses everything for the right reason and reads like a fault. */
  state.projects = d.projects.slice().sort((a, b) =>
    (a.source === 'workspace' ? 0 : 1) - (b.source === 'workspace' ? 0 : 1)
    || (b.has_simulator ? 1 : 0) - (a.has_simulator ? 1 : 0)
    || a.name.localeCompare(b.name));
  const sel = $('#project'); sel.textContent = '';
  state.projects.forEach(p => {
    const o = el('option', null, p.name + (p.source === 'template' ? '  (template)' : ''));
    o.value = p.project_id; sel.append(o);
  });
  sel.addEventListener('change', showProject);
  showProject();
}

async function showProject() {
  const id = $('#project').value;
  const row = state.projects.find(p => p.project_id === id);
  const note = $('#projectNote');
  if (!row) { note.textContent = ''; return; }
  note.textContent = row.has_simulator
    ? 'This project has a mechanistic model, so candidate parameters it covers get a prediction.'
    : 'This project has no mechanistic model. Parameters are real design variables; nothing '
      + 'here predicts what they would do, and no other project’s model is borrowed.';

  state.candidates = {};
  renderCandidatePicks();
  loadProjectParameters(id);
}

/* The parameters this project actually has. Read per project rather than from
   one global list, because offering a CAR-T process an M-CSF field because
   macrophages have one implies the model knows something about it. */
async function loadProjectParameters(id) {
  let d;
  try { d = await get('/api/projects'); } catch (_) { return; }
  const p = (d.projects || []).find(x => x.project_id === id);
  state.projectParams = p ? p.parameters : [];
  const sel = $('#candParam'); if (!sel) return;
  sel.textContent = '';
  (state.projectParams || []).forEach(q => {
    const o = el('option', null,
      `${q.label}${q.unit ? ' (' + q.unit + ')' : ''}`
      + (q.simulator_coverage === 'modelled' ? '' : '  \u2014 not modelled'));
    o.value = q.parameter_id; sel.append(o);
  });
}

function renderCandidatePicks() {
  const host = $('#candList'); if (!host) return;
  host.textContent = '';
  Object.entries(state.candidates || {}).forEach(([pid, v]) => {
    const q = (state.projectParams || []).find(x => x.parameter_id === pid) || {};
    const chip = el('span', 'pick');
    chip.append(el('span', null, `${q.label || pid} = ${v}${q.unit ? ' ' + q.unit : ''}`));
    if (q.simulator_coverage && q.simulator_coverage !== 'modelled') {
      chip.append(el('span', 'vis', 'not modelled'));
    }
    const x = el('button', 'btn', '\u00d7');
    x.type = 'button';
    x.addEventListener('click', () => { delete state.candidates[pid]; renderCandidatePicks(); });
    chip.append(x);
    host.append(chip);
  });
}

async function loadDatasets() {
  let d;
  try { d = await get('/api/datasets'); } catch (_) { return; }
  state.datasets = d.datasets || [];
  const host = $('#datasetPicks'); host.textContent = '';
  if (!state.datasets.length) {
    host.append(el('p', 'hint', d.note || 'No datasets are registered on this instance.'));
    return;
  }
  state.datasets.forEach(m => {
    const lab = el('label', 'pick');
    const i = el('input'); i.type = 'checkbox'; i.value = m.dataset_id;
    lab.append(i, el('span', null, m.title || m.dataset_id));
    if (m.visibility === 'private') lab.append(el('span', 'vis', 'private'));
    host.append(lab);
  });
  $('#datasetNote').textContent = d.note || '';
}

async function loadBenchmarks() {
  let d;
  try { d = await get('/api/benchmarks'); } catch (_) { return; }
  const host = $('#benchList'); if (!host) return;
  host.textContent = '';
  d.built.forEach(b => {
    const w = el('div', 'ds');
    const t = el('div', 't', b.title || b.benchmark_id); w.append(t);
    w.append(el('div', 'm', `${b.project} · ${b.passed} passed, ${b.failed} failed · ${b.privacy}`));
    const open = el('button', 'btn', 'Open');
    open.addEventListener('click', () => openBenchmark(b.benchmark_id));
    w.append(open);
    host.append(w);
  });
  const runHost = $('#benchRun'); runHost.textContent = '';
  d.configs.forEach(c => {
    const w = el('div', 'ds');
    w.append(el('div', 't', c.title), el('div', 'm', c.objective));
    const btn = el('button', 'btn go', 'Run this benchmark');
    btn.addEventListener('click', () => runBenchmark(c.benchmark_id, btn));
    w.append(btn);
    host.parentElement.querySelector('#benchRun').append(w);
  });
  $('#benchNote').textContent = d.note || '';
}

async function openBenchmark(id) {
  try {
    const b = await get('/api/benchmarks/' + encodeURIComponent(id));
    renderBenchmark(b);
  } catch (e) { fail(e); }
}

async function runBenchmark(id, btn) {
  btn.disabled = true;
  try {
    const run = await post('/api/benchmarks/run',
      { benchmark_id: id, runtime_mode: chosenRuntime() });
    startWatching(run);
  } catch (e) { fail(e); } finally { btn.disabled = false; }
}

/* ── the request ─────────────────────────────────────────────────────────
   Built field by field. Deliberately not one text blob: the ResearchContext
   carries a strictness setting and the fields a mismatch can be in, and
   flattening it into prose would throw all of that away. */
function listFrom(id) {
  return ($(id).value || '').split(',').map(s => s.trim()).filter(Boolean);
}

function buildRequest() {
  const ctxFields = {
    species: listFrom('#ctxSpecies'), cell_types: listFrom('#ctxCellTypes'),
    states: listFrom('#ctxStates'), tissues: listFrom('#ctxTissues'),
    disease_context: listFrom('#ctxDisease'),
  };
  const anyCtx = Object.values(ctxFields).some(v => v.length);
  const body = {
    project_id: $('#project').value,
    objective: $('#objective').value.trim(),
    runtime_mode: chosenRuntime(),
    dataset_ids: $$('#datasetPicks input:checked').map(i => i.value),
  };
  if (anyCtx) {
    body.research_context = Object.assign({
      schema_version: '2.0', strictness: $('#ctxStrictness').value,
      context_id: null, created_at: new Date().toISOString(),
      modalities: [], assays: [], therapy_context: [], exclude: [],
      publication_from_year: null, publication_to_year: null, time_range: null,
      notes: null, applied: null,
    }, ctxFields);
  }
  const constraints = ($('#constraints').value || '').trim();
  if (constraints) body.process_constraints = { notes: constraints, parameter_bounds: [] };
  if (Object.keys(state.candidates || {}).length) body.candidate_values = state.candidates;
  return body;
}

/* ── running ─────────────────────────────────────────────────────────────── */
function fail(e) {
  const box = $('#runState');
  box.hidden = false; box.textContent = '';
  const w = el('div', 'state');
  w.append(el('h4', null, e.reason === 'not_configured' ? 'Runtime not offered'
    : e.headline ? 'Real AI runtime unavailable' : 'Refused'));
  w.append(el('p', null, e.headline || e.message || String(e)));
  if (e.next_step) w.append(el('code', null, e.next_step));
  if (e.reason) w.append(el('p', 'dim mono', e.reason));
  box.append(w);
}

async function start() {
  const body = buildRequest();
  if ((body.objective || '').length < 10) {
    fail({ message: 'Write at least 10 characters describing what you want to find out.' });
    return;
  }
  $('#runBtn').disabled = true;
  $('#runState').hidden = true;
  ['resultPanel', 'protocolPanel', 'evidencePanel', 'analysisPanel', 'benchPanel']
    .forEach(i => { const n = $('#' + i); if (n) n.hidden = true; });
  try {
    const run = await post('/api/discovery', body);
    startWatching(run);
  } catch (e) {
    fail(e);
    $('#runBtn').disabled = false;
  }
}

function startWatching(run) {
  state.run = run;
  $('#progressPanel').hidden = false;
  $('#runBadge').textContent = run.runtime_label;
  $('#runBadge').className = 'rt-badge ' + run.runtime_mode;
  renderStages(run.progress);
  if (state.es) state.es.close();
  state.es = new EventSource(`/api/discovery/${run.run_id}/events`);
  state.es.onmessage = ev => {
    try { pushEvent(JSON.parse(ev.data)); } catch (_) { /* a malformed frame is not fatal */ }
  };
  state.es.addEventListener('closed', () => {
    state.es.close(); state.es = null; refresh(run.run_id);
  });
  state.es.onerror = () => { refresh(run.run_id); };
}

async function refresh(id) {
  try {
    const snap = await get(`/api/discovery/${id}?after=999999`);
    renderStages(snap.progress);
    $('#runBtn').disabled = false;
    if (snap.status === 'done' && snap.result) { state.result = snap.result; renderResult(snap); }
    else if (snap.status !== 'running' && snap.status !== 'queued') {
      fail({ headline: snap.error, reason: snap.error_reason, next_step: snap.next_step,
        message: snap.error });
    }
  } catch (_) { /* the run keeps going; the snapshot is a view of it */ }
}

function pushEvent(e) {
  const feed = $('#feed');
  if (feed.firstElementChild && feed.firstElementChild.tagName === 'P') feed.textContent = '';
  const row = el('div', 'ev');
  row.append(el('div', 't', e.t != null ? e.t.toFixed(1) + 's' : ''));
  const d = el('div', 'd');
  const k = el('span', 'k', (e.kind || '').replace(/_/g, ' '));
  d.append(k, document.createTextNode(e.simple || e.technical || ''));
  if (e.simple && e.technical && e.simple !== e.technical) {
    const t = el('div', 'tech'); t.textContent = e.technical; d.append(t);
  }
  row.append(d); feed.append(row);
  feed.scrollTop = feed.scrollHeight;
  $('#evCount').textContent = feed.querySelectorAll('.ev').length + ' events';
  if (e.stage) {
    const rows = $$('#stages .stg');
    const i = rows.findIndex(r => r.dataset.stage === e.stage);
    if (i >= 0) {
      rows.forEach((r, j) => {
        if (j < i) r.dataset.status = 'done';
        else if (j === i) r.dataset.status = 'current';
      });
    }
  }
}

function renderStages(progress) {
  const host = $('#stages'); if (!host) return;
  if (!progress) {
    host.textContent = '';
    host.append(el('li', 'dim', 'Nothing running yet.'));
    return;
  }
  host.textContent = '';
  const MARK = { done: '✓', current: '●', pending: '○', skipped: '–' };
  progress.forEach(p => {
    const li = el('li', 'stg'); li.dataset.status = p.status; li.dataset.stage = p.stage;
    li.append(el('div', 'm', MARK[p.status] || '○'));
    const b = el('div');
    b.append(el('div', 'lbl', p.label));
    b.append(el('div', 'bl', p.blurb));
    li.append(b);
    host.append(li);
  });
}

/* ── results ─────────────────────────────────────────────────────────────── */
function renderResult(snap) {
  const r = snap.result;
  renderEvidence(r.bundle.evidence);
  renderAnalyses(r.bundle.analyses);
  renderHypothesis(r.bundle.selected_hypothesis, r.bundle.hypotheses);
  renderCandidates(r.bundle.candidate_parameters);
  if (r.protocol) renderProtocol(r.protocol);
  renderExports(snap);
  const panel = $('#resultPanel'); panel.hidden = false;
  const head = $('#resultHead'); head.textContent = '';
  head.append(el('span', 'mut', `${r.bundle.artifacts_ingested} artifact(s) read`));
  if ((r.bundle.artifacts_rejected || []).length) {
    head.append(el('span', 'bad', ` · ${r.bundle.artifacts_rejected.length} could not be read`));
  }
  if (!r.bundle.hypotheses.length) {
    head.append(el('p', 'caveat',
      'This run produced no hypothesis that passed its contract. Nothing is shown rather than '
      + 'a summary of what it might have found.'));
  }
}

function renderEvidence(groups) {
  const host = $('#evidence'); if (!host) return;
  host.textContent = '';
  const live = (groups || []).filter(g => g.count);
  if (!live.length) { host.append(el('p', 'dim', 'No evidence was collected.')); }
  live.forEach(g => {
    const w = el('div', 'ds');
    const h = el('div', 't'); h.append(term('evidence_class', g.evidence_class, g.label));
    h.append(el('span', 'dim', `  ${g.count}`));
    w.append(h);
    g.items.slice(0, 6).forEach(it => {
      const row = el('div', 'm');
      row.textContent = it.summary || it.ref || '';
      if (it.context_match === 'context_mismatch') {
        row.append(' ');
        row.append(el('span', 'warnc', 'CONTEXT MISMATCH'));
        if (it.context_mismatch_note) row.append(el('div', 'dim', it.context_mismatch_note));
      }
      if (it.visibility) { row.append(' '); row.append(term('visibility', it.visibility)); }
      w.append(row);
    });
    host.append(w);
  });
  $('#evidencePanel').hidden = false;
}

function renderAnalyses(cards) {
  const host = $('#analyses'); if (!host) return;
  host.textContent = '';
  if (!cards.length) { $('#analysisPanel').hidden = true; return; }
  cards.forEach(a => {
    const w = el('div', 'hyp');
    w.append(el('h4', null, a.question));
    const meta = el('div', 'm');
    meta.append(document.createTextNode(
      (a.datasets || []).map(d => d.title || d.dataset_id).join(', ')));
    if (a.method && a.method.label) meta.append(document.createTextNode(' · ' + a.method.label));
    w.append(meta);
    const badges = el('div', 'ev-row');
    if (a.evidence_class) badges.append(term('evidence_class', a.evidence_class, null, 'ev'));
    if (a.source_evidence_class && a.source_evidence_class !== a.evidence_class) {
      badges.append(term('evidence_class', a.source_evidence_class,
        'source: ' + a.source_evidence_class.replace(/_/g, ' '), 'ev'));
    }
    if (a.visibility) badges.append(term('visibility', a.visibility, null, 'ev'));
    w.append(badges);
    (a.findings || []).forEach(f => {
      const row = el('div', 'eff');
      row.append(el('div', null, f.text));
      w.append(row);
    });
    (a.implications || []).slice(0, 1).forEach(i => {
      w.append(el('div', 'lab', 'Interpretation'));
      w.append(Object.assign(el('div'), { style: 'font-size:13.5px', textContent: i.text }));
    });
    const det = el('details');
    det.append(el('summary', null, 'Technical details'));
    const pre = el('pre');
    pre.style.cssText = 'font-family:var(--mono);font-size:11.5px;overflow:auto;max-height:18rem';
    pre.textContent = JSON.stringify({ method: a.method, comparison: a.comparison,
      statistics: a.technical.statistics, quality_control: a.technical.quality_control,
      provenance: a.technical.provenance }, null, 1);
    det.append(pre);
    w.append(det);
    host.append(w);
  });
  $('#analysisPanel').hidden = false;
}

function effectRow(e) {
  const r = el('div', 'eff');
  r.append(el('div', null, e.label || e.metric));
  const amt = el('div', 'amt');
  if (!e.magnitude_estimated) {
    amt.append(document.createTextNode(
      'direction ' + (e.direction || 'unknown') + ', magnitude not estimated'));
  } else {
    const sign = e.absolute_change > 0 ? '+' : '';
    amt.append(document.createTextNode(
      sign + num(e.absolute_change, 4) + ' ' + (e.change_unit || '')));
    if (e.relative_change_pct != null) {
      amt.append(el('span', 'rel',
        `  (${e.relative_change_pct > 0 ? '+' : ''}${e.relative_change_pct}%)`));
    } else if (e.relative_withheld_reason) {
      const s = el('span', 'rel', '  (relative change withheld)');
      s.title = e.relative_withheld_reason; amt.append(s);
    }
  }
  if (e.estimate_type) amt.append(term('estimate_type', e.estimate_type, null,
    'et ' + e.estimate_type));
  (e.model_basis || []).forEach(b => amt.append(term('model_basis', b, null, 'et')));
  r.append(amt);
  return r;
}

function renderHypothesis(selected, all) {
  const host = $('#hypothesis'); if (!host) return;
  host.textContent = '';
  if (!selected) { host.append(el('p', 'dim', 'No hypothesis was formed.')); return; }
  const w = el('div', 'hyp');
  w.append(el('h4', null, selected.statement));
  const p = selected.parameter;
  const move = el('div', 'move');
  if (p.current_value != null && p.candidate_value != null) {
    move.append(document.createTextNode(`${p.label || p.parameter_id}: ${p.current_value} → `));
    move.append(Object.assign(el('b'), { textContent: String(p.candidate_value) }));
    move.append(document.createTextNode(' ' + (p.unit || '')));
  } else {
    move.textContent = `${p.label || p.parameter_id}: ${p.direction || 'change'}`;
  }
  w.append(move);
  const cov = el('div', 'ev-row');
  cov.append(term('simulator_coverage', p.simulator_coverage, null, 'ev'));
  cov.append(term('hypothesis_status', selected.status, null, 'ev'));
  w.append(cov);
  if (p.coverage_note) w.append(el('p', 'caveat', p.coverage_note));
  w.append(el('div', 'lab', 'Expected effects'));
  (selected.effects || []).forEach(e => w.append(effectRow(e)));
  (selected.trade_offs || []).forEach(t => {
    if (t.summary) w.append(el('p', 'caveat', 'Trade-off: ' + t.summary));
  });
  const confLab = el('div', 'lab');
  confLab.append(document.createTextNode('Evidence · confidence '));
  confLab.append(document.createTextNode(selected.confidence || 'unstated'));
  w.append(confLab);
  (selected.evidence || []).forEach(ev => {
    const row = el('div', 'hev');
    row.append(term('evidence_class', ev.evidence_class, ev.label, 'cls'));
    row.append(el('div', null, ev.summary || ''));
    w.append(row);
  });
  const nx = selected.next_experiment || {};
  if (nx.summary) {
    w.append(el('div', 'lab', 'Recommended next experiment'));
    w.append(Object.assign(el('div'), { style: 'font-size:13.5px', textContent: nx.summary }));
  }
  host.append(w);
  if ((all || []).length > 1) {
    host.append(el('p', 'caveat',
      `${all.length} hypotheses were formed. All of them, and what became of each, are in the `
      + 'protocol below.'));
  }
}

function renderCandidates(rows) {
  const host = $('#candidates'); if (!host) return;
  host.textContent = '';
  if (!rows.length) { host.append(el('p', 'dim', 'No candidate parameter was produced.')); return; }
  rows.forEach(c => {
    const w = el('div', 'ds');
    const t = el('div', 't');
    t.append(document.createTextNode(c.label || c.parameter_id));
    t.append(document.createTextNode('  '));
    t.append(term('simulator_coverage', c.simulator_coverage, null, 'ev'));
    w.append(t);
    const m = el('div', 'm');
    m.textContent = [
      c.current_value != null ? `current ${c.current_value}` : null,
      c.candidate_value != null ? `candidate ${c.candidate_value}` : null,
      c.unit, c.direction, c.confidence ? `confidence ${c.confidence}` : null,
      c.suggested_range ? `range ${c.suggested_range.minimum}–${c.suggested_range.maximum}`
        : null,
    ].filter(Boolean).join(' · ');
    w.append(m);
    if (c.reason) w.append(Object.assign(el('div', 'dim'),
      { style: 'font-size:12.5px;margin-top:4px', textContent: c.reason }));
    if (c.modelled) {
      const b = el('button', 'btn', 'Open in simulator');
      b.addEventListener('click', () => openInSimulator(c));
      w.append(b);
    } else {
      w.append(el('p', 'caveat',
        'Not modelled here, so no effect is predicted for it. It is still a real design '
        + 'variable you can set in the lab.'));
    }
    host.append(w);
  });
}

/* The handoff: the simulator opens on this project with the control and the
   candidate already loaded, so nobody retypes a setpoint. */
function openInSimulator(c) {
  const payload = {
    project_id: (state.result && state.result.bundle.project_id) || $('#project').value,
    parameter_id: c.parameter_id, label: c.label, unit: c.unit,
    control: c.current_value, candidate: c.candidate_value,
    reason: c.reason, confidence: c.confidence,
    evidence_sources: c.evidence_sources || [],
    from_run: state.run && state.run.run_id,
  };
  try { localStorage.setItem('bs-sim-candidate', JSON.stringify(payload)); } catch (_) {}
  location.href = 'simulator.html';
}

function renderProtocol(p) {
  const host = $('#protocol'); if (!host) return;
  host.textContent = '';
  $('#protocolPanel').hidden = false;

  const head = el('div', 'ev-row');
  head.append(el('span', 'rt-badge ' + p.runtime_mode, p.badge));
  head.append(el('span', 'flag', 'PROPOSED — NOT APPROVED'));
  host.append(head);
  host.append(el('p', 'caveat', p.approval.how));
  if (p.blocks_wet_lab) {
    const s = el('div', 'state');
    s.append(el('h4', null, 'This protocol has gaps and cannot be run'));
    s.append(el('p', null, 'Each gap needs evidence or a named design choice before a wet-lab '
      + 'run. They are listed below.'));
    host.append(s);
  }

  const c = p.summary_counts;
  host.append(Object.assign(el('p', 'mut'), { style: 'margin:12px 0',
    textContent: `${c.parameters_changed} of ${c.parameters_total} parameters change. `
      + `${c.hypotheses_adopted} of ${c.hypotheses_total} hypotheses are in the protocol; `
      + `${c.hypotheses_discarded} are not.` }));

  p.stages.forEach(st => {
    const w = el('div', 'proto-stage');
    w.append(el('h4', null, st.label));
    if (st.goal) w.append(el('p', 'goal', st.goal));
    st.parameters.forEach(q => {
      const row = el('div', 'prow' + (q.changed ? ' changed' : ''));
      row.append(el('div', 'nm', q.label + (q.unit ? ` (${q.unit})` : '')));
      const vals = el('div', 'vals');
      if (q.changed) {
        vals.append(document.createTextNode(num(q.control_value) + ' → '));
        vals.append(el('span', 'to', num(q.recommended_value)));
      } else {
        vals.append(document.createTextNode(num(q.recommended_value)));
      }
      row.append(vals);
      const tags = el('div', 'tags');
      tags.append(term('provenance', q.provenance, null, 'tag ' + q.provenance));
      if (q.estimate_type) tags.append(term('estimate_type', q.estimate_type, null, 'tag'));
      tags.append(term('simulator_coverage', q.simulator_coverage, null,
        'tag ' + q.simulator_coverage));
      row.append(tags);
      w.append(row);
      if (q.reason && q.changed) {
        w.append(Object.assign(el('div', 'dim'),
          { style: 'font-size:12.5px;padding:0 0 6px', textContent: q.reason }));
      }
    });
    host.append(w);
  });

  host.append(el('div', 'lab', 'Hypotheses this run formed'));
  host.append(el('p', 'caveat',
    'Every one, including the ones that did not make it. A discarded hypothesis is part of the '
    + 'result, not a mistake to hide.'));
  const tbl = el('table', 'ledger');
  const thead = el('thead'); const hr = el('tr');
  ['Hypothesis', 'Status', 'In protocol', 'Why'].forEach(h => hr.append(el('th', null, h)));
  thead.append(hr); tbl.append(thead);
  const tb = el('tbody');
  p.hypothesis_ledger.forEach(r => {
    const tr = el('tr', r.adopted ? 'kept' : 'dropped');
    tr.append(el('td', null, r.statement));
    const st = el('td'); st.append(term('hypothesis_status', r.status, null, 'st')); tr.append(st);
    tr.append(el('td', null, r.adopted ? 'yes' : 'no'));
    tr.append(el('td', null, r.reason || ''));
    tb.append(tr);
  });
  tbl.append(tb); host.append(tbl);

  if ((p.gaps || []).length) {
    host.append(el('div', 'lab', 'Gaps — these block the wet lab'));
    const ul = el('ul', 'tight');
    p.gaps.forEach(g => ul.append(el('li', null, `${g.parameter_id}: ${g.why}`)));
    host.append(ul);
  }
  if ((p.limitations || []).length) {
    const det = el('details');
    det.append(el('summary', null, 'Limitations'));
    const ul = el('ul', 'tight');
    p.limitations.forEach(x => ul.append(el('li', null, x)));
    det.append(ul); host.append(det);
  }
}

function renderBenchmark(b) {
  const host = $('#benchmark'); if (!host) return;
  host.textContent = '';
  $('#benchPanel').hidden = false;
  const head = el('div', 'ev-row');
  head.append(el('span', 'rt-badge ' + (b.mode === 'ai' ? 'local_real_ai' : 'synthetic_demo'),
    b.mode === 'ai' ? 'AI-DRIVEN' : 'DETERMINISTIC'));
  head.append(el('span', 'flag', b.privacy.badge));
  host.append(head);
  host.append(el('h4', null, b.title || b.benchmark_id));
  host.append(el('p', 'mut', b.objective));
  host.append(el('p', 'caveat', b.scorecard.headline));
  const tbl = el('table', 'ledger');
  const tb = el('tbody');
  b.scorecard.rows.forEach(r => {
    const tr = el('tr', r.status === 'PASS' ? 'kept' : 'dropped');
    tr.append(el('td', null, r.capability.replace(/_/g, ' ')));
    tr.append(el('td', 'st', r.status));
    tr.append(el('td', null, r.detail || ''));
    tb.append(tr);
  });
  tbl.append(tb); host.append(tbl);
  if (b.privacy.refusal_reason) host.append(el('p', 'caveat', b.privacy.refusal_reason));
  $('#benchPanel').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function renderExports(snap) {
  const host = $('#exports'); if (!host) return;
  host.textContent = '';
  const id = snap.run_id;
  const mk = (label, href, note) => {
    const a = el('a', 'btn', label);
    a.href = href; a.target = '_blank'; a.rel = 'noopener';
    host.append(a);
    if (note) host.append(el('div', 'note', note));
  };
  if (snap.result && snap.result.protocol) {
    mk('Export protocol (Markdown)', `/api/discovery/${id}/protocol?format=md`,
      'The recommended protocol, its provenance tags and the hypothesis ledger.');
  }
  const b = el('button', 'btn go', 'Create benchmark from this run');
  b.addEventListener('click', async () => {
    b.disabled = true;
    try {
      const out = await post(`/api/discovery/${id}/benchmark`);
      state.benchmark = out.benchmark;
      renderBenchmark(out.benchmark);
      loadBenchmarks();
    } catch (e) { fail(e); } finally { b.disabled = false; }
  });
  host.append(b);
  host.append(el('div', 'note',
    'Built from the artifacts this run already produced. The biology is not run again.'));
}

/* ── wiring ──────────────────────────────────────────────────────────────── */
function wire() {
  $('#runBtn').addEventListener('click', start);
  const add = $('#candAdd');
  if (add) {
    add.addEventListener('click', () => {
      const pid = $('#candParam').value;
      const raw = ($('#candValue').value || '').trim();
      const v = Number(raw);
      if (!pid || raw === '' || !isFinite(v)) return;
      state.candidates = state.candidates || {};
      state.candidates[pid] = v;
      $('#candValue').value = '';
      renderCandidatePicks();
    });
  }
  const sign = $('#signinForm');
  if (sign) {
    sign.addEventListener('submit', async ev => {
      ev.preventDefault();
      const msg = $('#signinMsg'); msg.textContent = 'signing in…';
      try {
        await post('/api/auth/login', {
          server: $('#signinServer').value.trim(),
          username: $('#signinUser').value.trim(),
          password: $('#signinPass').value,
        });
        location.reload();
      } catch (e) { msg.textContent = e.message || 'sign-in failed'; }
    });
  }
  const view = $('#viewBtn');
  if (view) {
    view.addEventListener('click', () => {
      const tech = document.body.dataset.view !== 'technical';
      document.body.dataset.view = tech ? 'technical' : 'simple';
      view.textContent = tech ? 'Simple view' : 'Technical view';
      view.setAttribute('aria-pressed', String(tech));
    });
  }
}

boot();
