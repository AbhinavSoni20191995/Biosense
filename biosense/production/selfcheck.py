"""Does a prompt have everything it needs to become a protocol? Check it end to end.

Every real run so far has failed on something that was true before it started:
the orchestrator was told to wait for a person, the image had no sandbox, the
brief named a command that does not exist, and no command wrote a hypothesis at
all. Each cost a paid run to discover. This module runs the whole chain the
agents depend on, without a model, in a few seconds, and says which link breaks:

    sandbox            the agents' OS sandbox can start here (and how)
    agent_bundle       the orchestrator and its five specialists parse, the
                       orchestrator may dispatch to each, none is unsandboxed
    commands_named     every BioSense command named in the brief and the agent
                       prompts exists, down to the subcommand
    bioinformatics     datasets, tool registry, a gene annotation, and an
                       analysis planned and run on a committed fixture
    simulator          the project's model compares control with a candidate
    hypothesis         a draft becomes a validated hypothesis, with a measured
                       effect from the analysis and a simulated one
    parameters         the hypothesis names a lever, its direction and a value
    protocol           BioSense ingests the run directory and builds the
                       protocol summary, as it does after a real run
    bioreactor_loop    the stand-in bioreactor loop: protocol, run, analysis,
                       decision (`production.cli demo`)
    runtime            the AI runtime answers (when this deployment has one)
    agents_talk        --live only: a real session in which the orchestrator
                       dispatches to a specialist, which runs a BioSense tool
                       inside the sandbox and answers. Spends model credit.

The agent-facing steps run **as the agents run them**: through the same bwrap
command Omnigent builds from the discovery_loop's own `os_env` — read-only
workspace, writes only under the runs directory, no network — whenever that
sandbox can start here. When it cannot, they run directly and the row says so,
and the `sandbox` row fails on a hosted deployment.

Nothing here is evidence about a cell. The fixtures are synthetic and labelled
so, and the self-check's own directory is deleted unless `--keep` is given.

    python -m biosense.production.selfcheck            # offline, a few seconds
    python -m biosense.production.selfcheck --live     # plus a real agent round trip
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from .. import contracts as K
from . import sandbox as SB

OK, WARN, FAIL, SKIP = 'ok', 'warn', 'fail', 'skip'
STEP_TIMEOUT_S = 180
FIXTURE_DATASET = 'facs-mcsf-fixture'
FIXTURE_PROJECT = 'ipsc_macrophage'
GENE = 'MAFB'
# The helper environment Omnigent gives a sandboxed tool: deny by default.
TOOL_ENV = ('PATH', 'HOME', 'LANG', 'LC_ALL', 'TZ', 'PYTHONUNBUFFERED', 'PYTHONIOENCODING')
MODULE_RE = re.compile(r'-m\s+(biosense(?:\.[A-Za-z_]+)+)((?:\s+[a-z][a-z0-9_-]*)*)')


class Check:
    def __init__(self, workspace, work, *, sandboxed, bind_proc, python):
        self.workspace, self.work = Path(workspace), Path(work)
        self.sandboxed, self.bind_proc, self.python = sandboxed, bind_proc, python
        self.unsandboxed_why = 'no sandbox here'
        self.rows = []

    def row(self, cid, label, status, detail, started):
        self.rows.append({'id': cid, 'label': label, 'status': status, 'detail': detail,
                          'seconds': round(time.monotonic() - started, 2)})

    # ── running a command the way an agent does ───────────────────────────
    def agent_exec(self, args, *, timeout=STEP_TIMEOUT_S):
        """Run `python -m …` as an agent tool would. Returns (ok, stdout, stderr)."""
        argv = [self.python, *args]
        env = {k: os.environ[k] for k in TOOL_ENV if k in os.environ}
        env.setdefault('PATH', '/usr/local/bin:/usr/bin:/bin')
        if self.sandboxed:
            wrapped = SB.wrap(argv, self.workspace, bind_proc=self.bind_proc)
            if wrapped:
                argv = wrapped
        try:
            r = subprocess.run(argv, cwd=self.workspace, env=env, capture_output=True,
                               text=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as e:
            return False, '', f'{type(e).__name__}: {e}'
        return r.returncode == 0, r.stdout, r.stderr

    def where(self):
        if self.sandboxed:
            return 'inside the agents\' sandbox'
        return f'directly ({self.unsandboxed_why})'


def _tail(text, n=300):
    text = ' '.join((text or '').split())
    return text[-n:]


def _json_out(stdout):
    try:
        return json.loads(stdout)
    except ValueError:
        return None


# ── the checks ──────────────────────────────────────────────────────────
def check_sandbox(c, found, hosted):
    t = time.monotonic()
    from . import runtime as RT
    state = RT.agent_sandbox_state()
    if state['sandbox'] == 'off':
        c.row('sandbox', 'Agents\' sandbox', WARN if state['model_key'] == 'proxy' else FAIL,
              'OFF by operator choice (BIOSENSE_AGENT_SANDBOX=off): agent commands run '
              'unconfined in this container. Model key: '
              + ('held by a separate process the agents cannot read.'
                 if state['model_key'] == 'proxy' else
                 'IN THE AGENTS\' ENVIRONMENT — the key proxy is not running.'), t)
        return
    if found['ok']:
        how = {'fresh_proc': 'bubblewrap works as Omnigent configures it',
               'bind_proc': 'bubblewrap works with the container /proc bound in'}.get(
                   found['mode'], found.get('detail') or 'not Linux')
        c.row('sandbox', 'Agents\' sandbox', OK, how, t)
    else:
        c.row('sandbox', 'Agents\' sandbox', FAIL if hosted else WARN,
              f'{found["problem"]}: {found["detail"]}. Real runs are refused until this is '
              f'fixed; the agents are never run unsandboxed.', t)


def check_bundle(c):
    t = time.monotonic()
    try:
        from omnigent.spec import parser
    except ImportError:
        c.row('agent_bundle', 'Agent bundle', SKIP,
              'Omnigent is not installed here (uv sync --extra omnigent)', t)
        return
    root = K.ROOT / 'discovery_loop'
    problems = []
    try:
        spec = parser.parse(root)
    except Exception as e:  # noqa: BLE001 - a broken bundle is the finding
        c.row('agent_bundle', 'Agent bundle', FAIL, f'orchestrator does not parse: {e}', t)
        return
    declared = sorted(getattr(a, 'name', a) for a in (spec.tools.agents or []))
    present = sorted(d.name for d in (root / 'agents').iterdir()
                     if (d / 'config.yaml').is_file())
    if declared != present:
        problems.append(f'declared {declared} but found {present}')
    for name in present:
        try:
            sub = parser.parse(root / 'agents' / name)
        except Exception as e:  # noqa: BLE001
            problems.append(f'{name} does not parse: {e}')
            continue
        sb = getattr(getattr(sub, 'os_env', None), 'sandbox', None)
        if sb is None or sb.type == 'none':
            problems.append(f'{name} runs without a sandbox')
    if not getattr(spec, 'async_enabled', True):
        problems.append('the orchestrator is not async; the inbox will not wake it')
    c.row('agent_bundle', 'Agent bundle', FAIL if problems else OK,
          '; '.join(problems) or f'orchestrator + {len(present)} specialists parse; it may '
                                 f'dispatch to {", ".join(declared)}', t)


def named_commands(brief):
    """Every `python -m biosense.… sub …` the agents are told to run, deduplicated."""
    texts = [brief]
    for p in sorted((K.ROOT / 'discovery_loop').rglob('prompt.md')):
        texts.append(p.read_text(encoding='utf-8'))
    found = {}
    for text in texts:
        for m in MODULE_RE.finditer(text):
            subs = tuple(m.group(2).split())
            found[(m.group(1), subs)] = True
    return sorted(found)


def check_commands(c, brief):
    t = time.monotonic()
    bad, checked = [], 0
    for module, subs in named_commands(brief):
        checked += 1
        try:
            r = subprocess.run([c.python, '-m', module, *subs, '--help'], cwd=c.workspace,
                               capture_output=True, text=True, timeout=60)
            ok = r.returncode == 0
            why = _tail(r.stderr, 160)
        except (OSError, subprocess.SubprocessError) as e:
            ok, why = False, str(e)
        if not ok:
            bad.append(f'`{module} {" ".join(subs)}` ({why})')
    c.row('commands_named', 'Commands named to the agents', FAIL if bad else OK,
          ('does not exist: ' + '; '.join(bad)) if bad else
          f'all {checked} BioSense commands in the brief and the six prompts exist', t)


def check_bioinformatics(c):
    t = time.monotonic()
    w = c.work
    steps = [
        ('datasets list', ['-m', 'biosense.bioinformatics.cli', 'datasets', 'list']),
        ('tool registry', ['-m', 'biosense.bioinformatics.cli', 'tools']),
        (f'annotate {GENE} knockout', ['-m', 'biosense.bioinformatics.cli', 'annotate',
                                       '--genes', GENE, '--perturbation', 'knockout',
                                       '--cell-type', 'macrophage']),
        ('analyse plan', ['-m', 'biosense.bioinformatics.cli', 'analyse', 'plan',
                          '--plan-id', 'selfcheck-P1',
                          '--question', 'Does M-CSF dose change monocyte output?',
                          '--evidence-gap', 'U1',
                          '--uncertainty', 'Whether M-CSF dose limits monocyte yield',
                          '--why', 'self-check', '--dataset-ids', FIXTURE_DATASET,
                          '--analysis-type', 'population_comparison',
                          '--tool', 'cytometry.population_comparison',
                          '--decision-relevance', 'M-CSF dose',
                          '--parameters', 'mcsf_ng_ml',
                          '--out', str(w / 'analysis_plan.json')]),
        ('analyse run', ['-m', 'biosense.bioinformatics.cli', 'analyse', 'run',
                         '--plan', str(w / 'analysis_plan.json'),
                         '--out', str(w / 'analysis_result.json')]),
    ]
    annotated = None
    for name, args in steps:
        ok, out, err = c.agent_exec(args)
        if name.startswith('annotate'):
            # Exit 2 is the tool's documented answer for a gene no loaded set
            # covers: found=false plus the public queries to run. That is the
            # correct output, not a failure, and it is never an invented effect.
            doc = _json_out(out)
            if doc and doc.get('genes'):
                ok = True
                annotated = 'annotated' if any(g.get('found') for g in doc['genes']) else \
                    'not in the bundled sets (found=false, public queries listed)'
        if not ok:
            c.row('bioinformatics', 'Bioinformatics tools', FAIL,
                  f'{name} failed {c.where()}: {_tail(err or out)}', t)
            return None
    try:
        result = K.read_json(w / 'analysis_result.json')
    except (OSError, ValueError) as e:
        c.row('bioinformatics', 'Bioinformatics tools', FAIL, f'no analysis result: {e}', t)
        return None
    stats = result.get('statistics') or []
    sig = [s for s in stats if s.get('q_value') is not None and s['q_value'] < 0.05]
    c.row('bioinformatics', 'Bioinformatics tools', OK if stats else FAIL,
          f'5 commands ran {c.where()}{", network off" if c.sandboxed else ""}; '
          f'{GENE} knockout: {annotated}; '
          f'{len(stats)} readouts compared on the SYNTHETIC fixture {FIXTURE_DATASET}, '
          f'{len(sig)} at q < 0.05', t)
    return sig[0]['readout'] if sig else (stats[0]['readout'] if stats else None)


def check_simulator(c):
    t = time.monotonic()
    ok, out, err = c.agent_exec(['-m', 'biosense.evidence.cli', 'simulate',
                                 '--project', FIXTURE_PROJECT, '--set', 'mcsf_ng_ml=50',
                                 '--out', str(c.work / 'simulation.json')])
    doc = _json_out(out) if ok else None
    if not ok or not doc:
        c.row('simulator', 'Bioreactor simulator', FAIL,
              f'simulate failed {c.where()}: {_tail(err or out)}', t)
        return False
    good = doc.get('prediction') == 'simulated' and doc.get('effects')
    c.row('simulator', 'Bioreactor simulator', OK if good else FAIL,
          f'{FIXTURE_PROJECT}: {doc.get("prediction")}, {len(doc.get("effects") or [])} '
          f'effects labelled SIMULATED (an uncalibrated model, not a measurement)', t)
    return bool(good)


def check_hypothesis(c, readout, simulated):
    t = time.monotonic()
    effects = []
    if readout:
        effects.append({'from_analysis_result': str(c.work / 'analysis_result.json'),
                        'readout': readout, 'unit': '%', 'higher_is_better': True})
    if simulated:
        effects.append({'from_simulation': str(c.work / 'simulation.json')})
    effects.append({'metric': 'macrophage_yield', 'unit': 'cells', 'direction': 'increase',
                    'higher_is_better': True,
                    'reason': 'self-check: the direction is the claim; no magnitude is given'})
    draft = {
        'hypothesis_id': 'H-SELFCHECK', 'statement':
            'SELF-CHECK on synthetic fixtures: raising M-CSF may change monocyte output.',
        'uncertainty': {'kind': 'evidence_gap', 'ref': 'U1',
                        'statement': 'Whether M-CSF dose limits monocyte yield.'},
        'parameter_id': 'mcsf_ng_ml', 'direction': 'increase', 'candidate_value': 50,
        'effects': effects,
        'evidence': [{'evidence_class': 'synthetic_fixture', 'stance': 'supportive',
                      'summary': 'SYNTHETIC fixture analysis, for the self-check only.',
                      'ref': 'analysis-selfcheck-P1', 'strength': 'weak'}],
        'next_experiment': {'summary': 'Not a recommendation: this is a self-check.'},
        'limitations': ['SELF-CHECK on synthetic fixtures. Not evidence about any cell.'],
    }
    K.write_json_atomic(c.work / 'H01.draft.json', draft)
    ok, out, err = c.agent_exec(['-m', 'biosense.evidence.cli', 'hypothesis',
                                 '--project', FIXTURE_PROJECT,
                                 '--draft', str(c.work / 'H01.draft.json'),
                                 '--out', str(c.work / 'quantified_hypothesis.json')])
    ok2, _, err2 = c.agent_exec(['-m', 'biosense.evidence.cli', 'context',
                                 '--species', 'human', '--cell-types', 'macrophage',
                                 '--out', str(c.work / 'research_context.json')])
    doc = _json_out(out) if ok else None
    if not ok or not doc or not ok2:
        c.row('hypothesis', 'Hypothesis and context files', FAIL,
              f'evidence.cli failed {c.where()}: {_tail(err or err2 or out)}', t)
        return None
    c.row('hypothesis', 'Hypothesis and context files', OK,
          f'built {c.where()} from a draft: claim level {doc["claim_level"]}, confidence '
          f'{doc["confidence"]}, {len(effects)} effects (measured from the analysis, simulated, '
          f'direction-only)',
          t)
    return doc


def check_parameters(c, hyp):
    t = time.monotonic()
    if not hyp:
        c.row('parameters', 'Parameter recommendation', SKIP, 'no hypothesis to read', t)
        return
    h = K.read_json(c.work / 'quantified_hypothesis.json')
    p = h['parameter']
    good = p.get('parameter_id') and p.get('direction') and p.get('registered')
    c.row('parameters', 'Parameter recommendation', OK if good else FAIL,
          f'{p.get("label") or p.get("parameter_id")}: {p.get("direction")} to '
          f'{p.get("candidate_value")} {p.get("unit") or ""} (simulator coverage '
          f'{p.get("simulator_coverage")}); next experiment and limitations attached', t)


def check_protocol(c):
    t = time.monotonic()
    from . import discovery as DISC
    from . import discovery_runner as DRUN
    try:
        req = DISC.build(project_id=FIXTURE_PROJECT,
                         objective='SELF-CHECK: can a run directory become a protocol?',
                         runtime_mode='synthetic_demo')
        final = DRUN.finish(req, c.work, runtime_mode='local_real_ai')
    except Exception as e:  # noqa: BLE001 - the failure is the finding
        c.row('protocol', 'Protocol from the run directory', FAIL,
              f'{type(e).__name__}: {e}', t)
        return
    bundle = final.get('bundle') or {}
    rejected = bundle.get('rejected') or []
    proto = final.get('protocol')
    good = proto and bundle.get('hypotheses') and not rejected
    detail = (f'ingested {bundle.get("artifacts_ingested", 0)} artifacts, '
              f'{len(bundle.get("hypotheses") or [])} hypothesis; protocol summary written '
              f'(protocol_summary.json/.md), status proposed, not approved')
    if rejected:
        detail = 'rejected: ' + '; '.join(f'{r["file"]}: {r["why"][:100]}' for r in rejected)
    elif not proto:
        detail = 'no protocol was built from the ingested hypothesis'
    c.row('protocol', 'Protocol from the run directory', OK if good else FAIL, detail, t)


def check_bioreactor_loop(c):
    t = time.monotonic()
    out = c.work / 'bioreactor_loop'
    ok, so, err = c.agent_exec(['-m', 'biosense.production.cli', 'demo', '--out', str(out)],
                               timeout=300)
    files = sorted(p.name for p in out.rglob('*') if p.is_file()) if out.is_dir() else []
    has = {k: any(k in f for f in files) for k in ('protocol', 'run', 'analysis', 'decision')}
    good = ok and all(has.values())
    c.row('bioreactor_loop', 'Bioreactor loop (stand-in)', OK if good else FAIL,
          (f'protocol → SYNTHETIC stand-in bioreactor run → analysis → decision, '
           f'{len(files)} files {c.where()}' if good else
           f'demo failed {c.where()}: {_tail(err or so)}; missing '
           f'{[k for k, v in has.items() if not v]}'), t)


def check_runtime(c, cfg):
    t = time.monotonic()
    from . import runtime as RT
    if cfg is None or not cfg.is_real:
        c.row('runtime', 'AI runtime', SKIP, 'this deployment runs no AI runtime', t)
        return False
    found = RT.available(cfg, cfg.mode)
    if found.get('ok'):
        c.row('runtime', 'AI runtime', OK,
              f'{cfg.label}: agent {cfg.agent} registered, {found.get("executor") or "executor"} '
              f'online', t)
        return True
    c.row('runtime', 'AI runtime', FAIL,
          f'{found.get("reason")}: {found.get("headline")} {found.get("detail") or ""}'.strip(), t)
    return False


LIVE_BRIEF = '''# BioSense self-check

This is an automated plumbing check, not a scientific request. Nobody will
answer in this session. Do not ask questions.

1. Call `sys_session_send` exactly once with `agent: "bioinformatics"`,
   `title: "selfcheck"` and `args`:
   "Self-check. Run this one shell command and nothing else:
   `.venv/bin/python -m biosense.bioinformatics.cli tools`
   Then reply with exactly READY if it exited 0, or FAILED: <the error> if not."
2. End your turn. When the inbox wakes you, read the answer with
   `sys_read_inbox` and reply with exactly what the specialist said.
'''


def check_agents_talk(c, cfg):
    t = time.monotonic()
    from . import omnigent_runtime as OMNI
    events = []
    try:
        out = OMNI.drive(cfg, LIVE_BRIEF, title='BioSense self-check',
                         on_event=events.append)
    except Exception as e:  # noqa: BLE001 - reported with its reason
        c.row('agents_talk', 'Agents talk to each other (live)', FAIL,
              f'{type(e).__name__}: {e}', t)
        return
    tools = [e for e in events if e.get('kind') == 'tool' and e.get('agent')]
    said = (out.get('last_message') or '').strip()
    good = (out.get('terminal') == 'complete' and out.get('sub_agents', 0) >= 1
            and 'READY' in said and 'FAILED' not in said)
    c.row('agents_talk', 'Agents talk to each other (live)', OK if good else FAIL,
          f'{out.get("sub_agents", 0)} specialist session(s), {len(tools)} specialist tool '
          f'call(s) seen; the orchestrator relayed: {said[:160] or "(nothing)"}', t)


# ── the run ─────────────────────────────────────────────────────────────
def run(*, live=False, runs_dir=None, keep=False, cfg=None, hosted=None):
    """Run every check. Returns {'ok', 'rows', 'sandboxed', 'workdir'}."""
    from . import discovery as DISC
    from . import runtime as RT
    workspace = Path(getattr(cfg, 'workspace', None) or K.ROOT)
    runs = Path(runs_dir or os.environ.get('RUNS_DIR') or K.ROOT / 'runs')
    work = runs / f'selfcheck-{time.strftime("%Y%m%d-%H%M%S")}-{uuid.uuid4().hex[:4]}'
    work.mkdir(parents=True, exist_ok=True)
    hosted = bool(getattr(cfg, 'hosted', False)) if hosted is None else hosted
    found = SB.diagnose(str(workspace))
    sandboxed = found['ok'] and found['mode'] in ('fresh_proc', 'bind_proc')
    if RT.agent_sandbox_state()['sandbox'] == 'off':
        sandboxed = False     # the agents themselves run unconfined; so do these steps
    # The agents may write only under the runs directories inside the workspace.
    # A self-check pointed elsewhere cannot run its steps the way they do, so it
    # runs them directly and every row says so.
    writable = [workspace / 'runs', workspace / 'data' / 'runs']
    if sandboxed and not any(work.resolve().is_relative_to(w.resolve()) for w in writable):
        sandboxed = False
    python = str(workspace / '.venv' / 'bin' / 'python')
    if not Path(python).exists():
        python = sys.executable
    c = Check(workspace, work, sandboxed=sandboxed, bind_proc=found['mode'] == 'bind_proc',
              python=python)
    if RT.agent_sandbox_state()['sandbox'] == 'off':
        c.unsandboxed_why = 'sandbox off by operator choice, as the agents run here'
    elif found['ok'] and not sandboxed:
        c.unsandboxed_why = 'this runs directory is outside the agents\' write paths'
    elif not found['ok']:
        c.unsandboxed_why = 'the sandbox cannot start here'
    try:
        check_sandbox(c, found, hosted)
        check_bundle(c)
        req = DISC.build(project_id=FIXTURE_PROJECT,
                         objective='SELF-CHECK: does every named command exist?',
                         runtime_mode='synthetic_demo')
        rel = os.path.relpath(work, workspace) if work.is_relative_to(workspace) else str(work)
        check_commands(c, DISC.render_brief(req, loop_dir=rel))
        readout = check_bioinformatics(c)
        simulated = check_simulator(c)
        hyp = check_hypothesis(c, readout, simulated)
        check_parameters(c, hyp)
        check_protocol(c)
        check_bioreactor_loop(c)
        up = check_runtime(c, cfg)
        if live:
            if up:
                check_agents_talk(c, cfg)
            else:
                c.row('agents_talk', 'Agents talk to each other (live)', SKIP,
                      'the AI runtime is not available, so no session was started',
                      time.monotonic())
        else:
            c.row('agents_talk', 'Agents talk to each other (live)', SKIP,
                  'offline check; run with --live to start a real session (spends credit)',
                  time.monotonic())
    finally:
        if not keep:
            shutil.rmtree(work, ignore_errors=True)
    ok = not any(r['status'] == FAIL for r in c.rows)
    return {'ok': ok, 'rows': c.rows, 'sandboxed': sandboxed,
            'sandbox_mode': found['mode'], 'workdir': str(work) if keep else None,
            'live': live, 'at': K.now_iso()}


MARK = {OK: '✓', WARN: '!', FAIL: '✗', SKIP: '·'}


def render(report):
    lines = [f'BioSense self-check — {"PASS" if report["ok"] else "FAIL"}']
    for r in report['rows']:
        lines.append(f'  {MARK[r["status"]]} {r["label"]:<34} {r["detail"]}')
    return '\n'.join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--live', action='store_true',
                    help='also start a real agent session (spends model credit)')
    ap.add_argument('--runs', default=None, help='runs directory (default: $RUNS_DIR or ./runs)')
    ap.add_argument('--keep', action='store_true', help='keep the self-check directory')
    ap.add_argument('--json', action='store_true')
    a = ap.parse_args(argv)
    from . import runtime as RT
    try:
        cfg = RT.from_env(runs_dir=Path(a.runs) if a.runs else None)
    except K.ContractError:
        cfg = None
    report = run(live=a.live, runs_dir=a.runs, keep=a.keep, cfg=cfg)
    print(json.dumps(report, indent=2) if a.json else render(report))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
