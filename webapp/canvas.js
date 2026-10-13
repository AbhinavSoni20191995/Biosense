'use strict';
/* The run page's boxes, arranged by the person reading them.
 *
 * The page is two columns of cards in the order a person reads a run once.
 * Working with one is different: the round plan beside the protocol, the
 * activity feed small in a corner, the benchmarks out of the way. So every
 * box carries its own controls, and there are two ways to lay them out:
 *
 *   in the page  — a box can be detached into a floating window (and docked
 *                  back), or minimised to its title, while the rest stay in
 *                  the reading order;
 *   as a canvas  — every box is free: dragged by its title, widened from
 *                  either side, heightened from its bottom, minimised.
 *
 * A minimised box is its title as a translucent strip; clicking the strip
 * brings the box back. Nothing is ever hidden where it cannot be found.
 *
 * The arrangement is remembered in this browser for every run — a per-viewer
 * convenience, so browser storage, guarded, and nothing breaks without it.
 * Nothing here renders content: the cards are the page's own, so a box the
 * run fills later is placed where there is room, and leaving the canvas puts
 * every box back in the reading order.
 */
(function () {
  const LAYOUT_KEY = 'bs-canvas-layout-v2';
  const OLD_KEY = 'bs-canvas-layout-v1';
  const ON_KEY = 'bs-canvas-on';
  const SNAP = 8, GAP = 16, MIN_W = 260, MIN_H = 90, KEEP = 80;
  const NARROW = window.matchMedia('(max-width: 900px)');
  const cols = document.querySelector('.cols');
  if (!cols) return;

  const read = (k, dflt) => { try { const v = localStorage.getItem(k); return v == null ? dflt : JSON.parse(v); } catch (_) { return dflt; } };
  const write = (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch (_) { /* private mode */ } };

  let layout = read(LAYOUT_KEY, null);
  if (!layout) {
    // The first canvas kept positions and "set aside"; a set-aside box is
    // now a minimised one, so nobody's arrangement is lost.
    const old = read(OLD_KEY, {}) || {};
    layout = {};
    Object.entries(old).forEach(([id, b]) => {
      layout[id] = { canvas: { x: b.x, y: b.y, w: b.w, h: b.h }, min: !!b.aside, z: b.z };
    });
  }
  let on = false, zTop = 10, observer = null, resizeObs = null;

  const snap = v => Math.round(v / SNAP) * SNAP;
  const titleOf = c => ((c.querySelector('.head h3') || {}).textContent || 'this box').trim();
  const cards = () => Array.from(cols.querySelectorAll(':scope > .stack > section.card'));
  const isFloat = c => c.classList.contains('cv-float');
  const isFree = c => on || isFloat(c);

  const ids = new Map();
  function idOf(c) {
    if (ids.has(c)) return ids.get(c);
    let id = c.id || 'cv-' + titleOf(c).toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
    let n = 2; const base = id;
    while ([...ids.values()].includes(id)) id = `${base}-${n++}`;
    ids.set(c, id);
    return id;
  }
  const entry = c => (layout[idOf(c)] = layout[idOf(c)] || {});
  const save = () => write(LAYOUT_KEY, layout);

  /* ── the toolbar ─────────────────────────────────────────────────── */
  const bar = document.createElement('div');
  bar.className = 'cv-bar';
  const toggle = button('btn sm', '');
  const reset = button('btn sm', 'Reset layout',
    'Put every box back where the page puts it, open, docked, at its own size');
  const openAll = button('btn sm', '', 'Open every minimised box');
  const hint = document.createElement('span');
  hint.className = 'hint';
  bar.append(toggle, reset, openAll, hint);
  cols.parentNode.insertBefore(bar, cols);

  function button(cls, text, title) {
    const b = document.createElement('button');
    b.type = 'button'; b.className = cls; b.textContent = text;
    if (title) b.title = title;
    return b;
  }

  function paintBar() {
    toggle.textContent = on ? 'Back to the page layout' : 'Arrange as a canvas';
    toggle.setAttribute('aria-pressed', String(on));
    toggle.title = on ? 'Put every box back in the reading order (your canvas is kept)'
      : 'Free every box: move, widen and minimise them; the layout is remembered here';
    const mins = cards().filter(c => c.classList.contains('cv-min')).length;
    openAll.hidden = !mins;
    openAll.textContent = `Open ${mins} minimised`;
    hint.textContent = on
      ? 'Drag a box by its title. Widen it from either side, heighten it from its bottom. '
        + '– minimises it to its title; click the title to bring it back.'
      : '⇱ detaches a box into a window you can move and widen; – minimises it to its title.';
  }

  /* ── each box's controls and handles ─────────────────────────────── */
  const OWN = n => n.nodeType === 1 && (n.classList.contains('head') || n.classList.contains('cv-body')
    || n.classList.contains('cv-ctl') || n.classList.contains('cv-h'));
  function dress(c) {
    const head = c.querySelector(':scope > .head');
    if (!head || c.querySelector(':scope > .cv-ctl')) return;
    // Everything under the title goes in one body. In the page it lays out as
    // if it were not there (display: contents); in a free box it is what
    // scrolls, so the title — the grip — the controls and the edges stay put.
    const body = document.createElement('div');
    body.className = 'cv-body';
    [...c.childNodes].forEach(n => { if (n !== head) body.append(n); });
    c.append(body);
    // Whatever the page adds to a box later belongs in its body too.
    new MutationObserver(muts => muts.forEach(m => m.addedNodes.forEach(n => {
      if (!OWN(n) && n.parentNode === c) body.append(n);
    }))).observe(c, { childList: true });
    const ctl = document.createElement('span');
    ctl.className = 'cv-ctl';
    const det = button('cv-b cv-det', '⇱');
    const min = button('cv-b cv-mn', '–');
    ctl.append(det, min);
    c.append(ctl);
    det.addEventListener('click', e => { e.stopPropagation(); isFloat(c) ? dock(c) : detach(c); });
    min.addEventListener('click', e => {
      e.stopPropagation();
      c.classList.contains('cv-min') ? restore(c) : minimise(c);
    });
    ['e', 'w', 's', 'se', 'sw'].forEach(k => {
      const h = document.createElement('span');
      h.className = 'cv-h cv-h-' + k;
      h.dataset.edge = k;
      c.append(h);
    });
    paintControls(c);
  }
  function paintControls(c) {
    const det = c.querySelector('.cv-det'), min = c.querySelector('.cv-mn');
    if (!det) return;
    det.hidden = on || NARROW.matches;
    det.textContent = isFloat(c) ? '⇲' : '⇱';
    det.title = isFloat(c) ? 'Dock this box back into the page' : 'Detach this box into a window';
    det.setAttribute('aria-label', (isFloat(c) ? 'Dock: ' : 'Detach: ') + titleOf(c));
    const m = c.classList.contains('cv-min');
    min.textContent = m ? '▢' : '–';
    min.title = m ? 'Open this box' : 'Minimise this box to its title';
    min.setAttribute('aria-label', (m ? 'Open: ' : 'Minimise: ') + titleOf(c));
    c.classList.toggle('cv-free', isFree(c));
  }

  /* ── geometry ────────────────────────────────────────────────────── */
  function apply(c, g) {
    c.style.left = g.x + 'px';
    c.style.top = g.y + 'px';
    c.style.width = g.w ? g.w + 'px' : '';
    c.style.height = g.h && !c.classList.contains('cv-min') ? g.h + 'px' : '';
  }
  function clearGeometry(c) {
    ['left', 'top', 'width', 'height', 'zIndex'].forEach(p => { c.style[p] = ''; });
  }
  function front(c) {
    c.style.zIndex = ++zTop;
    entry(c).z = zTop;
  }
  /* What a box is now, in the frame it lives in: the canvas, or the window. */
  function current(c) {
    if (isFloat(c)) {
      const r = c.getBoundingClientRect();
      return { x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width),
               h: c.style.height ? Math.round(r.height) : undefined };
    }
    return { x: c.offsetLeft, y: c.offsetTop, w: c.offsetWidth,
             h: c.style.height ? c.offsetHeight : undefined };
  }
  function keep(c) {
    const e = entry(c), g = current(c);
    if (c.classList.contains('cv-min') && e[isFloat(c) ? 'float' : 'canvas']) {
      g.h = e[isFloat(c) ? 'float' : 'canvas'].h;      // a strip is not a new height
    }
    e[isFloat(c) ? 'float' : 'canvas'] = g;
    save();
  }
  function grow() {
    if (!on) return;
    let bottom = 0;
    cards().forEach(c => {
      if (!c.hidden && !isFloat(c)) bottom = Math.max(bottom, c.offsetTop + c.offsetHeight);
    });
    cols.style.minHeight = (bottom + 48) + 'px';
  }
  function freeSpot(c) {
    let bottom = 0;
    cards().forEach(o => {
      if (o !== c && !o.hidden && o.style.top) bottom = Math.max(bottom, o.offsetTop + o.offsetHeight);
    });
    return { x: 0, y: snap(bottom ? bottom + GAP : 0), w: snap(Math.min(640, cols.clientWidth)) };
  }
  function inView(g) {
    const vw = window.innerWidth, vh = window.innerHeight;
    return Object.assign({}, g, {
      x: Math.min(Math.max(g.x, KEEP - (g.w || MIN_W)), vw - KEEP),
      y: Math.min(Math.max(g.y, 0), vh - 40) });
  }

  /* ── minimised: the title as a translucent strip ─────────────────── */
  function minimise(c) {
    if (isFree(c)) keep(c);
    c.classList.add('cv-min');
    c.style.height = '';
    entry(c).min = true; save();
    paintControls(c); paintBar(); grow();
  }
  function restore(c) {
    c.classList.remove('cv-min');
    const e = entry(c);
    e.min = false; save();
    const g = e[isFloat(c) ? 'float' : 'canvas'];
    if (isFree(c) && g && g.h) c.style.height = g.h + 'px';
    front(c);
    paintControls(c); paintBar(); grow();
  }

  /* ── detached: one box in a window over the page ─────────────────── */
  function detach(c, saved) {
    if (on || NARROW.matches) return;
    const r = c.getBoundingClientRect();
    // Lifted near where it was, but wholly on screen: a box low on the page
    // must not open as a window whose title is the last 40 pixels of it.
    const vh = window.innerHeight;
    const h = snap(Math.min(r.height, vh * 0.7));
    const g = saved || { x: snap(r.left + 24), w: snap(r.width), h,
                         y: snap(Math.min(Math.max(r.top + 24, 16), vh - h - 16)) };
    c.classList.add('cv-float');
    apply(c, inView(g));
    front(c);
    const e = entry(c); e.floating = true; e.float = inView(g); save();
    paintControls(c);
  }
  function dock(c) {
    c.classList.remove('cv-float');
    clearGeometry(c);
    const e = entry(c); e.floating = false; save();
    paintControls(c);
  }

  /* ── the canvas ──────────────────────────────────────────────────── */
  function enter() {
    if (on || NARROW.matches) return;
    // Measured before the layout changes, so a box with no place of its own
    // starts where the page had it.
    const origin = cols.getBoundingClientRect();
    const flow = new Map(cards().map(c => {
      const r = c.getBoundingClientRect();
      return [c, c.hidden || isFloat(c) ? null
        : { x: r.left - origin.left, y: r.top - origin.top, w: r.width }];
    }));
    cards().forEach(c => { if (isFloat(c)) { c.classList.remove('cv-float'); clearGeometry(c); } });
    on = true; write(ON_KEY, true);
    cols.classList.add('canvas');
    cards().forEach(c => {
      const e = entry(c), f = flow.get(c);
      const g = e.canvas || (f && { x: snap(f.x), y: snap(f.y), w: snap(f.w) });
      if (g) apply(c, g);
      if (e.z) { zTop = Math.max(zTop, e.z); c.style.zIndex = e.z; }
      paintControls(c);
    });
    observer = new MutationObserver(muts => muts.forEach(m => {
      const c = m.target;
      if (!on || c.hidden || c.style.top) return;
      apply(c, entry(c).canvas || freeSpot(c));
      grow();
    }));
    cards().forEach(c => observer.observe(c, { attributes: true, attributeFilter: ['hidden'] }));
    resizeObs = new ResizeObserver(() => grow());
    cards().forEach(c => resizeObs.observe(c));
    grow(); paintBar();
  }
  function leave() {
    if (!on) return;
    on = false; write(ON_KEY, false);
    if (observer) observer.disconnect();
    if (resizeObs) resizeObs.disconnect();
    cols.classList.remove('canvas');
    cols.style.minHeight = '';
    cards().forEach(c => { clearGeometry(c); c.classList.remove('cv-drag'); });
    // A box that was detached in the page goes back to its window.
    if (!NARROW.matches) cards().forEach(c => { const e = entry(c); if (e.floating) detach(c, e.float); });
    cards().forEach(paintControls);
    paintBar();
  }

  /* ── dragging by the title, resizing by an edge ──────────────────── */
  const INTERACTIVE = 'button, a, input, select, textarea, summary, label, [contenteditable]';
  let act = null, justMoved = false;
  cols.addEventListener('pointerdown', e => {
    justMoved = false;            // a drag that ended outside a click target
    if (e.button !== 0) return;
    const c = e.target.closest('section.card');
    if (!c || !cols.contains(c) || !isFree(c)) return;
    front(c);
    const edge = e.target.closest('.cv-h');
    const head = e.target.closest('section.card > .head');
    if (!edge && (!head || e.target.closest(INTERACTIVE))) return;
    const g = isFloat(c) ? c.getBoundingClientRect() : null;
    act = { c, edge: edge && edge.dataset.edge, x0: e.clientX, y0: e.clientY, moved: false,
            left: isFloat(c) ? g.left : c.offsetLeft, top: isFloat(c) ? g.top : c.offsetTop,
            w: c.offsetWidth, h: c.offsetHeight };
    (edge || head).setPointerCapture(e.pointerId);
    e.preventDefault();
  });
  cols.addEventListener('pointermove', e => {
    if (!act) return;
    const dx = e.clientX - act.x0, dy = e.clientY - act.y0;
    if (!act.moved && Math.abs(dx) + Math.abs(dy) < 4) return;
    act.moved = true;
    const c = act.c;
    c.classList.add('cv-drag');
    if (!act.edge) {
      const maxX = isFloat(c) ? window.innerWidth - KEEP : Math.max(0, cols.clientWidth - KEEP);
      const minX = isFloat(c) ? KEEP - act.w : 0;
      // A window keeps its title on screen; the canvas grows downward instead.
      const maxY = isFloat(c) ? window.innerHeight - 40 : Infinity;
      c.style.left = Math.min(maxX, Math.max(minX, snap(act.left + dx))) + 'px';
      c.style.top = Math.min(maxY, Math.max(0, snap(act.top + dy))) + 'px';
      return;
    }
    const k = act.edge;
    if (k.includes('e')) c.style.width = Math.max(MIN_W, snap(act.w + dx)) + 'px';
    if (k.includes('w')) {
      const w = Math.max(MIN_W, snap(act.w - dx));
      c.style.width = w + 'px';
      c.style.left = (act.left + act.w - w) + 'px';
    }
    if (k.includes('s') && !c.classList.contains('cv-min')) {
      c.style.height = Math.max(MIN_H, snap(act.h + dy)) + 'px';
    }
  });
  const end = () => {
    if (!act) return;
    const { c, moved } = act;
    act = null;
    c.classList.remove('cv-drag');
    if (moved) { justMoved = true; keep(c); grow(); }
  };
  cols.addEventListener('pointerup', end);
  cols.addEventListener('pointercancel', end);
  // A drag must not also fold a box, and a minimised box's title opens it.
  cols.addEventListener('click', e => {
    if (justMoved) { e.stopImmediatePropagation(); e.preventDefault(); justMoved = false; return; }
    const c = e.target.closest('section.card');
    if (c && c.classList.contains('cv-min') && e.target.closest('section.card > .head')
        && !e.target.closest(INTERACTIVE)) {
      e.stopImmediatePropagation(); e.preventDefault();
      restore(c);
    }
  }, true);

  /* Alt + arrow keys move the free box the focus is in. */
  document.addEventListener('keydown', e => {
    if (!e.altKey || !/^Arrow/.test(e.key)) return;
    const c = document.activeElement && document.activeElement.closest('section.card');
    if (!c || !cols.contains(c) || !isFree(c)) return;
    e.preventDefault();
    const step = e.shiftKey ? SNAP * 4 : SNAP;
    const g = current(c);
    g.x += e.key === 'ArrowLeft' ? -step : e.key === 'ArrowRight' ? step : 0;
    g.y = Math.max(0, g.y + (e.key === 'ArrowUp' ? -step : e.key === 'ArrowDown' ? step : 0));
    c.style.left = g.x + 'px'; c.style.top = g.y + 'px';
    keep(c); grow();
  });

  toggle.addEventListener('click', () => (on ? leave() : enter()));
  reset.addEventListener('click', () => {
    const wasOn = on;
    if (on) leave();
    cards().forEach(c => { dock(c); c.classList.remove('cv-min'); });
    layout = {}; save();
    if (wasOn) enter();
    cards().forEach(paintControls); paintBar();
  });
  openAll.addEventListener('click', () => {
    cards().filter(c => c.classList.contains('cv-min')).forEach(restore);
  });
  // A window that no longer fits keeps its title on screen; a phone keeps
  // the reading order, with nothing floating over it.
  window.addEventListener('resize', () => cards().forEach(c => {
    if (isFloat(c)) { apply(c, inView(current(c))); keep(c); }
  }));
  NARROW.addEventListener('change', () => {
    if (NARROW.matches) {
      leave();
      cards().forEach(c => { if (isFloat(c)) { c.classList.remove('cv-float'); clearGeometry(c); } });
    } else {
      cards().forEach(c => { const e = entry(c); if (e.floating && !on) detach(c, e.float); });
    }
    cards().forEach(paintControls); paintBar();
  });

  /* ── start where this person left it ─────────────────────────────── */
  cards().forEach(c => {
    dress(c);
    if (entry(c).min) { c.classList.add('cv-min'); paintControls(c); }
  });
  paintBar();
  requestAnimationFrame(() => requestAnimationFrame(() => {
    if (read(ON_KEY, false) && !NARROW.matches) {
      enter();
    } else if (!NARROW.matches) {
      cards().forEach(c => { const e = entry(c); if (e.floating) detach(c, e.float); });
    }
    paintBar();
  }));
  window.BSCanvas = { enter, leave, detach, dock, minimise, restore,
                      isOn: () => on, layout: () => layout };
})();
