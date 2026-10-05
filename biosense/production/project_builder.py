"""Making a project: the bioreactor it shares, and the biology it does not.

Every stirred-tank process in this system turns the same vessel controls. An iPSC
to macrophage run and a CAR-T expansion both have an impeller, a dissolved-oxygen
setpoint, a feed schedule and a seeding density, and those mean the same thing in
both — which is why they are the **universal** set here, offered to every new
project with the canonical registry's own bounds.

What is not shared is the biology. M-CSF belongs to a myeloid process and IL-7 to
a T-lineage one, and offering a CAR-T scientist an M-CSF slider because
macrophages needed one implies the model knows something about it. So a project
adds its own parameters, and each one arrives with a declared relationship to the
simulator:

    not_modelled      a real design and evidence variable with no equation. The
                      honest default, and the only one that needs no justifying.
    de_novo_ai        a response an agent proposed from cited claims. It predicts,
                      and every number it produces is uncalibrated and says so.
    expert_declared   the same, supplied by a person from their own experience.
                      Expert knowledge: private, not citable, a design choice.

A fourth kind, `modelled`, cannot be created here at all. That one means the
project's calibrated model has a fitted term, which is a property of the model
and not something a form can confer. `from_template` carries the committed
macrophage profile's modelled parameters across unchanged, because there the
claim is already true.

Nothing in this module writes into the repository's `projects/` directory. A
project a person makes belongs to their workspace, under the private data root.
"""
from __future__ import annotations

import re

from .. import contracts as K
from .. import parameters as PR
from .. import projects as PJ
from .. import workspace as WS

PROJECT_ID = re.compile(r'^[a-z][a-z0-9_]{2,63}$')
STAGE_ID = re.compile(r'^[a-z][a-z0-9_]{1,31}$')
MAX_STAGES = 12
MAX_PARAMETERS = 48
MAX_READOUTS = 12

# The vessel controls every stirred-tank process has. Same canonical id, same
# meaning, same registry bounds, whatever the cells are. A new project gets these
# offered by default; it may decline any of them, because a process without a
# feed schedule should not carry a feed slider.
UNIVERSAL_BIOREACTOR = (
    'seed_density', 'agitation_rpm', 'do_setpoint', 'feed_fraction', 'feed_interval_h',
    'temperature_c', 'stage_duration_days',
)
# Those of the above that the committed iPSC->monocyte model actually has a term
# for. Used only when a project is built on that model; a project with its own
# model or none gets no `modelled` claim from this module.
IPSC_MONOCYTE_KNOBS = {
    'seed_density': 'seed_density', 'agitation_rpm': 'agitation_rpm',
    'do_setpoint': 'do_setpoint', 'feed_fraction': 'feed_fraction',
    'feed_interval_h': 'feed_interval_h', 'rocki_hours': 'rocki_hours',
    'bmp4_ng_ml': 'bmp4', 'vegf_ng_ml': 'vegf', 'mcsf_ng_ml': 'mcsf', 'il3_ng_ml': 'il3',
}
CREATABLE_COVERAGE = ('not_modelled', 'de_novo_ai', 'expert_declared')

DEFAULT_READOUTS = (
    {'readout_id': 'target_cell_yield', 'label': 'Target cells per input cell',
     'unit': 'cells/input_cell', 'kind': 'yield', 'higher_is_better': True,
     'simulator_metric': None},
    {'readout_id': 'viability', 'label': 'Final viability', 'unit': '%',
     'kind': 'viability', 'higher_is_better': True, 'simulator_metric': None},
    {'readout_id': 'identity_purity', 'label': 'Identity marker purity', 'unit': '%',
     'kind': 'identity', 'higher_is_better': True, 'simulator_metric': None},
)
READOUT_KINDS = ('yield', 'viability', 'identity', 'phenotype', 'purity', 'stress',
                 'exhaustion', 'metabolic', 'physical', 'process')


