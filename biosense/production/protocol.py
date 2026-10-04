"""ProductionProtocol 2.0: validation, provenance, per-arm schedules, run sheet, approval.

A protocol is a proposal for human review. Its status is computed, never declared:
  invalid         schema/provenance/schedule errors
  blocked         at least one quantity is a gap (no value) -> cannot go to the wet lab
  needs_approval  complete, but a named human must sign off (design choices listed)
  approved        a valid approval record matches this exact protocol content
"""
import copy
import math

from agent_tools import convert_unit

from .. import contracts as K

DIRECT_LEVELS = ('direct_same_cell_type', 'direct_related_cell_type')
FACTOR_ACTIONS = ('add_factor', 'remove_factor')


def protocol_sha256(protocol):
    """Hash of the protocol content without its approval record."""
    return K.sha256_obj({k: v for k, v in protocol.items() if k != 'approval'})


def iter_quantities(protocol):
    """Yield (path, quantity) for every provenance-carrying value."""
    cs = protocol.get('culture_system', {})
    if 'working_volume' in cs:
        yield 'culture_system.working_volume', cs['working_volume']
    for name, q in cs.get('parameters', {}).items():
        yield f'culture_system.parameters.{name}', q
    for st in protocol.get('stages', []):
        yield f'stages.{st["stage_id"]}.medium', st['medium']
        for s in st['steps']:
            if s.get('quantity') is not None:
                yield f'steps.{s["step_id"]}.quantity', s['quantity']
    for a in protocol.get('arm_adjustments', []):
        if a.get('quantity') is not None:
            yield f'arm_adjustments.{a["adjustment_id"]}.quantity', a['quantity']
    for e in protocol.get('genotype_effects', []):
        if e.get('predicted_magnitude') is not None:
            yield f'genotype_effects.{e["effect_id"]}.predicted_magnitude', e['predicted_magnitude']
    for i, o in enumerate(protocol.get('expected_outcomes', [])):
        yield f'expected_outcomes.{i}.{o["arm_id"]}.{o["metric"]}', o['quantity']


def load_evidence(handoffs):
    """Index accepted, rejected, excluded and conflicting claims across handoffs."""
    ev = {'claims': {}, 'rejected': set(), 'excluded': set(), 'conflict_of': {}}
    for h in handoffs or []:
        for c in h.get('claims', []):
            ev['claims'][c['id']] = c
        ev['rejected'] |= {r['claim'].get('id') for r in h.get('rejected_claims', []) if isinstance(r.get('claim'), dict)}
        ev['excluded'] |= {e['claim_id'] for e in h.get('excluded_claims', [])}
        for conf in h.get('conflicts', []):
            for cid in conf['claim_ids']:
                ev['conflict_of'][cid] = conf['claim_ids']
    return ev


def _check_claim_refs(path, claim_ids, ev, errors, warnings):
    if ev is None:
        if claim_ids:
            warnings.append(f'{path}: claim IDs {claim_ids} not verified (no handoff supplied)')
        return
    for cid in claim_ids:
        if cid in ev['rejected']:
            errors.append(f'{path}: claim {cid!r} was rejected by the literature compiler')
        elif cid in ev['excluded']:
            errors.append(f'{path}: claim {cid!r} was excluded by request constraints')
        elif cid not in ev['claims']:
            errors.append(f'{path}: claim {cid!r} not found among accepted handoff claims')


