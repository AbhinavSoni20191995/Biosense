"""One request, three runtimes, one set of artifacts.

A discovery request is answered either by deterministic code over committed
fixtures, or by the real agents through an Omnigent session. The point of this
module is that the *shape* of what comes back does not depend on which: the same
contracts, the same cards, the same protocol summary. What differs is the badge,
and the badge is never cosmetic — it is recorded on the run, carried in every
event and printed on every export.

The synthetic path is `benchmark.runner.run`, which already executes exactly this
flow (objective → uncertainty → evidence → plan → analysis → estimates →
hypothesis → candidate → simulator → next experiment) and scores itself against
seventeen capabilities. Reusing it rather than writing a second deterministic
pipeline means the demonstration and the benchmark cannot drift apart, because
they are the same code.

The real path is `omnigent_runtime.drive`, and afterwards `ingest.run_bundle`
reads whatever the agents wrote. Nothing is taken from the conversation: if the
agents produced no valid artifact, the run says so rather than narrating one.

**There is no fallback between them.** A real run that cannot start raises
`RuntimeUnavailable`. It does not quietly become a synthetic one, here or
anywhere else.
"""
from __future__ import annotations

import re
from pathlib import Path

from .. import contracts as K
from .. import projects as PJ
from ..benchmark import runner as BR
from . import discovery as DISC
from . import ingest as IN
from . import protocol_summary as PS
from . import runtime as RT

# Capabilities the deterministic path is expected to demonstrate. The same list
# the committed public benchmark declares, so a web run is scored no more gently
# than one started from the command line.
EXPECTED = (
    'objective_interpreted', 'project_selected', 'research_context_applied',
    'uncertainty_identified', 'evidence_collected', 'analysis_plan_produced',
    'deterministic_analysis_executed', 'provenance_complete', 'hypothesis_generated',
    'quantitative_effect_supported', 'candidate_parameter_generated',
    'simulator_coverage_checked', 'simulator_handoff', 'predicted_improvement_generated',
    'next_experiment_generated', 'plain_language_generated', 'privacy_validated',
)
BENCH_ID = re.compile(r'[^a-z0-9_]+')


def benchmark_id_for(request):
    """A benchmark id derived from the request id, in the shape the contract wants."""
    raw = BENCH_ID.sub('_', (request['request_id'] or '').lower()).strip('_')
    raw = raw or 'discovery_run'
    if not raw[0].isalpha():
        raw = 'run_' + raw
    return raw[:64].ljust(3, '_')


def _default_uncertainty(request, project):
    """The uncertainty a request did not state, phrased so a plan can cite it.

    Deliberately explicit about being a default: a run that invented its own
    question should say so, because "what the system decided to look into" and
    "what the scientist asked" are different things and only one of them is the
    person's.
    """
    u = request.get('uncertainty')
    if u:
        return u
    pid = next(iter(sorted(request.get('candidate_values') or {})), None)
    if pid is None:
        modelled = sorted(project.modelled_ids()) or sorted(project.parameter_ids)
        pid = modelled[0] if modelled else None
    label = project.parameter(pid).label if pid and project.has(pid) else 'a process parameter'
    return {
        'kind': 'evidence_gap',
        'ref': f'GAP-{pid or "unstated"}',
        'statement': (f'Nothing in this project says whether the current {label} setting is '
                      f'limiting the objective, so a revision would move it blind. This '
                      f'question was chosen by BioSense, not stated in the request.'),
    }


def synthetic_config(request, *, projects_dir=None):
    """A BenchmarkConfig from a DiscoveryRequest.

    They are nearly the same document, which is not a coincidence: the benchmark
    configuration was always the structured form of "what shall we look into".
    """
    K.require_valid('discovery_request', request)
    project = PJ.load(request['project_id'], projects_dir)
    priv = DISC.privacy(request)
    cfg = {
        'schema_version': K.PRODUCTION_VERSION,
        'benchmark_id': benchmark_id_for(request),
        'title': request.get('title') or request['objective'][:120],
        'description': 'A BioSense discovery run started from the web application, answered by '
                       'the deterministic path over committed fixtures.',
        'project_id': project.project_id,
        'objective': request['objective'],
        'mode': 'offline',
        'export_policy': priv['export_policy'],
        'research_context': request.get('research_context'),
        'datasets': list(request.get('dataset_ids') or []),
        'expert_knowledge': list(request.get('expert_knowledge_ids') or []),
        'uncertainty': _default_uncertainty(request, project),
        'control': request.get('control'),
        'candidate_values': request.get('candidate_values'),
        'expected_capabilities': list(EXPECTED),
        'seed': 7,
        'notes': 'SYNTHETIC DEMONSTRATION. No model was called and no number here measures any '
                 'real cell.',
    }
    K.require_valid('benchmark_config', cfg)
    return cfg


