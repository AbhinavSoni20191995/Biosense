"""Production loop: protocol -> bioreactor run -> analysis -> orchestrator (offline, synthetic fixtures)."""
import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from agent_tools import compile_handoff, convert_unit
from analysis_agent.protocol_runner import protocol_to_setpoints, simulate_protocol
from biosense import contracts as K
from biosense.production import analysis as AN
from biosense.production import orchestrator as OR
from biosense.production import protocol as PR
from biosense.production import qc as QC
from biosense.production.runs import validate_run

PX = Path(__file__).resolve().parent.parent / 'examples' / 'production'


def load(name):
    return json.loads((PX / name).read_text())


def approved(it):
    p, h, r = load(f'protocol.it{it}.synthetic.json'), load(f'handoff.it{it}.synthetic.json'), load('request.monocyte_d25.json')
    return PR.approve_protocol(p, PR.validate_protocol(p, [h], r), 'Test Reviewer')


def run_for(protocol, **kw):
    kw.setdefault('truth', load('standin_truth.synthetic.json'))
    return simulate_protocol(protocol, PR.protocol_sha256(protocol), **kw)


def minimal_run(protocol, arms, qc_tests=None):
    """Hand-written minimal-mode record: arms = {arm_id: (input, viable, viability, marker, residual)}."""
    out = []
    for aid, (inp, viable, via, marker, resid) in arms.items():
        a = {'arm_id': aid, 'replicate': 1, 'input_cells': inp, 'deviations': [],
             'harvest': {'day': 25, 'viable_cells_total': viable, 'viability_pct': via, 'target_marker_pct': marker,
                         'residual_pluripotency_pct': resid}}
        if qc_tests is not None:
            a['qc_tests'] = qc_tests
        out.append(a)
    return {'schema_version': '2.0', 'run_id': 'hand-1', 'protocol_id': protocol['protocol_id'],
            'protocol_sha256': PR.protocol_sha256(protocol), 'mode': 'minimal', 'source': 'wet_lab', 'arms': out}


CLEAN_QC = {'sterility': 'no_growth', 'mycoplasma': 'not_detected', 'endotoxin_EU_per_mL': 0.1, 'karyotype': 'normal'}