def universal_parameters():
    """The shared bioreactor set, as the interface offers it.

    Read from the canonical registry rather than restated, so a bound only ever
    exists in one place and a project that narrows it has to say why.
    """
    out = []
    for pid in UNIVERSAL_BIOREACTOR:
        p = PR.BY_ID[pid]
        out.append({
            'parameter_id': pid, 'label': p.label, 'unit': p.unit, 'meaning': p.meaning,
            'value_type': p.value_type, 'global_min': p.global_min, 'global_max': p.global_max,
            'stages': list(p.stages), 'universal': True,
            'modelled_by_ipsc_monocyte': pid in IPSC_MONOCYTE_KNOBS,
        })
    return out


def catalogue():
    """Every canonical parameter, split into the shared set and the rest.

    The rest are not "extra": they are the biology-specific knobs a project picks
    from, or extends. Shown with their meanings so the person choosing knows what
    they are selecting rather than recognising an abbreviation.
    """
    universal = universal_parameters()
    uids = {r['parameter_id'] for r in universal}
    specific = []
    for p in PR.PARAMETERS:
        if p.parameter_id in uids:
            continue
        specific.append({
            'parameter_id': p.parameter_id, 'label': p.label, 'unit': p.unit,
            'meaning': p.meaning, 'value_type': p.value_type,
            'global_min': p.global_min, 'global_max': p.global_max,
            'stages': list(p.stages), 'universal': False,
            'modelled_by_ipsc_monocyte': p.parameter_id in IPSC_MONOCYTE_KNOBS,
        })
    return {
        'universal': universal,
        'process_specific': specific,
        'coverage_options': [
            {'value': 'not_modelled',
             'label': 'Design variable only',
             'blurb': 'Real and usable in the lab; no prediction is produced for it. The '
                      'honest default.'},
            {'value': 'de_novo_ai',
             'label': 'AI-proposed response (de novo)',
             'blurb': 'The discovery agents propose how it behaves from cited evidence. It '
                      'predicts, and every number is uncalibrated and labelled DE NOVO.'},
            {'value': 'expert_declared',
             'label': 'You describe how it behaves',
             'blurb': 'You supply the shape and the numbers from your own experience. Private '
                      'expert knowledge: it predicts, and a value resting on it is a design '
                      'choice, never a cited one.'},
        ],
        'note': 'The universal set is the vessel: the same controls, meaning the same things, '
                'in every project. The process-specific set is the biology, and a project only '
                'has what it declares.',
    }


def _stage(raw, index):
    sid = (raw.get('stage_id') or '').strip().lower().replace(' ', '_')
    if not STAGE_ID.match(sid):
        raise K.ContractError(
            f'stage {index + 1}: a stage id is lower-case letters, digits and underscores, '
            f'2 to 32 characters; got {raw.get("stage_id")!r}')
    return {'stage_id': sid, 'label': (raw.get('label') or sid.replace('_', ' ').title()),
            'default_days': raw.get('default_days'), 'min_days': raw.get('min_days'),
            'max_days': raw.get('max_days'), 'wants': raw.get('wants')}


