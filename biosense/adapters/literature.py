"""Version-0.1 literature handoff -> explicit SimulationScenario 1.0.

The handoff is a read-only upstream artifact; it is never modified and never
treated as simulator-ready. Only claim IDs that a selection names explicitly can
enter the model. Candidate-parameter lists alone are not a recipe.
"""
import math

from agent_tools import convert_unit

from .. import contracts as K
from ..models import get_model

_COUNT_UNITS = {'cells': 1.0}
_DENSITY_TO_PER_ML = {'cells/mL': 1.0, 'cells/L': 1e-3}
_VOLUME_TO_ML = {'mL': 1.0, 'L': 1000.0}

# Request-style literature parameter IDs that a gap maps back to.
_LITERATURE_IDS = {
    'ipsc_expansion.mu_R': 'ipsc_expansion:maximum_growth_rate',
    'cardiac_differentiation.k_C': 'cardiac_differentiation:differentiation_transition_rate',
    'initial.R': 'ipsc_expansion:initial_cell_density',
    'initial.C': 'ipsc_expansion:initial_cardiomyocyte_count',
    'initial.O': 'ipsc_expansion:initial_off_target_cell_count',
    'initial.D': 'ipsc_expansion:initial_dead_cell_count',
    'global.K': 'ipsc_expansion:carrying_capacity',
    'schedule.ipsc_expansion.duration': 'ipsc_expansion:duration',
    'schedule.cardiac_differentiation.duration': 'cardiac_differentiation:duration',
}
_QUERY_HINTS = {
    'rate': '({cell}) AND ({stage_terms}) AND (kinetic OR "growth rate" OR "death rate" OR "transition rate" OR "mathematical model")',
    'capacity': '({cell}) AND (bioreactor OR suspension OR microcarrier) AND ("maximum cell density" OR "carrying capacity")',
    'initial': '({cell}) AND ({stage_terms}) AND ("seeding density" OR "inoculation density") AND ("working volume")',
    'schedule': '({cell}) AND ({stage_terms}) AND (protocol OR timeline OR "day 0")',
}
_STAGE_TERMS = {
    'ipsc_expansion': 'expansion OR proliferation OR "population doubling"',
    'cardiac_differentiation': 'cardiomyocyte AND differentiation',
    'all': 'expansion OR differentiation',
}
_CELL_TERMS = 'hiPSC OR "human induced pluripotent stem cell"'


def literature_parameter_id(model_input):
    if model_input in _LITERATURE_IDS:
        return _LITERATURE_IDS[model_input]
    spec = get_model('compartment_growth_v1').INPUTS[model_input]
    return f"{spec['stage']}:{spec['symbol']}"


def suggested_query(spec):
    return _QUERY_HINTS[spec['kind']].format(cell=_CELL_TERMS, stage_terms=_STAGE_TERMS[spec['stage']])