class ProtocolValidationTests(unittest.TestCase):
    def setUp(self):
        self.p, self.h, self.r = load('protocol.it0.synthetic.json'), load('handoff.it0.synthetic.json'), load('request.monocyte_d25.json')

    def v(self, p=None, handoffs='default', r='default'):
        return PR.validate_protocol(p or self.p, [self.h] if handoffs == 'default' else handoffs, self.r if r == 'default' else r)

    def test_fixture_needs_human_approval_and_lists_design_choices(self):
        v = self.v()
        self.assertEqual(v['status'], 'needs_approval', v['errors'])
        self.assertIn('culture_system.parameters.feed_fraction', [d['path'] for d in v['design_choices']])
        self.assertEqual([e['effect_id'] for e in v['hypothesis_effects']], ['E1'])

    def test_reported_value_must_equal_claim(self):
        self.p['stages'][1]['steps'][0]['quantity']['value'] = 30  # BMP4 claim c06 says 25 ng/mL
        self.assertTrue(any('differs from claim c06' in e for e in self.v()['errors']))

    def test_reported_value_unit_conversion_accepted(self):
        self.p['stages'][1]['steps'][0]['quantity'].update(value=0.025, unit='ug/mL')
        self.assertEqual(self.v()['status'], 'needs_approval')

    def test_unknown_or_rejected_claim_refused(self):
        self.p['stages'][1]['steps'][0]['quantity']['claim_ids'] = ['c99']
        self.assertTrue(any("'c99' not found" in e for e in self.v()['errors']))
        self.h['rejected_claims'] = [{'claim': {'id': 'c06'}, 'reasons': ['x']}]
        self.p['stages'][1]['steps'][0]['quantity']['claim_ids'] = ['c06']
        self.assertTrue(any('rejected' in e for e in self.v()['errors']))

    def test_gap_blocks_wet_lab(self):
        self.p['stages'][3]['steps'][1]['quantity'] = {'value': None, 'unit': 'ng/mL', 'provenance': 'gap', 'claim_ids': [], 'rationale': 'IL-3 dose not found'}
        v = self.v()
        self.assertEqual(v['status'], 'blocked')
        with self.assertRaises(K.ContractError):
            PR.approve_protocol(self.p, v, 'Reviewer')

    def test_null_value_must_be_declared_gap(self):
        self.p['stages'][3]['steps'][1]['quantity']['value'] = None
        self.assertTrue(any('use provenance "gap"' in e for e in self.v()['errors']))

    def test_design_choice_needs_rationale(self):
        self.p['culture_system']['parameters']['feed_fraction']['rationale'] = None
        self.assertTrue(any('design choice needs a rationale' in e for e in self.v()['errors']))

    def test_knockout_needs_wild_type_control(self):
        self.p['genotype_arms'] = [a for a in self.p['genotype_arms'] if a['genotype'] != 'wild_type']
        self.assertTrue(any('wild_type control' in e for e in self.v(r=None)['errors']))

    def test_direct_genotype_effect_needs_claims(self):
        self.p['genotype_effects'][0]['evidence_level'] = 'direct_same_cell_type'
        self.assertTrue(any('needs claim IDs' in e for e in self.v()['errors']))

    def test_adjustment_effect_must_belong_to_arm(self):
        self.p['arm_adjustments'] = [{'adjustment_id': 'A1', 'arm_id': 'WT', 'step_id': 'S07', 'day_shift': 1,
                                      'rationale': 'x', 'effect_ids': ['E1']}]
        self.assertTrue(any('belongs to arm GENEX_KO' in e for e in self.v()['errors']))

    def test_harvest_day_must_answer_question(self):
        self.r['desired_output']['at_day'] = 30
        self.assertTrue(any('does not answer the question' in e for e in self.v()['errors']))

    def test_overlapping_stages_and_step_outside_stage(self):
        self.p['stages'][1]['start_day'] = 3
        self.p['stages'][0]['steps'][0]['day'] = 5
        errs = self.v()['errors']
        self.assertTrue(any('must not overlap' in e for e in errs))
        self.assertTrue(any('outside stage' in e for e in errs))

    def test_approval_goes_stale_when_protocol_changes(self):
        a = PR.approve_protocol(self.p, self.v(), 'Reviewer')
        self.assertEqual(self.v(a)['status'], 'approved')
        a['stages'][1]['steps'][0]['description'] = 'edited after approval'
        self.assertTrue(any('stale' in e for e in self.v(a)['errors']))

    def test_arm_schedule_applies_adjustments(self):
        p1 = load('protocol.it1.synthetic.json')
        ko = {s['step_id']: s for s in PR.arm_schedule(p1, 'GENEX_KO')}
        wt = {s['step_id']: s for s in PR.arm_schedule(p1, 'WT')}
        self.assertEqual(ko['S07']['quantity']['value'], 80)
        self.assertEqual(ko['S07']['adjusted_by'], ['A1'])
        self.assertEqual(wt['S07']['quantity']['value'], 50)

    def test_run_sheet_and_template(self):
        v = self.v()
        sheet = PR.render_run_sheet(self.p, v, self.r)
        self.assertIn('NEEDS_APPROVAL', sheet)
        self.assertIn('Arm `GENEX_KO`', sheet)
        t = PR.measurement_template(self.p, v)
        self.assertEqual({a['arm_id'] for a in t['arms']}, {'WT', 'GENEX_KO'})
        self.assertTrue(K.schema_errors('bioreactor_run', t))  # nulls must be filled before use


