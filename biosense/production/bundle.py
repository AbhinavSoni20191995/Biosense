"""A copy of the agent bundle with the operator's model choices written in.

Omnigent's Claude harness honours a per-agent ``executor.config.model`` and
``reasoning_effort``; nothing in the committed bundle sets them, so every agent
runs on the one model ``ANTHROPIC_MODEL`` names. The single biggest time lever
in a run is letting the specialists — whose work is searching, reading and
extracting — run on a faster model while the orchestrator keeps the strongest.
This writes those choices into a *copy* of the bundle, line by line, touching
nothing else; the committed bundle is never edited, and the self-check
validates the copy Omnigent actually registers (``BIOSENSE_AGENT_BUNDLE``).

Standard library only, so the boot scripts can run it before anything else.

    python -m biosense.production.bundle --src discovery_loop --dst /tmp/x \\
        --specialist-model claude-sonnet-5-5 --specialist-effort medium
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

MODEL_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$')
EFFORTS = ('low', 'medium', 'high')
SPECIALISTS = ('literature', 'bioinformatics', 'analysis', 'biosimulator', 'outcome')


def check_model(value):
    v = (value or '').strip()
    if not MODEL_RE.match(v):
        raise ValueError(f'not a model id: {value!r}')
    return v


def check_effort(value):
    v = (value or '').strip().lower()
    if v not in EFFORTS:
        raise ValueError(f'reasoning effort must be one of {EFFORTS}; got {value!r}')
    return v


def set_executor_config(text, **fields):
    """Set keys under ``executor: config:`` in a config.yaml, textually.

    The block is ``executor:`` → ``  config:`` → ``    harness: claude-sdk``.
    A key already there is replaced where it stands; a missing one is added
    straight after ``harness``. Line-based on purpose: the bundle's comments
    and order survive, and nothing outside that block is touched.
    """
    lines = text.splitlines(keepends=True)
    start = next((i for i, l in enumerate(lines) if l.rstrip('\n') == 'executor:'), None)
    if start is None:
        raise ValueError('no executor: block in this config.yaml')
    end = start + 1
    while end < len(lines) and (lines[end].startswith(' ') or not lines[end].strip()):
        end += 1
    block = lines[start + 1:end]

    def key_of(line):
        m = re.match(r'^(\s+)(\w+):\s*(.*?)\s*$', line)
        return (m.group(1), m.group(2)) if m and len(m.group(1)) >= 4 else (None, None)

    present = {key_of(l)[1] for l in block} - {None}
    if 'harness' not in present:
        raise ValueError(f'no executor.config.harness line to attach {sorted(fields)} to')
    out = []
    for line in block:
        indent, key = key_of(line)
        if key in fields:
            out.append(f'{indent}{key}: {fields[key]}\n')
            continue
        out.append(line)
        if key == 'harness':
            for k, v in fields.items():
                if k not in present:
                    out.append(f'{indent}{k}: {v}\n')
    return ''.join(lines[:start + 1] + out + lines[end:])


def write_copy(src, dst, *, specialist_model=None, specialist_effort=None,
               orchestrator_model=None, orchestrator_effort=None):
    """Copy *src* to *dst* (replacing it) and write the choices into the copy."""
    src, dst = Path(src), Path(dst)
    if not (src / 'config.yaml').is_file():
        raise ValueError(f'{src} is not an agent bundle (no config.yaml)')
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    changed = []
    spec = {}
    if specialist_model:
        spec['model'] = check_model(specialist_model)
    if specialist_effort:
        spec['reasoning_effort'] = check_effort(specialist_effort)
    orch = {}
    if orchestrator_model:
        orch['model'] = check_model(orchestrator_model)
    if orchestrator_effort:
        orch['reasoning_effort'] = check_effort(orchestrator_effort)
    if spec:
        for name in SPECIALISTS:
            cfg = dst / 'agents' / name / 'config.yaml'
            if cfg.is_file():
                cfg.write_text(set_executor_config(cfg.read_text(), **spec))
                changed.append(f'agents/{name}')
    if orch:
        cfg = dst / 'config.yaml'
        cfg.write_text(set_executor_config(cfg.read_text(), **orch))
        changed.append('orchestrator')
    return {'dst': str(dst), 'specialists': spec, 'orchestrator': orch, 'changed': changed}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--src', required=True)
    ap.add_argument('--dst', required=True)
    ap.add_argument('--specialist-model')
    ap.add_argument('--specialist-effort')
    ap.add_argument('--orchestrator-model')
    ap.add_argument('--orchestrator-effort')
    a = ap.parse_args(argv)
    try:
        r = write_copy(a.src, a.dst, specialist_model=a.specialist_model,
                       specialist_effort=a.specialist_effort,
                       orchestrator_model=a.orchestrator_model,
                       orchestrator_effort=a.orchestrator_effort)
    except (ValueError, OSError) as e:
        print(f'bundle: {e}', file=sys.stderr)
        return 1
    print(f"bundle: {r['dst']} · specialists {r['specialists'] or 'unchanged'} · "
          f"orchestrator {r['orchestrator'] or 'unchanged'}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