def _check_quantity(path, q, ev, out):
    errors, warnings = out['errors'], out['warnings']
    prov, cids, value = q['provenance'], q['claim_ids'], q['value']
    if prov == 'gap':
        if value is not None:
            errors.append(f'{path}: a gap must have value null (got {value!r})')
        out['gaps'].append({'path': path, 'unit': q['unit'], 'rationale': q.get('rationale')})
        return
    if value is None:
        errors.append(f'{path}: value is null but provenance is {prov!r}; use provenance "gap"')
        return
    if isinstance(value, float) and not math.isfinite(value):
        errors.append(f'{path}: value must be finite')
    if prov in ('reported', 'adapted'):
        if not cids:
            errors.append(f'{path}: {prov} value must cite claim IDs')
        _check_claim_refs(path, cids, ev, errors, warnings)
    if prov == 'reported':
        if len(cids) != 1:
            errors.append(f'{path}: a reported value cites exactly one claim (use "adapted" to combine or transfer)')
        elif ev is not None and cids[0] in ev['claims']:
            c = ev['claims'][cids[0]]
            if cids[0] in ev['conflict_of'] and not q.get('rationale'):
                errors.append(f'{path}: claim {cids[0]!r} is in an unresolved conflict with '
                              f'{ev["conflict_of"][cids[0]]}; state the resolution rationale')
            try:
                same = (value == c['value']) if q['unit'] == 'text' or c['unit'] == 'text' else math.isclose(
                    convert_unit(c['value'], c['unit'], q['unit']), value, rel_tol=1e-6, abs_tol=1e-12)
            except (ValueError, TypeError):
                same = None
                errors.append(f'{path}: cannot verify {value} {q["unit"]} against claim {cids[0]} '
                              f'({c["value"]} {c["unit"]}); unit not convertible')
            if same is False:
                errors.append(f'{path}: reported value {value} {q["unit"]} differs from claim {cids[0]} '
                              f'({c["value"]} {c["unit"]}); use "adapted" with a rationale if intentional')
    if prov == 'adapted':
        if not q.get('rationale'):
            errors.append(f'{path}: adapted value needs a rationale (what was transferred or converted, and why)')
        out['adapted'].append({'path': path, 'value': value, 'unit': q['unit'], 'claim_ids': cids,
                               'rationale': q.get('rationale')})
    if prov == 'design_choice':
        if cids:
            _check_claim_refs(path, cids, ev, errors, warnings)
        if not q.get('rationale'):
            errors.append(f'{path}: design choice needs a rationale')
        out['design_choices'].append({'path': path, 'value': value, 'unit': q['unit'],
                                      'rationale': q.get('rationale')})


