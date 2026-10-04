"""QC profiles and release-criterion scoring.

Per-criterion status:
  PASS / FAIL       measured and compared with a set limit
  MARGINAL          passes on the mean, but replicates or the fused-purity interval straddle the limit
  NOT_TESTED        the test was not reported (never assumed to pass)
  SPEC_MISSING      the profile leaves the limit to the user/QA team and none was set
  NOT_APPLICABLE    the criterion needs data that the measurement mode does not collect
"""
import copy

from .. import contracts as K

PROFILE_DIR = K.ROOT / 'qc_profiles'
_OPS = {'>=': lambda a, b: a >= b, '<=': lambda a, b: a <= b, '==': lambda a, b: a == b}
SAFETY_SEVERITY = 'safety'


def load_profile(profile_id, overrides=None):
    """Load a profile with inheritance (child criteria replace parent criteria by id) and overrides."""
    chain, pid = [], profile_id
    while pid:
        path = PROFILE_DIR / f'{pid}.json'
        if not path.exists():
            raise K.ContractError(f'unknown QC profile {pid!r} (looked in {PROFILE_DIR})')
        prof = K.read_json(path)
        chain.append(prof)
        pid = prof.get('inherits')
        if len(chain) > 5:
            raise K.ContractError('QC profile inheritance too deep')
    criteria = {}
    for prof in reversed(chain):
        for c in prof['criteria']:
            criteria[c['id']] = copy.deepcopy(c)
    for o in overrides or []:
        if o['id'] not in criteria:
            raise K.ContractError(f'QC override for unknown criterion {o["id"]!r}')
        criteria[o['id']]['value'] = o['value']
        criteria[o['id']]['value_status'] = 'user_override'
        criteria[o['id']]['basis'] = o['basis']
    return {'profile_id': profile_id, 'chain': [p['profile_id'] for p in chain],
            'label': chain[0]['label'], 'criteria': list(criteria.values())}


def score(profile, mode, values, replicate_values=None, intervals=None):
    """Score every criterion.

    values: metric -> aggregated value (mean for numbers, worst case for categorical tests)
    replicate_values: metric -> list of per-replicate values (for MARGINAL detection)
    intervals: metric -> [low, high] (e.g. fused purity CI from max mode)
    """
    replicate_values, intervals = replicate_values or {}, intervals or {}
    rows = []
    for c in profile['criteria']:
        row = {k: c[k] for k in ('id', 'metric', 'op', 'value', 'unit', 'category', 'severity', 'value_status', 'basis')}
        m = c['metric']
        if mode not in c['modes']:
            row.update(status='NOT_APPLICABLE', observed=None, note=f'not collected in {mode} mode')
        elif values.get(m) is None:
            row.update(status='NOT_TESTED', observed=None, note='not reported; never assumed to pass')
        elif c['value'] is None:
            row.update(status='SPEC_MISSING', observed=values[m],
                       note='limit is product-specific; set it in request.qc_overrides')
        else:
            v = values[m]
            ok = _OPS[c['op']](v, c['value'])
            row.update(status='PASS' if ok else 'FAIL', observed=v)
            if ok and c['op'] in ('>=', '<='):
                reps = [x for x in replicate_values.get(m, []) if x is not None]
                if len(reps) > 1 and not all(_OPS[c['op']](x, c['value']) for x in reps):
                    row.update(status='MARGINAL', note=f'mean passes but replicates {reps} straddle the limit')
                lo_hi = intervals.get(m)
                if lo_hi and lo_hi[0] < c['value'] < lo_hi[1]:
                    row.update(status='MARGINAL', note=f'interval {lo_hi} straddles the limit {c["value"]}')
        rows.append(row)
    return rows


def overall(rows):
    """FAIL > INCOMPLETE (untested or unspecified limit) > MARGINAL > PASS."""
    st = {r['status'] for r in rows}
    if 'FAIL' in st:
        return 'FAIL'
    if st & {'NOT_TESTED', 'SPEC_MISSING'}:
        return 'INCOMPLETE'
    if 'MARGINAL' in st:
        return 'MARGINAL'
    return 'PASS'