def _normalize(value, unit, spec):
    """Convert to the registry unit. Raises ValueError rather than guessing."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('numeric finite value required')
    target = spec['unit']
    if spec['family'] == 'count':
        if unit not in _COUNT_UNITS:
            raise ValueError(f'count input needs unit "cells", got {unit!r}; densities require declared geometry')
        return float(value) * _COUNT_UNITS[unit]
    return float(convert_unit(value, unit, target))


def _check_bounds(v, spec):
    if 'min' in spec and v < spec['min']:
        return f'value {v} below allowed minimum {spec["min"]}'
    if 'min_exclusive' in spec and v <= spec['min_exclusive']:
        return f'value {v} must exceed {spec["min_exclusive"]}'
    return None


def _gap(gap_id, model_input, reason, spec, detail=None):
    g = {'gap_id': gap_id, 'model_input': model_input, 'reason': reason,
         'literature_parameter_id': literature_parameter_id(model_input),
         'suggested_query': suggested_query(spec)}
    if detail:
        g['detail'] = detail
    return g


def assemble_scenario(handoff, selection, assumptions=None, model_id=None):
    """Build a scenario. Missing/invalid inputs yield gaps and a blocked status."""
    K.require_valid('literature_handoff', handoff)
    assumptions = assumptions or {'assumption_set_id': None, 'assumptions': []}
    mode = selection.get('mode')
    if mode not in K.MODES:
        raise K.ContractError(f'selection.mode must be one of {K.MODES}')
    model = get_model(model_id or selection.get('model_id', 'compartment_growth_v1'))
    protocol_id = selection.get('protocol_id')
    errors, gaps, params = [], [], {}

    claims = {c['id']: c for c in handoff['claims']}
    rejected = {r['claim'].get('id') for r in handoff.get('rejected_claims', []) if isinstance(r.get('claim'), dict)}
    excluded = {e['claim_id'] for e in handoff.get('excluded_claims', [])}
    conflict_of = {}
    for conf in handoff.get('conflicts', []):
        for cid in conf['claim_ids']:
            conflict_of[cid] = conf
    resolutions = {}
    for res in selection.get('conflict_resolutions', []):
        for cid in res.get('claim_ids', []):
            resolutions[cid] = res

    def claim_entry(model_input, sel, spec):
        cid = sel['claim_id']
        if cid in rejected:
            return None, f'claim {cid!r} was rejected by the literature compiler'
        if cid in excluded:
            return None, f'claim {cid!r} was excluded by request constraints'
        c = claims.get(cid)
        if c is None:
            return None, f'claim {cid!r} not found among accepted handoff claims'
        if c['protocol_id'] != protocol_id:
            return None, f'claim {cid!r} belongs to protocol {c["protocol_id"]!r}, not selected {protocol_id!r}; protocols are never mixed'
        if spec['stage'] != 'all' and c['stage'] != spec['stage']:
            return None, f'claim {cid!r} stage {c["stage"]!r} does not match input stage {spec["stage"]!r}'
        if c['parameter'] not in spec['accepted_claim_parameters']:
            return None, (f'claim parameter {c["parameter"]!r} is not an accepted meaning for {model_input} '
                          f'(accepted: {spec["accepted_claim_parameters"]})')
        if sel.get('condition_signature') and sel['condition_signature'] != c['condition_signature']:
            return None, f'claim {cid!r} condition_signature differs from the selected one'
        resolution = None
        if cid in conflict_of:
            res = resolutions.get(cid)
            if not res or res.get('chosen_claim_id') != cid or not res.get('rationale'):
                return None, (f'claim {cid!r} is in an unresolved conflict with '
                              f'{conflict_of[cid]["claim_ids"]}; supply an explicit resolution record')
            resolution = res
        try:
            v = _normalize(c['value'], c['unit'], spec)
        except ValueError as e:
            return None, f'claim {cid!r}: {e}'
        return {'original_value': c['value'], 'original_unit': c['unit'], 'value': v,
                'provenance_type': 'reported', 'source_claim_ids': [cid], 'assumption_ids': [],
                'context': c['context'], 'uncertainty': c.get('uncertainty'),
                'applicability': f'Reported for protocol {protocol_id}; transfer to other lines/formats is unvalidated',
                'transformation': None, 'resolution': resolution, 'depends_on_synthetic': False}, None

    amap = {a['id']: a for a in assumptions.get('assumptions', [])}

    def assumption_entry(a, spec):
        ptype = a.get('provenance_type', 'synthetic_assumption')
        if ptype not in ('synthetic_assumption', 'fitted', 'derived'):
            return None, f'assumption {a["id"]!r}: provenance {ptype!r} not allowed in an assumption file'
        if ptype == 'synthetic_assumption' and mode == 'evidence_based':
            return None, f'synthetic assumption {a["id"]!r} is not permitted in evidence_based mode'
        if ptype == 'fitted' and not a.get('training_data_ids'):
            return None, f'fitted record {a["id"]!r} lacks training_data_ids'
        try:
            v = _normalize(a['value'], a['unit'], spec)
        except (ValueError, KeyError) as e:
            return None, f'assumption {a["id"]!r}: {e}'
        return {'original_value': a['value'], 'original_unit': a['unit'], 'value': v,
                'provenance_type': ptype, 'source_claim_ids': [], 'assumption_ids': [a['id']],
                'context': None, 'uncertainty': a.get('uncertainty'),
                'applicability': a.get('applicability', 'Synthetic demonstration value; not biological evidence'),
                'transformation': None, 'resolution': None,
                'depends_on_synthetic': ptype == 'synthetic_assumption'}, None

    def operand(ref, label):
        """Resolve a derived-input operand (claim or assumption) to (value, unit, entry_meta)."""
        if 'claim_id' in ref:
            cid = ref['claim_id']
            if cid in rejected or cid in excluded or cid not in claims:
                raise ValueError(f'{label} claim {cid!r} unavailable (rejected, excluded or missing)')
            c = claims[cid]
            if c['protocol_id'] != protocol_id:
                raise ValueError(f'{label} claim {cid!r} is from another protocol')
            if cid in conflict_of and resolutions.get(cid, {}).get('chosen_claim_id') != cid:
                raise ValueError(f'{label} claim {cid!r} is in an unresolved conflict')
            return c['value'], c['unit'], {'claim_id': cid, 'synthetic': False}
        a = amap.get(ref.get('assumption_id'))
        if a is None:
            raise ValueError(f'{label} assumption {ref.get("assumption_id")!r} not found')
        synthetic = a.get('provenance_type', 'synthetic_assumption') == 'synthetic_assumption'
        if synthetic and mode == 'evidence_based':
            raise ValueError(f'{label} uses synthetic assumption {a["id"]!r} in evidence_based mode')
        return a['value'], a['unit'], {'assumption_id': a['id'], 'synthetic': synthetic}

    def derived_entry(d, spec):
        if d.get('formula') != 'density_times_volume':
            return None, f'unsupported derivation formula {d.get("formula")!r}'
        try:
            dv, du, dmeta = operand(d['density'], 'density')
            vv, vu, vmeta = operand(d['volume'], 'volume')
            if du not in _DENSITY_TO_PER_ML:
                raise ValueError(f'density unit {du!r} is not a volumetric density; '
                                 'surface densities need declared area, not volume')
            if vu not in _VOLUME_TO_ML:
                raise ValueError(f'volume unit {vu!r} unsupported')
            v = float(dv) * _DENSITY_TO_PER_ML[du] * float(vv) * _VOLUME_TO_ML[vu]
        except (ValueError, KeyError, TypeError) as e:
            return None, f'derivation failed: {e}'
        parents = [dmeta, vmeta]
        return {'original_value': None, 'original_unit': None, 'value': v,
                'provenance_type': 'derived',
                'source_claim_ids': [p['claim_id'] for p in parents if 'claim_id' in p],
                'assumption_ids': [p['assumption_id'] for p in parents if 'assumption_id' in p],
                'context': claims[dmeta['claim_id']]['context'] if 'claim_id' in dmeta else None,
                'uncertainty': None,
                'applicability': 'Count derived from density and declared working volume',
                'transformation': {'formula': 'count = density[cells/mL] * working_volume[mL]',
                                   'parents': [{'value': dv, 'unit': du, **dmeta}, {'value': vv, 'unit': vu, **vmeta}]},
                'resolution': None,
                'depends_on_synthetic': any(p['synthetic'] for p in parents)}, None

    sources = {}
    for sel in selection.get('claim_selections', []):
        sources.setdefault(sel['model_input'], []).append(('claim', sel))
    for d in selection.get('derived_inputs', []):
        sources.setdefault(d['model_input'], []).append(('derived', d))
    for a in assumptions.get('assumptions', []):
        if a.get('model_input') is None:
            continue  # operand-only record (e.g. a working volume) used by a derivation
        sources.setdefault(a['model_input'], []).append(('assumption', a))

    for unknown in sorted(set(sources) - set(model.INPUTS)):
        errors.append(f'unknown model input {unknown!r} is refused (model {model.MODEL_ID})')
    used_assumptions = []
    for n, (mi, spec) in enumerate(sorted(model.INPUTS.items())):
        options = sources.get(mi, [])
        gid = f'gap-{n:03d}'
        if not options:
            gaps.append(_gap(gid, mi, 'missing', spec))
            continue
        if len(options) > 1:
            gaps.append(_gap(gid, mi, 'ambiguous', spec, f'{len(options)} sources supplied; exactly one allowed'))
            continue
        kind, item = options[0]
        if kind == 'claim':
            entry, why = claim_entry(mi, item, spec)
        elif kind == 'derived':
            entry, why = derived_entry(item, spec)
        else:
            entry, why = assumption_entry(item, spec)
        if entry is None:
            gaps.append(_gap(gid, mi, 'invalid_source', spec, why))
            continue
        bound = _check_bounds(entry['value'], spec)
        if bound:
            gaps.append(_gap(gid, mi, 'out_of_bounds', spec, bound))
            continue
        params[mi] = {'id': mi, 'meaning': spec['meaning'], 'population': spec['population'],
                      'stage': spec['stage'], 'unit': spec['unit'], **entry}
        if kind == 'assumption':
            used_assumptions.append(item)
        elif kind == 'derived':
            used_assumptions += [amap[i] for i in entry['assumption_ids']]

    mapped_claims = {cid for p in params.values() for cid in p['source_claim_ids']}
    protocol_claims = [c for c in handoff['claims'] if c['protocol_id'] == protocol_id]
    unsupported = []
    for c in protocol_claims:
        if c['id'] in mapped_claims or c['role'] not in ('operating_condition',):
            continue
        conf = conflict_of.get(c['id'])
        unsupported.append({'claim_id': c['id'], 'stage': c['stage'], 'parameter': c['parameter'],
                            'value': c['value'], 'unit': c['unit'],
                            'conflict_claim_ids': conf['claim_ids'] if conf else [],
                            'conflict_status': 'unresolved' if conf else 'none',
                            'reason': f'{model.MODEL_ID} has no response function for this control; '
                                      'changing it has no simulated effect'})
    unmapped = [{'claim_id': c['id'], 'stage': c['stage'], 'parameter': c['parameter'], 'role': c['role']}
                for c in protocol_claims
                if c['id'] not in mapped_claims and c['role'] != 'operating_condition']

    if not protocol_claims and mode == 'evidence_based':
        errors.append(f'protocol {protocol_id!r} has no accepted claims in the handoff')
    ctx0 = next((c['context'] for c in protocol_claims), {})
    request_ctx = {k: ctx0.get(k) for k in ('species', 'cell_origin', 'target_cell', 'cell_line', 'culture_format')}
    complete = not gaps and not errors
    seen = set()
    manifest = []
    for a in used_assumptions:
        if a['id'] in seen:
            continue
        seen.add(a['id'])
        manifest.append({'id': a['id'], 'model_input': a['model_input'],
                         'provenance_type': a.get('provenance_type', 'synthetic_assumption'),
                         'value': a['value'], 'unit': a['unit'], 'rationale': a.get('rationale', ''),
                         'uncertainty': a.get('uncertainty')})

    schedule = []
    for stage in model.STAGES:
        p = params.get(f'schedule.{stage}.duration')
        schedule.append({'stage': stage, 'duration': p['value'] if p else None, 'unit': 'h',
                         'input_id': f'schedule.{stage}.duration', 'time_origin': 'stage_start'})
    initial = {s: params[f'initial.{s}']['value'] if f'initial.{s}' in params else None for s in model.STATE}

    synthetic_inputs = sorted(i for i, p in params.items() if p['depends_on_synthetic'])
    scenario = {
        'schema_version': K.SCENARIO_VERSION,
        'scenario_id': selection.get('scenario_id', f'scenario-{selection.get("selection_id", "unnamed")}'),
        'mode': mode,
        'label': ('SYNTHETIC DEMO: toy-model behaviour only; not biological evidence or validated cell production'
                  if mode == 'synthetic_demo' else
                  'EVIDENCE-BASED: model predictions conditional on cited evidence, derivations and model assumptions'),
        'upstream': {
            'request_id': handoff['request_id'],
            'handoff_schema_version': handoff['schema_version'],
            'handoff_sha256': K.sha256_obj(handoff),
            'selection_id': selection.get('selection_id'),
            'selection_sha256': K.sha256_obj(selection),
            'assumption_set_id': assumptions.get('assumption_set_id'),
            'assumptions_sha256': K.sha256_obj(assumptions),
        },
        'protocol_id': protocol_id,
        'model_id': model.MODEL_ID,
        'model_contract': model.contract(),
        'context': {**request_ctx, 'target_subtype': selection.get('target_subtype', 'unspecified')},
        'time_unit': 'h',
        'population_unit': 'cells',
        'stage_schedule': schedule,
        'initial_state': initial,
        'parameters': [params[k] for k in sorted(params)],
        'interventions': [],
        'unsupported_controls': unsupported,
        'unmapped_protocol_claims': unmapped,
        'assumption_manifest': manifest,
        'synthetic_inputs': synthetic_inputs,
        'gaps': gaps,
        'errors': errors,
        'validation': {
            'numerically_complete': complete,
            'biologically_calibrated': False,
            'status': 'runnable' if complete else 'blocked',
            'note': ('Numerical completeness is not biological validation; '
                     'no input has been calibrated against measured trajectories.'),
        },
    }
    return scenario
