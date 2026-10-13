'use strict';
/* Canvas layout for the run page: every box can be moved, resized and set
 * aside, and the arrangement is the viewer's own.
 *
 * The page is two columns of cards in the order a person acts on them. That
 * order is right for reading a run once and wrong for working with one: a
 * scientist comparing the round plan with the protocol wants them side by
 * side, the activity feed small in a corner, and the benchmarks out of the
 * way. So the same cards can be laid out freely — dragged by their title,
 * resized from their corner, set aside and brought back — and the layout is
 * remembered in this browser for every run (it is a per-viewer convenience,
 * so browser storage is the right home for it, and nothing breaks without it).
 *
 * Nothing here renders content. The cards are the page's own; this only moves
 * them, so a card the run fills later is placed where there is room, and
 * leaving canvas mode puts every card back in the reading order.
 */
(function () {
  const LAYOUT_KEY = 'bs-canvas-layout-v1';
  const ON_KEY = 'bs-canvas-on';
  const SNAP = 8, GAP = 16, MIN_VISIBLE = 80;
  const NARROW = window.matchMedia('(max-width: 900px)');
  const cols = document.querySelector('.cols');
  if (!cols) return;

  const read = (k, dflt) => { try { const v = localStorage.getItem(k); return v == null ? dflt : JSON.parse(v); } catch (_) { return dflt; } };
  const write = (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch (_) { /* private mode */ } };

  let layout = read(LAYOUT_KEY, {}) || {};
  let on = false, zTop = 10, observer = null, resizeObs = null;

  const snap = v => Math.round(v / SNAP) * SNAP;
  const titleOf = c => ((c.querySelector('.head h3') || {}).textContent || 'Untitled box').trim();
  const cards = () => Array.from(cols.querySelectorAll(':scope > .stack > section.card'));

  /* A stable id per card: its own id, else one from its title. */
  const ids = new Map();
  function idOf(c) {
    if (ids.has(c)) return ids.get(c);
    let id = c.id || 'cv-' + titleOf(c).toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
    let n = 2; const base = id;
    while ([...ids.values()].includes(id)) id = `${base}-${n++}`;
    ids.set(c, id);
    return id;
  }

  /* ── the toolbar ─────────────────────────────────────────────────── */
  const bar = document.createElement('div');
  bar.className = 'cv-bar';
  const toggle = document.createElement('button');
  toggle.className = 'btn sm'; toggle.type = 'button';
  toggle.setAttribute('aria-pressed', 'false');
  const reset = document.createElement('button');
  reset.className = 'btn sm'; reset.type = 'button'; reset.textContent = 'Reset layout';
  reset.title = 'Put every box back where the page puts it, in a canvas you can rearrange again';
  const asideMenu = document.createElement('details');
  asideMenu.className = 'cv-aside-menu';
  const hint = document.createElement('span');
  hint.className = 'hint';
  hint.textContent = 'Drag a box by its title, resize it from its corner, set it aside with ×. '
    + 'Alt + arrow keys move the box you are in.';
  bar.append(toggle, reset, asideMenu, hint);
  cols.parentNode.insertBefore(bar, cols);

  function paintBar() {
    toggle.textContent = on ? 'Back to the page layout' : 'Arrange as a canvas';
    toggle.setAttribute('aria-pressed', String(on));
    toggle.title = on ? 'Put every box back in the reading order (your canvas is kept for next time)'
      : 'Move, resize and set aside the boxes on this page; the layout is remembered in this browser';
    reset.hidden = hint.hidden = !on;
    const out = cards().filter(c => (layout[idOf(c)] || {}).aside);
    asideMenu.hidden = !on || !out.length;
    asideMenu.textContent = '';
    const sum = document.createElement('summary');
    sum.textContent = `Set aside (${out.length})`;
    asideMenu.append(sum);
    out.forEach(c => {
      const b = document.createElement('button');
      b.type = 'button'; b.className = 'btn sm';
      b.textContent = 'Bring back: ' + titleOf(c);
      b.addEventListener('click', () => { bringBack(c); });
      asideMenu.append(b);
    });
  }

  /* ── geometry ────────────────────────────────────────────────────── */
  function place(c, box) {
    c.style.left = box.x + 'px';
    c.style.top = box.y + 'px';
    c.style.width = box.w ? box.w + 'px' : '';
    c.style.height = box.h ? box.h + 'px' : '';
    c.style.zIndex = box.z || '';
    c.classList.toggle('cv-out', !!box.aside);
  }
  function grow() {
    let bottom = 0;
    cards().forEach(c => {
      if (c.hidden || c.classList.contains('cv-out')) return;
      bottom = Math.max(bottom, c.offsetTop + c.offsetHeight);
    });
    cols.style.minHeight = (bottom + 48) + 'px';
  }
  function store(c, patch) {
    const id = idOf(c);
    layout[id] = Object.assign({}, layout[id] || {}, patch);
    write(LAYOUT_KEY, layout);
  }
  /* Where a card goes that has no place yet: below everything, at the width
     of the column it came from. */
  function freeSpot(c, flowRect) {
    let bottom = 0;
    cards().forEach(o => {
      if (o === c || o.hidden || o.classList.contains('cv-out') || !o.style.top) return;
      bottom = Math.max(bottom, o.offsetTop + o.offsetHeight);
    });
    const w = flowRect && flowRect.width ? flowRect.width : Math.min(640, cols.clientWidth);
    return { x: flowRect ? snap(flowRect.x) : 0, y: snap(bottom ? bottom + GAP : 0), w: snap(w) };
  }

  /* ── entering and leaving ────────────────────────────────────────── */
  function enter() {
    if (on || NARROW.matches) return;
    // Measured before the layout changes, so nothing jumps: a card with no
    // saved place starts exactly where the page had it.
    const origin = cols.getBoundingClientRect();
    const flow = new Map(cards().map(c => {
      const r = c.getBoundingClientRect();
      return [c, c.hidden ? null : { x: r.left - origin.left, y: r.top - origin.top, width: r.width }];
    }));
    on = true;
    write(ON_KEY, true);
    cols.classList.add('canvas');
    cards().forEach(c => {
      const id = idOf(c), saved = layout[id];
      if (saved) {
        zTop = Math.max(zTop, saved.z || 0);
        place(c, saved);
      } else if (flow.get(c)) {
        const f = flow.get(c);
        place(c, { x: snap(f.x), y: snap(f.y), w: snap(f.width) });
      }
      addAsideButton(c);
    });
    // A card the run reveals later (the result, the round, the help panel)
    // gets a free spot rather than landing on top of another.
    observer = new MutationObserver(muts => muts.forEach(m => {
      const c = m.target;
      if (!on || c.hidden || c.style.top) return;
      const box = layout[idOf(c)] || freeSpot(c, null);
      place(c, box);
      grow();
    }));
    cards().forEach(c => observer.observe(c, { attributes: true, attributeFilter: ['hidden'] }));
    resizeObs = new ResizeObserver(() => grow());
    cards().forEach(c => resizeObs.observe(c));
    grow();
    paintBar();
  }
  function leave() {
    if (!on) return;
    on = false;
    write(ON_KEY, false);
    if (observer) observer.disconnect();
    if (resizeObs) resizeObs.disconnect();
    cols.classList.remove('canvas');
    cols.style.minHeight = '';
    cards().forEach(c => {
      ['left', 'top', 'width', 'height', 'zIndex'].forEach(p => { c.style[p] = ''; });
      c.classList.remove('cv-out', 'cv-drag');
      const b = c.querySelector(':scope > .head > .cv-x');
      if (b) b.remove();
    });
    paintBar();
  }

  /* ── setting a box aside ─────────────────────────────────────────── */
  function addAsideButton(c) {
    const head = c.querySelector(':scope > .head');
    if (!head || head.querySelector('.cv-x')) return;
    const x = document.createElement('button');
    x.type = 'button'; x.className = 'cv-x'; x.textContent = '×';
    x.title = 'Set this box aside (bring it back from "Set aside" above)';
    x.setAttribute('aria-label', 'Set aside: ' + titleOf(c));
    x.addEventListener('click', e => {
      e.stopPropagation();           // not a fold toggle
      store(c, { aside: true });
      c.classList.add('cv-out');
      grow(); paintBar();
    });
    head.append(x);
  }
  function bringBack(c) {
    const box = Object.assign({}, layout[idOf(c)] || {}, { aside: false, z: ++zTop });
    store(c, box);
    place(c, box);
    grow(); paintBar();
    c.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }

  /* ── dragging by the title, resizing from the corner ─────────────── */
  const INTERACTIVE = 'button, a, input, select, textarea, summary, label, [contenteditable]';
  let drag = null, justDragged = false;
  cols.addEventListener('pointerdown', e => {
    if (!on || e.button !== 0) return;
    const c = e.target.closest('section.card');
    if (!c || !cols.contains(c)) return;
    c.style.zIndex = ++zTop;
    const start = { w: c.offsetWidth, h: c.offsetHeight };
    // A press anywhere on the box may end as a resize from its corner: the
    // size is read again on release and kept if it changed.
    const onUp = () => {
      window.removeEventListener('pointerup', onUp);
      if (c.offsetWidth !== start.w || c.offsetHeight !== start.h) {
        store(c, { w: snap(c.offsetWidth), h: snap(c.offsetHeight), z: zTop,
                   x: c.offsetLeft, y: c.offsetTop });
        grow();
      } else {
        store(c, { z: zTop, x: c.offsetLeft, y: c.offsetTop });
      }
    };
    window.addEventListener('pointerup', onUp);
    const head = e.target.closest('section.card > .head');
    if (!head || e.target.closest(INTERACTIVE)) return;
    drag = { c, x0: e.clientX, y0: e.clientY, left: c.offsetLeft, top: c.offsetTop, moved: false };
    head.setPointerCapture(e.pointerId);
    e.preventDefault();
  });
  cols.addEventListener('pointermove', e => {
    if (!drag) return;
    const dx = e.clientX - drag.x0, dy = e.clientY - drag.y0;
    if (!drag.moved && Math.abs(dx) + Math.abs(dy) < 4) return;
    drag.moved = true;
    drag.c.classList.add('cv-drag');
    const maxX = Math.max(0, cols.clientWidth - MIN_VISIBLE);
    drag.c.style.left = Math.min(maxX, Math.max(0, snap(drag.left + dx))) + 'px';
    drag.c.style.top = Math.max(0, snap(drag.top + dy)) + 'px';
  });
  const endDrag = () => {
    if (!drag) return;
    const { c, moved } = drag;
    drag = null;
    c.classList.remove('cv-drag');
    if (moved) {
      justDragged = true;
      store(c, { x: c.offsetLeft, y: c.offsetTop, w: c.offsetWidth, z: zTop });
      grow();
    }
  };
  cols.addEventListener('pointerup', endDrag);
  cols.addEventListener('pointercancel', endDrag);
  // A drag that ends on a folding title must not also fold the box.
  cols.addEventListener('click', e => {
    if (justDragged) { e.stopImmediatePropagation(); e.preventDefault(); justDragged = false; }
  }, true);

  /* Alt + arrow keys move the box the focus is in. */
  document.addEventListener('keydown', e => {
    if (!on || !e.altKey || !/^Arrow/.test(e.key)) return;
    const c = document.activeElement && document.activeElement.closest('section.card');
    if (!c || !cols.contains(c)) return;
    e.preventDefault();
    const step = e.shiftKey ? SNAP * 4 : SNAP;
    const dx = e.key === 'ArrowLeft' ? -step : e.key === 'ArrowRight' ? step : 0;
    const dy = e.key === 'ArrowUp' ? -step : e.key === 'ArrowDown' ? step : 0;
    c.style.left = Math.max(0, c.offsetLeft + dx) + 'px';
    c.style.top = Math.max(0, c.offsetTop + dy) + 'px';
    store(c, { x: c.offsetLeft, y: c.offsetTop });
    grow();
  });

  toggle.addEventListener('click', () => (on ? leave() : enter()));
  reset.addEventListener('click', () => {
    layout = {};
    write(LAYOUT_KEY, layout);
    leave();
    enter();
  });
  // A canvas on a phone is a box you cannot get out from under.
  NARROW.addEventListener('change', () => { if (NARROW.matches) leave(); paintBar(); });

  paintBar();
  if (read(ON_KEY, false) && !NARROW.matches) {
    // After the page's own scripts have laid it out, so the flow positions
    // a first canvas starts from are the real ones.
    requestAnimationFrame(() => requestAnimationFrame(enter));
  }
  window.BSCanvas = { enter, leave, isOn: () => on, layout: () => layout };
})();
