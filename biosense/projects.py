"""Projects: which knobs a particular biological system actually has.

BioSense does not have one control panel. An iPSC-to-macrophage process and a
CAR-T expansion share an orchestrator and share nothing else — different stages,
different parameters, different readouts, and in one case no mechanistic model at
all. A ProjectProfile is the answer to "what can I turn, here?".

Three rules this module enforces, because each of them is a way the system could
quietly mislead someone:

* **A project does not inherit a parameter because the registry defines one.**
  `load('cart_expansion_v1').parameter_ids` contains no M-CSF. Showing a CAR-T
  scientist an M-CSF slider because a macrophage project needed one is not a
  cosmetic problem: it implies the model knows something about it.

* **Every narrowed bound names its origin.** A project may say agitation is
  80-130 rpm where the registry allows 0-400, but it says whether that came from
  equipment, literature, an internal experiment or a design choice. A bound with
  no origin is a guess wearing the clothes of a constraint.

* **Coverage is declared, never inferred.** `simulator_coverage` is `modelled`
  only when the project's model has a term for the parameter. A real parameter
  the model cannot predict is `not_modelled` and stays visible and usable as a
  design and evidence variable; it simply produces no prediction. Dropping it, or
  predicting it anyway, are both worse than saying so.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import contracts as K
from . import parameters as PR

PROFILE_DIR = K.ROOT / 'projects'
COVERAGE = ('modelled', 'not_modelled', 'no_simulator', 'de_novo_ai', 'expert_declared')
# Coverage kinds that produce a prediction. `modelled` is the calibrated model's
# own term; the other two are a shape and some constants somebody proposed, which
# deterministic code then evaluates. They predict, and every number they produce
# carries that provenance.
PREDICTING = ('modelled', 'de_novo_ai', 'expert_declared')
UNCALIBRATED = ('de_novo_ai', 'expert_declared')
RESPONSE_SHAPES = ('bell', 'saturating', 'linear', 'threshold')
RESPONSE_TARGETS = ('growth', 'death', 'viability', 'transition_efficiency', 'harvest')
RESPONSE_ORIGINS = ('ai_proposed', 'expert_declared')
BOUND_SOURCES = ('process_design', 'literature', 'expert_knowledge', 'prior_experiment',
                 'equipment_limit', 'safety_constraint', 'model_validity', 'design_choice')


@dataclass(frozen=True)
class ProjectParameter:
    """One knob, as this project has it."""
    parameter_id: str
    stage: str
    simulator_coverage: str
    simulator_mapping: str = None
    display_name: str = None
    default_value: object = None
    minimum: float = None
    maximum: float = None
    step: float = None
    editable: bool = True
    bound_origin: dict = None
    response_model: dict = None
    notes: str = None

    @property
    def canonical(self):
        return PR.get(self.parameter_id)

    @property
    def label(self):
        return self.display_name or self.canonical.label

    @property
    def unit(self):
        return self.canonical.unit

    @property
    def modelled(self):
        """Whether the project's own calibrated model has a term for this."""
        return self.simulator_coverage == 'modelled'

    @property
    def predicts(self):
        """Whether anything at all can produce a number for this parameter."""
        return self.simulator_coverage in PREDICTING

    @property
    def uncalibrated(self):
        """Whether a prediction for this rests on a proposed term rather than a fitted one."""
        return self.simulator_coverage in UNCALIBRATED

    def to_dict(self):
        c = self.canonical
        return {**self.__dict__, 'label': self.label, 'unit': c.unit,
                'meaning': c.meaning, 'value_type': c.value_type,
                'global_min': c.global_min, 'global_max': c.global_max}


