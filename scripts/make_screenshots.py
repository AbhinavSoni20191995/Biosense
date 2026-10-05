"""Regenerate the README screenshots from the real pages.

    python3 scripts/make_screenshots.py            # every shot
    python3 scripts/make_screenshots.py console    # just one

Why this exists: the screenshots in the README went stale when the interface
gained a section, and nothing noticed, because they had been taken by hand. The
architecture diagrams have a generator and a test that catches them drifting;
the screenshots had neither. This is the generator half.

It needs **playwright**, which is deliberately NOT a project dependency: nothing
in ordinary CI may require a browser, and the base install stays small. Run it
with whatever interpreter has playwright installed. It starts the real app server
on a loopback port, drives the real pages, and writes light and dark PNGs into
docs/assets/.

Everything it photographs is the synthetic stand-in. No number in any of these
images measures a real cell.
"""
from __future__ import annotations

import argparse
import glob
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / 'docs' / 'assets'
CHROME = next(iter(glob.glob('/opt/pw-browsers/chromium*/chrome-linux/chrome')), None)
# 1x. These pages are mostly text, and at 2x the set came to 8.6MB in a
# repository that has to stay clonable. The viewports below are wider than the
# column a README renders them into, so they are downscaled on display anyway.
SCALE = 1

# name -> which page, viewport, what to photograph, how long to let it settle.
# `full` captures the whole scroll height; without it the shot is the viewport,
# which is what a README wants for a wide page whose columns are unequal -- a
# full-page capture of those is mostly the empty side of the taller column.
# `run` drives a real loop first, for the panels that only exist once one has
# finished. That is slower and it is the point: a screenshot of those taken any
# other way would be a drawing of an interface rather than a photograph of one.
# `discovery` drives the real structured form: pick the dataset, name a value to
# test, press the button and wait for the panels that only exist after a run. A
# screenshot of those taken any other way would be a drawing of an interface
# rather than a photograph of one.
SHOTS = {
    'console': dict(page='console.html', w=1440, h=1000, wait=2600),
    'hypothesis-card': dict(page='console.html', w=1060, h=1200, sel='#resultPanel',
                            wait=2200, discover=True),
    'protocol-card': dict(page='console.html', w=1060, h=1400, sel='#protocolPanel',
                          wait=2200, discover=True),
    'analysis-card': dict(page='console.html', w=900, h=900, sel='#analysisPanel',
                          wait=2200, discover=True),
    'progress': dict(page='console.html', w=620, h=900, sel='#progressPanel',
                     wait=2200, discover=True),
    'data-panel': dict(page='data.html', w=1340, h=1000, wait=2600),
    'simulator': dict(page='simulator.html', w=1500, h=1000, wait=3400),
    'simulator-no-model': dict(page='simulator.html', w=700, h=940, sel='.knobs', wait=3400,
                               select=('#project', 'cart_expansion')),
    'agents': dict(page='loop.html', w=1440, h=940, wait=2200, run=True, sel='#flowPanel'),
    'trajectory': dict(page='loop.html', w=1000, h=900, wait=2200, run=True, sel='#chartPanel'),
}

# The two-arm comparison, so the trajectory shot actually shows two lines and
# the alt text describing them stays true.
PROMPT = ('Optimise the conditions for growing wild-type T cells from iPSC, and compare '
          'with a BACH2 knockout that must still be expanded.')
OBJECTIVE = ('Increase viable macrophage production while maintaining macrophage identity '
             'and viability.')


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def serve(port):
    proc = subprocess.Popen(
        ['uv', 'run', '--frozen', '--no-sync', 'python', '-m', 'biosense.production.app',
         '--port', str(port), '--runs', 'runs', '--static', 'webapp'],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz', timeout=2):
                return proc
        except OSError:
            time.sleep(0.5)
    proc.terminate()
    raise SystemExit('the app server did not come up')


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('only', nargs='*', help='shot names; default every one')
    a = ap.parse_args(argv)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit(
            'this needs playwright, which is not a project dependency on purpose: ordinary CI '
            'must not require a browser. Run it with an interpreter that has it installed.')
    wanted = a.only or list(SHOTS)
    unknown = [n for n in wanted if n not in SHOTS]
    if unknown:
        raise SystemExit(f'unknown shot(s): {", ".join(unknown)}; have: {", ".join(SHOTS)}')

    port = free_port()
    proc = serve(port)
    errors = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=CHROME)
            for name in wanted:
                cfg = SHOTS[name]
                for theme in ('light', 'dark'):
                    pg = browser.new_page(viewport={'width': cfg['w'], 'height': cfg['h']},
                                          color_scheme=theme, device_scale_factor=SCALE)
                    pg.on('pageerror', lambda e, n=name: errors.append(f'{n}: {e}'))
                    pg.goto(f'http://127.0.0.1:{port}/{cfg["page"]}')
                    pg.wait_for_timeout(cfg['wait'])
                    if cfg.get('select'):
                        pg.select_option(*cfg['select'])
                        pg.wait_for_timeout(1400)
                    if cfg.get('run'):
                        pg.fill('#prompt', PROMPT)
                        pg.click('#run')
                        pg.wait_for_selector(f'{cfg["sel"]}:not([hidden])', timeout=90_000)
                        pg.wait_for_timeout(2500)
                    if cfg.get('discover'):
                        pg.fill('#objective', OBJECTIVE)
                        pg.eval_on_selector('details', 'd => d.open = true')
                        pg.wait_for_timeout(250)
                        pg.check('#datasetPicks input[value="facs-mcsf-fixture"]')
                        pg.select_option('#candParam', 'mcsf_ng_ml')
                        pg.fill('#candValue', '50')
                        pg.click('#candAdd')
                        pg.click('#runBtn')
                        pg.wait_for_selector(f'{cfg["sel"]}:not([hidden])', timeout=90_000)
                        pg.wait_for_timeout(2000)
                    out = ASSETS / f'{name}-{theme}.png'
                    if cfg.get('sel'):
                        pg.locator(cfg['sel']).screenshot(path=str(out))
                    else:
                        pg.screenshot(path=str(out), full_page=bool(cfg.get('full')))
                    print(f'wrote {out.relative_to(ROOT)}')
                    pg.close()
            browser.close()
    finally:
        proc.terminate()
    if errors:
        # a page error means the shot photographed a broken page
        raise SystemExit('page errors:\n  ' + '\n  '.join(errors))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