def _parameter(raw, *, stage_ids, simulator_status, model_knobs):
    pid = PR.resolve(raw['parameter_id'])
    p = PR.BY_ID[pid]
    stage = (raw.get('stage') or 'all').strip()
    if stage not in stage_ids and stage != 'all':
        raise K.ContractError(
            f'{pid} names stage {stage!r}, which this project does not have '
            f'({", ".join(sorted(stage_ids))})')
    coverage = raw.get('simulator_coverage') or 'not_modelled'
    if coverage == 'modelled' and simulator_status != 'current':
        # Downgrading this silently would answer "make it modelled" with
        # "fine" and then quietly not do it.
        raise K.ContractError(
            f'{PR.resolve(raw["parameter_id"])} cannot be marked "modelled": this project has '
            f'no calibrated model to have a term in it. Choose "design variable only", or have '
            f'the response proposed or declared.')
    if simulator_status == 'none' and coverage not in PJ.UNCALIBRATED:
        # No calibrated model: an ordinary parameter is simply not predicted.
        # A proposed response survives, because it is the thing that lets a brand
        # new process say anything at all about a dose.
        coverage = 'no_simulator'
    elif coverage == 'modelled':
        # Only a knob the calibrated model genuinely has may claim this, and the
        # mapping has to exist. A form cannot talk a model into having a term.
        if pid not in model_knobs:
            raise K.ContractError(
                f'{pid} cannot be marked "modelled": the calibrated model has no term for it. '
                f'Choose "design variable only", or have the response proposed or declared.')
    elif coverage not in CREATABLE_COVERAGE:
        raise K.ContractError(
            f'{pid}: simulator_coverage must be one of {CREATABLE_COVERAGE} '
            f'(or "modelled" for a knob the calibrated model already has)')

    out = {'parameter_id': pid, 'stage': stage, 'simulator_coverage': coverage}
    for key in ('display_name', 'default_value', 'minimum', 'maximum', 'step', 'notes'):
        if raw.get(key) is not None:
            out[key] = raw[key]
    if raw.get('editable') is not None:
        out['editable'] = bool(raw['editable'])
    if coverage == 'modelled':
        out['simulator_mapping'] = model_knobs[pid]
    if raw.get('bound_origin'):
        out['bound_origin'] = raw['bound_origin']
    # A narrowed bound needs an origin. Supply the person's own statement rather
    # than refusing, when they gave one; refuse when they did not, which is what
    # Project's own validation would do a moment later anyway.
    lo, hi = out.get('minimum'), out.get('maximum')
    narrowed = ((lo is not None and p.global_min is not None and lo > p.global_min)
                or (hi is not None and p.global_max is not None and hi < p.global_max))
    if narrowed and not out.get('bound_origin'):
        raise K.ContractError(
            f'{pid}: this project narrows the range to [{lo}, {hi}] but says nothing about '
            f'where the limit comes from. Name it — equipment, safety, literature, an internal '
            f'experiment or a design choice — because a bound with no origin is a guess '
            f'wearing the clothes of a constraint.')
    if coverage in PJ.UNCALIBRATED:
        out['response_model'] = _response_model(raw.get('response_model'), pid, coverage)
    return out


def _response_model(rm, pid, coverage):
    if not rm:
        raise K.ContractError(
            f'{pid}: you asked for a prediction from a proposed response, so the response has '
            f'to say what it is — a shape, its constants, what it acts on, and why.')
    origin = 'ai_proposed' if coverage == 'de_novo_ai' else 'expert_declared'
    out = {
        'shape': rm.get('shape'), 'target': rm.get('target'),
        'stage': rm.get('stage'), 'optimum': rm.get('optimum'),
        'tolerance': rm.get('tolerance'), 'half_max': rm.get('half_max'),
        'threshold': rm.get('threshold'), 'slope': rm.get('slope'),
        'max_effect': rm.get('max_effect'),
        'origin': origin, 'basis': (rm.get('basis') or '').strip(),
        'references': list(rm.get('references') or []),
        'proposed_by': rm.get('proposed_by'), 'proposed_at': rm.get('proposed_at') or K.now_iso(),
        'calibrated': False,
        'evidence_status': rm.get('evidence_status'),
    }
    return {k: v for k, v in out.items() if v is not None or k in (
        'stage', 'optimum', 'tolerance', 'half_max', 'threshold', 'slope', 'evidence_status',
        'proposed_by')}