def run_synthetic(request, out_dir, *, on_event=None, projects_dir=None, dirs=None,
                  private_root=None):
    """The deterministic path. Writes its artifacts under *out_dir*."""
    emit = on_event or (lambda _e: None)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    cfg = synthetic_config(request, projects_dir=projects_dir)
    K.write_json_atomic(out / 'discovery_request.json', request)
    emit({'kind': 'accepted', 'stage': 'understanding_objective',
          'simple': 'Running the deterministic demonstration path. Nothing here calls a model.',
          'technical': f'benchmark config {cfg["benchmark_id"]}'})

    emit({'kind': 'note', 'stage': 'identifying_uncertainty',
          'simple': cfg['uncertainty']['statement'],
          'technical': f'uncertainty {cfg["uncertainty"]["ref"]}'})
    result = BR.run(cfg, dirs=dirs, private_root=private_root, artifacts_out=out)
    BR.write(result, out)

    # The artifacts the ingestion path reads, written out individually so a
    # synthetic run and a real one leave the same files behind.
    for i, h in enumerate(result.get('hypotheses') or []):
        K.write_json_atomic(out / f'quantified_hypothesis-{i:02d}.json', h)
    if result.get('research_context'):
        K.write_json_atomic(out / 'research_context.json', result['research_context'])
    emit({'kind': 'tool', 'stage': 'running_analysis',
          'simple': f'{len(result.get("analyses") or [])} analysis/analyses executed by '
                    f'deterministic tools.',
          'technical': ', '.join(a['analysis_id'] for a in result.get('analyses') or [])})
    emit({'kind': 'tool', 'stage': 'building_hypothesis',
          'simple': (result['hypotheses'][0]['statement'] if result.get('hypotheses')
                     else 'No hypothesis could be formed from the evidence available.'),
          'technical': f'{len(result.get("hypotheses") or [])} hypothesis/hypotheses'})
    sim = result.get('simulator') or {}
    emit({'kind': 'tool', 'stage': 'testing_simulator',
          'simple': sim.get('prediction_note') or 'Simulator coverage checked.',
          'technical': f'prediction={sim.get("prediction")}'})
    emit({'kind': 'tool', 'stage': 'generating_report',
          'simple': 'Report written: the summary, the limitations and the recommended next '
                    'experiment.',
          'technical': 'summary.md, tables/, figures/, detailed/ written beside the artifacts'})
    emit({'kind': 'terminal', 'stage': 'complete',
          'simple': 'The demonstration run finished.',
          'technical': f'scorecard {result["scorecard"]["passed"]} passed, '
                       f'{result["scorecard"]["failed"]} failed'})
    return {'benchmark': result, 'config': cfg, 'out_dir': str(out)}


def run_real(cfg_runtime, request, out_dir, *, on_event=None, on_session=None,
             should_stop=None, projects_dir=None):
    """The agent path. Starts an Omnigent session and streams it.

    Never falls back: a runtime that cannot start raises, and the caller records
    the reason rather than producing a different kind of answer.
    """
    from . import omnigent_runtime as OMNI
    if not cfg_runtime.is_real:
        raise K.ContractError(
            'run_real needs a real runtime configuration; the synthetic path is a separate '
            'function so that one can never stand in for the other.')
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    K.write_json_atomic(out / 'discovery_request.json', request)
    brief = DISC.render_brief(request, loop_dir=out.name, projects_dir=projects_dir)
    (out / 'brief.md').write_text(brief, encoding='utf-8')
    summary = OMNI.drive(cfg_runtime, brief,
                         title=f'BioSense: {request["objective"][:80]}',
                         on_event=on_event, on_session=on_session,
                         should_stop=should_stop)
    return {'omnigent': summary, 'out_dir': str(out)}


def finish(request, out_dir, *, runtime_mode, projects_dir=None, benchmark=None,
           session=None):
    """Read what the run produced and assemble the payload the interface renders.

    Called for both paths. A real run that wrote nothing valid comes back with
    empty lists, `artifacts_ingested: 0` and the rejections — which the interface
    states plainly instead of showing a result that was never produced.
    """
    bundle = IN.run_bundle(out_dir, project_id=request['project_id'],
                           projects_dir=projects_dir)
    project = PJ.load(request['project_id'], projects_dir)
    priv = DISC.privacy(request)
    simulator = (benchmark or {}).get('simulator')

    protocol = None
    if benchmark is not None:
        protocol = PS.from_benchmark(
            benchmark, project=project, runtime_mode=runtime_mode,
            run_id=Path(out_dir).name, request_id=request['request_id'],
            projects_dir=projects_dir)
    elif bundle['hypotheses']:
        protocol = PS.from_bundle(
            bundle, project=project, objective=request['objective'],
            runtime_mode=runtime_mode, run_id=Path(out_dir).name,
            request_id=request['request_id'],
            research_context=request.get('research_context'), privacy=priv)
    if protocol is not None:
        K.write_json_atomic(Path(out_dir) / 'protocol_summary.json', protocol)
        (Path(out_dir) / 'protocol_summary.md').write_text(
            PS.markdown(protocol), encoding='utf-8')

    return {
        'request': request,
        'runtime_mode': runtime_mode,
        'runtime_label': RT.LABELS[runtime_mode],
        'bundle': bundle,
        'protocol': PS.display(protocol) if protocol else None,
        'benchmark': benchmark,
        'privacy': priv,
        'session': session,
        'summary_text': DISC.summarise(request, projects_dir=projects_dir),
    }
