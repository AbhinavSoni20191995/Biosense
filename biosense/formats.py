"""Where the cells actually are: a plate well, a rotating tube, a stirred tank.

Nobody starts in a bioreactor. The work starts in a 6-well plate, because that
is where you can run eight conditions for the price of one and look at them
under a microscope; it moves to a small rotating-tube system (CERO) when
suspension aggregates are the point and a plate cannot make them; and it ends
in a stirred tank when the number of cells is the point. A protocol that only
exists in stirred-tank units is a protocol most people cannot start.

So the vessel is a first-class thing here. A run can be asked for in one
format, and a protocol written for one can be laid out in another.

What travels between formats, and what does not, is the whole content of this
module, and the honest answer is that less travels than people assume:

* **a concentration travels.** 100 ng/mL is 100 ng/mL in a well, a tube and a
  tank. What changes is the amount you weigh out, which is arithmetic over the
  working volume and is done here so nobody does it at the bench at 7am;
* **a time, a temperature and a fraction travel.** Days, degrees, and "half
  the medium" mean the same thing anywhere;
* **a mixing rate does not travel, in either direction.** 60 rpm on an
  impeller and 60 rpm of tube rotation are different physical quantities with
  different shear fields, and a static well has neither. There is no
  conversion, so none is offered: the target format's own mixing is a value
  somebody sets and the first run measures;
* **dissolved oxygen does not travel.** A tank controls it with a probe; a
  well and a tube have whatever the incubator and the medium depth give them.
  A DO setpoint carried into a plate would be a number describing nothing;
* **a per-mL seeding density is not a per-cm² seeding density.** If the plate
  culture is adherent, the density that matters is per area and this cannot
  convert one into the other, because the answer depends on biology the vessel
  does not know.

Every number about a vessel here says where it came from: `catalogue` for the
labware's own published geometry, `convention` for what is standard in
mammalian culture, `design_choice` for something the person sets. Nothing is
inferred from what another format does.

A translated protocol is a **starting point in the new format, not the same
process**. Oxygen transfer, shear, settling, evaporation and edge effects all
change, and the cells notice. The first run in a new format is a comparison
against the old one, not a continuation of it — and this says so wherever it
produces one.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import contracts as K

BASIS = ('catalogue', 'convention', 'design_choice')


@dataclass(frozen=True)
class Spec:
    """One number about a vessel, with where it came from."""
    value: float
    unit: str
    basis: str
    note: str = ''
    lower: float = None
    upper: float = None

    def public(self):
        return {'value': self.value, 'unit': self.unit, 'basis': self.basis,
                'note': self.note or None,
                'range': ({'lower': self.lower, 'upper': self.upper}
                          if self.lower is not None else None)}


@dataclass(frozen=True)
class Format:
    format_id: str
    label: str
    family: str                 # static_plate | rotating_tube | stirred_tank
    summary: str
    vessel: str
    working_volume: Spec
    mixing: str
    gas_exchange: str
    feeding: str
    scale_note: str
    good_for: tuple
    not_for: tuple
    simulator: str              # modelled | not_modelled_here
    simulator_note: str
    growth_area: Spec = None
    vessels_per_unit: int = 1   # wells on a plate, tubes in a carrier
    unit_label: str = ''
    decide: tuple = ()          # what this format needs that no other sets

    def public(self):
        return {
            'format_id': self.format_id, 'label': self.label, 'family': self.family,
            'summary': self.summary, 'vessel': self.vessel,
            'working_volume': self.working_volume.public(),
            'growth_area': self.growth_area.public() if self.growth_area else None,
            'vessels_per_unit': self.vessels_per_unit, 'unit_label': self.unit_label,
            'mixing': self.mixing, 'gas_exchange': self.gas_exchange,
            'feeding': self.feeding, 'scale_note': self.scale_note,
            'good_for': list(self.good_for), 'not_for': list(self.not_for),
            'simulator': self.simulator, 'simulator_note': self.simulator_note,
            'decide': [dict(d) for d in self.decide],
        }


SIX_WELL = Format(
    format_id='six_well',
    label='6-well plate',
    family='static_plate',
    summary='Where a protocol is tried first: eight conditions at once, under a microscope, '
            'for the cost of one bioreactor run.',
    vessel='one well of a 6-well plate',
    working_volume=Spec(2.0, 'mL', 'convention', lower=2.0, upper=3.0,
                        note='2 mL is the usual working volume for a 6-well well; plates '
                             'differ, so take the volume from the plate you have.'),
    growth_area=Spec(9.6, 'cm2', 'catalogue',
                     note='The growth area of a standard 6-well well. It only applies to '
                          'the culture if the cells are adherent.'),
    vessels_per_unit=6,
    unit_label='plate of 6 wells',
    mixing='Static. Nothing mixes the well, so suspension aggregates settle and sit on the '
           'bottom, where they fuse and go hypoxic in the middle. An orbital shaker changes '
           'that, and is a different setting with its own number.',
    gas_exchange='Through the medium surface from the incubator atmosphere. Not controlled '
                 'and not measured: there is no dissolved-oxygen setpoint in a plate.',
    feeding='By hand: aspirate and replace some fraction of the medium on a schedule.',
    scale_note='Per well. A condition that works here says nothing yet about how it behaves '
               'when the vessel mixes.',
    good_for=('trying a factor or a dose before it costs a bioreactor run',
              'many conditions in parallel, each one cheap',
              'watching the cells directly, every day',
              'the smallest amount of an expensive factor'),
    not_for=('making a useful number of cells',
             'anything where aggregate size, shear or dissolved oxygen is the question',
             'a process that must stay in suspension: aggregates settle and fuse here'),
    simulator='not_modelled_here',
    simulator_note='The project model was built for a mixed vessel. It has no representation '
                   'of settling, of a static oxygen gradient through 2 mm of medium, or of '
                   'edge-well evaporation, so its numbers do not describe a plate.',
    decide=(
        {'what': 'medium volume per well', 'why': 'everything per-vessel is computed from it',
         'typical': '2 mL (2–3 mL), from the plate you have'},
        {'what': 'adherent or suspension',
         'why': 'an adherent culture is seeded per cm2, a suspension one per mL, and they '
                'are different numbers that do not convert into each other',
         'typical': None},
        {'what': 'whether the plate sits still or on an orbital shaker',
         'why': 'a static well and a shaken well are different mixing regimes; the shaker '
                'speed is its own setting, not the bioreactor rpm',
         'typical': None},
        {'what': 'the medium-change schedule',
         'why': 'there is no perfusion here; the exchange fraction and interval are the '
                'whole feeding strategy',
         'typical': None},
    ),
)

CERO = Format(
    format_id='cero',
    label='CERO 3D rotating tube',
    family='rotating_tube',
    summary='The step between a plate and a tank: real suspension aggregates in a 50 mL '
            'tube, at a volume one person can run several of.',
    vessel='one CERO 50 mL tube with internal paddles',
    working_volume=Spec(None, 'mL', 'design_choice', lower=10.0, upper=30.0,
                        note='A nominal 50 mL tube is not run full. The working volume is '
                             'something you set and record; this computes per-vessel amounts '
                             'from whatever you set and will not assume one.'),
    vessels_per_unit=1,
    unit_label='tube',
    mixing='Rotation of the whole tube, in programmed cycles, against internal paddles. This '
           'keeps aggregates in suspension at low shear. The rotation rate is not an impeller '
           'speed: the same number on a stirred tank is a different physical quantity.',
    gas_exchange='Through the tube and the medium surface in the incubator atmosphere. There '
                 'is no dissolved-oxygen probe or setpoint.',
    feeding='By hand between cycles: the tube is taken out, aggregates settle, medium is '
            'exchanged.',
    scale_note='Per tube. Aggregates form here as they do in a tank, which is why this is the '
               'honest intermediate step; the mixing that makes them is not the same mixing.',
    good_for=('getting a suspension-aggregate process working before a bioreactor',
              'aggregate size and morphology, at a volume one person can handle',
              'several conditions in real suspension, in one incubator'),
    not_for=('dissolved-oxygen or pH control, which the vessel does not have',
             'the cell numbers a tank produces',
             'anything that needs online measurement during the run'),
    simulator='not_modelled_here',
    simulator_note='The project model\'s mixing and oxygen terms were written for a stirred '
                   'tank with a DO setpoint. A rotating tube has neither, so those terms do '
                   'not describe it and the model cannot be read as a prediction here.',
    decide=(
        {'what': 'working volume in the tube',
         'why': 'per-vessel amounts and seeding totals are computed from it',
         'typical': 'commonly 10–30 mL in a 50 mL tube; record what you use'},
        {'what': 'the rotation programme (rate, direction, cycle, rest)',
         'why': 'this is what keeps aggregates suspended and sets their size; it is NOT '
                'converted from a bioreactor rpm, in either direction',
         'typical': None},
        {'what': 'how often the tube comes out to be fed',
         'why': 'every handling is a settling step and a temperature excursion',
         'typical': None},
    ),
)

STIRRED_TANK = Format(
    format_id='stirred_tank',
    label='Stirred-tank bioreactor',
    family='stirred_tank',
    summary='Where the process ends up: controlled, instrumented, and the only one of the '
            'three that makes cells by the billion.',
    vessel='a stirred-tank vessel with an impeller and probes',
    working_volume=Spec(None, 'mL', 'design_choice',
                        note='Set by the vessel. Per-vessel amounts are computed from the '
                             'working volume you give.'),
    vessels_per_unit=1,
    unit_label='vessel',
    mixing='An impeller at a set speed, which fixes the shear field and with it the '
           'equilibrium aggregate diameter.',
    gas_exchange='Sparged and controlled against a dissolved-oxygen setpoint, measured by a '
                 'probe.',
    feeding='Scheduled exchange or perfusion, as a fraction of the working volume.',
    scale_note='Per vessel. This is the format the project\'s parameters and its model are '
               'written for.',
    good_for=('the cell numbers a therapy needs',
              'controlled and recorded dissolved oxygen, pH and temperature',
              'aggregate size as a controlled variable rather than an outcome'),
    not_for=('a first look at a factor nobody has tried in this system',
             'many conditions at once, at a sensible cost'),
    simulator='modelled',
    simulator_note='The project\'s model was written for this format: its mixing, oxygen and '
                   'feed terms refer to an impeller, a DO setpoint and an exchange fraction. '
                   'It is still uncalibrated, and every number it produces says so.',
    decide=(
        {'what': 'working volume', 'why': 'per-vessel amounts are computed from it',
         'typical': None},
    ),
)

FORMATS = {f.format_id: f for f in (SIX_WELL, CERO, STIRRED_TANK)}
# The order a process actually moves through, which is the order to show them in.
ORDER = ('six_well', 'cero', 'stirred_tank')
DEFAULT = 'stirred_tank'


def get(format_id, *, required=True):
    f = FORMATS.get(str(format_id or '').strip().lower())
    if f is None and required:
        raise K.ContractError(
            f'{format_id!r} is not a culture format this system knows. '
            f'The formats are: {", ".join(ORDER)}.')
    return f


def listing():
    return [FORMATS[f].public() for f in ORDER]


# ── what each parameter means in each format ──────────────────────────
# A parameter is handled by what it IS, not by the format it came from.
# 'carry' a concentration or a fraction; 'recompute' nothing automatically;
# 'set_here' what the target format needs and the source cannot supply;
# 'drop' what the target vessel does not have at all.
CARRIED = 'carried'            # the same number means the same thing there
AMOUNT = 'amount'              # carried, and what to weigh out per vessel
SET_HERE = 'set_here'          # the target needs its own value; nothing converts
DROPPED = 'dropped'            # the target vessel does not have this at all
UNKNOWN = 'unknown'            # a unit this does not recognise: left alone, flagged
KINDS = (CARRIED, AMOUNT, SET_HERE, DROPPED, UNKNOWN)

NOT_IN_STATIC = {
    'agitation_rpm': (SET_HERE,
                      'A static well has no impeller. If you shake the plate, the shaker '
                      'speed is its own setting and is not this number.'),
    'do_setpoint': (DROPPED,
                    'A plate has no dissolved-oxygen control and no probe. Oxygen is '
                    'whatever the incubator and the depth of the medium give it, which is '
                    'a property of the format, not a setpoint.'),
}
NOT_IN_TUBE = {
    'agitation_rpm': (SET_HERE,
                      'Tube rotation is not impeller speed: different geometry, different '
                      'shear. Set the rotation programme for this vessel and measure what '
                      'it does; nothing here converts between the two.'),
    'do_setpoint': (DROPPED,
                    'A CERO tube has no dissolved-oxygen probe or control loop. The value '
                    'would describe nothing.'),
}
BY_FAMILY = {'static_plate': NOT_IN_STATIC, 'rotating_tube': NOT_IN_TUBE, 'stirred_tank': {}}

# Concentration units this can turn into an amount per vessel, exactly.
# (unit -> (amount unit, multiplier applied to value * mL))
AMOUNTS = {
    'ng/ml': ('ng', 1.0), 'ug/ml': ('ug', 1.0), 'µg/ml': ('µg', 1.0),
    'mg/ml': ('mg', 1.0), 'u/ml': ('U', 1.0), 'iu/ml': ('IU', 1.0),
    'nm': ('nmol', 1e-3), 'um': ('µmol', 1e-3), 'µm': ('µmol', 1e-3),
    'mm': ('µmol', 1.0), 'pm': ('pmol', 1e-3),
    '1e6 cells/ml': ('1e6 cells', 1.0), 'cells/ml': ('cells', 1.0),
}
# Units that mean the same thing in any vessel.
FORMAT_FREE = ('degc', 'h', 'hr', 'hours', 'd', 'day', 'days',
               'fraction', 'fraction of volume', 'fraction of working volume',
               'dimensionless', '%')


def _u(unit):
    return str(unit or '').strip().lower()


def amount_per_vessel(value, unit, working_volume_ml):
    """{amount, unit} of a concentration in one vessel, or None if not computable.

    Pure arithmetic over the working volume: concentration × volume. It is here
    because it is the one calculation everybody does by hand at the bench and
    occasionally gets wrong by a factor of a thousand.
    """
    if value is None or working_volume_ml in (None, 0):
        return None
    spec = AMOUNTS.get(_u(unit))
    if spec is None:
        return None
    amount_unit, mult = spec
    return {'amount': float(value) * float(working_volume_ml) * mult, 'unit': amount_unit}


def _fmt(x):
    if x is None:
        return '—'
    if isinstance(x, float):
        if x and (abs(x) >= 1e5 or abs(x) < 1e-2):
            return f'{x:.3g}'
        return f'{x:g}'
    return str(x)


# ── laying one format's protocol out in another ───────────────────────
HEADLINE = {
    'six_well': 'A plate run of this protocol: the same concentrations, in 2 mL, with no '
                'mixing and no oxygen control. It is where you find out whether a factor '
                'does anything, not whether the process works.',
    'cero': 'A tube run of this protocol: the same concentrations, in real suspension, with '
            'a rotation programme you set and measure. Aggregates form here; the mixing that '
            'makes them is not the tank\'s mixing.',
    'stirred_tank': 'A tank run of this protocol: the same concentrations, with the mixing '
                    'and oxygen control the plate and the tube do not have — which is where '
                    'the process changes most.',
}
STANDING = ('A protocol laid out in another format is a starting point in that format, not '
            'the same process. Oxygen transfer, shear, settling and evaporation all change '
            'with the vessel, and the cells respond to them. The first run in a new format '
            'is a comparison against the old one — never a continuation of it, and never '
            'evidence that the protocol transferred.')


def _row(item, kind, why, *, amount=None, value=None):
    return {'parameter_id': item.get('parameter_id'), 'label': item.get('label'),
            'unit': item.get('unit'), 'stage_id': item.get('stage_id'),
            'stage_label': item.get('stage_label'), 'value': item.get('value'),
            'provenance': item.get('provenance'), 'kind': kind, 'why': why,
            'amount': amount}


def classify(item, target):
    """(kind, why) for one protocol line in *target*. Pure; no vessel arithmetic."""
    unit, pid = _u(item.get('unit')), item.get('parameter_id')
    if item.get('value') is None and item.get('provenance') == 'gap':
        # A gap is a gap in every vessel. Nothing to carry, nothing to
        # convert, and nothing here fills it in because the format changed.
        return CARRIED, ('No dose was established for this in any vessel, so there is '
                         'none to carry. It is a gap here too.')
    rule = BY_FAMILY.get(target.family, {}).get(pid)
    if rule:
        return rule
    if unit in FORMAT_FREE:
        return CARRIED, 'The same in any vessel.'
    if unit in AMOUNTS:
        if pid == 'seed_density' or 'cells/ml' in unit:
            return AMOUNT, ('A seeding concentration carries, and the cells per vessel '
                            'follow from the working volume. If this culture is adherent in '
                            'the new format, the density that matters is per cm2 instead, '
                            'and that is a different number nothing here can convert.')
        return AMOUNT, 'A concentration is the same concentration in any vessel.'
    if 'rpm' in unit:
        return SET_HERE, ('A mixing rate does not convert between vessel geometries. Set it '
                          'for this format and measure what it does.')
    return UNKNOWN, (f'No rule for a value in {item.get("unit") or "no unit"}: it is shown '
                     f'unchanged and is yours to check against the new vessel.')


def translate(protocol, to, *, from_=None, working_volume_ml=None):
    """Lay a protocol's first-pass plan out in another culture format.

    *protocol* is the display payload (it carries `first_pass`); *to* is a
    format id. *working_volume_ml* overrides the format's own default, which is
    how a CERO tube — whose working volume is a choice, not a specification —
    gets per-vessel amounts at all.
    """
    target = get(to)
    source = get(from_, required=False)
    vol = working_volume_ml if working_volume_ml is not None else target.working_volume.value
    fp = (protocol or {}).get('first_pass') or {}
    rows, stages = [], []
    for st in fp.get('stages') or []:
        here = []
        for i in st.get('inducers') or []:
            kind, why = classify(i, target)
            amount = (amount_per_vessel(i.get('value'), i.get('unit'), vol)
                      if kind == AMOUNT else None)
            here.append(_row(i, kind, why, amount=amount))
        stages.append({'stage_id': st.get('stage_id'), 'label': st.get('label'),
                       'days': st.get('days'), 'day_start': st.get('day_start'),
                       'items': here})
        rows += here
    physical = []
    for i in fp.get('physical') or []:
        kind, why = classify(i, target)
        amount = (amount_per_vessel(i.get('value'), i.get('unit'), vol)
                  if kind == AMOUNT else None)
        physical.append(_row(i, kind, why, amount=amount))
    rows += physical

    counts = {k: sum(1 for r in rows if r['kind'] == k)
              for k in (CARRIED, AMOUNT, SET_HERE, DROPPED, UNKNOWN)}
    return {
        'kind': 'format_translation', 'schema_version': K.PRODUCTION_VERSION,
        'created_at': K.now_iso(),
        'from': source.public() if source else None,
        'to': target.public(),
        'working_volume_ml': vol,
        'working_volume_basis': ('given for this translation' if working_volume_ml is not None
                                 else target.working_volume.basis),
        'headline': HEADLINE[target.format_id],
        'stages': stages, 'physical': physical,
        'decide': [dict(d) for d in target.decide],
        'counts': counts,
        'per_unit': ({'vessels': target.vessels_per_unit, 'label': target.unit_label}
                     if target.vessels_per_unit > 1 else None),
        'simulator': target.simulator, 'simulator_note': target.simulator_note,
        'note': STANDING,
    }


def markdown(t):
    """The translation as a page somebody can take to the bench."""
    to = t['to']
    vol = t['working_volume_ml']
    L = [f'# This protocol in a {to["label"]}', '', t['headline'], '', t['note'], '',
         f'**Vessel** {to["vessel"]}  ',
         f'**Working volume** {_fmt(vol) if vol else "not set — set it to get amounts"} mL  ',
         f'**Mixing** {to["mixing"]}  ', f'**Oxygen** {to["gas_exchange"]}  ',
         f'**Feeding** {to["feeding"]}', '']
    for st in t['stages']:
        L += [f'## {st["label"] or st["stage_id"]}', '']
        if not st['items']:
            L += ['Nothing is set for this stage.', '']
            continue
        for r in st['items']:
            amount = (f' → {_fmt(r["amount"]["amount"])} {r["amount"]["unit"]} per vessel'
                      if r.get('amount') else '')
            val = (f'{_fmt(r["value"])} {r["unit"] or ""}'.strip() if r['value'] is not None
                   else 'no value')
            L.append(f'- **{r["label"]}** — {val}{amount} · {r["kind"]}: {r["why"]}')
        L.append('')
    if t['physical']:
        L += ['## Vessel settings', '']
        for r in t['physical']:
            val = (f'{_fmt(r["value"])} {r["unit"] or ""}'.strip() if r['value'] is not None
                   else 'no value')
            L.append(f'- **{r["label"]}** — {val} · {r["kind"]}: {r["why"]}')
        L.append('')
    L += ['## What this format needs you to decide', '']
    for d in t['decide']:
        L.append(f'- **{d["what"]}** — {d["why"]}'
                 + (f' (typical: {d["typical"]})' if d.get('typical') else ''))
    L += ['', '## The simulator', '', t['simulator_note'], '']
    return '\n'.join(L) + '\n'


# ── the simulator, when the vessel is not the one it was written for ──
VESSEL_TERMS = ('agitation_rpm', 'do_setpoint', 'feed_fraction', 'feed_interval_h')


def simulatable(format_id, settings):
    """What of *settings* the project's model can be asked in this format.

    The model's mixing, oxygen and feed terms describe an impeller, a probe
    and an exchange schedule. In a static well or a rotating tube those terms
    refer to equipment that is not there, so they are dropped rather than fed
    a number that means nothing — and what was dropped is returned, because a
    prediction that quietly lost half its inputs is worse than no prediction.

    Returns (usable settings, [{parameter_id, why}], note). A format the model
    was written for drops nothing.
    """
    f = get(format_id, required=False) or get(DEFAULT)
    if f.simulator == 'modelled':
        return dict(settings or {}), [], f.simulator_note
    rules = BY_FAMILY.get(f.family, {})
    usable, dropped = {}, []
    for pid, v in (settings or {}).items():
        rule = rules.get(pid)
        if rule or pid in VESSEL_TERMS:
            dropped.append({'parameter_id': pid,
                            'why': rule[1] if rule else
                            f'{f.label}: this is a stirred-tank control, and the model\'s '
                            f'term for it describes equipment this vessel does not have.'})
            continue
        usable[pid] = v
    note = (f'{f.simulator_note} What it can still be asked is the part that does not depend '
            f'on the vessel, and even that is the tank\'s dose response, read in a vessel it '
            f'was not fitted in. Treat it as a hypothesis about the model, not a prediction '
            f'about this format.')
    return usable, dropped, note
