"""Is the configured real-AI runtime ready? Prints the verdict, exits 0 or 1.

Used by scripts/start_local_ai.sh and scripts/check_local_ai.sh so both answer
with BioSense's own probe rather than a second opinion about the same processes:
the only question that matters is whether a session can be started, and this is
the code that will be asked to start one.

It prints no token, no path and no address beyond scheme and host.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from biosense import contracts as K            # noqa: E402
from biosense.production import runtime as RT  # noqa: E402

GREEN, RED, YELLOW, OFF = '\033[32m', '\033[31m', '\033[33m', '\033[0m'


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--runs', default='runs')
    ap.add_argument('--quiet', action='store_true')
    a = ap.parse_args(argv)
    try:
        cfg = RT.from_env(runs_dir=Path(a.runs))
    except K.ContractError as e:
        print(f'{RED}✗{OFF} configuration refused: {e}')
        return 1
    if not cfg.is_real:
        print(f'{YELLOW}!{OFF} this environment is configured for {cfg.label}, not real AI.')
        print('  Set BIOSENSE_RUNTIME_MODE=local, or use ./scripts/start_local_ai.sh')
        return 1
    found = RT.available(cfg, cfg.mode)
    if found.get('ok'):
        if not a.quiet:
            where = found.get('executor') or 'executor'
            print(f'{GREEN}✓{OFF} READY — {cfg.label} via {cfg.server_display} '
                  f'as {cfg.agent} ({where})')
        return 0
    print(f'{RED}✗{OFF} NOT READY — {found.get("headline")}')
    if found.get('detail'):
        print(f'  {found["detail"]}')
    if found.get('next_step'):
        print(f'  fix: {found["next_step"]}')
    print(f'  reason code: {found.get("reason")}')
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