def validate_protocol(protocol, handoffs=None, request=None):
    """Return {status, errors, warnings, gaps, design_choices, adapted, hypothesis_effects, protocol_sha256}."""
    out = {'status': None, 'errors': [], 'warnings': [], 'gaps': [], 'design_choices': [], 'adapted': [],
           'hypothesis_effects': [], 'protocol_sha256': None}
    errors, warnings = out['errors'], out['warnings']
    errors += K.schema_errors('production_protocol', protocol)
    if errors:
        out['status'] = 'invalid'
        return out
    out['protocol_sha256'] = protocol_sha256(protocol)
    ev = load_evidence(handoffs) if handoffs is not None else None
    if ev is None:
        warnings.append('no literature handoff supplied: claim references are unverified')

    # ── schedule ──
    stages = protocol['stages']
    ids = [s['stage_id'] for s in stages]
    if len(set(ids)) != len(ids):
        errors.append('duplicate stage IDs')
    prev_end = None
    step_ids, steps = set(), {}
    for st in stages:
        if st['end_day'] <= st['start_day']:
            errors.append(f'stage {st["stage_id"]}: end_day must exceed start_day')
        if prev_end is not None and st['start_day'] < prev_end:
            errors.append(f'stage {st["stage_id"]} starts on day {st["start_day"]} before the previous stage ends '
                          f'(day {prev_end}); stages must not overlap')
        if prev_end is not None and st['start_day'] > prev_end:
            warnings.append(f'gap in schedule between day {prev_end} and day {st["start_day"]}')
        prev_end = st['end_day']
        for s in st['steps']:
            if s['step_id'] in step_ids:
                errors.append(f'duplicate step ID {s["step_id"]!r}')
            step_ids.add(s['step_id'])
            steps[s['step_id']] = (st, s)
            if not st['start_day'] <= s['day'] <= st['end_day']:
                errors.append(f'step {s["step_id"]}: day {s["day"]} outside stage {st["stage_id"]} '
                              f'[{st["start_day"]}, {st["end_day"]}]')
            if s.get('end_day') is not None and s['end_day'] < s['day']:
                errors.append(f'step {s["step_id"]}: end_day before day')
            if s['action'] in FACTOR_ACTIONS and not s.get('factor'):
                errors.append(f'step {s["step_id"]}: {s["action"]} needs a factor name')
            if s['action'] in ('add_factor', 'seed') and s.get('quantity') is None:
                errors.append(f'step {s["step_id"]}: {s["action"]} needs a quantity (use provenance "gap" if unknown)')
    actions = [s['action'] for st in stages for s in st['steps']]
    if 'seed' not in actions:
        errors.append('protocol has no seed step (seeding density/time)')
    if 'harvest' not in actions:
        errors.append('protocol has no harvest step')
    harvest_days = [s['day'] for st in stages for s in st['steps'] if s['action'] == 'harvest']
    plan = protocol['measurement_plan']
    if harvest_days and plan['harvest_day'] not in harvest_days:
        errors.append(f'measurement_plan.harvest_day {plan["harvest_day"]} matches no harvest step {harvest_days}')

    # ── provenance ──
    for path, q in iter_quantities(protocol):
        _check_quantity(path, q, ev, out)
    for ins in protocol['insights']:
        if ins['evidence_level'] in DIRECT_LEVELS and not ins['claim_ids']:
            errors.append(f'insight {ins["insight_id"]}: {ins["evidence_level"]} needs claim IDs')
        _check_claim_refs(f'insight {ins["insight_id"]}', ins['claim_ids'], ev, errors, warnings)

    # ── genotype arms, effects, adjustments ──
    arms = {a['arm_id']: a for a in protocol['genotype_arms']}
    if len(arms) != len(protocol['genotype_arms']):
        errors.append('duplicate arm IDs')
    engineered = [a for a in arms.values() if a['genotype'] != 'wild_type']
    if engineered and not any(a['genotype'] == 'wild_type' for a in arms.values()):
        errors.append('engineered arms need a wild_type control arm run in parallel')
    for a in engineered:
        if not a.get('gene'):
            errors.append(f'arm {a["arm_id"]}: {a["genotype"]} arm must name the gene')
    effects = {}
    for e in protocol['genotype_effects']:
        effects[e['effect_id']] = e
        arm = arms.get(e['arm_id'])
        if arm is None:
            errors.append(f'effect {e["effect_id"]}: unknown arm {e["arm_id"]!r}')
        elif arm['genotype'] == 'wild_type':
            errors.append(f'effect {e["effect_id"]}: genotype effects are predicted for engineered arms, not wild type')
        if e.get('stage_id') and e['stage_id'] not in ids:
            errors.append(f'effect {e["effect_id"]}: unknown stage {e["stage_id"]!r}')
        if e['evidence_level'] in DIRECT_LEVELS and not e['claim_ids']:
            errors.append(f'effect {e["effect_id"]}: {e["evidence_level"]} needs claim IDs')
        _check_claim_refs(f'effect {e["effect_id"]}', e['claim_ids'], ev, errors, warnings)
        if e['evidence_level'] not in DIRECT_LEVELS:
            out['hypothesis_effects'].append({'effect_id': e['effect_id'], 'arm_id': e['arm_id'],
                                              'evidence_level': e['evidence_level'],
                                              'statement': f'{e["gene"]}: {e["affected_quantity"]} -> {e["predicted_direction"]}'})
    for e in engineered:
        if not any(x['arm_id'] == e['arm_id'] for x in protocol['genotype_effects']):
            warnings.append(f'arm {e["arm_id"]}: no predicted genotype effect; the comparison will be descriptive only')
    for adj in protocol['arm_adjustments']:
        p = f'adjustment {adj["adjustment_id"]}'
        if adj['arm_id'] not in arms:
            errors.append(f'{p}: unknown arm {adj["arm_id"]!r}')
        if adj['step_id'] not in steps:
            errors.append(f'{p}: unknown step {adj["step_id"]!r}')
        if 'quantity' not in adj and not adj.get('day_shift') and not adj.get('omit'):
            errors.append(f'{p}: must change a quantity, shift the day or omit the step')
        for eid in adj['effect_ids']:
            if eid not in effects:
                errors.append(f'{p}: unknown effect {eid!r}')
            elif effects[eid]['arm_id'] != adj['arm_id']:
                errors.append(f'{p}: effect {eid} belongs to arm {effects[eid]["arm_id"]}, not {adj["arm_id"]}')
        if adj['arm_id'] in arms and arms[adj['arm_id']]['genotype'] == 'wild_type':
            warnings.append(f'{p}: adjusts the wild-type control; the base protocol should define the control')
        if adj['step_id'] in steps and adj.get('day_shift'):
            st, s = steps[adj['step_id']]
            new = s['day'] + adj['day_shift']
            if not st['start_day'] <= new <= st['end_day']:
                errors.append(f'{p}: shifted day {new} leaves stage {st["stage_id"]}')
    for o in protocol.get('expected_outcomes', []):
        if o['arm_id'] not in arms:
            errors.append(f'expected outcome for unknown arm {o["arm_id"]!r}')

    # ── request coherence ──
    if request is not None:
        rerr = K.schema_errors('production_request', request)
        if rerr:
            errors += [f'request: {e}' for e in rerr]
        else:
            if protocol['request_id'] != request['request_id']:
                errors.append('protocol.request_id does not match the request')
            missing = {a['arm_id'] for a in request['genotype_arms']} - set(arms)
            if missing:
                errors.append(f'request arms missing from protocol: {sorted(missing)}')
            d = request['desired_output']
            if abs(plan['harvest_day'] - d['at_day']) > d['day_tolerance']:
                errors.append(f'harvest day {plan["harvest_day"]} does not answer the question '
                              f'(target at day {d["at_day"]} +/- {d["day_tolerance"]})')
            maxd = request.get('constraints', {}).get('max_culture_days')
            if maxd is not None and stages[-1]['end_day'] > maxd:
                errors.append(f'protocol runs to day {stages[-1]["end_day"]}, beyond max_culture_days {maxd}')
            if plan['mode'] != request['measurement_mode']:
                warnings.append(f'measurement plan mode {plan["mode"]!r} differs from request {request["measurement_mode"]!r}')

    # ── approval ──
    appr = protocol.get('approval')
    approved = False
    if appr:
        if appr['protocol_sha256'] != out['protocol_sha256']:
            errors.append('approval is stale: protocol content changed after approval')
        else:
            unack = {d['path'] for d in out['design_choices']} - set(appr['acknowledged_design_choices'])
            if unack:
                errors.append(f'approval does not acknowledge design choices {sorted(unack)}')
            else:
                approved = True
    if errors:
        out['status'] = 'invalid'
    elif out['gaps']:
        out['status'] = 'blocked'
    elif approved:
        out['status'] = 'approved'
    else:
        out['status'] = 'needs_approval'
    return out