class LiteratureCompilerTests(unittest.TestCase):
    def test_fixture_handoffs_compile_without_rejections(self):
        src = load('sources/SYNTH-MONO-001.json')
        h = compile_handoff(load('request.monocyte_d25.json'), load('extraction.it1.synthetic.json'), [src])
        self.assertEqual(len(h['claims']), 14)
        self.assertEqual(h['rejected_claims'], [])
        self.assertEqual(h['conflicts'], [])  # different seeding arms are not a conflict

    def test_genotype_effect_claim_needs_genotype_context(self):
        src, ex = load('sources/SYNTH-MONO-001.json'), load('extraction.it1.synthetic.json')
        c14 = next(c for c in ex['claims'] if c['id'] == 'c14')
        del c14['context']['genotype']
        h = compile_handoff(load('request.monocyte_d25.json'), ex, [src])
        self.assertIn('c14', [r['claim']['id'] for r in h['rejected_claims']])

    def test_growth_factor_units(self):
        self.assertAlmostEqual(convert_unit(50, 'ng/mL', 'ug/mL'), 0.05)
        with self.assertRaises(ValueError):
            convert_unit(50, 'ng/mL', 'uM')  # mass vs molar needs a molecular weight


class QCTests(unittest.TestCase):
    def test_inheritance_and_override(self):
        prof = QC.load_profile('monocyte_macrophage_research', [{'id': 'qc-endotoxin', 'value': 0.5, 'basis': 'test'}])
        crit = {c['id']: c for c in prof['criteria']}
        self.assertEqual(crit['qc-identity-purity']['value'], 85.0)  # child replaces parent's null
        self.assertEqual(crit['qc-viability']['value'], 70.0)  # inherited
        self.assertEqual(crit['qc-endotoxin']['value_status'], 'user_override')

    def test_untested_and_unspecified_are_never_pass(self):
        prof = QC.load_profile('generic_ipsc_derived_product')
        rows = {r['id']: r for r in QC.score(prof, 'minimal', {'viability_pct': 80, 'target_marker_pct': 99})}
        self.assertEqual(rows['qc-viability']['status'], 'PASS')
        self.assertEqual(rows['qc-identity-purity']['status'], 'SPEC_MISSING')
        self.assertEqual(rows['qc-sterility']['status'], 'NOT_TESTED')
        self.assertEqual(QC.overall(list(rows.values())), 'INCOMPLETE')

    def test_replicates_straddling_limit_are_marginal(self):
        prof = QC.load_profile('generic_ipsc_derived_product')
        rows = {r['id']: r for r in QC.score(prof, 'minimal', {'viability_pct': 72}, {'viability_pct': [66, 78]})}
        self.assertEqual(rows['qc-viability']['status'], 'MARGINAL')

    def test_max_only_criterion_not_applicable_in_minimal(self):
        prof = QC.load_profile('monocyte_macrophage_research')
        rows = {r['id']: r for r in QC.score(prof, 'minimal', {})}
        self.assertEqual(rows['qc-monocyte-diameter-min']['status'], 'NOT_APPLICABLE')


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.r = load('request.monocyte_d25.json')
        self.p1 = approved(1)

    def test_hand_calculated_target_and_success(self):
        run = minimal_run(self.p1, {'WT': (5e7, 1.5e9, 85, 90, 0.1), 'GENEX_KO': (5e7, 1.5e9, 85, 90, 0.1)}, CLEAN_QC)
        rep = AN.analyze(run, self.p1, self.r)
        wt = rep['arms'][0]
        self.assertAlmostEqual(wt['metrics']['target_cells_per_input_cell']['mean'], 27.0)  # 1.5e9*0.9/5e7
        self.assertEqual(wt['target']['status'], 'MET')
        self.assertEqual(rep['verdict']['status'], 'SUCCESS')

    def test_sterility_failure_is_safety_hold_not_redesign(self):
        qc = dict(CLEAN_QC, sterility='growth_detected')
        run = minimal_run(self.p1, {'WT': (5e7, 1.5e9, 85, 90, 0.1), 'GENEX_KO': (5e7, 1.5e9, 85, 90, 0.1)}, qc)
        rep = AN.analyze(run, self.p1, self.r)
        self.assertEqual(rep['verdict']['status'], 'SAFETY_HOLD')
        self.assertTrue(rep['diagnosis'][0]['blocks_protocol_revision'])

    def test_wrong_harvest_day_is_not_comparable(self):
        run = minimal_run(self.p1, {'WT': (5e7, 1.5e9, 85, 90, 0.1), 'GENEX_KO': (5e7, 1.5e9, 85, 90, 0.1)}, CLEAN_QC)
        for a in run['arms']:
            a['harvest']['day'] = 21
        rep = AN.analyze(run, self.p1, self.r)
        self.assertEqual(rep['arms'][0]['target']['status'], 'NOT_COMPARABLE')
        self.assertIn('schedule', [h['category'] for h in rep['diagnosis']])

    def test_run_from_other_protocol_version_refused(self):
        run = minimal_run(self.p1, {'WT': (5e7, 1e9, 85, 90, 0.1)})
        run['protocol_sha256'] = 'x'
        self.assertFalse(validate_run(run, self.p1)['valid'])
        with self.assertRaises(K.ContractError):
            AN.analyze(run, self.p1, self.r)

    def test_genotype_prediction_verdicts(self):
        p = copy.deepcopy(self.p1)
        p['genotype_effects'][0]['predicted_direction'] = 'decrease'
        p.pop('approval')
        cases = {(1.0e9, 'decrease'): 'consistent', (1.5e9, 'decrease'): 'inconclusive', (2.0e9, 'decrease'): 'contradicted'}
        for (ko_viable, _), want in cases.items():
            run = minimal_run(p, {'WT': (5e7, 1.5e9, 85, 90, 0.1), 'GENEX_KO': (5e7, ko_viable, 85, 90, 0.1)})
            cmp_ = AN.compare_genotypes(p, {a['arm_id']: [AN.replicate_metrics(a, 'minimal')[0]] for a in run['arms']})
            self.assertEqual(cmp_[0]['predicted_effects'][0]['verdict'], want)

    def test_replicates_use_standard_error_band(self):
        t = AN._direction('target_cells_per_input_cell', [20, 21, 22], [30, 31, 29])
        self.assertEqual((t['observed_sign'], t['strength']), (-1, 'replicated'))

    def test_iteration0_standin_diagnoses_genotype_specific_failure(self):
        p0 = approved(0)
        rep = AN.analyze(run_for(p0), p0, self.r)
        self.assertEqual(rep['verdict']['status'], 'FAILED')
        self.assertEqual(rep['verdict']['target_met_arms'], ['WT'])
        self.assertEqual(rep['diagnosis'][0]['category'], 'genotype_specific')
        self.assertTrue(any(lv['step_id'] == 'S07' for lv in rep['diagnosis'][0]['levers']))

    def test_max_mode_sensor_fault_is_data_unreliable(self):
        rep = AN.analyze(run_for(self.p1, mode='max', scenario='sensor_fault'), self.p1, self.r)
        self.assertEqual(rep['verdict']['status'], 'DATA_UNRELIABLE')
        self.assertTrue(any('sensor drift' in s for s in rep['data_integrity']['checks']))

    def test_max_mode_staining_fault_is_data_unreliable(self):
        rep = AN.analyze(run_for(self.p1, mode='max', scenario='stain_fault'), self.p1, self.r)
        self.assertEqual(rep['verdict']['status'], 'DATA_UNRELIABLE')
        self.assertTrue(any('staining failure' in s for s in rep['data_integrity']['checks']))

    def test_max_mode_clonal_drift_is_safety_hold(self):
        rep = AN.analyze(run_for(self.p1, mode='max', scenario='clonal'), self.p1, self.r)
        self.assertEqual(rep['verdict']['status'], 'SAFETY_HOLD')
        self.assertIn('genetic_stability', [h['category'] for h in rep['diagnosis']])

    def test_max_mode_clean_reports_integrity_ok_and_kinetics(self):
        rep = AN.analyze(run_for(self.p1, mode='max'), self.p1, self.r)
        self.assertEqual(rep['data_integrity']['status'], 'OK')
        self.assertIsNotNone(rep['arms'][0]['metrics']['growth_rate_per_h']['mean'])
        self.assertIsNotNone(rep['arms'][0]['max_mode'][0]['fused_purity'])


class OrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.r = load('request.monocyte_d25.json')
        OR.loop_init(self.r, self.tmp / 'loop')
        self.loop = self.tmp / 'loop'

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def decide(self, protocol, run):
        return OR.decide(AN.analyze(run, protocol, self.r), protocol, self.r, self.loop)

    def test_full_offline_loop_revise_then_complete_qc_then_success(self):
        p0, p1 = approved(0), approved(1)
        d0, _, prompt = self.decide(p0, run_for(p0))
        self.assertEqual((d0['type'], d0['route_to'], d0['protocol_worked']), ('revise_protocol', 'literature', 'no'))
        b = d0['revision_brief']
        self.assertTrue(any('WT met the target' in k for k in b['keep_fixed']))
        self.assertTrue(any('GENEX' in q for q in b['suggested_queries']))
        self.assertIn('Why it did not work', prompt.read_text())
        self.assertEqual(p1['revision_brief_id'], b['brief_id'])
        d1, _, _ = self.decide(p1, run_for(p1, run_id='it1-min'))
        self.assertEqual(d1['type'], 'complete_qc')
        self.assertTrue(any('qc-sterility: NOT_TESTED' in a for a in d1['operator_actions']))
        d2, _, _ = self.decide(p1, run_for(p1, run_id='it1-rt', release_tests=True))
        self.assertEqual((d2['type'], d2['protocol_worked'], d2['route_to']), ('protocol_succeeded', 'yes', 'human'))
        self.assertEqual([i['decision_type'] for i in OR.load_state(self.loop)['iterations']],
                         ['revise_protocol', 'complete_qc', 'protocol_succeeded'])

    def test_unreliable_data_repeats_then_escalates(self):
        p1 = approved(1)
        d0, _, _ = self.decide(p1, run_for(p1, mode='max', scenario='stain_fault', run_id='a'))
        self.assertEqual((d0['type'], d0['route_to'], d0['revision_brief']), ('repeat_measurement', 'operator', None))
        d1, _, _ = self.decide(p1, run_for(p1, mode='max', scenario='stain_fault', run_id='b'))
        self.assertEqual(d1['type'], 'escalate_to_human')

    def test_budget_exhaustion_stops(self):
        p0 = approved(0)
        types = [self.decide(p0, run_for(p0, run_id=f'r{i}'))[0]['type'] for i in range(4)]
        self.assertEqual(types, ['revise_protocol'] * 3 + ['stop_budget'])

    def test_request_cannot_change_mid_loop(self):
        p0 = approved(0)
        rep = AN.analyze(run_for(p0), p0, self.r)
        moved = copy.deepcopy(self.r)
        moved['desired_output']['value'] = 5
        with self.assertRaises(K.ContractError):
            OR.decide(rep, p0, moved, self.loop)

    def test_decision_is_immutable(self):
        p0 = approved(0)
        rep = AN.analyze(run_for(p0), p0, self.r)
        OR.decide(rep, p0, self.r, self.loop)
        with self.assertRaises(K.ContractError):
            OR.decide(rep, p0, self.r, self.loop)


class StandinTests(unittest.TestCase):
    def test_standin_maps_factors_and_reports_unsimulated_steps(self):
        p1 = load('protocol.it1.synthetic.json')
        sp, days, vol, notes = protocol_to_setpoints(p1, 'GENEX_KO')
        self.assertEqual((sp.mcsf, sp.il3, sp.bmp4, sp.seed_density, sp.do_setpoint), (80, 25, 25, 0.5, 0.2))
        self.assertEqual(sum(days.values()), 25)
        self.assertTrue(any('Y-27632' in n for n in notes))

    def test_standin_output_is_valid_labelled_and_deterministic(self):
        p1 = load('protocol.it1.synthetic.json')
        a, b = run_for(p1), run_for(p1)
        self.assertEqual(K.schema_errors('bioreactor_run', a), [])
        self.assertEqual(a['source'], 'synthetic_standin')
        self.assertEqual(a['arms'][0]['harvest'], b['arms'][0]['harvest'])

    def test_standin_refuses_unmappable_stage_count(self):
        p1 = load('protocol.it1.synthetic.json')
        p1['stages'] = p1['stages'][:3]
        with self.assertRaises(ValueError):
            protocol_to_setpoints(p1, 'WT')


if __name__ == '__main__':
    unittest.main()
