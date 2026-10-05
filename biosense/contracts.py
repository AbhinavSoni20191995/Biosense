"""Versioned contracts, schema validation, hashing and atomic artifact writes."""
import datetime as dt
import hashlib
import json
import os
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path

import jsonschema

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_DIR = ROOT / 'schemas'
PACKAGE_DIR = Path(__file__).resolve().parent

SCENARIO_VERSION = '1.0'
EXPERIMENT_VERSION = '1.0'
RESULT_VERSION = '1.0'
EVALUATION_VERSION = '1.0'
CAMPAIGN_VERSION = '1.0'
PRODUCTION_VERSION = '2.0'

MODES = ('synthetic_demo', 'evidence_based')
PROVENANCE_TYPES = ('reported', 'derived', 'fitted', 'synthetic_assumption')
NEXT_ACTION_TYPES = ('run_experiment', 'request_evidence', 'request_calibration',
                     'stop_target', 'stop_budget', 'stop_invalid_model')

SCHEMAS = {
    'scenario': 'simulation_scenario.schema.json',
    'experiment': 'experiment.schema.json',
    'objective': 'objective.schema.json',
    'result': 'simulation_result.schema.json',
    'evaluation': 'outcome_evaluation.schema.json',
    'next_action': 'next_action.schema.json',
    'literature_handoff': '../output.schema.json',
    # production loop (literature protocol -> bioreactor -> analysis -> orchestrator)
    'production_request': 'production_request.schema.json',
    'production_protocol': 'production_protocol.schema.json',
    'bioreactor_run': 'bioreactor_run.schema.json',
    'analysis_report': 'analysis_report.schema.json',
    'loop_decision': 'loop_decision.schema.json',
    'human_consult': 'human_consult.schema.json',
    'bioinformatics_report': 'bioinformatics_report.schema.json',
    # data-aware bioinformatics: a dataset, the analysis planned against one named
    # uncertainty, and what that analysis computed
    'dataset_manifest': 'dataset_manifest.schema.json',
    'analysis_plan': 'analysis_plan.schema.json',
    'analysis_result': 'analysis_result.schema.json',
    # a project: which knobs a particular biological system actually has
    'project_profile': 'project_profile.schema.json',
    # one number and where it came from; the unit of quantified evidence
    'estimate': 'estimate.schema.json',
    'quantified_hypothesis': 'quantified_hypothesis.schema.json',
    'research_context': 'research_context.schema.json',
    'expert_knowledge': 'expert_knowledge.schema.json',
    # benchmarks: a reproducible demonstration and its capability scorecard
    'benchmark_config': 'benchmark_config.schema.json',
    'benchmark_result': 'benchmark_result.schema.json',
}

BIOSENSE_VERSION = '0.2.0'

# What a piece of evidence IS, kept separate from where it came from and from who
# may see it. An analysis computed over a private FACS table is a derived_analysis
# whose source is private_user_dataset and whose visibility is private; collapsing
# those three into one label is how provenance gets lost.
EVIDENCE_CLASSES = ('published_literature', 'public_dataset', 'private_user_dataset',
                    'derived_analysis', 'simulation', 'real_measurement',
                    'synthetic_fixture', 'expert_knowledge')
VISIBILITIES = ('public', 'private')


class ContractError(ValueError):
    """Raised when an artifact violates its declared contract."""


@lru_cache(maxsize=None)
def load_schema(kind):
    return json.loads((SCHEMA_DIR / SCHEMAS[kind]).read_text())


@lru_cache(maxsize=None)
def _registry():
    """All project schemas by $id, so cross-schema $ref works offline."""
    from referencing import Registry, Resource
    resources = [(s['$id'], Resource.from_contents(s))
                 for s in (load_schema(k) for k in SCHEMAS) if '$id' in s]
    return Registry().with_resources(resources)


def schema_errors(kind, obj):
    """Return a sorted list of human-readable schema violations (empty if valid)."""
    schema = load_schema(kind)
    validator = jsonschema.validators.validator_for(schema)(schema, registry=_registry())
    errors = []
    for e in validator.iter_errors(obj):
        where = '/'.join(str(p) for p in e.absolute_path) or '<root>'
        errors.append(f'{where}: {e.message}')
    return sorted(errors)


def require_valid(kind, obj):
    errors = schema_errors(kind, obj)
    if errors:
        raise ContractError(f'{kind} violates schema: ' + '; '.join(errors[:10]))
    return obj


def canonical_json(obj):
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def sha256_obj(obj):
    return hashlib.sha256(canonical_json(obj).encode('utf-8')).hexdigest()


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def code_sha256():
    """Hash of every Python file in the package, in path order."""
    h = hashlib.sha256()
    for p in sorted(PACKAGE_DIR.rglob('*.py')):
        h.update(str(p.relative_to(PACKAGE_DIR)).encode())
        h.update(p.read_bytes())
    return h.hexdigest()


def code_revision():
    """Git commit and dirty flag when available; None otherwise (never fatal)."""
    try:
        rev = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, capture_output=True, text=True, timeout=5)
        dirty = subprocess.run(['git', 'status', '--porcelain', '--', 'biosense'], cwd=ROOT, capture_output=True, text=True, timeout=5)
        if rev.returncode != 0:
            return None
        return {'commit': rev.stdout.strip(), 'package_dirty': bool(dirty.stdout.strip())}
    except (OSError, subprocess.SubprocessError):
        return None


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def read_json(path):
    return json.loads(Path(path).read_text())


def write_json_atomic(path, obj, *, overwrite=True):
    """Write JSON via a temporary file and rename. overwrite=False refuses to replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not overwrite and path.exists():
        raise ContractError(f'Refusing to overwrite immutable artifact {path}')
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
        if not overwrite:
            os.link(tmp, path)  # fails if path appeared meanwhile
            os.unlink(tmp)
        else:
            os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return path


def write_text_atomic(path, text, *, overwrite=True):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not overwrite and path.exists():
        raise ContractError(f'Refusing to overwrite immutable artifact {path}')
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name, suffix='.tmp')
    with os.fdopen(fd, 'w') as f:
        f.write(text)
    os.replace(tmp, path)
    return path
