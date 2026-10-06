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

/* The one thing this page keeps in the browser: which run you were watching.
   A real AI run outlives a page load, and losing the only reference to one you
   are paying for because you reloaded a tab is not acceptable. The id is enough
   — the run journals itself beside its artifacts on the server. */
const LAST_RUN_KEY = 'bs-last-discovery-run';
function rememberRun(id) {
  try { localStorage.setItem(LAST_RUN_KEY, id); } catch (_) { /* private mode */ }
}
function forgetRun() {
  try { localStorage.removeItem(LAST_RUN_KEY); } catch (_) { /* private mode */ }
}
function rememberedRun() {
  try { return localStorage.getItem(LAST_RUN_KEY); } catch (_) { return null; }
}

const state = {
  glossary: null, runtime: null, projects: [], project: null, datasets: [],
  run: null, es: null, detach: null, result: null, benchmark: null,
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
  mountRunBeacon();
  /* The selector's active-run counts come from the same list the Runs page
     reads, so "1 active investigation" means the same thing on both. */
  BS.onChange(s => {
    renderProjectRunList(s);
    /* Not while somebody is using it: rebuilding a select that has focus closes
       the list under their cursor. */
    if (document.activeElement === $('#project')) return;
    loadProjects().catch(() => {});
  });
  await resume();
}

/* ── picking a run back up ───────────────────────────────────────────────
   Three outcomes, and the difference between them is the point: a run still
   going is re-attached to its live stream; a finished one is rendered from the
   record it wrote; one the server has no record of is forgotten rather than
   reported as anything. */
async function resume() {
  const id = (new URLSearchParams(location.search)).get('run') || rememberedRun();
  if (!id) return;
  /* Re-attach to the run that is already going. Never start another: the browser
     observes a server-side job, and a reload is a new observer, not a new run. */
  watchRun(id);
}

function note(text) {
  const box = $('#runState');
  if (!box) return;
  if (!text) { box.hidden = true; return; }
  box.hidden = false; box.textContent = '';
  const w = el('div', 'state info'); w.append(el('p', 'dim', text)); box.append(w);
}

async function loadRuntime() {
  try { state.runtime = await get('/api/runtime?probe=1'); }
  catch (_) { return; }
  renderRuntimePicker();
  renderIdentity();
}

/* The account area. Three things belong here and nothing else: who you are,
   what this deployment lets you do, and the way in or out. The role comes from
   the server on every load — the page cannot award itself one, and setting
   is_admin in a console does nothing but lie to the person who typed it. */
function renderIdentity() {
  const box = $('#identity'); if (!box) return;
  const id = state.runtime && state.runtime.identity;
  box.textContent = '';
  if (!id) return;
  if (id.is_admin) box.append(el('span', 'rt-badge admin', 'ADMIN'));
  if (id.authenticated) {
    box.append(el('span', 'mono dim', id.display_name || id.display), ' ');
    const out = el('button', 'btn', 'Sign out');
    out.addEventListener('click', async () => {
      await post('/api/auth/logout'); location.reload();
    });
    box.append(out);
  } else if (id.sign_in_available) {
    const inBtn = el('button', 'btn', 'Sign in');
    inBtn.addEventListener('click', () => $('#signinCard').hidden = !$('#signinCard').hidden);
    box.append(inBtn);
  }
  const note = $('#identityNote');
  if (note) note.textContent = id.note || '';
  renderAllowance(id);
}

/* What is left, and only where the server actually knows. A page that invents a
   remaining-run count is worse than one that shows none. */
function renderAllowance(id) {
  const host = $('#allowance'); if (!host) return;
  host.textContent = '';
  const a = (id && id.allowance) || null;
  if (!a) { host.hidden = true; return; }
  host.hidden = false;
  if (a.role === 'admin') {
    host.append(el('span', 'rt-badge admin', 'ADMIN ACCESS'),
      el('span', 'dim', 'Public demo limits do not apply. Runs still spend this '
        + 'deployment\u2019s model credits.'));
    return;
  }
  if (!a.capped) { host.hidden = true; return; }
  const bits = [];
  if (a.real_runs_remaining_today != null) {
    bits.push(`${a.real_runs_remaining_today} Real AI run`
      + (a.real_runs_remaining_today === 1 ? '' : 's') + ' remaining today');
  }
  if (a.next_run_in_s) bits.push(`next run available in ${a.next_run_in_s}s`);
  if (a.deployment_runs_remaining_today != null) {
    bits.push(`${a.deployment_runs_remaining_today} left across this service today`);
  }
  host.append(el('span', 'rt-badge demo-limits', 'BIOSENSE DEMO AI'),
    el('span', 'dim', bits.join(' \u00b7 ')));
}