def approve_protocol(protocol, validation, approved_by, note=''):
    """Attach a human approval. Only for needs_approval protocols; returns a new protocol."""
    if validation['status'] != 'needs_approval':
        raise K.ContractError(f'cannot approve a protocol with status {validation["status"]!r}: '
                              + '; '.join(validation['errors'][:5] or [g['path'] for g in validation['gaps']][:5]))
    if not approved_by or not approved_by.strip():
        raise K.ContractError('approval needs the name of the human reviewer')
    p = copy.deepcopy(protocol)
    p['approval'] = {'approved_by': approved_by.strip(), 'approved_at': K.now_iso(),
                     'protocol_sha256': validation['protocol_sha256'],
                     'acknowledged_design_choices': sorted(d['path'] for d in validation['design_choices']),
                     'note': note}
    return p


def arm_schedule(protocol, arm_id):
    """All steps for one arm, sorted by day, with that arm's adjustments applied and marked."""
    adj = {}
    for a in protocol['arm_adjustments']:
        if a['arm_id'] == arm_id:
            adj.setdefault(a['step_id'], []).append(a)
    rows = []
    for st in protocol['stages']:
        for step in st['steps']:
            s = copy.deepcopy(step)
            s['stage_id'], s['adjusted_by'] = st['stage_id'], []
            omitted = False
            for a in adj.get(step['step_id'], []):
                s['adjusted_by'].append(a['adjustment_id'])
                if a.get('omit'):
                    omitted = True
                if 'quantity' in a:
                    s['quantity'] = a['quantity']
                if a.get('day_shift'):
                    s['day'] += a['day_shift']
                    if s.get('end_day') is not None:
                        s['end_day'] += a['day_shift']
            if not omitted:
                rows.append(s)
    return sorted(rows, key=lambda r: (r['day'], r['step_id']))


