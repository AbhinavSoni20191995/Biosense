'use strict';
/* How a run looks, wherever it is looked at.
 *
 * The Discovery page and the Runs page render the same run from the same
 * snapshot with the same code. That is the point: two renderers would be two
 * chances to disagree about whether something is still working.
 *
 * What this shows, and why each piece is here rather than a spinner:
 *
 *   status + engine    what kind of answer this will be, and what is producing it
 *   activity bar       indeterminate on purpose. It means THE SYSTEM IS ACTIVE.
 *                      No percentage is computed, because the remaining work is
 *                      not proportional to the remaining stages and a bar that
 *                      implies it would be a lie told once a second.
 *   elapsed / last     the two numbers that answer "is it stuck?"
 *   stages             the deterministic workflow, as a count, never a percent
 *   agents             which specialist is working and on what
 *   timeline           what actually happened, with the clock time it happened
 *   limitations        refusals kept in front of the reader instead of in a log
 */

const RV = (() => {
  const el = (t, c, x) => { const n = document.createElement(t); if (c) n.className = c;
    if (x != null) n.textContent = x; return n; };

  const LIVE = new Set(['queued', 'running', 'finalizing']);
  const STATUS_CLASS = { queued: 'q', running: 'go', finalizing: 'go', done: 'ok',
    stopped: 'warn', interrupted: 'warn', error: 'bad', refused: 'bad', unavailable: 'bad' };

  function isLive(snap) { return LIVE.has(snap && snap.status); }

  /* A results box survives the header being redrawn on the next snapshot:
     what a person typed is theirs until they send it. */
  const followDrafts = {};

  function lineage(snap) {
    const c = snap.continued_from;
    if (!c || !c.run_id) return null;
    if (c.kind !== 'follow_up') return `Continues run ${c.run_id}`;
    const chain = (c.chain || []).map(x => `round ${x.round} (${x.run_id})`).join(' → ');
    return `Round ${c.round || 2} · connected runs: ${chain || 'run ' + c.run_id}`;
  }

  /* The next round: the person ran the experiment and brings the results back.
     A fresh run starts from everything this one wrote and reads the results
     against the round plan it fixed before they existed. */
  function followUpForm(snap, onFollowUp) {
    const d = followDrafts[snap.run_id] || (followDrafts[snap.run_id] = { open: false, text: '', ds: '' });
    const wrap = el('div', 'rv-follow');
    const round = ((snap.continued_from || {}).kind === 'follow_up'
      ? (snap.continued_from.round || 2) : 1) + 1;
    const toggle = el('button', 'btn more', d.open ? 'Hide' : `Follow up with results (round ${round})`);
    toggle.title = 'You ran this run’s round plan (or another experiment): bring the results '
      + 'back, and a new run starts from everything this one wrote.';
    toggle.addEventListener('click', () => {
      d.open = !d.open; toggle.textContent = d.open ? 'Hide' : `Follow up with results (round ${round})`;
      box.hidden = !d.open;
    });
    const box = el('div', 'rv-follow-box');
    box.hidden = !d.open;
    const measured = ((snap.measurements || {}).count || 0);
    if (measured) box.append(el('p', null, `${measured} value(s) recorded against this run's round `
      + 'plan go with the follow-up; anything you add below goes too.'));
    box.append(el('p', 'dim', 'What did you measure? Per arm, the readouts and their values, '
      + 'with n. Numbers you type are your report of this process; for a full table, register '
      + 'it on the Data page and name its id below so the tools analyse it.'));
    const ta = el('textarea');
    ta.rows = 5; ta.maxLength = 4000; ta.value = d.text;
    ta.placeholder = 'e.g. A0 control: CD206+ 31% (n=3). A2 GM-CSF 50 ng/mL: CD206+ 52% (n=3); '
      + 'viable cells per input iPSC unchanged.';
    ta.addEventListener('input', () => { d.text = ta.value; });
    const ds = el('input');
    ds.type = 'text'; ds.value = d.ds; ds.placeholder = 'results dataset id(s), comma-separated (optional)';
    ds.addEventListener('input', () => { d.ds = ds.value; });
    const go = el('button', 'btn go', `Start round ${round}`);
    const msg = el('span', 'dim');
    go.addEventListener('click', async () => {
      const ids = d.ds.split(',').map(s => s.trim()).filter(Boolean);
      if (!d.text.trim() && !ids.length && !measured) {
        msg.textContent = ' Enter results against the round plan, describe them, or name a dataset.'; return;
      }
      go.disabled = true; go.textContent = 'starting…';
      try {
        await onFollowUp(snap.run_id, { results: d.text.trim(), dataset_ids: ids });
        delete followDrafts[snap.run_id];
      } catch (e) {
        go.disabled = false; go.textContent = `Start round ${round}`;
        msg.textContent = ' ' + (e.message || e);
      }
    });
    const row = el('div', 'rv-actions'); row.append(go, msg);
    box.append(ta, ds, row);
    wrap.append(toggle, box);
    return wrap;
  }

  /* The header: what it is, what it is doing, and whether it is alive. */
  function header(host, snap, opts) {
    opts = opts || {};
    host.textContent = '';
    const top = el('div', 'rv-top');
    const badge = el('span', 'rt-badge ' + (snap.runtime_mode || ''), '');
    badge.append(el('span', 'dot'), document.createTextNode(snap.runtime_label || ''));
    top.append(badge);
    if (snap.engine) top.append(el('span', 'rv-engine', engineLabel(snap)));
    top.append(el('span', 'rv-status ' + (STATUS_CLASS[snap.status] || ''), statusText(snap)));
    host.append(top);
    const from = lineage(snap);
    if (from) host.append(el('p', 'rv-note', from));

    if (isLive(snap)) {
      const bar = el('div', 'rv-bar');
      bar.append(el('span', 'rv-bar-run'));
      bar.setAttribute('role', 'progressbar');
      bar.setAttribute('aria-label', 'The system is still active');
      host.append(bar);
    }

    const act = (snap.activity || {});
    const stats = el('div', 'rv-stats');
    stats.append(stat('Elapsed', BS.fmt.elapsed(snap.started_at, snap.finished_at)));
    if (isLive(snap)) stats.append(stat('Last activity', BS.fmt.ago(act.last_activity_at)));
    const sc = snap.stage_counts || {};
    if (sc.total) stats.append(stat('Stages', `${sc.done} / ${sc.total}`));
    if ((act.active_agents || []).length) {
      stats.append(stat('Working', act.active_agents.join(', ')));
    }
    if (snap.paused) {
      stats.append(stat('Paused', 'waiting for you'));
    } else if (snap.deadline_in_s) {
      const left = Math.max(0, snap.deadline_in_s);
      /* An operator's run pauses at its limit and asks; it does not stop. */
      stats.append(stat(snap.pauses_at_limit ? 'Pauses in' : 'Stops in', left < 10 * 60
        ? `${Math.floor(left / 60)}:${String(Math.round(left % 60)).padStart(2, '0')}`
        : `${Math.round(left / 60)} min`));
    }
    host.append(stats);

    /* The time limit is a bill cap, not a verdict. Near it the orchestrator is
       told to write what it has, and the person may buy it more time — a
       bounded choice shown exactly when it matters, never taken for them. */
    /* Paused at the limit: the agents are stopped and nothing is spent until
       the person chooses. Never decided for them, but not held for ever. */
    if (isLive(snap) && snap.paused) {
      const box = el('div', 'rv-pause');
      const mins = Math.round((snap.extension_s || 600) / 60);
      const hold = snap.pause_hold_s != null ? Math.max(0, Math.round(snap.pause_hold_s / 60)) : null;
      const byPerson = snap.pause_reason === 'person';
      if (byPerson) {
        box.append(el('b', null, 'Paused by you.'));
        box.append(el('p', null, 'The agents are held and the clock is stopped; the run keeps '
          + 'the time it had left. Continue when you are ready, or finish with what they have '
          + 'written so far (shown as a partial result).'
          + (hold != null ? ` If nobody chooses, it finishes in about ${hold} min.` : '')));
      } else {
        box.append(el('b', null, 'Paused at the time limit.'));
        box.append(el('p', null, 'The agents are stopped; nothing is being spent. Continue to give '
          + `them ${mins} more minutes from where they left off, or finish with what they have `
          + 'written so far (shown as a partial result).'
          + (hold != null ? ` If nobody chooses, it finishes in about ${hold} min.` : '')));
      }
      const row = el('div', 'rv-actions');
      const resume = byPerson ? opts.onContinue : (snap.extendable ? opts.onExtend : null);
      if (resume) {
        const go = el('button', 'btn go',
          byPerson ? 'Continue run' : `Continue (${mins} more minutes)`);
        go.addEventListener('click', () => {
          go.disabled = true; go.textContent = 'continuing…'; resume(snap.run_id);
        });
        row.append(go);
      }
      if (opts.onStop) {
        const end = el('button', 'btn stop', 'Finish with what it has');
        end.addEventListener('click', () => {
          end.disabled = true; end.textContent = 'finishing…'; opts.onStop(snap.run_id);
        });
        row.append(end);
      }
      box.append(row);
      host.append(box);
    } else if (isLive(snap) && snap.wrap_up_sent) {
      host.append(el('p', 'rv-note', 'Time is nearly up. The orchestrator has been told to '
        + 'write the hypothesis with what it already has.'));
    }

    if (snap.status === 'finalizing') {
      host.append(el('p', 'rv-note', 'The agents have finished. BioSense is collecting the '
        + 'files they wrote, validating them and building the result — a run is not complete '
        + 'until that is done.'));
    }
    if (opts.onStop && isLive(snap)) {
      const stop = el('button', 'btn', 'Stop run');
      stop.addEventListener('click', () => {
        if (!confirm('Stop this run?\n\nThe agents are interrupted. Whatever they have already '
          + 'written is kept, and the run is recorded as cancelled — not as an answer.')) return;
        stop.disabled = true; stop.textContent = 'stopping…';
        opts.onStop(snap.run_id);
      });
      const row = el('div', 'rv-actions'); row.append(stop);
      /* Offered for the whole run, not only at the end: the person watching
         can see the agents are mid-work long before the countdown says so. */
      if (opts.onExtend && snap.extendable && !snap.paused) {
        const mins = Math.round((snap.extension_s || 600) / 60);
        const more = el('button', 'btn more', `Give it ${mins} more minutes`
          + (snap.extensions_left != null ? ` (${snap.extensions_left} left)` : ''));
        more.title = 'Pushes the time limit out and tells the orchestrator it has more time.';
        more.addEventListener('click', () => {
          more.disabled = true; more.textContent = 'extending…';
          opts.onExtend(snap.run_id);
        });
        row.append(more);
      }
      if (opts.onPause && snap.pausable) {
        const hold = el('button', 'btn more', 'Pause run');
        hold.title = 'Holds the agents and stops the clock. Continue later with the time '
          + 'the run has left.';
        hold.addEventListener('click', () => {
          hold.disabled = true; hold.textContent = 'pausing…'; opts.onPause(snap.run_id);
        });
        row.append(hold);
      }
      host.append(row);
    }
    /* A stopped or interrupted run is not a dead end: it can continue as a
       fresh run seeded with everything this one wrote. */
    if (!isLive(snap) && snap.continuable && opts.onContinue) {
      const row = el('div', 'rv-actions');
      const go = el('button', 'btn more', 'Continue this run');
      go.title = 'Starts a fresh run seeded with everything this one wrote; the agents '
        + 'read it first instead of redoing it.';
      go.addEventListener('click', () => {
        go.disabled = true; go.textContent = 'continuing…'; opts.onContinue(snap.run_id);
      });
      row.append(go);
      host.append(row);
    }
    if (!isLive(snap) && snap.followable && opts.onFollowUp) {
      host.append(followUpForm(snap, opts.onFollowUp));
    }
  }

  function engineLabel(snap) {
    const e = (snap.engine || '').toLowerCase();
    const name = e.includes('codex') ? 'Codex' : e.includes('claude') ? 'Claude' : snap.engine;
    return snap.model ? `${name} · ${snap.model}` : `AI engine: ${name}`;
  }

  function statusText(snap) {
    if (snap.status === 'running' && (snap.activity || {}).active_agents
      && snap.activity.active_agents.length) {
      return 'WAITING ON AGENT';
    }
    return BS.fmt.status(snap);
  }

  function stat(label, value) {
    const d = el('div', 'rv-stat');
    d.append(el('div', 'k', label), el('div', 'v', value));
    return d;
  }

  /* The deterministic workflow. A count, never a percentage. */
  function stages(host, snap) {
    host.textContent = '';
    const rows = (snap.progress || []);
    if (!rows.length) { host.append(el('p', 'dim', 'Not started.')); return; }
    const ul = el('ul', 'stages');
    rows.forEach(r => {
      const li = el('li', 'stg ' + r.status);
      li.dataset.stage = r.stage;
      li.append(el('span', 'tick', r.status === 'done' ? '✓'
        : r.status === 'current' ? '●' : '○'));
      const d = el('div', null);
      d.append(el('div', 't', r.label));
      if (r.status === 'current') d.append(el('div', 'b', r.blurb));
      li.append(d);
      ul.append(li);
    });
    host.append(ul);
  }

  /* Who is working. Operational state only — never the model's reasoning. */
  function agents(host, snap) {
    host.textContent = '';
    const rows = ((snap.activity || {}).agents || []);
    if (!rows.length) { host.append(el('p', 'dim', 'No agent activity yet.')); return; }
    rows.forEach(a => {
      const row = el('div', 'agent ' + a.status);
      const mark = { running: '●', complete: '✓', failed: '✕', cancelled: '✕' }[a.status] || '○';
      row.append(el('span', 'tick', mark));
      const d = el('div', null);
      d.append(el('div', 't', a.label));
      const st = { queued: 'Waiting', running: 'Running', complete: 'Complete',
        failed: 'Failed', cancelled: 'Cancelled' }[a.status] || a.status;
      d.append(el('div', 's', st));
      if (a.task) d.append(el('div', 'b', a.task));
      row.append(d);
      host.append(row);
    });
  }

  /* What happened, with the time it happened. */
  function timeline(host, snap) {
    host.textContent = '';
    const rows = ((snap.activity || {}).timeline || []);
    if (!rows.length) { host.append(el('p', 'dim', 'Nothing yet.')); return; }
    rows.slice(-60).forEach(t => {
      const row = el('div', 'tl ' + (t.kind || ''));
      row.append(el('span', 'at', BS.fmt.clock(t.at) || `${Math.round(t.rel_s)}s`));
      row.append(el('span', 'tx', t.text));
      host.append(row);
    });
    host.scrollTop = host.scrollHeight;
  }

  /* Refusals are findings. "The metadata join is unsupported" is a fact about
     the data, and it belongs in front of a scientist, not in a log file. */
  function limitations(host, snap) {
    host.textContent = '';
    const rows = ((snap.activity || {}).limitations || []);
    if (!rows.length) { host.hidden = true; return; }
    host.hidden = false;
    rows.slice(-12).forEach(l => {
      const row = el('div', 'lim');
      row.append(el('span', 'flag', l.tool ? 'TOOL REFUSAL' : 'ANALYSIS LIMITATION'));
      row.append(el('div', 'tx', l.text));
      host.append(row);
    });
  }

  /* Watch one run: snapshot first, then the live stream from where it left off.
     Reconnecting asks for events after the last sequence number it saw, so a
     dropped connection costs nothing but the gap. */
  function attach(runId, handlers) {
    let es = null, stopped = false, lastSeq = -1;
    const on = handlers || {};
    async function snapshot() {
      const snap = await BS.json(`/api/discovery/${runId}`);
      lastSeq = (snap.events || []).reduce((m, e) => Math.max(m, e.seq), lastSeq);
      if (on.snapshot) on.snapshot(snap);
      return snap;
    }
    function stream() {
      if (stopped) return;
      if (es) es.close();
      es = new EventSource(`/api/discovery/${runId}/events?after=${lastSeq}`);
      es.onmessage = ev => {
        try {
          const e = JSON.parse(ev.data);
          lastSeq = Math.max(lastSeq, e.seq);
          if (on.event) on.event(e);
        } catch (_) { /* a malformed frame is not fatal */ }
      };
      es.addEventListener('closed', () => { es.close(); es = null; snapshot().catch(() => {}); });
      es.onerror = () => {
        /* SSE is observation and never execution: the run is unaffected by this
           connection dying. Re-snapshot, then try the stream again. */
        if (es) { es.close(); es = null; }
        if (!stopped) setTimeout(() => snapshot().then(s => { if (isLive(s)) stream(); })
          .catch(() => { if (!stopped) setTimeout(stream, 4000); }), 1500);
      };
    }
    snapshot().then(s => { if (isLive(s)) stream(); }).catch(e => on.error && on.error(e));
    const poll = setInterval(() => { if (!stopped) snapshot().catch(() => {}); }, 10000);
    return () => { stopped = true; clearInterval(poll); if (es) es.close(); };
  }

  return { header, stages, agents, timeline, limitations, attach, isLive, statusText, el };
})();
