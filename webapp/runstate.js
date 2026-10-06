'use strict';
/* The one place any page learns about runs, projects and who is asking.
 *
 * Before this, the Discovery page and the Runs page each kept their own idea of
 * what a run was, and they could disagree — one showing RUNNING while the other
 * showed nothing, because one was reading a different endpoint. There is now a
 * single authoritative object per run on the server, and this file is the single
 * client for it. Every page includes it; no page keeps its own run state.
 *
 * Two rules it exists to hold:
 *
 * 1. The browser observes; it does not own. Starting a run creates it on the
 *    server, and the server keeps it going whether or not a page is open. A
 *    reload re-attaches to the same run by id — it never starts a second one.
 * 2. Nothing is rendered from this file. It fetches, caches briefly, and hands
 *    back exactly what the server said, so a page cannot invent a status.
 */

const BS = (() => {
  const listeners = new Set();
  const state = { identity: null, runs: [], counts: {}, projects: {}, at: 0 };
  let timer = null;

  async function json(path, opts) {
    const r = await fetch(path, opts);
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw Object.assign(new Error(d.error || `${path} answered ${r.status}`), d);
    return d;
  }

  async function identity() {
    state.identity = await json('/api/identity');
    return state.identity;
  }

  /* The authoritative list: live runs and durable ones, already merged and
     already scoped to whoever is asking. */
  async function runs(projectId) {
    const q = projectId ? `?project_id=${encodeURIComponent(projectId)}` : '';
    const d = await json('/api/discovery' + q);
    state.runs = d.runs || [];
    state.counts = d.counts || {};
    state.projects = d.projects || {};
    state.at = Date.now();
    listeners.forEach(fn => { try { fn(state); } catch (_) { /* a view is not the model */ } });
    return d;
  }

  function onChange(fn) { listeners.add(fn); return () => listeners.delete(fn); }

  /* A slow poll, for the pages that are not watching a specific run. The live
     stream is SSE; this is only so a global indicator stays roughly true. */
  function watch(everyMs = 7000) {
    if (timer) clearInterval(timer);
    runs().catch(() => {});
    timer = setInterval(() => runs().catch(() => {}), everyMs);
    return () => clearInterval(timer);
  }

  const fmt = {
    elapsed(from, to) {
      if (!from) return '—';
      const s = Math.max(0, Math.round(((to || Date.now() / 1000) - from)));
      const m = Math.floor(s / 60), h = Math.floor(m / 60);
      return h ? `${h}:${String(m % 60).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`
        : `${m}:${String(s % 60).padStart(2, '0')}`;
    },
    ago(at) {
      if (!at) return 'never';
      const s = Math.max(0, Math.round(Date.now() / 1000 - at));
      if (s < 60) return `${s} sec ago`;
      if (s < 3600) return `${Math.floor(s / 60)} min ago`;
      return `${Math.floor(s / 3600)} h ago`;
    },
    clock(at) {
      if (!at) return '';
      const d = new Date(at * 1000);
      return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
    },
    status(row) {
      const s = row.status;
      return ({ queued: 'QUEUED', running: 'RUNNING', finalizing: 'FINALIZING RESULTS',
        done: 'COMPLETE', stopped: 'CANCELLED', interrupted: 'INTERRUPTED',
        refused: 'REFUSED', unavailable: 'UNAVAILABLE', error: 'FAILED' })[s]
        || (s || '').toUpperCase();
    },
  };

  /* The project the person last chose. A preference, not a record: the projects
     themselves live on the server, and losing this picks the first one again. */
  const SELECTED = 'bs-project';
  const selected = {
    get() { try { return localStorage.getItem(SELECTED); } catch (_) { return null; } },
    set(v) { try { v ? localStorage.setItem(SELECTED, v) : localStorage.removeItem(SELECTED); }
      catch (_) { /* private mode */ } },
  };

  return { json, identity, runs, onChange, watch, state, fmt, selected };
})();

/* The global indicator: how many of your runs are working, on every page, with
   a link to where they are. Mounted into any element with id "runbeacon". */
function mountRunBeacon(host) {
  host = host || document.getElementById('runbeacon');
  if (!host) return;
  const paint = s => {
    const live = (s.runs || []).filter(r => r.group === 'active');
    host.textContent = '';
    if (!live.length) { host.hidden = true; return; }
    host.hidden = false;
    const a = document.createElement('a');
    a.className = 'beacon';
    a.href = 'runs.html';
    const dot = document.createElement('span'); dot.className = 'pulse';
    a.append(dot);
    const one = live.length === 1 ? live[0] : null;
    a.append(document.createTextNode(
      one ? `${(one.active_agents || [])[0] || 'AI run'} · ${BS.fmt.elapsed(one.started_at)}`
        : `${live.length} AI RUNS ACTIVE`));
    a.title = live.map(r => `${r.project_id}: ${BS.fmt.status(r)}`).join('\n');
    host.append(a);
  };
  BS.onChange(paint);
  paint(BS.state);
  BS.watch();
}