class Project:
    """A loaded, validated profile."""

    def __init__(self, doc):
        K.require_valid('project_profile', doc)
        self.doc = doc
        self.project_id = doc['project_id']
        self.name = doc['name']
        self.version = doc['version']
        self.stages = doc['stages']
        self.readouts = doc['readouts']
        self.simulator = doc['simulator']
        self._params = {}
        stage_ids = {s['stage_id'] for s in doc['stages']} | {'all'}
        for p in doc['parameters']:
            pid = PR.resolve(p['parameter_id'])
            if pid != p['parameter_id']:
                raise K.ContractError(
                    f'{self.project_id}: parameter {p["parameter_id"]!r} is an alias of '
                    f'{pid!r}. A profile stores the canonical id, so that two profiles '
                    f'naming the same knob are comparable.')
            if p['stage'] not in stage_ids:
                raise K.ContractError(
                    f'{self.project_id}: parameter {pid} names stage {p["stage"]!r}, which this '
                    f'project does not have ({", ".join(sorted(stage_ids))})')
            self._check_bounds(pid, p)
            self._check_coverage(pid, p)
            self._params[pid] = ProjectParameter(
                parameter_id=pid, stage=p['stage'],
                simulator_coverage=p['simulator_coverage'],
                simulator_mapping=p.get('simulator_mapping'),
                display_name=p.get('display_name'), default_value=p.get('default_value'),
                minimum=p.get('minimum'), maximum=p.get('maximum'), step=p.get('step'),
                editable=p.get('editable', True), bound_origin=p.get('bound_origin'),
                response_model=p.get('response_model'), notes=p.get('notes'))

    # ── validation ─────────────────────────────────────────────────────
    def _check_bounds(self, pid, p):
        c = PR.BY_ID[pid]
        lo, hi = p.get('minimum'), p.get('maximum')
        if lo is not None and hi is not None and lo >= hi:
            raise K.ContractError(f'{self.project_id}/{pid}: minimum must be below maximum')
        for name, v, cmpv, bad in (('minimum', lo, c.global_min, lambda a, b: a < b),
                                   ('maximum', hi, c.global_max, lambda a, b: a > b)):
            if v is not None and cmpv is not None and bad(v, cmpv):
                raise K.ContractError(
                    f'{self.project_id}/{pid}: {name} {v} is outside the canonical range '
                    f'[{c.global_min}, {c.global_max}]. A project narrows a global bound; it '
                    f'does not widen one.')
        narrowed = ((lo is not None and c.global_min is not None and lo > c.global_min)
                    or (hi is not None and c.global_max is not None and hi < c.global_max))
        if narrowed and not p.get('bound_origin'):
            raise K.ContractError(
                f'{self.project_id}/{pid}: this project narrows the range to '
                f'[{lo}, {hi}] but records no bound_origin. Say whether that came from '
                f'equipment, literature, an internal experiment or a design choice — a bound '
                f'with no origin is a guess wearing the clothes of a constraint.')
        origin = p.get('bound_origin')
        if origin and origin['source'] not in BOUND_SOURCES:
            raise K.ContractError(f'{self.project_id}/{pid}: bound_origin.source must be one of '
                                  f'{BOUND_SOURCES}')
        d = p.get('default_value')
        if isinstance(d, (int, float)) and not isinstance(d, bool):
            if (lo is not None and d < lo) or (hi is not None and d > hi):
                raise K.ContractError(
                    f'{self.project_id}/{pid}: default {d} is outside this project\'s own '
                    f'range [{lo}, {hi}]')

    def _check_coverage(self, pid, p):
        cov, mapping = p['simulator_coverage'], p.get('simulator_mapping')
        status = self.doc['simulator']['status']
        if cov not in COVERAGE:
            raise K.ContractError(
                f'{self.project_id}/{pid}: simulator_coverage must be one of {COVERAGE}')
        if cov == 'modelled' and not mapping:
            raise K.ContractError(
                f'{self.project_id}/{pid}: simulator_coverage is "modelled" but no '
                f'simulator_mapping is given. Claiming a model covers a parameter without '
                f'saying which term it is would make a prediction unverifiable.')
        if cov == 'modelled' and status != 'current':
            raise K.ContractError(
                f'{self.project_id}/{pid}: simulator_coverage "modelled" needs a simulator with '
                f'status "current"; this project\'s simulator is {status!r}.')
        if cov != 'modelled' and mapping:
            raise K.ContractError(
                f'{self.project_id}/{pid}: a simulator_mapping is given but coverage is '
                f'{cov!r}. One of the two is wrong.')
        if status == 'none' and cov not in ('no_simulator',) + UNCALIBRATED:
            # `simulator.status` describes the project's CALIBRATED model. A
            # project without one can still carry a response somebody proposed —
            # that is the ordinary case for a new process, where the literature
            # says a dose has an optimum and no model has ever been fitted. What
            # it cannot carry is a `modelled` claim, which is checked above.
            raise K.ContractError(
                f'{self.project_id}/{pid}: this project has no calibrated model, so a '
                f'parameter is either "no_simulator", or predicts through a response that '
                f'says it was proposed rather than fitted (got {cov!r}).')
        self._check_response_model(pid, p, cov)

    def _check_response_model(self, pid, p, cov):
        """A proposed term must say what it does, and only a proposed term may.

        The point of separating `de_novo_ai` and `expert_declared` from
        `modelled` is that a reader can tell a fitted term from a suggested one.
        That only holds if a suggested term actually carries its shape, its
        constants and who suggested them -- and if a calibrated parameter cannot
        quietly acquire a second, unfitted definition sitting beside the real one.
        """
        rm = p.get('response_model')
        if cov not in UNCALIBRATED:
            if rm:
                raise K.ContractError(
                    f'{self.project_id}/{pid}: a response_model is given but coverage is '
                    f'{cov!r}. A response_model is how a de_novo_ai or expert_declared '
                    f'parameter acts; a calibrated parameter\'s behaviour belongs in the '
                    f'model, where it can be versioned and checked.')
            return
        if not rm:
            raise K.ContractError(
                f'{self.project_id}/{pid}: coverage {cov!r} promises a prediction but no '
                f'response_model says how the parameter acts. A term that predicts without '
                f'saying what it computes is not reviewable.')
        if rm.get('shape') not in RESPONSE_SHAPES:
            raise K.ContractError(
                f'{self.project_id}/{pid}: response_model.shape must be one of '
                f'{RESPONSE_SHAPES}')
        if rm.get('target') not in RESPONSE_TARGETS:
            raise K.ContractError(
                f'{self.project_id}/{pid}: response_model.target must be one of '
                f'{RESPONSE_TARGETS}')
        if rm.get('origin') not in RESPONSE_ORIGINS:
            raise K.ContractError(
                f'{self.project_id}/{pid}: response_model.origin must be one of '
                f'{RESPONSE_ORIGINS}')
        want_origin = 'ai_proposed' if cov == 'de_novo_ai' else 'expert_declared'
        if rm['origin'] != want_origin:
            raise K.ContractError(
                f'{self.project_id}/{pid}: coverage {cov!r} and response_model.origin '
                f'{rm["origin"]!r} disagree about who proposed this term.')
        if rm.get('calibrated') is not False:
            raise K.ContractError(
                f'{self.project_id}/{pid}: response_model.calibrated must be false. A term '
                f'fitted to data is not de novo; give it a model version instead.')
        if len((rm.get('basis') or '').strip()) < 10:
            raise K.ContractError(
                f'{self.project_id}/{pid}: response_model.basis must say why this shape and '
                f'these constants, in at least 10 characters. A proposed term with no stated '
                f'reasoning is a number nobody can argue with.')
        needed = {'bell': ('optimum', 'tolerance'), 'saturating': ('half_max',),
                  'linear': ('slope',), 'threshold': ('threshold',)}[rm['shape']]
        missing = [k for k in needed if rm.get(k) is None]
        if missing:
            raise K.ContractError(
                f'{self.project_id}/{pid}: a {rm["shape"]!r} response needs '
                f'{", ".join(needed)}; missing {", ".join(missing)}.')
        if rm['shape'] == 'bell' and float(rm['tolerance']) <= 0:
            raise K.ContractError(
                f'{self.project_id}/{pid}: response_model.tolerance must be above zero')
        eff = rm.get('max_effect')
        if eff is None or not -1.0 <= float(eff) <= 4.0:
            raise K.ContractError(
                f'{self.project_id}/{pid}: response_model.max_effect must be between -1 and 4. '
                f'It is bounded so a proposed term cannot overwhelm the calibrated biology it '
                f'sits beside.')

    # ── access ─────────────────────────────────────────────────────────
    @property
    def parameter_ids(self):
        return list(self._params)

    def parameter(self, name):
        pid = PR.resolve(name)
        if pid not in self._params:
            raise K.ContractError(
                f'{self.project_id} does not expose {pid!r}. This project\'s parameters are: '
                f'{", ".join(sorted(self._params))}. A parameter is not available here merely '
                f'because the canonical registry defines it.')
        return self._params[pid]

    def has(self, name):
        pid = PR.resolve(name, required=False)
        return pid is not None and pid in self._params

    def coverage(self, name):
        """'modelled' / 'not_modelled' / 'no_simulator' / 'not_in_project'."""
        pid = PR.resolve(name, required=False)
        if pid is None or pid not in self._params:
            return 'not_in_project'
        return self._params[pid].simulator_coverage

    def modelled_ids(self):
        return [p.parameter_id for p in self._params.values() if p.modelled]

    def readout(self, readout_id):
        r = next((x for x in self.readouts if x['readout_id'] == readout_id), None)
        if r is None:
            raise K.ContractError(f'{self.project_id} has no readout {readout_id!r}')
        return r

    def defaults(self):
        return {pid: p.default_value for pid, p in self._params.items()
                if p.default_value is not None}

    def summary(self):
        return {
            'project_id': self.project_id, 'name': self.name, 'version': self.version,
            'biological_system': self.doc['biological_system'],
            'stages': [s['stage_id'] for s in self.stages],
            'parameters': [p.to_dict() for p in self._params.values()],
            'readouts': self.readouts,
            'simulator': self.simulator,
            'modelled': self.modelled_ids(),
            'not_modelled': [p.parameter_id for p in self._params.values()
                             if p.simulator_coverage == 'not_modelled'],
            'limitations': self.doc['limitations'],
        }


# ── loading ──────────────────────────────────────────────────────────────
def available(directory=None):
    d = Path(directory or PROFILE_DIR)
    return sorted(p.stem for p in d.glob('*.json')) if d.is_dir() else []


def load(project_id, directory=None):
    d = Path(directory or PROFILE_DIR)
    path = d / f'{project_id}.json'
    if not path.is_file():
        raise K.ContractError(
            f'no project profile {project_id!r} in {d}. Available: '
            f'{", ".join(available(d)) or "none"}')
    return Project(K.read_json(path))


def load_all(directory=None):
    return [load(pid, directory) for pid in available(directory)]