def build(*, project_id, name, biological_system, stages, parameters, readouts=None,
          simulator=None, description=None, version='0.1.0', limitations=None,
          template_of=None):
    """A validated ProjectProfile. Refuses rather than guessing.

    The returned document is checked twice: against the schema, and by loading it
    as a `Project`, which is where the bound, coverage and response-model rules
    live. A profile that would not load is never written.
    """
    pid = (project_id or '').strip().lower().replace(' ', '_').replace('-', '_')
    if not PROJECT_ID.match(pid):
        raise K.ContractError(
            'a project id is lower-case letters, digits and underscores, 3 to 64 characters')
    if len((name or '').strip()) < 3:
        raise K.ContractError('a project needs a name of at least 3 characters')
    if not stages:
        raise K.ContractError('a project needs at least one stage: a process happens in order')
    if len(stages) > MAX_STAGES:
        raise K.ContractError(f'{len(stages)} stages; the limit is {MAX_STAGES}')
    if len(parameters or []) > MAX_PARAMETERS:
        raise K.ContractError(f'{len(parameters)} parameters; the limit is {MAX_PARAMETERS}')

    sim = dict(simulator or {'model_id': None, 'status': 'none'})
    sim.setdefault('status', 'none' if not sim.get('model_id') else 'current')
    if sim['status'] == 'none':
        sim.setdefault('not_modelled_note',
                       'This project has no mechanistic model. Its parameters are real design '
                       'variables; nothing here predicts what they would do, and no other '
                       "project's model is borrowed.")
    model_knobs = IPSC_MONOCYTE_KNOBS if sim.get('model_id') == 'ipsc_monocyte_v1' else {}

    stage_docs = [_stage(s, i) for i, s in enumerate(stages)]
    stage_ids = {s['stage_id'] for s in stage_docs}
    if len(stage_ids) != len(stage_docs):
        raise K.ContractError('two stages share an id; a stage is named once')

    seen, param_docs = set(), []
    for raw in parameters or []:
        doc = _parameter(raw, stage_ids=stage_ids, simulator_status=sim['status'],
                         model_knobs=model_knobs)
        if doc['parameter_id'] in seen:
            raise K.ContractError(
                f'{doc["parameter_id"]} is listed twice. One project, one definition of a knob.')
        seen.add(doc['parameter_id'])
        param_docs.append(doc)

    reads = [dict(r) for r in (readouts or DEFAULT_READOUTS)]
    if len(reads) > MAX_READOUTS:
        raise K.ContractError(f'{len(reads)} readouts; the limit is {MAX_READOUTS}')
    for r in reads:
        if r.get('kind') not in READOUT_KINDS:
            raise K.ContractError(
                f'readout {r.get("readout_id")!r}: say what kind of measurement it is — one of '
                f'{", ".join(READOUT_KINDS)}. A readout with no kind cannot be grouped with the '
                f'ones it belongs beside.')
        # A readout a project's model does not produce gets no simulator metric,
        # whatever was asked for: that is how a project ends up showing a
        # prediction for something nothing computed.
        if sim['status'] != 'current':
            r['simulator_metric'] = None

    limits = list(limitations or [])
    if any(p['simulator_coverage'] in PJ.UNCALIBRATED for p in param_docs):
        limits.append(
            'Some parameters predict through a response that was proposed rather than fitted to '
            'data. Those predictions show what the proposal implies and are not calibrated.')
    if sim['status'] == 'none':
        limits.append('No mechanistic model: this project produces no simulated prediction.')
    limits.append('A project profile records what a process has and what is known about it. It '
                  'is not a validated manufacturing process and no part of it is approved.')

    doc = {
        'schema_version': K.PRODUCTION_VERSION,
        'project_id': pid, 'name': name.strip(), 'version': version,
        'created_at': K.now_iso(), 'description': description,
        'template_of': template_of,
        'biological_system': biological_system,
        'stages': stage_docs, 'parameters': param_docs, 'readouts': reads,
        'simulator': sim, 'limitations': sorted(set(limits)),
    }
    K.require_valid('project_profile', doc)
    PJ.Project(doc)          # the rules that live in the loader, not the schema
    return doc