_TAG = {'reported': 'R', 'adapted': 'A', 'design_choice': 'D', 'gap': 'GAP'}


def _fmt_q(q):
    if q is None:
        return ''
    v = 'MISSING' if q['value'] is None else f'{q["value"]:g}' if isinstance(q['value'], (int, float)) else str(q['value'])
    cites = ','.join(q['claim_ids'])
    unit = '' if q['unit'] == 'text' else f' {q["unit"]}'
    return f'{v}{unit} [{_TAG[q["provenance"]]}{":" + cites if cites else ""}]'


def render_run_sheet(protocol, validation, request=None):
    """Operator-facing Markdown run sheet: one day-by-day table per genotype arm."""
    L = [f'# Run sheet: {protocol["title"]}', '',
         f'- Protocol `{protocol["protocol_id"]}` (iteration {protocol["iteration"]}), sha256 `{validation["protocol_sha256"]}`',
         f'- **Status: {validation["status"].upper()}**' + (
             '' if validation['status'] == 'approved' else ' — do not start the wet-lab run until it is approved'),
         f'- Day 0 = {protocol["day_origin"]}']
    if protocol.get('label'):
        L.append(f'- Label: {protocol["label"]}')
    if request:
        d = request['desired_output']
        L.append(f'- Question: {request["question"]}')
        L.append(f'- Target: {d["metric"]} {d["op"]} {d["value"]:g} {d["unit"]} at day {d["at_day"]:g} (+/- {d["day_tolerance"]:g})')
    L += ['', 'Provenance tags: R = reported (claim ID), A = adapted from cited claims, D = design choice (needs approval), GAP = no value.', '']
    if validation['errors']:
        L += ['## Errors (fix before review)', ''] + [f'- {e}' for e in validation['errors']] + ['']
    if validation['gaps']:
        L += ['## Gaps (block the wet lab)', ''] + [f'- `{g["path"]}` ({g["unit"]}): {g.get("rationale") or "no evidence found"}' for g in validation['gaps']] + ['']
    if validation['design_choices']:
        L += ['## Design choices needing human approval', ''] + [
            f'- `{d["path"]}` = {d["value"]} {d["unit"]}: {d["rationale"]}' for d in validation['design_choices']] + ['']
    cs = protocol['culture_system']
    L += ['## Culture system', '', f'- Format: {cs["format"]}; vessel: {cs["vessel"]}; working volume: {_fmt_q(cs["working_volume"])}']
    L += [f'- {k}: {_fmt_q(q)}' for k, q in cs['parameters'].items()] + ['']
    L += ['## Stages', '', '| Stage | Days | Medium | Goal |', '|---|---|---|---|']
    L += [f'| {s["name"]} (`{s["stage_id"]}`) | {s["start_day"]:g}–{s["end_day"]:g} | {_fmt_q(s["medium"])} | {s.get("goal", "")} |'
          for s in protocol['stages']] + ['']
    for arm in protocol['genotype_arms']:
        desc = arm['genotype'] + (f' {arm["gene"]}' if arm.get('gene') else '') + (f' ({arm["modification"]})' if arm.get('modification') else '')
        L += [f'## Arm `{arm["arm_id"]}`: {desc}', '', '| Day | Until | Stage | Action | Factor | Amount | Note |', '|---|---|---|---|---|---|---|']
        for s in arm_schedule(protocol, arm['arm_id']):
            until = '' if s.get('end_day') is None else f'{s["end_day"]:g}'
            note = s['description'] + (f' **(arm adjustment {", ".join(s["adjusted_by"])})**' if s['adjusted_by'] else '')
            L.append(f'| {s["day"]:g} | {until} | {s["stage_id"]} | {s["action"]} | {s.get("factor") or ""} | {_fmt_q(s.get("quantity"))} | {note} |')
        L.append('')
    if protocol['genotype_effects']:
        L += ['## Predicted genotype effects (to be tested against WT)', '', '| Effect | Arm | Gene | Quantity | Readout | Prediction | Evidence |', '|---|---|---|---|---|---|---|']
        L += [f'| {e["effect_id"]} | {e["arm_id"]} | {e["gene"]} | {e["affected_quantity"]} | {e["readout_metric"]} | '
              f'{e["predicted_direction"]} | {e["evidence_level"]} {",".join(e["claim_ids"])} |' for e in protocol['genotype_effects']] + ['']
    mp = protocol['measurement_plan']
    L += ['## Measurements to return', '', f'- Mode: **{mp["mode"]}**; harvest day {mp["harvest_day"]:g}; sampling days {mp["sampling_days"]}',
          f'- Replicates per arm: {mp.get("replicates_per_arm", 1)}',
          '- Minimal (every arm, every replicate): input viable iPSCs; at harvest: total viable cells, viability %, '
          'target marker %, residual pluripotency % (marker named); every deviation from this sheet.',
          '- Release QC (if run): sterility, mycoplasma, endotoxin (EU/mL), karyotype.']
    if mp['mode'] == 'max':
        L.append('- Max: plus the daily integrated-machine record (M1 pH/DO/capacitance/OUR, M3a metabolites, M3b aggregates, '
                 'M3c VCD/viability, M3d impedance, M3e panels, M4 harvest stream), executed setpoints and prior runs of the same line.')
    L += ['- Use the measurement template (`measurement_template.json`) and validate it with `validate-run` before analysis.', '']
    if protocol['open_questions']:
        L += ['## Open questions', ''] + [f'- {q}' for q in protocol['open_questions']] + ['']
    L += ['## Limitations', ''] + [f'- {x}' for x in protocol['limitations']] + ['']
    return '\n'.join(L)