function renderRuntimePicker() {
  const host = $('#runtimes'); if (!host || !state.runtime) return;
  host.textContent = '';
  const avail = state.runtime.availability || {};
  /* The real runtime first where this deployment offers one. On the hosted
     service that is the product: pressing the button runs the actual agents, and
     the demonstration path is the deliberate second choice, not the default a
     visitor lands on by accident. */
  const def = state.runtime.default_mode;
  const order = state.runtime.modes.slice().sort((a, b) => {
    const rank = m => (m.mode === def && m.offered ? 0 : m.offered ? 1 : 2);
    return rank(a) - rank(b);
  });
  order.forEach(m => {
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
      // The sandbox's own words (e.g. "this host does not let bwrap create a user
      // namespace") are what an operator needs to fix it, and carry no secret.
      if (a.reason === 'agent_sandbox_unavailable' && a.detail) {
        why.append(el('div', 'dim mono', a.detail));
      }
      lab.append(why);
    }
    // Said, not hidden: a demo deployment that runs the agents without their OS
    // sandbox labels the real-AI choice with it.
    const sb = state.runtime.agent_sandbox;
    if (sb && sb.sandbox === 'off' && m.mode === 'local_real_ai' && sb.note) {
      lab.append(el('div', 'why', sb.note));
    }
    lab.addEventListener('change', updateBadge);
    host.append(lab);
  });
  if (!host.querySelector('input:checked')) {
    const first = host.querySelector('input:not(:disabled)');
    if (first) first.checked = true;
  }
  renderLimits();
  updateBadge();
}

/* What a visitor is allowed to spend, before they are refused rather than
   after. A cap nobody could read first reads like a fault. */
function renderLimits() {
  const lim = state.runtime && state.runtime.limits;
  const host = $('#limits');
  if (!host) return;
  host.textContent = '';
  if (!lim || !lim.public_demo) { host.hidden = true; return; }
  host.hidden = false;
  host.append(el('span', 'rt-badge demo-limits', 'DEMO LIMITS'));
  const bits = [];
  if (lim.max_real_runs_per_client_per_day)
    bits.push(`${lim.max_real_runs_per_client_per_day} real AI runs per visitor per day`);
  if (lim.max_real_runs_per_day)
    bits.push(`${lim.max_real_runs_per_day} across the service per day`);
  if (lim.real_run_timeout_s)
    bits.push(`each run stops after ${Math.round(lim.real_run_timeout_s / 60)} minutes `
      + '(the live panel offers more time before it does)');
  if (lim.real_run_cooldown_s)
    bits.push(`${lim.real_run_cooldown_s}s between runs`);
  host.append(el('span', 'dim', bits.join(' · ')));
  host.append(el('div', 'note', 'This service runs real AI on its own model credentials, so '
    + 'real runs are capped. The demonstration path has no cap, and running BioSense '
    + 'yourself has none either: ./scripts/start_local_ai.sh'));
}

/* The badge this deployment uses for a mode. A hosted service runs its own
   runtime on its own loopback, which is `local_real_ai` in the code and ONLINE
   to the person reading it: "LOCAL" in a browser means "your laptop". */
function modeLabel(mode) {
  const m = ((state.runtime && state.runtime.modes) || []).find(x => x.mode === mode);
  return (m && m.label) || mode;
}

function chosenRuntime() {
  const r = $('#runtimes input:checked');
  return r ? r.value : (state.runtime && state.runtime.default_mode) || 'synthetic_demo';
}

function updateBadge() {
  const mode = chosenRuntime();
  const host = $('#runtimeBadge'); if (!host) return;
  const id = state.runtime && state.runtime.identity;
  host.textContent = '';
  host.className = 'rt-badge ' + mode;
  host.append(el('span', 'dot'));
  /* "ADMIN · REAL AI — ONLINE". The role says who is running it; the runtime
     says what kind of answer it is. Neither word changes the other's meaning:
     an operator's synthetic run is still synthetic. */
  if (id && id.is_admin) host.append(el('span', 'role', 'ADMIN \u00b7 '));
  host.append(term('runtime_mode', mode, modeLabel(mode)));
}

async function loadProjects(select) {
  let d;
  try { d = await get('/api/workspace/projects'); } catch (_) { return; }
  /* Your own projects first, then templates; a project with a model ahead of one
     without. The default matters: landing on an unrelated project produces a run
     that refuses everything for the right reason and reads like a fault. */
  state.projects = d.projects.slice().sort((a, b) =>
    (a.source === 'workspace' ? 0 : 1) - (b.source === 'workspace' ? 0 : 1)
    || (b.has_simulator ? 1 : 0) - (a.has_simulator ? 1 : 0)
    || a.name.localeCompare(b.name));
  const sel = $('#project');
  const want = select || sel.value || BS.selected.get();
  sel.textContent = '';
  state.projects.forEach(p => {
    const runs = p.runs || {};
    const o = el('option', null, p.name
      + (p.source === 'template' ? '  (template)' : '')
      + (runs.active ? `   ● ${runs.active} active` : ''));
    o.value = p.project_id; sel.append(o);
  });
  if (want && state.projects.some(p => p.project_id === want)) sel.value = want;
  if (!sel.dataset.wired) {
    sel.dataset.wired = '1';
    /* Switching project changes what you are looking at and nothing else. No
       run is touched: they are server-side jobs, and the one you were watching
       keeps working while you read another project. */
    sel.addEventListener('change', () => { BS.selected.set(sel.value); showProject(); });
  }
  showProject();
}