def from_template(template_id, *, project_id, name, biological_system=None,
                  description=None, templates_dir=None):
    """A new project starting from a committed profile.

    Everything is carried across, `modelled` claims included, because they were
    true of that model and the new project runs the same one. The id, the name
    and the biological system are the person's.
    """
    base = PJ.load(template_id, templates_dir)
    doc = dict(base.doc)
    doc['project_id'] = (project_id or '').strip().lower().replace(' ', '_').replace('-', '_')
    doc['name'] = (name or '').strip()
    doc['created_at'] = K.now_iso()
    doc['template_of'] = base.project_id
    doc['version'] = '0.1.0'
    if description is not None:
        doc['description'] = description
    if biological_system:
        doc['biological_system'] = biological_system
    if not PROJECT_ID.match(doc['project_id']):
        raise K.ContractError(
            'a project id is lower-case letters, digits and underscores, 3 to 64 characters')
    if len(doc['name']) < 3:
        raise K.ContractError('a project needs a name of at least 3 characters')
    K.require_valid('project_profile', doc)
    PJ.Project(doc)
    return doc


def templates(directory=None):
    """The committed profiles, as starting points."""
    out = []
    for p in PJ.load_all(directory):
        d = p.summary()
        out.append({'project_id': p.project_id, 'name': p.name, 'version': p.version,
                    'description': p.doc.get('description'),
                    'biological_system': d['biological_system'],
                    'stages': d['stages'], 'parameter_count': len(p.parameter_ids),
                    'has_simulator': bool(p.simulator.get('model_id')),
                    'modelled': d['modelled']})
    return out


# ── storage: a workspace's own projects ─────────────────────────────────
def save(identity, doc, *, overwrite=False):
    """Write a project into its owner's workspace. Never into the repository."""
    d = WS.projects_dir(identity, create=True)
    path = d / f'{doc["project_id"]}.json'
    if path.exists() and not overwrite:
        raise K.ContractError(
            f'you already have a project called {doc["project_id"]!r}. Give this one another '
            f'id, or open the existing one.')
    WS.assert_owned(identity, path)
    K.write_json_atomic(path, doc)
    return path


def delete(identity, project_id):
    path = WS.assert_owned(identity, WS.projects_dir(identity) / f'{project_id}.json')
    if not path.is_file():
        raise K.ContractError(f'no project {project_id!r} in this workspace')
    path.unlink()
    return True


def workspace_projects(identity):
    """The projects this workspace owns. Templates are listed separately."""
    d = WS.projects_dir(identity)
    if not d.is_dir():
        return []
    out = []
    for path in sorted(d.glob('*.json')):
        try:
            out.append(PJ.Project(K.read_json(path)))
        except (K.ContractError, ValueError, OSError):
            continue
    return out


def load_for(identity, project_id, *, templates_dir=None):
    """A project by id: the workspace's own first, then the committed templates.

    Own first, deliberately: a person who makes a project named after a template
    means theirs.
    """
    d = WS.projects_dir(identity)
    path = d / f'{project_id}.json'
    if path.is_file():
        WS.assert_owned(identity, path)
        return PJ.Project(K.read_json(path))
    return PJ.load(project_id, templates_dir)


def available_for(identity, *, templates_dir=None):
    """Every project this workspace may open, with where each came from."""
    rows = []
    for p in workspace_projects(identity):
        rows.append({'project_id': p.project_id, 'name': p.name, 'version': p.version,
                     'source': 'workspace', 'owned': True,
                     'has_simulator': bool(p.simulator.get('model_id')),
                     'template_of': p.doc.get('template_of')})
    own = {r['project_id'] for r in rows}
    for p in PJ.load_all(templates_dir):
        if p.project_id in own:
            continue
        rows.append({'project_id': p.project_id, 'name': p.name, 'version': p.version,
                     'source': 'template', 'owned': False,
                     'has_simulator': bool(p.simulator.get('model_id')),
                     'template_of': None})
    return rows