def measurement_template(protocol, validation):
    """A BioreactorRun skeleton for the operator. Null fields must be filled before validate-run."""
    mp = protocol['measurement_plan']
    arms = []
    for arm in protocol['genotype_arms']:
        for rep in range(1, mp.get('replicates_per_arm', 1) + 1):
            a = {'arm_id': arm['arm_id'], 'replicate': rep, 'line_id': arm.get('line_id'), 'passage_number': None,
                 'input_cells': None, 'deviations': [],
                 'timepoints': [{'day': d, 'viable_cells_total': None, 'viability_pct': None} for d in mp['sampling_days']],
                 'harvest': {'day': mp['harvest_day'], 'viable_cells_total': None, 'viability_pct': None,
                             'target_marker_pct': None, 'marker_method': None, 'residual_pluripotency_pct': None,
                             'residual_pluripotency_marker': None, 'harvest_onset_day': None},
                 'qc_tests': {}}
            if mp['mode'] == 'max':
                a.update({'observations': [], 'setpoints': {}, 'outcome': {}, 'history': []})
            arms.append(a)
    return {'schema_version': K.PRODUCTION_VERSION, 'run_id': f'run-{protocol["protocol_id"]}-REPLACE',
            'protocol_id': protocol['protocol_id'], 'protocol_sha256': validation['protocol_sha256'],
            'mode': mp['mode'], 'source': 'wet_lab', 'operator': None, 'started_at': None,
            'notes': 'TEMPLATE: replace nulls with measured values; omit qc_tests entries that were not run.',
            'arms': arms}
