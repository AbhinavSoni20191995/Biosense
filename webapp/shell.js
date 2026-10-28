'use strict';
/* The application shell: a rail on the left, the work on the right.
 *
 * Every page had the same horizontal header — brand, a row of mode links,
 * the account, the theme switch — and then stacked everything else beneath
 * it. On a run page that produced a column of cards a person had to scroll
 * past to reach the thing they came for, and there was nowhere to put
 * "where am I" or "what did I run last week" that was not another card.
 *
 * So the chrome moves to a rail: the brand, a primary action, the sections of
 * the application, and a slot each page fills with its own navigation (on the
 * run page: the parts of this run, and the runs before it). The work gets the
 * rest of the width, with a slim bar above it for the account and the theme.
 *
 * This file MOVES the nodes the page already has rather than rebuilding them,
 * so every handler a page attached to its theme button, its account area or
 * its links keeps working. A page with no header is left exactly as it is.
 *
 * The rail collapses under 64rem, where it becomes a row of links above the
 * work, which is the layout the pages had before — a phone has no room for a
 * rail and a person on one is reading, not navigating.
 */
(function () {
  // Every page's chrome is a `.bar` holding the brand — `header` on most,
  // a plain `div` on the loop tracker. What makes it the chrome is the brand
  // inside it, not the tag it happens to use.
  // The shell brings its own stylesheet, so it works on a page that does not
  // use the shared one (the loop tracker is such a page).
  if (!document.querySelector('link[href="shell.css"]')) {
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = 'shell.css';
    document.head.append(link);
  }
  const header = Array.from(document.querySelectorAll('.bar'))
    .find(n => n.querySelector('.brand'));
  if (!header || document.querySelector('.shell-app')) return;
  const body = document.body;

  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };

  // What the page already has. Each is moved, never copied.
  const brand = header.querySelector('.brand');
  const modes = header.querySelector('.modes');
  const identity = header.querySelector('#identity');
  const theme = header.querySelector('#theme');
  const beacon = header.querySelector('#runbeacon');
  const here = (modes && modes.querySelector('a[aria-current]')) || null;

  // `shell-app`, not `app`: the loop tracker already has an `.app` of its
  // own, and taking that name would have meant the shell silently skipping
  // that page and, where it did build, fighting the page's own layout.
  const app = el('div', 'shell-app');
  const rail = el('aside', 'rail');
  rail.setAttribute('aria-label', 'BioSense');
  const main = el('div', 'main');

  // ── the rail ──────────────────────────────────────────────
  if (brand) {
    const home = el('a', 'rail-brand');
    home.href = 'console.html';
    home.append(brand);
    rail.append(home);
  }

  // One primary action, the way a person starts: a new run.
  const start = el('a', 'rail-new');
  start.href = 'console.html#new';
  start.append(el('span', 'rail-ico', '+'), el('span', null, 'New run'));
  start.addEventListener('click', e => {
    // Already on the run page: scroll to the form rather than reload it.
    const form = document.querySelector('#runBtn');
    if (form) {
      e.preventDefault();
      const card = form.closest('section.card') || form;
      card.scrollIntoView({ behavior: 'smooth', block: 'start' });
      const obj = document.querySelector('#objective');
      if (obj) setTimeout(() => obj.focus(), 320);
    }
  });
  rail.append(start);

  if (modes) {
    const nav = el('nav', 'rail-nav');
    nav.setAttribute('aria-label', 'Sections');
    Array.from(modes.querySelectorAll('a')).forEach(a => {
      a.classList.add('rail-link');
      nav.append(a);
    });
    modes.remove();
    rail.append(nav);
  }

  // Filled by the page: on the run page, the parts of this run and the runs
  // before it. Empty everywhere else, and then it takes no room.
  const extra = el('div', 'rail-extra');
  extra.id = 'railExtra';
  rail.append(extra);

  // ── the bar above the work ────────────────────────────────
  const top = el('div', 'topbar');
  const title = el('div', 'topbar-t', here ? here.textContent.trim() : (document.title || '')
    .replace(/^BioSense\s*·\s*/, ''));
  top.append(title, el('div', 'spacer'));
  // Everything the header still holds goes to the bar above the work, in the
  // order it was in. Naming the few nodes this file knows about would drop
  // the ones it does not — the simulator's own theme button among them — and
  // a page whose script then asks for that node gets null and stops.
  Array.from(header.childNodes).forEach(n => {
    if (n.nodeType === 1 && n.classList.contains('spacer')) return;
    if (n.nodeType === 3 && !n.textContent.trim()) return;
    top.append(n);
  });
  void beacon; void identity; void theme;

  // Under 64rem the rail is a strip above the work; this opens it.
  const burger = el('button', 'btn sm rail-toggle', 'Menu');
  burger.type = 'button';
  burger.setAttribute('aria-expanded', 'false');
  burger.addEventListener('click', () => {
    const open = body.classList.toggle('rail-open');
    burger.setAttribute('aria-expanded', String(open));
  });
  top.prepend(burger);

  // ── assemble, moving what is already on the page ──────────
  const rest = [];
  Array.from(body.childNodes).forEach(n => { if (n !== header) rest.push(n); });
  header.remove();
  main.append(top);
  rest.forEach(n => main.append(n));
  app.append(rail, main);
  body.append(app);
  body.classList.add('shelled');

  // Closing the rail by tapping the work, on a narrow screen only.
  main.addEventListener('click', () => {
    if (body.classList.contains('rail-open')) {
      body.classList.remove('rail-open');
      burger.setAttribute('aria-expanded', 'false');
    }
  }, true);
})();
