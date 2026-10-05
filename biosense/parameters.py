"""The canonical identity of a process parameter.

Phase 1 ended with three vocabularies for the same quantities: the simulator
called it `mcsf`, the implication map called it `mcsf_ng_ml`, and an analysis
report's lever called it "M-CSF dose/window". Four of ten names lined up. A
candidate parameter could not reach the simulator, and the failure was silent.

This module is the single answer to "which parameter is this?". It holds what is
true of a parameter **everywhere**:

    parameter_id      the canonical name, used in every contract
    label, unit       how to show it
    value_type        number | integer | choice | text
    aliases           every spelling seen in the wild, resolved to the id
    meaning           what it is, biologically
    global bounds     limits that hold whatever the project (often none)
    stages            process stages it can apply to
    provenance        what a value of it must carry

What it deliberately does **not** hold is a simulator mapping. Whether a
parameter is modelled depends on which model a project runs, so that belongs to
the ProjectProfile. A registry entry that claimed `mcsf_ng_ml -> mcsf` would be
asserting something about every project, including the ones whose simulator has
no M-CSF term at all.

`resolve` never guesses. A name that is not a parameter_id and not a registered
alias raises, and the error lists what was close. Silent fuzzy matching here
would reintroduce exactly the bug this module exists to kill.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

from . import contracts as K

VALUE_TYPES = ('number', 'integer', 'choice', 'text')
PROVENANCE_CLASSES = ('reported', 'adapted', 'design_choice', 'gap')
ID_RE = re.compile(r'^[a-z][a-z0-9_]{2,47}$')


@dataclass(frozen=True)
class Parameter:
    parameter_id: str
    label: str
    unit: str
    meaning: str
    value_type: str = 'number'
    aliases: tuple = ()
    global_min: float = None
    global_max: float = None
    stages: tuple = ()
    choices: tuple = ()
    provenance_required: bool = True
    notes: str = ''

    def __post_init__(self):
        if not ID_RE.match(self.parameter_id):
            raise K.ContractError(f'{self.parameter_id!r} is not a usable parameter_id')
        if self.value_type not in VALUE_TYPES:
            raise K.ContractError(f'{self.parameter_id}: value_type must be one of {VALUE_TYPES}')
        if (self.global_min is not None and self.global_max is not None
                and self.global_min >= self.global_max):
            raise K.ContractError(f'{self.parameter_id}: global_min must be below global_max')

    def clamp(self, value):
        """Clamp to the global bounds, returning (value, was_clamped)."""
        if self.value_type in ('choice', 'text'):
            return value, False
        v = float(value)
        lo = self.global_min if self.global_min is not None else v
        hi = self.global_max if self.global_max is not None else v
        out = min(max(v, lo), hi)
        return out, abs(out - v) > 1e-12

    def to_dict(self):
        d = dict(self.__dict__)
        for key in ('aliases', 'stages', 'choices'):
            d[key] = list(d[key])
        return d


def _p(*a, **kw):
    return Parameter(*a, **kw)


# ── the registry ─────────────────────────────────────────────────────────
# Bounds here are the limits of physical or biological sense, not a process
# window. A project narrows them, and records why it narrowed them.
PARAMETERS = (
    _p('mcsf_ng_ml', 'M-CSF', 'ng/mL',
       'Macrophage colony-stimulating factor concentration. Drives myeloid '
       'commitment and monocyte/macrophage survival.',
       aliases=('mcsf', 'm-csf', 'm_csf', 'csf1', 'macrophage colony stimulating factor',
                'M-CSF dose/window'),
       global_min=0.0, global_max=500.0, stages=('myeloid', 'macrophage_maturation')),
    _p('il3_ng_ml', 'IL-3', 'ng/mL',
       'Interleukin-3 concentration. Supports myeloid progenitor expansion.',
       aliases=('il3', 'il-3', 'IL-3 dose/window'),
       global_min=0.0, global_max=300.0, stages=('myeloid', 'hemato')),
    _p('il7_ng_ml', 'IL-7', 'ng/mL',
       'Interleukin-7 concentration. Supports T-lineage survival and expansion.',
       aliases=('il7', 'il-7', 'IL-7 dose/window'),
       global_min=0.0, global_max=200.0, stages=('t_commitment', 'maturation', 'expansion')),
    _p('il15_ng_ml', 'IL-15', 'ng/mL',
       'Interleukin-15 concentration. Supports memory-phenotype T-cell expansion.',
       aliases=('il15', 'il-15', 'IL-15 dose/window'),
       global_min=0.0, global_max=200.0, stages=('expansion', 'maturation')),
    _p('il2_ng_ml', 'IL-2', 'ng/mL',
       'Interleukin-2 concentration. Drives T-cell proliferation.',
       aliases=('il2', 'il-2', 'IL-2 dose/window'),
       global_min=0.0, global_max=500.0, stages=('expansion',)),
    _p('bmp4_ng_ml', 'BMP4', 'ng/mL',
       'Bone morphogenetic protein 4 concentration. Mesoderm specification.',
       aliases=('bmp4', 'bmp-4', 'BMP4 dose/window'),
       global_min=0.0, global_max=200.0, stages=('mesoderm',)),
    _p('vegf_ng_ml', 'VEGF', 'ng/mL',
       'Vascular endothelial growth factor concentration. Mesoderm and '
       'hemogenic endothelium specification.',
       aliases=('vegf', 'VEGF dose/window'),
       global_min=0.0, global_max=400.0, stages=('mesoderm', 'hemato')),
    _p('agitation_rpm', 'Agitation', 'rpm',
       'Impeller speed. Sets shear stress and the equilibrium aggregate '
       'diameter, and therefore trades aggregate size against cell death.',
       aliases=('agitation', 'rpm', 'stirring', 'impeller_speed'),
       global_min=0.0, global_max=400.0, stages=('all',)),
    _p('do_setpoint', 'Dissolved oxygen', 'fraction',
       'Dissolved-oxygen setpoint as a fraction of saturation. Sets how deep '
       'into an aggregate the viable shell reaches.',
       aliases=('do', 'dissolved_oxygen', 'oxygen_tension', 'po2'),
       global_min=0.01, global_max=1.0, stages=('all',)),
    _p('feed_fraction', 'Feed exchange', 'fraction of volume',
       'Fraction of the working volume exchanged at each feed.',
       aliases=('feed', 'medium_exchange', 'exchange_fraction'),
       global_min=0.0, global_max=1.0, stages=('all',)),
    _p('feed_interval_h', 'Feed interval', 'h',
       'Hours between medium exchanges.',
       aliases=('feed_interval', 'feeding_interval'),
       global_min=1.0, global_max=240.0, stages=('all',)),
    _p('seed_density', 'Seed density', '1e6 cells/mL',
       'Viable cells seeded per mL at the start of the stage.',
       aliases=('seeding density', 'seeding_density', 'inoculation_density', 'cell_density'),
       global_min=0.001, global_max=50.0, stages=('all',)),
    _p('rocki_hours', 'ROCK inhibitor', 'h',
       'Hours of ROCK-inhibitor exposure after seeding. Single-cell survival.',
       aliases=('rocki', 'rock_inhibitor', 'y27632_hours'),
       global_min=0.0, global_max=96.0, stages=('expansion',)),
    _p('stage_duration_days', 'Stage duration', 'days',
       'Length of a named process stage.',
       aliases=('duration', 'stage_days', 'expansion duration',
                'production/harvest stage duration'),
       global_min=0.25, global_max=90.0, stages=('all',)),
    _p('activation_duration_h', 'Activation duration', 'h',
       'Hours of anti-CD3/CD28 (or equivalent) stimulation before washout.',
       aliases=('activation', 'activation_window', 'stimulation_duration'),
       global_min=0.0, global_max=240.0, stages=('activation',)),
    _p('transduction_moi', 'Transduction MOI', 'dimensionless',
       'Vector multiplicity of infection at transduction.',
       aliases=('moi', 'vector_moi'),
       global_min=0.0, global_max=100.0, stages=('transduction',)),
    _p('temperature_c', 'Temperature', 'degC',
       'Culture temperature setpoint.',
       aliases=('temperature', 'temp'),
       global_min=20.0, global_max=42.0, stages=('all',)),
)

BY_ID = {p.parameter_id: p for p in PARAMETERS}

_ALIAS = {}
for _p_ in PARAMETERS:
    _ALIAS[_p_.parameter_id.lower()] = _p_.parameter_id
    _ALIAS[_p_.label.lower()] = _p_.parameter_id
    for _a in _p_.aliases:
        key = _a.lower()
        if key in _ALIAS and _ALIAS[key] != _p_.parameter_id:
            raise K.ContractError(
                f'alias {_a!r} is claimed by both {_ALIAS[key]!r} and {_p_.parameter_id!r}; '
                f'an ambiguous alias is worse than no alias')
        _ALIAS[key] = _p_.parameter_id


def _normalise(name):
    return re.sub(r'\s+', ' ', str(name or '').strip()).lower()


def resolve(name, *, required=True):
    """A name of any provenance -> the canonical parameter_id.

    Exact match on the id, the label, or a registered alias, case-insensitively.
    Nothing else. A near miss raises and lists the closest ids, because the whole
    point of this module is that `mcsf` silently failing to reach `mcsf_ng_ml`
    cost a release's worth of confusion.
    """
    key = _normalise(name)
    if key in _ALIAS:
        return _ALIAS[key]
    if not required:
        return None
    close = difflib.get_close_matches(key, sorted(_ALIAS), n=4, cutoff=0.6)
    hint = (' Did you mean ' + ', '.join(sorted({_ALIAS[c] for c in close})) + '?'
            if close else '')
    raise K.ContractError(
        f'{name!r} is not a known process parameter.{hint} Register it in '
        f'biosense/parameters.py, or add it as an alias of an existing one. '
        f'Guessing which parameter this is would be how a candidate silently '
        f'reaches the wrong knob.')


def get(name):
    """The Parameter for any spelling of its name."""
    return BY_ID[resolve(name)]


def applies_to_stage(name, stage):
    p = get(name)
    return 'all' in p.stages or stage in p.stages


def describe():
    return {'parameters': [p.to_dict() for p in PARAMETERS],
            'count': len(PARAMETERS),
            'note': 'Canonical identities only. Whether a parameter is modelled depends on the '
                    'project\'s simulator, so the mapping lives in the ProjectProfile.'}
