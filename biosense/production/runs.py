"""BioreactorRun 2.0 ingestion: schema plus consistency with the protocol that was run."""
from .. import contracts as K
from .protocol import protocol_sha256

MAX_MODE_OBS_KEYS = ('day', 'stage', 'vcd_e6_per_ml', 'viability_pct', 'glucose_mM', 'lactate_mM')


def validate_run(run, protocol):
    """Return {'valid': bool, 'errors': [...], 'warnings': [...]}."""
    errors = list(K.schema_errors('bioreactor_run', run))
    warnings = []
    if errors:
        return {'valid': False, 'errors': errors, 'warnings': warnings}
    if run['protocol_id'] != protocol['protocol_id']:
        errors.append(f'run is for protocol {run["protocol_id"]!r}, not {protocol["protocol_id"]!r}')
    if run['protocol_sha256'] != protocol_sha256(protocol):
        errors.append('protocol_sha256 mismatch: the run was executed with a different version of this protocol')
    arms = {a['arm_id']: a for a in protocol['genotype_arms']}
    seen = set()
    for a in run['arms']:
        key = (a['arm_id'], a['replicate'])
        if key in seen:
            errors.append(f'duplicate arm/replicate {key}')
        seen.add(key)
        if a['arm_id'] not in arms:
            errors.append(f'arm {a["arm_id"]!r} is not in the protocol')
        h = a['harvest']
        if h['viable_cells_total'] > 0 and h['target_marker_pct'] == 0:
            warnings.append(f'{key}: target marker 0% with viable cells; check the staining/gating')
        if abs(h['day'] - protocol['measurement_plan']['harvest_day']) > 1e-9:
            warnings.append(f'{key}: harvested on day {h["day"]}, protocol planned day '
                            f'{protocol["measurement_plan"]["harvest_day"]}; record why under deviations')
        days = [t['day'] for t in a.get('timepoints', [])]
        if days != sorted(days):
            errors.append(f'{key}: timepoints are not in day order')
        if run['mode'] == 'max':
            obs = a.get('observations') or []
            if not obs:
                errors.append(f'{key}: max mode needs the daily integrated-machine observations')
            for i, o in enumerate(obs):
                miss = [k for k in MAX_MODE_OBS_KEYS if k not in o]
                if miss:
                    errors.append(f'{key}: observation {i} missing {miss}')
                    break
            if not a.get('setpoints'):
                warnings.append(f'{key}: no executed setpoints; process attribution will be skipped')
            if len(a.get('history') or []) < 4:
                warnings.append(f'{key}: fewer than 4 prior runs of this line; sensor-drift and clonal-drift tests are not testable')
        elif a.get('observations'):
            warnings.append(f'{key}: observations supplied in minimal mode are ignored; use mode "max" to analyse them')
    missing = set(arms) - {a['arm_id'] for a in run['arms']}
    if missing:
        warnings.append(f'protocol arms without data: {sorted(missing)}')
    if run['mode'] != protocol['measurement_plan']['mode']:
        warnings.append(f'run mode {run["mode"]!r} differs from the planned {protocol["measurement_plan"]["mode"]!r}')
    if run['source'] == 'synthetic_standin':
        warnings.append('synthetic stand-in data: demonstrates the workflow, not biology')
    return {'valid': not errors, 'errors': errors, 'warnings': warnings}