async function showProject() {
  const id = $('#project').value;
  const row = state.projects.find(p => p.project_id === id);
  const note = $('#projectNote');
  if (!row) { note.textContent = ''; return; }
  BS.selected.set(id);
  renderProjectRunList(BS.state);
  note.textContent = row.has_simulator
    ? 'This project has a mechanistic model, so candidate parameters it covers get a prediction.'
    : 'This project has no mechanistic model. Parameters are real design variables; nothing '
      + 'here predicts what they would do, and no other project’s model is borrowed.';
  const runs = row.runs || {};
  const r = $('#projectRuns');
  if (r) {
    r.textContent = runs.active
      ? `● ${runs.active} active investigation${runs.active === 1 ? '' : 's'}`
        + (runs.total > runs.active ? ` · ${runs.total} total` : '')
      : (runs.total ? `${runs.total} run${runs.total === 1 ? '' : 's'}` : 'No runs yet');
  }
  if (!$('#projectDetail').hidden) renderProjectDetail(id);

  state.candidates = {};
  renderCandidatePicks();
  loadProjectParameters(id);
}

function currentProject() { const s = $('#project'); return s ? s.value : null; }

/* The project's runs, right under the picker: a scientist comes back to a
   project to see what it already found, so that is one click, not three. Fed
   by the same listing as the Runs page, scoped to the account. */
