'use strict';
/* The Runs page: durable history and live monitoring, from the one run object.
 *
 * Nothing here keeps its own idea of a run. It asks the server for the list,
 * groups it, and when a run is opened it renders exactly what the Discovery
 * page renders, with the same component, from the same snapshot.
 */

const $ = s => document.querySelector(s);
const el = RV.el;

const page = { runs: [], projects: {}, filter: null, open: null, detach: null };

async function boot() {
  try { page.identity = await BS.identity(); } catch (_) { /* a view is not the model */ }
  renderIdentity();
  mountSystemCheck();
  mountRunBeacon();
  BS.onChange(s => { page.runs = s.runs; page.projects = s.projects; render(); });
  // A link from a project (runs.html?project=<id>) opens on that project's runs.
  const asked = new URL(location.href).searchParams.get('project');
  if (asked) BS.selected.set(asked);
  page.filter = asked || BS.selected.get();
  await load();
  const want = new URLSearchParams(location.search).get('run');
  if (want) openRun(want);
  $('#projectFilter').addEventListener('change', async e => {
    page.filter = e.target.value || null;
    BS.selected.set(page.filter);
    await load();
  });
}

function renderIdentity() {
  const box = $('#identity'); if (!box || !page.identity) return;
  box.textContent = '';
  if (page.identity.is_admin) box.append(el('span', 'rt-badge admin', 'ADMIN'));
  if (page.identity.authenticated) box.append(el('span', 'mono dim', page.identity.display_name));
}

async function load() {
  const d = await BS.runs(page.filter);
  page.runs = d.runs; page.projects = d.projects;
  render();
}

function render() {
  const sel = $('#projectFilter');
  const current = sel.value;
  sel.textContent = '';
  const all = el('option', null, 'All projects'); all.value = '';
  sel.append(all);
  Object.keys(page.projects).sort().forEach(pid => {
    const c = page.projects[pid];
    const o = el('option', null, `${pid}${c.active ? `  ● ${c.active} active` : ''}`);
    o.value = pid;
    sel.append(o);
  });
  sel.value = page.filter || current || '';

  const counts = $('#counts');
  const live = page.runs.filter(r => r.group === 'active').length;
  counts.textContent = `${page.runs.length} run${page.runs.length === 1 ? '' : 's'}`
    + (live ? ` · ${live} active` : '');

  const host = $('#runs');
  host.textContent = '';
  const groups = [['active', 'Active runs'], ['complete', 'Completed'],
    ['failed', 'Failed'], ['cancelled', 'Cancelled']];
  let shown = 0;
  groups.forEach(([key, label]) => {
    const rows = page.runs.filter(r => r.group === key);
    if (!rows.length) return;
    shown += rows.length;
    const g = el('div', 'grp');
    const h = el('h3', null, label);
    h.append(el('span', 'n', String(rows.length)));
    g.append(h);
    rows.forEach(r => g.append(runRow(r)));
    host.append(g);
  });
  if (!shown) {
    host.append(el('p', 'empty', page.filter
      ? 'No runs in this project yet.'
      : 'No runs yet. Start one from AI Discovery — it keeps going whether or not this page '
        + 'is open.'));
  }
}

function runRow(r) {
  const card = el('div', 'run');
  card.append(el('div', 'obj', r.objective || '(no objective recorded)'));
  const right = el('div', 'right');
  right.append(el('span', 'rv-status ' + (r.group === 'active' ? 'go'
    : r.group === 'complete' ? 'ok' : r.group === 'cancelled' ? 'warn' : 'bad'),
  BS.fmt.status(r)));
  right.append(el('span', 'dim mono', BS.fmt.elapsed(r.started_at, r.finished_at)));
  card.append(right);
  const meta = el('div', 'meta');
  [[r.project_id, 'project'], [r.runtime_label, null],
    [r.engine ? (r.engine.includes('codex') ? 'Codex' : 'Claude') : null, 'engine'],
    [r.current_stage, 'stage'],
    [(r.active_agents || []).join(', ') || null, 'agent'],
    [r.started_at ? new Date(r.started_at * 1000).toLocaleString() : null, null],
    [r.stage_counts ? `${r.stage_counts.done}/${r.stage_counts.total} stages` : null, null],
    [r.benchmark ? 'benchmark built' : null, null],
  ].forEach(([v, k]) => { if (v) meta.append(el('span', null, k ? `${k}: ${v}` : v)); });
  card.append(meta);
  if (r.hypothesis) card.append(el('div', 'hyp', r.hypothesis));
  card.addEventListener('click', () => openRun(r.run_id));
  return card;
}

/* Opening a run shows the same scientific state the Discovery page shows,
   rendered by the same component from the same snapshot. */