const RUN_LIST_MAX = 8;
function renderProjectRunList(snapshot) {
  const host = $('#projectRunList'); if (!host) return;
  const id = currentProject();
  const runs = ((snapshot && snapshot.runs) || []).filter(r => !id || r.project_id === id);
  host.textContent = '';
  if (!runs.length) return;
  host.append(el('div', 'lab', `Runs in this project (${runs.length})`));
  runs.slice(0, RUN_LIST_MAX).forEach(r => {
    const row = el('div', 'prun');
    const when = r.started_at ? new Date(r.started_at * 1000).toLocaleString([], {
      month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '';
    row.append(el('span', 'dim mono', when));
    row.append(el('span', 'rv-status ' + (r.live ? 'go' : r.group || ''), BS.fmt.status(r)));
    const what = el('span', 'pobj', r.hypothesis || r.objective || r.run_id);
    what.title = r.objective || '';
    row.append(what);
    if (r.hypothesis_count > 1) row.append(el('span', 'dim', `+${r.hypothesis_count - 1}`));
    const open = el('button', 'btn', r.live ? 'Watch' : 'Open');
    open.addEventListener('click', () => watchRun(r.run_id));
    row.append(open);
    host.append(row);
  });
  if (runs.length > RUN_LIST_MAX) {
    const a = el('a', 'dim', `All ${runs.length} runs of this project →`);
    a.href = `runs.html?project=${encodeURIComponent(id)}`;
    host.append(a);
  }
}

/* The project as a workspace: what the process is, what is running in it, and
   what it has produced. A panel, deliberately — not a management screen. */
async function renderProjectDetail(id) {
  const host = $('#projectDetail');
  host.hidden = false;
  host.textContent = '';
  let d;
  try { d = await get(`/api/projects/${encodeURIComponent(id)}`); }
  catch (e) { host.append(el('p', 'dim', e.message || 'That project is not available.')); return; }
  const p = d.project || {};
  const box = el('div', 'pdetail');
  const bio = p.biological_system || {};
  const facts = [
    ['Species', bio.species], ['From', bio.starting_cell], ['To', bio.target_cell],
    ['State', bio.target_subtype], ['Process', bio.culture_format],
    ['Stages', (p.stages || []).map(s => s.label || s.stage_id).join(' → ')],
    ['Parameters', String((p.parameters || []).length)],
    ['Simulator', p.has_simulator ? p.simulator.model_id : 'none — no prediction is produced'],
  ];
  const dl = el('div', 'pfacts');
  facts.forEach(([k, v]) => {
    if (!v) return;
    const row = el('div', 'pf');
    row.append(el('div', 'k', k), el('div', 'v', v));
    dl.append(row);
  });
  box.append(dl);
  if ((d.active_runs || []).length) {
    box.append(el('div', 'lab', 'Active investigation'));
    d.active_runs.forEach(r => {
      const row = el('div', 'prun');
      row.append(el('span', 'rv-status go', BS.fmt.status(r)));
      row.append(el('span', 'dim', [r.engine ? (r.engine.includes('codex') ? 'Codex' : 'Claude')
        : null, (r.active_agents || [])[0], BS.fmt.elapsed(r.started_at, r.finished_at)]
        .filter(Boolean).join(' · ')));
      const open = el('button', 'btn', 'Open run');
      open.addEventListener('click', () => watchRun(r.run_id));
      row.append(open);
      box.append(row);
    });
  }
  if ((d.recent_runs || []).length) {
    box.append(el('div', 'lab', 'Recent runs'));
    d.recent_runs.slice(0, 5).forEach(r => {
      const row = el('div', 'prun');
      row.append(el('span', 'dim mono', BS.fmt.status(r)));
      row.append(el('span', null, r.objective || r.run_id));
      const open = el('button', 'btn', 'Open');
      open.addEventListener('click', () => watchRun(r.run_id));
      row.append(open);
      box.append(row);
    });
  }
  if ((d.hypotheses || []).length) {
    box.append(el('div', 'lab', 'Hypotheses from this project'));
    d.hypotheses.slice(0, 5).forEach(h => box.append(el('p', 'phyp', h.statement)));
  }
  if ((p.limitations || []).length) {
    box.append(el('div', 'lab', 'Limitations'));
    p.limitations.forEach(l => box.append(el('p', 'dim', l)));
  }
  host.append(box);
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
  /* Private uploads are never listed by the server; the Data page remembers the
     ones made from this browser, and they are offered here for the same person. */
  let mine = [];
  try { mine = JSON.parse(localStorage.getItem('biosense.uploads') || '[]'); } catch (_) {}
  const known = new Set(state.datasets.map(m => m.dataset_id));
  mine.filter(u => !known.has(u.dataset_id)).forEach(u => state.datasets.push(
    { dataset_id: u.dataset_id, title: u.title || u.dataset_id, visibility: 'private',
      yours: true }));
  const host = $('#datasetPicks'); host.textContent = '';
  if (!state.datasets.length) {
    host.append(el('p', 'hint', d.note || 'No datasets are registered on this instance.'));
    return;
  }
  state.datasets.forEach(m => {
    const lab = el('label', 'pick');
    const i = el('input'); i.type = 'checkbox'; i.value = m.dataset_id;
    lab.append(i, el('span', null, m.title || m.dataset_id));
    if (m.visibility === 'private') lab.append(el('span', 'vis', m.yours ? 'private · yours' : 'private'));
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
    effort: ($('#effort') && $('#effort').value) || 'standard',
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
  w.append(el('h4', null,
    e.reason === 'not_configured' ? 'Runtime not offered'
      : e.reason === 'budget' ? 'Run limit reached'
      : e.reason === 'cancelled' || e.reason === 'timed_out' ? 'Run stopped'
      : e.reason === 'interrupted' ? 'Run interrupted'
      : e.headline ? 'Real AI runtime unavailable' : 'Refused'));
  w.append(el('p', null, e.headline || e.message || String(e)));
  if (e.next_step) w.append(el('code', null, e.next_step));
  if (e.retry_after_s) {
    w.append(el('p', 'dim', `Try again in about ${Math.ceil(e.retry_after_s / 60)} minute(s).`));
  }
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
  watchRun(run.run_id);
}

/* One run, one viewer, one source of truth. The Runs page renders this same
   snapshot with this same component — so the two pages cannot disagree about
   whether something is working, which they could when each kept its own idea
   of a run. */
function watchRun(runId) {
  if (state.detach) state.detach();
  rememberRun(runId);
  $('#progressPanel').hidden = false;
  $('#runRef').textContent = runId;
  state.detach = RV.attach(runId, {
    snapshot(snap) {
      state.run = snap;
      RV.header($('#rvHead'), snap, { onStop: stopRun, onExtend: extendRun });
      RV.stages($('#rvStages'), snap);
      RV.agents($('#rvAgents'), snap);
      RV.timeline($('#rvTimeline'), snap);
      RV.limitations($('#rvLims'), snap);
      renderInsights(snap);
      renderStages(snap.progress);
      (snap.events || []).forEach(seedEvent);
      $('#runBtn').disabled = RV.isLive(snap);
      showStop(snap);
      if (snap.status === 'done' && snap.result) {
        state.result = snap.result; renderResult(snap);
        note(snap.recovered ? 'Reopened from the server: this is the run you were last '
          + 'watching, read back from its saved record. Nothing went wrong.' : null);
      } else if (!RV.isLive(snap) && snap.result && hasPartial(snap.result)) {
        /* Stopped by the time limit, by a person, or by a restart: what the
           agents had written is still worth reading. Shown, and labelled
           partial — never as a finished answer. */
        state.result = snap.result; renderResult(snap);
        fail({ headline: 'Partial result — ' + (snap.error || 'this run did not finish'),
          reason: snap.error_reason, next_step: snap.next_step, message: snap.error });
      } else if (!RV.isLive(snap)) {
        fail({ headline: snap.error, reason: snap.error_reason, next_step: snap.next_step,
          message: snap.error });
      }
      BS.runs(currentProject()).catch(() => {});
    },
    event(e) { pushEvent(e); },
    error() {
      $('#runBtn').disabled = false;
      showStop(null);
      forgetRun();
      $('#progressPanel').hidden = true;
    },
  });
}

async function stopRun(runId) {
  try { await post(`/api/discovery/${runId}/cancel`); }
  catch (e) { fail(e); }
}

/* More time for a live run, within the deployment's bounds. The server says no
   when there is none to give, and that answer is shown rather than swallowed. */
async function extendRun(runId) {
  try { await post(`/api/discovery/${runId}/extend`); }
  catch (e) { fail(e); }
}

/* The stop button beside "Run", visible exactly while the watched run is live —
   so stopping a run never depends on scrolling to the live panel. */
function showStop(snap) {
  const b = $('#stopBtn'); if (!b) return;
  const live = !!snap && RV.isLive(snap) && snap.cancellable !== false;
  b.hidden = !live;
  if (!live) { b.disabled = false; b.textContent = 'Stop run'; delete b.dataset.run; return; }
  if (b.dataset.run !== snap.run_id) { b.disabled = false; b.textContent = 'Stop run'; }
  b.dataset.run = snap.run_id;
}

function stopFromButton() {
  const b = $('#stopBtn'); const id = b && b.dataset.run;
  if (!id) return;
  if (state.run && state.run.paused) { b.disabled = true; b.textContent = 'finishing…'; stopRun(id); return; }
  if (!confirm('Stop this run?\n\nThe orchestrator and every specialist still working are '
    + 'interrupted. Whatever they have already written is kept, and the run is recorded as '
    + 'stopped — not as an answer.')) return;
  b.disabled = true; b.textContent = 'stopping…';
  stopRun(id);
}

/* What the bioinformatics agent is doing, from its own files: the analyses it
   planned (question, dataset, tool, the uncertainty it targets), what came
   back, and its running notes. */
let bioSeen = '';
function renderBioInsights(bio) {
  const host = $('#bioInsights'); if (!host) return;
  const key = JSON.stringify(bio || null);
  if (key === bioSeen) return;
  bioSeen = key;
  host.textContent = '';
  if (!bio) return;
  host.append(el('div', 'lab', 'Bioinformatics'));
  (bio.plans || []).forEach(p => {
    const row = el('div', 'm');
    row.append(el('span', 'tag', 'PLANNED'), ' ', el('b', null, p.question || p.plan_id));
    const bits = [p.tool && `tool ${p.tool}`, (p.datasets || []).length && `data ${p.datasets.join(', ')}`,
      p.analysis_type].filter(Boolean);
    if (bits.length) row.append(el('div', 'dim', bits.join(' · ')));
    if (p.uncertainty) row.append(el('div', 'dim', 'Settles: ' + p.uncertainty));
    host.append(row);
  });
  (bio.results || []).forEach(r => {
    const row = el('div', 'm');
    row.append(el('span', 'tag', 'RESULT'), ' ', el('b', null, r.question || r.analysis_id));
    if (r.source) row.append(el('span', 'dim', `  from ${r.source}${r.confidence ? ' · ' + r.confidence : ''}`));
    (r.key_findings || []).forEach(f => row.append(el('div', null, '• ' + (typeof f === 'string' ? f : (f.statement || f.summary || JSON.stringify(f))))));
    host.append(row);
  });
  if (!(bio.plans || []).length && !(bio.results || []).length) {
    host.append(el('p', 'dim', 'No analysis planned yet.'));
  }
  if (bio.notes) {
    bio.notes.text.split(/\n{2,}/).map(b => b.trim()).filter(Boolean)
      .forEach(b => host.append(el('p', 'm insight', b)));
  }
}

function hasPartial(r) {
  const b = (r && r.bundle) || {};
  return !!(r.protocol || (b.hypotheses || []).length || (b.artifacts_ingested || 0) > 0);
}

/* The literature agent's running notes and the papers it found, live. These
   are leads written while reading, shown before the orchestrator has weighed
   them; the panel says so, and never calls them findings. */
let insightsSeen = '';
function renderInsights(snap) {
  const panel = $('#insightsPanel'); if (!panel) return;
  const ins = snap.insights, papers = snap.papers_found || [], bio = snap.bioinformatics;
  if (!ins && !papers.length && !bio) { panel.hidden = true; return; }
  panel.hidden = false;
  renderBioInsights(bio);
  const key = `${ins ? ins.updated_at : 0}:${papers.length}`;
  if (key === insightsSeen) return;
  insightsSeen = key;
  $('#insightsHead').textContent = ins ? `updated ${BS.fmt.ago(ins.updated_at)}` : '';
  $('#insightsNote').textContent = ins ? ins.note : '';
  const host = $('#insights'); host.textContent = '';
  if (ins) {
    ins.text.split(/\n{2,}/).map(b => b.trim()).filter(Boolean).forEach(block => {
      host.append(el('p', 'm', block));
    });
  }
  const list = $('#papersFound'); list.textContent = '';
  if (!papers.length) list.append(el('p', 'dim', 'No search has been saved yet.'));
  papers.forEach(p => {
    const row = el('div', 'm');
    const a = el('a', null, p.id || '?');
    if (p.pmcid) a.href = `https://europepmc.org/articles/${p.pmcid}`;
    else if (p.pmid) a.href = `https://pubmed.ncbi.nlm.nih.gov/${p.pmid}/`;
    a.target = '_blank'; a.rel = 'noopener';
    row.append(a, ` · ${p.year || '?'} · `, el('span', null, p.title));
    const tags = [];
    if (p.open_access === 'Y' || p.full_text_read) tags.push(p.full_text_read ? 'full text read' : 'open access');
    if (p.cited_by) tags.push(`cited ${p.cited_by}`);
    if (p.matched_queries > 1) tags.push(`${p.matched_queries} queries`);
    if (tags.length) row.append(el('div', 'dim', tags.join(' · ')));
    list.append(row);
  });
}

/* Events seen in a snapshot are replayed into the record feed once each, so a
   reload shows the history rather than starting the feed empty. */
const seenEvents = new Set();
function seedEvent(e) {
  const key = `${e.seq}`;
  if (seenEvents.has(key)) return;
  seenEvents.add(key);
  pushEvent(e, { quiet: true });
}

function pushEvent(e, opts) {
  if (!(opts && opts.quiet)) {
    if (seenEvents.has(`${e.seq}`)) return;
    seenEvents.add(`${e.seq}`);
  }
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
      } else if (it.context_match === 'partial_match') {
        row.append(' ');
        row.append(el('span', 'warnc', 'PARTIAL CONTEXT MATCH'));
        if (it.context_mismatch_note) row.append(el('div', 'dim', it.context_mismatch_note));
      }
      /* Indirect evidence is shown with its inference chain: the reader judges
         the step from what the source shows to what the hypothesis claims. */
      if (it.relevance && it.relevance !== 'direct') {
        row.append(' ');
        row.append(el('span', 'tag', it.relevance.toUpperCase()));
        if (it.bearing) row.append(el('div', 'dim', 'Bears on it because: ' + it.bearing));
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
  if (e && e.magnitude_estimated === false) {
    /* The honest shape of "we know which way, not how far". A blank row here
       reads as a missing number; this reads as the claim it is. */
    const row = el('div', 'eff');
    const left = el('div', null);
    left.append(el('div', null, e.label || e.metric));
    if (e.withheld_reason) left.append(el('div', 'm', e.withheld_reason));
    const amt = el('div', 'amt');
    amt.append(el('span', 'notest', 'NOT ESTABLISHED'));
    if (e.direction && e.direction !== 'unknown') {
      amt.append(el('span', 'rel', `direction: ${e.direction}`));
    }
    amt.append(term('estimate_type', e.estimate_type, null, 'et ' + e.estimate_type));
    row.append(left, amt);
    return row;
  }
  return effectRowSized(e);
}

function effectRowSized(e) {
  const r = el('div', 'eff');
  if (e.estimate_type === 'judgement') return effectRowGuess(e, r);
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

/* A best guess: a range and a confidence, never a bare number. It reads as a
   guess on purpose, so nobody mistakes it for a measurement. */
function effectRowGuess(e, r) {
  const j = e.judgement || {};
  const iv = e.interval || {};
  const left = el('div', null);
  left.append(el('div', null, e.label || e.metric));
  if (j.rationale) left.append(el('div', 'm', j.rationale));
  if (j.would_change_it) left.append(el('div', 'm', 'Would change it: ' + j.would_change_it));
  const amt = el('div', 'amt');
  const sgn = v => (v > 0 ? '+' : '') + num(v, 4);
  amt.append(document.createTextNode(
    `best guess ${sgn(iv.lower)} to ${sgn(iv.upper)} ${e.change_unit || ''}`));
  if (e.absolute_change != null) amt.append(el('span', 'rel', `  (central ${sgn(e.absolute_change)})`));
  amt.append(el('span', 'conf ' + (j.confidence || 'low'), (j.confidence || 'low') + ' confidence'));
  amt.append(term('estimate_type', e.estimate_type, null, 'et ' + e.estimate_type));
  r.append(left, amt);
  return r;
}

/* The recommendation is always shown; how far to trust it sits right under
   it, as a bar, with the reasons the code computed and the sources behind
   them — supporting and contradicting — so "why high" or "why low" is
   checkable without leaving the card. */
const CONF_LEVELS = { low: 1, moderate: 2, high: 3 };
function sourceLink(ref) {
  const r = String(ref || '');
  let m = r.match(/^PMC\d+$/);
  if (m) return `https://europepmc.org/articles/${r}`;
  m = r.match(/^PMID:?\s*(\d+)$/i);
  if (m) return `https://pubmed.ncbi.nlm.nih.gov/${m[1]}/`;
  m = r.match(/^(10\.\d{4,9}\/\S+)$/);
  if (m) return `https://doi.org/${m[1]}`;
  return null;
}
function confidenceBar(h) {
  const level = h.confidence || 'low';
  const box = el('div', 'confbox');
  const top = el('div', 'confhead');
  top.append(el('span', 'lab', 'Confidence'));
  const bar = el('div', 'confbar ' + level);
  for (let i = 1; i <= 3; i++) bar.append(el('span', i <= (CONF_LEVELS[level] || 1) ? 'on' : ''));
  top.append(bar, el('span', 'conf ' + level, level));
  box.append(top);
  const why = el('ul', 'tight');
  (h.confidence_basis || []).forEach(r => why.append(el('li', null, r)));
  if (why.children.length) box.append(why);
  const ev = (h.evidence || []).filter(e => e.stance === 'supportive' || e.stance === 'contradicting');
  if (ev.length) {
    const det = el('details');
    const sup = ev.filter(e => e.stance === 'supportive').length;
    det.append(el('summary', null,
      `Backing sources: ${sup} supporting, ${ev.length - sup} contradicting`));
    const ul = el('ul', 'tight');
    ev.forEach(e => {
      const li = el('li', e.stance === 'contradicting' ? 'contra' : null);
      li.append(el('span', 'tag', e.stance === 'contradicting' ? 'AGAINST' : 'FOR'), ' ');
      const href = sourceLink(e.ref);
      if (href) {
        const a = el('a', null, e.ref); a.href = href; a.target = '_blank'; a.rel = 'noopener';
        li.append(a, ' — ');
      } else if (e.ref) {
        li.append(el('code', null, e.ref), ' — ');
      }
      li.append(document.createTextNode(e.summary || ''));
      const bits = [e.strength && e.strength !== 'not_assessed' ? e.strength : null,
        e.relevance,
        e.context_match === 'partial_match' ? 'partial context match'
          : e.context_match === 'context_mismatch' ? 'context mismatch' : null].filter(Boolean);
      if (bits.length) li.append(el('span', 'dim', ` (${bits.join(' · ')})`));
      if (e.bearing) li.append(el('div', 'dim', 'Bears on it because: ' + e.bearing));
      ul.append(li);
    });
    det.append(ul); box.append(det);
  }
  return box;
}

function renderHypothesis(selected, all) {
  const host = $('#hypothesis'); if (!host) return;
  host.textContent = '';
  if (!selected) {
    /* "No hypothesis" with no reason is indistinguishable from a fault. Two
       different things can be true here and they need different words: the
       science withheld one, or the run never produced anything to form one
       from. */
    const diag = state.result && state.result.diagnosis;
    if (diag) {
      const box = el('div', 'diag');
      box.append(el('h4', null, diag.headline));
      box.append(el('p', null, diag.why));
      const facts = [];
      if (diag.turns) facts.push(`${diag.turns} orchestrator turn(s)`);
      facts.push((diag.agents_dispatched || []).length
        ? `agents asked: ${diag.agents_dispatched.join(', ')}`
        : 'no specialist agent was asked');
      facts.push((diag.files_written || []).length
        ? `files written: ${diag.files_written.join(', ')}`
        : 'no file written to the run directory');
      box.append(el('p', 'dim mono', facts.join(' · ')));
      if (diag.orchestrator_said) {
        box.append(el('div', 'lab', 'What the orchestrator said last'));
        box.append(el('blockquote', 'said', diag.orchestrator_said));
      }
      (diag.limitations || []).forEach(l => box.append(el('p', 'caveat', l)));
      if ((diag.next_steps || []).length) {
        const ul = el('ul', 'tight');
        diag.next_steps.forEach(n => ul.append(el('li', null, n)));
        box.append(el('div', 'lab', 'What to check'), ul);
      }
      host.append(box);
      return;
    }
    const why = (state.result && state.result.bundle
      && state.result.bundle.hypothesis_withheld_reason)
      || (state.result && state.result.benchmark
        && state.result.benchmark.hypothesis_withheld_reason);
    host.append(el('p', 'dim', why || 'No hypothesis was formed by this run.'));
    return;
  }
  const w = el('div', 'hyp');
  /* The claim level comes first, because it is what tells a reader how much
     weight the sentence under it can carry. A CANDIDATE is a real output —
     most useful hypotheses start there — and not a degraded QUANTIFIED one. */
  const level = selected.claim_level || 'candidate';
  const head = el('div', 'ev-row');
  head.append(el('span', 'claim ' + level,
    { candidate: 'CANDIDATE HYPOTHESIS', quantified: 'QUANTIFIED HYPOTHESIS',
      simulated: 'SIMULATED PREDICTION' }[level] || level.toUpperCase()));
  if (selected.parameter && selected.parameter.registered === false) {
    head.append(el('span', 'flag', 'CANDIDATE PARAMETER — NOT YET REGISTERED'));
  }
  w.append(head);
  w.append(el('h4', null, selected.statement));
  w.append(confidenceBar(selected));
  if (selected.claim_level_reason) {
    w.append(el('p', 'caveat', selected.claim_level_reason));
  }
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

/* The whole derived protocol, every setpoint with its provenance, handed to
   the Simulator — so a scientist can see what the estimate would do in the
   most realistic reactor model there is, labelled for what that model is. */
function simulateProtocol(p) {
  const values = [];
  (p.stages || []).forEach(s => (s.parameters || []).forEach(q => {
    if (q.recommended_value == null || typeof q.recommended_value !== 'number') return;
    values.push({ parameter_id: q.parameter_id, label: q.label, unit: q.unit,
      value: q.recommended_value, provenance: q.provenance, stage: s.stage_id });
  }));
  const proj = p.project || {};
  const payload = {
    kind: 'protocol', project_id: proj.project_id, project_name: proj.name,
    has_simulator: !!(proj.simulator && proj.simulator !== 'none'
      && (proj.simulator.model_id || proj.simulator.status !== 'none')),
    values, gaps: (p.gaps || []).map(g => g.parameter_id),
    from_run: state.run && state.run.run_id, title: p.title,
  };
  try { localStorage.setItem('bs-sim-candidate', JSON.stringify(payload)); } catch (_) {}
  location.href = 'simulator.html';
}

function renderProtocol(p) {
  const host = $('#protocol'); if (!host) return;
  host.textContent = '';
  $('#protocolPanel').hidden = false;
  const sim = el('button', 'btn go', 'Simulate this protocol');
  sim.title = 'Open the Simulator with every recommended setpoint as the candidate';
  sim.addEventListener('click', () => simulateProtocol(p));
  host.append(el('div', 'rv-actions', null));
  host.lastChild.append(sim);

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
      /* A reasoned starting value shows its basis and how sure it is, so it
         reads as a proposal to approve — never as a measured setting. */
      if (q.design_choice) {
        const dc = q.design_choice;
        const box = Object.assign(el('div', 'dim'), { style: 'font-size:12.5px;padding:0 0 6px' });
        box.append(el('span', 'conf ' + dc.confidence, dc.confidence + ' confidence'), ' ');
        box.append(document.createTextNode(q.reason || ''));
        box.append(el('div', null, 'Derived from: ' + (dc.derived_from || []).join('; ')));
        if (dc.would_settle_it) box.append(el('div', null, 'Would settle it: ' + dc.would_settle_it));
        w.append(box);
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
  /* Grouped by what the reader can do about them, restatements folded. The
     first two groups are open; what the machine lacks and the standing rules
     are one click away, because they are true of every run. */
  const groups = p.limitation_groups || [];
  if (groups.length) {
    host.append(el('div', 'lab', 'Limitations'));
    groups.forEach(g => {
      const det = el('details');
      if (g.kind === 'blocks' || g.kind === 'unsettled') det.open = true;
      det.append(el('summary', null, `${g.label} (${g.items.length})`));
      const ul = el('ul', 'tight');
      g.items.forEach(x => ul.append(el('li', null, x)));
      det.append(ul); host.append(det);
    });
  } else if ((p.limitations || []).length) {
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

/* ── creating a project ───────────────────────────────────────────────────
   A project is a durable scientific workspace, so this writes one on the server
   and then selects it. Nothing about a project lives in this page: refreshing,
   restarting the server or redeploying leaves it exactly where it was. */
async function createProject(ev) {
  ev.preventDefault();
  const msg = $('#npMsg');
  const name = $('#npName').value.trim();
  if (name.length < 3) { msg.textContent = 'a project needs a name'; return; }
  msg.textContent = 'creating…';
  const body = {
    name,
    species: $('#npSpecies').value.trim(),
    starting_cell: $('#npStart').value.trim(),
    target_cell: $('#npTarget').value.trim(),
    cell_state: $('#npState').value.trim(),
    process_context: $('#npProcess').value.trim(),
    goal: $('#npGoal').value.trim(),
  };
  const template = $('#npTemplate').value;
  if (template) { body.template_of = template; } else { body.quick = true; }
  try {
    const d = await post('/api/projects', body);
    msg.textContent = '';
    $('#newProjectCard').hidden = true;
    $('#newProjectForm').reset();
    /* Selected immediately, and nothing else disturbed: a run in another
       project keeps working while this one is created. */
    await loadProjects(d.selected || d.project_id);
    await BS.runs(currentProject()).catch(() => {});
  } catch (e) {
    msg.textContent = e.message || 'could not create that project';
  }
}

async function loadProjectTemplates() {
  const sel = $('#npTemplate'); if (!sel) return;
  sel.textContent = '';
  sel.append(Object.assign(el('option', null, 'None — shared bioreactor set only'),
    { value: '' }));
  try {
    const d = await get('/api/project-templates');
    (d.templates || []).forEach(t => {
      const o = el('option', null, `${t.name || t.project_id}`
        + (t.has_simulator ? '  (has a model)' : '  (no model)'));
      o.value = t.project_id; sel.append(o);
    });
  } catch (_) { /* the form still works without templates */ }
}

/* ── wiring ──────────────────────────────────────────────────────────────── */
function wire() {
  const np = $('#newProjectBtn');
  if (np) {
    np.addEventListener('click', () => {
      const card = $('#newProjectCard');
      card.hidden = !card.hidden;
      if (!card.hidden) { loadProjectTemplates(); $('#npName').focus(); }
    });
    $('#npCancel').addEventListener('click', () => { $('#newProjectCard').hidden = true; });
    $('#newProjectForm').addEventListener('submit', createProject);
  }
  const pd = $('#projectDetailBtn');
  if (pd) {
    pd.addEventListener('click', () => {
      const host = $('#projectDetail');
      const open = host.hidden;
      pd.setAttribute('aria-expanded', String(open));
      if (open) renderProjectDetail(currentProject()); else host.hidden = true;
    });
  }
  $('#runBtn').addEventListener('click', start);
  $('#stopBtn').addEventListener('click', stopFromButton);
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