function openRun(runId) {
  if (page.detach) page.detach();
  page.open = runId;
  $('#detailCard').hidden = false;
  const host = $('#detail');
  host.textContent = '';
  const head = el('div', 'rv-head'), stages = el('div'), agents = el('div', 'agents');
  const tl = el('div', 'tlbox'), lims = el('div', 'lims');
  host.append(head, el('div', 'lab', 'Workflow'), stages, el('div', 'lab', 'Agents'), agents,
    el('div', 'lab', 'Activity'), tl, lims);
  const url = new URL(location.href);
  url.searchParams.set('run', runId);
  history.replaceState(null, '', url);
  page.detach = RV.attach(runId, {
    snapshot(snap) {
      $('#detailTitle').textContent = snap.objective || 'Run';
      $('#detailHint').textContent = `${snap.project_id || ''} · ${snap.run_id}`;
      RV.header(head, snap, { onStop: stop, onExtend: extend,
                              onPause: pause, onContinue: cont });
      RV.stages(stages, snap);
      RV.agents(agents, snap);
      RV.timeline(tl, snap);
      RV.limitations(lims, snap);
      if (!RV.isLive(snap)) load().catch(() => {});
    },
    error() {
      host.textContent = '';
      host.append(el('p', 'dim', 'That run is not available to this account.'));
    },
  });
}

async function extend(runId) {
  try { await BS.json(`/api/discovery/${runId}/extend`, { method: 'POST' }); }
  catch (e) { alert(e.message || 'that run cannot be extended'); }
}

async function stop(runId) {
  try { await BS.json(`/api/discovery/${runId}/cancel`, { method: 'POST' }); }
  catch (_) { /* the snapshot will say what happened */ }
}

async function pause(runId) {
  try { await BS.json(`/api/discovery/${runId}/pause`, { method: 'POST' }); }
  catch (e) { alert(e.message || 'that run cannot pause'); }
}

/* A paused run resumes in place. An ended one continues as a fresh run seeded
   with its artifacts, and the detail view follows the new run. */
async function cont(runId) {
  try {
    const d = await BS.json(`/api/discovery/${runId}/continue`, { method: 'POST' });
    if (d && d.run_id && d.run_id !== runId) { await load(); openRun(d.run_id); }
  } catch (e) { alert(e.message || 'that run cannot be continued'); }
}

boot();

/* The system check. Shown only when the server says this caller may run it;
   the page never decides that itself. */
async function mountSystemCheck() {
  let st;
  try { st = await BS.json('/api/admin/selfcheck'); } catch (_) { return; }
  $('#syscheckCard').hidden = false;
  $('#syscheckRun').addEventListener('click', () => startCheck(false));
  $('#syscheckLive').addEventListener('click', () => {
    if (confirm('Run the live check?\n\nIt starts a real agent session: the orchestrator '
      + 'asks the bioinformatics agent to run one tool inside its sandbox. It spends a '
      + 'small amount of model credit.')) startCheck(true);
  });
  renderCheck(st);
  if (st.state === 'running') pollCheck();
}

async function startCheck(live) {
  try { renderCheck(await BS.json('/api/admin/selfcheck', { method: 'POST',
    body: JSON.stringify({ live }), headers: { 'Content-Type': 'application/json' } })); }
  catch (e) { $('#syscheckState').textContent = e.message || String(e); return; }
  pollCheck();
}

function pollCheck() {
  const t = setInterval(async () => {
    let st; try { st = await BS.json('/api/admin/selfcheck'); } catch (_) { return; }
    renderCheck(st);
    if (st.state !== 'running') clearInterval(t);
  }, 2000);
}

function renderCheck(st) {
  const busy = st.state === 'running';
  $('#syscheckRun').disabled = busy; $('#syscheckLive').disabled = busy;
  const r = st.report;
  $('#syscheckState').textContent = busy ? `running${st.live ? ' (live)' : ''}…`
    : st.error ? `failed to run: ${st.error}`
    : r ? `${r.ok ? 'PASS' : 'FAIL'} · ${BS.fmt.ago(st.finished_at)}${r.live ? ' · live' : ''}`
    : 'not run yet';
  const box = $('#syscheckRows'); box.textContent = '';
  if (!r) return;
  const mark = { ok: '✓', warn: '!', fail: '✕', skip: '·' };
  const cls = { ok: 'ok', warn: 'warn', fail: 'bad', skip: 'dim' };
  r.rows.forEach(row => {
    const line = el('div', 'syscheck-row');
    line.append(el('span', 'syscheck-mark ' + (cls[row.status] || ''), mark[row.status] || '?'));
    line.append(el('b', null, row.label));
    line.append(el('span', 'dim', row.detail));
    box.append(line);
  });
}
