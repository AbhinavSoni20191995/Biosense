"""Agent-driven loop: autonomy gates, the decision envelope, consults, bioinformatics, CAR-T stand-in."""
import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from biosense import contracts as K
from biosense.bioinformatics import knowledge as KB
from biosense.bioinformatics import tools as BT
from biosense.production import analysis as AN
from biosense.production import autonomy as AU
from biosense.production import orchestrator as OR
from biosense.production import protocol as PR
from standins import get_standin, tcell

CART = Path(__file__).resolve().parent.parent / 'examples' / 'cart'


def load(name):
    return json.loads((CART / name).read_text())


def approved(it, request=None):
    p, h = load(f'protocol.it{it}.synthetic.json'), load(f'handoff.it{it}.synthetic.json')
    return PR.approve_protocol(p, PR.validate_protocol(p, [h], request or load('request.cart_d10.json')),
                               'Test Reviewer')


def run_for(protocol, **kw):
    kw.setdefault('truth', load('standin_truth.synthetic.json'))
    return tcell.simulate_protocol(protocol, PR.protocol_sha256(protocol), **kw)


def base_decision(**kw):
    d = {'type': 'revise_protocol', 'reason': 'x' * 40, 'reasoning': 'y' * 80,
         'route_to': 'literature', 'protocol_worked': 'no', 'evidence': ['e'],
         'hypotheses': [{'hypothesis_id': 'OH1', 'statement': 's', 'basis': ['analysis H01']}]}
    d.update(kw)
    return d


class AutonomyTests(unittest.TestCase):
    def gates(self, **hitl):
        r = load('request.cart_d10.json')
        r['human_in_the_loop'] = {'mode': 'full', **hitl}
        return r, AU.resolve(r)

    def test_full_mode_gates_everything(self):
        _, g = self.gates(mode='full')
        self.assertTrue(g['protocol_approval_required'])
        self.assertTrue(g['decision_approval_required'])

    def test_autonomous_mode_commits_decisions(self):
        _, g = self.gates(mode='autonomous')
        self.assertFalse(g['decision_approval_required'])

    def test_wet_lab_forces_protocol_approval_over_autonomous_mode(self):
        r = load('request.cart_d10.json')
        r['human_in_the_loop'] = {'mode': 'autonomous'}
        r['bioreactor_source'] = 'wet_lab'
        g = AU.resolve(r)
        self.assertTrue(g['protocol_approval_required'])
        self.assertTrue(any('wet_lab' in o for o in g['safety_overrides']))
        self.assertIn('Overridden by a safety rule', AU.describe(g))

    def test_standin_may_waive_protocol_approval(self):
        r = load('request.cart_d10.json')
        r['human_in_the_loop'] = {'mode': 'autonomous'}
        r['bioreactor_source'] = 'synthetic_standin'
        g = AU.resolve(r)
        self.assertFalse(g['protocol_approval_required'])
        self.assertEqual(g['safety_overrides'], [])

    def test_bad_mode_refused(self):
        with self.assertRaises(ValueError):
            self.gates(mode='whatever')


class EnvelopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.r = load('request.cart_d10.json')
        self.loop = self.tmp / 'loop'
        OR.loop_init(self.r, self.loop)
        self.p = approved(0, self.r)
        self.rep = AN.analyze(run_for(self.p), self.p, self.r)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def env(self, report=None, state=None, bio=()):
        return OR.allowed_actions(report or self.rep, self.p, self.r, state or OR.load_state(self.loop), bio)

    def test_failed_allows_revision_and_information(self):
        e = self.env()
        self.assertEqual(e['verdict'], 'FAILED')
        self.assertIsNone(e['mandatory'])
        for a in ('revise_protocol', 'request_bioinformatics', 'request_literature', 'consult_human'):
            self.assertIn(a, e['allowed'])
        self.assertIn('protocol_succeeded', e['forbidden'])

    def test_safety_hold_admits_only_escalation(self):
        rep = copy.deepcopy(self.rep)
        rep['verdict']['status'] = 'SAFETY_HOLD'
        e = self.env(rep)
        self.assertEqual(e['mandatory'], 'escalate_to_human')
        self.assertEqual(e['allowed'], ['escalate_to_human'])
        self.assertIn('revise_protocol', e['forbidden'])

    def test_unreliable_data_forbids_touching_the_protocol(self):
        rep = copy.deepcopy(self.rep)
        rep['verdict']['status'] = 'DATA_UNRELIABLE'
        e = self.env(rep)
        self.assertIn('repeat_measurement', e['allowed'])
        self.assertIn('revise_protocol', e['forbidden'])
        self.assertIn('protocol_succeeded', e['forbidden'])

    def test_success_cannot_be_stopped_on_budget(self):
        rep = copy.deepcopy(self.rep)
        rep['verdict']['status'] = 'SUCCESS'
        e = self.env(rep)
        self.assertEqual(e['allowed'], ['protocol_succeeded', 'escalate_to_human'])

    def test_spent_budget_replaces_revision_with_stop(self):
        state = OR.load_state(self.loop)
        state['iterations'] = [{'iteration': i, 'decision_type': 'revise_protocol', 'advances_iteration': True,
                               'analysis_id': f'a{i}', 'verdict': 'FAILED'} for i in range(4)]
        e = self.env(state=state)
        self.assertIn('stop_budget', e['allowed'])
        self.assertIn('revise_protocol', e['forbidden'])

    def test_information_cap_closes_gathering(self):
        state = OR.load_state(self.loop)
        state['iterations'] = [{'iteration': i, 'decision_type': 'request_bioinformatics',
                                'advances_iteration': False, 'analysis_id': f'a{i}', 'verdict': 'FAILED'}
                               for i in range(4)]
        e = self.env(state=state)
        self.assertIn('revise_protocol', e['allowed'])
        for a in ('request_bioinformatics', 'consult_human'):
            self.assertIn(a, e['forbidden'])

    def test_consults_off_removes_the_action(self):
        self.r['human_in_the_loop'] = {'mode': 'checkpoints', 'consults': 'never'}
        self.assertIn('consult_human', self.env()['forbidden'])

    def test_bioinformatics_off_removes_the_action(self):
        self.r['bioinformatics'] = {'allowed': False}
        self.assertIn('request_bioinformatics', self.env()['forbidden'])

    def test_policy_advice_is_recorded_alongside(self):
        self.assertEqual(self.env()['policy_advice']['type'], 'revise_protocol')

    def test_info_actions_do_not_spend_the_budget(self):
        OR.commit_decision(base_decision(
            type='request_bioinformatics', route_to='bioinformatics', protocol_worked='undetermined',
            instruction={'to': 'bioinformatics', 'ask': 'EXH1 knockout effects'}),
            self.rep, self.p, self.r, self.loop)
        state = OR.load_state(self.loop)
        self.assertEqual(OR.iterations_used(state), 0)
        self.assertEqual(OR.info_actions_since_advance(state), 1)


class DecisionValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.r = load('request.cart_d10.json')
        self.loop = self.tmp / 'loop'
        OR.loop_init(self.r, self.loop)
        self.p = approved(0, self.r)
        self.rep = AN.analyze(run_for(self.p), self.p, self.r)
        self.brief = OR.build_revision_brief(self.rep, self.p, self.r, 0)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def commit(self, **kw):
        return OR.commit_decision(base_decision(**kw), self.rep, self.p, self.r, self.loop)

    def test_valid_revision_commits(self):
        d, path, prompt = self.commit(revision_brief=self.brief)
        self.assertEqual(d['status'], 'committed')
        self.assertTrue(d['advances_iteration'])
        self.assertTrue(Path(path).exists() and Path(prompt).exists())
        self.assertEqual(d['policy_advice']['type'], 'revise_protocol')

    def test_action_outside_the_envelope_refused(self):
        with self.assertRaises(K.ContractError) as e:
            self.commit(type='protocol_succeeded', route_to='human', protocol_worked='yes')
        self.assertIn('not allowed', str(e.exception))

    def test_thin_reasoning_refused(self):
        with self.assertRaises(K.ContractError) as e:
            self.commit(reasoning='because', revision_brief=self.brief)
        self.assertIn('reasoning', str(e.exception))

    def test_hypothesis_without_basis_refused(self):
        with self.assertRaises(K.ContractError) as e:
            self.commit(revision_brief=self.brief,
                        hypotheses=[{'hypothesis_id': 'OH1', 'statement': 's', 'basis': []}])
        self.assertIn('no basis', str(e.exception))

    def test_revision_without_levers_refused(self):
        bad = copy.deepcopy(self.brief)
        bad['levers'] = []
        with self.assertRaises(K.ContractError) as e:
            self.commit(revision_brief=bad)
        self.assertIn('no lever', str(e.exception))

    def test_information_action_needs_an_instruction(self):
        with self.assertRaises(K.ContractError) as e:
            self.commit(type='request_literature', route_to='literature', protocol_worked='undetermined')
        self.assertIn('instruction', str(e.exception))

    def test_consult_decision_must_reference_its_notice(self):
        with self.assertRaises(K.ContractError) as e:
            self.commit(type='consult_human', route_to='human', protocol_worked='undetermined',
                        instruction={'to': 'human', 'ask': 'q'})
        self.assertIn('consult', str(e.exception))

    def test_unknown_consult_reference_refused(self):
        with self.assertRaises(K.ContractError) as e:
            self.commit(revision_brief=self.brief, consult_ids=['consult-99'])
        self.assertIn('does not exist', str(e.exception))

    def test_missing_tool_result_file_refused(self):
        with self.assertRaises(K.ContractError) as e:
            self.commit(revision_brief=self.brief,
                        tool_calls=[{'tool': 'bio', 'purpose': 'p', 'result_ref': '/nope/missing.json'}])
        self.assertIn('missing result file', str(e.exception))

    def test_one_advancing_decision_per_analysis(self):
        self.commit(revision_brief=self.brief)
        with self.assertRaises(K.ContractError):
            self.commit(revision_brief=self.brief)

    def test_request_cannot_change_mid_loop(self):
        moved = copy.deepcopy(self.r)
        moved['desired_output']['value'] = 1
        with self.assertRaises(K.ContractError) as e:
            OR.commit_decision(base_decision(revision_brief=self.brief), self.rep, self.p, moved, self.loop)
        self.assertIn('changed after the loop started', str(e.exception))


class GatedDecisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.r = load('request.cart_d10.json')
        self.r['human_in_the_loop'] = {'mode': 'full'}
        self.loop = self.tmp / 'loop'
        OR.loop_init(self.r, self.loop)
        self.p = approved(0, self.r)
        self.rep = AN.analyze(run_for(self.p), self.p, self.r)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_full_mode_holds_the_decision_then_a_person_releases_it(self):
        brief = OR.build_revision_brief(self.rep, self.p, self.r, 0)
        d, _, _ = OR.commit_decision(base_decision(revision_brief=brief), self.rep, self.p, self.r, self.loop)
        self.assertEqual(d['status'], 'pending_human')
        released = OR.approve_decision(self.loop, d['decision_id'], 'A Person', 'looks right')
        self.assertEqual(released['status'], 'committed')
        self.assertEqual(released['autonomy']['approved_by'], 'A Person')
        self.assertEqual(OR.load_state(self.loop)['iterations'][0]['status'], 'committed')

    def test_approval_needs_a_name(self):
        brief = OR.build_revision_brief(self.rep, self.p, self.r, 0)
        d, _, _ = OR.commit_decision(base_decision(revision_brief=brief), self.rep, self.p, self.r, self.loop)
        with self.assertRaises(K.ContractError):
            OR.approve_decision(self.loop, d['decision_id'], '   ')

    def test_cannot_approve_twice(self):
        brief = OR.build_revision_brief(self.rep, self.p, self.r, 0)
        d, _, _ = OR.commit_decision(base_decision(revision_brief=brief), self.rep, self.p, self.r, self.loop)
        OR.approve_decision(self.loop, d['decision_id'], 'A Person')
        with self.assertRaises(K.ContractError):
            OR.approve_decision(self.loop, d['decision_id'], 'A Person')

    def test_policy_path_is_gated_too(self):
        d, _, _ = OR.decide(self.rep, self.p, self.r, self.loop)
        self.assertEqual((d['authored_by'], d['status']), ('policy', 'pending_human'))


class ConsultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.r = load('request.cart_d10.json')
        self.loop = self.tmp / 'loop'
        OR.loop_init(self.r, self.loop)
        self.p = approved(0, self.r)
        self.rep = AN.analyze(run_for(self.p), self.p, self.r)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def notice(self, **kw):
        c = {'iteration': 0, 'raised_by': 'orchestrator_agent',
             'question': 'Do you have an in-house read on the knockout?',
             'why_it_matters': 'It picks the lever.',
             'blocking': False,
             'fallback': {'action': 'apply both levers', 'consequence': 'we will not know which carried it'}}
        c.update(kw)
        return c

    def test_consult_requires_a_fallback(self):
        bad = self.notice()
        del bad['fallback']
        with self.assertRaises(K.ContractError):
            OR.raise_consult(self.loop, bad)

    def test_raise_then_answer_records_evidence_status(self):
        c, path = OR.raise_consult(self.loop, self.notice())
        self.assertTrue(Path(path).exists())
        self.assertEqual(OR.list_consults(OR.load_state(self.loop), 'open')[0]['consult_id'], c['consult_id'])
        a = OR.answer_consult(self.loop, c['consult_id'], 'A Scientist', 'over-stimulation',
                              evidence_status='unpublished_in_house_result', citable_as='adapted')
        self.assertEqual(a['status'], 'answered')
        self.assertEqual(a['answer']['citable_as'], 'adapted')
        self.assertEqual(OR.list_consults(OR.load_state(self.loop), 'open'), [])

    def test_declining_keeps_it_a_design_choice(self):
        c, _ = OR.raise_consult(self.loop, self.notice())
        a = OR.answer_consult(self.loop, c['consult_id'], 'A Scientist', 'no data', declined=True)
        self.assertEqual((a['status'], a['answer']['citable_as']), ('declined', 'design_choice'))

    def test_answers_are_not_overwritten(self):
        c, _ = OR.raise_consult(self.loop, self.notice())
        OR.answer_consult(self.loop, c['consult_id'], 'A', 'x')
        with self.assertRaises(K.ContractError):
            OR.answer_consult(self.loop, c['consult_id'], 'B', 'y')

    def test_blocking_consult_blocks_a_revision(self):
        OR.raise_consult(self.loop, self.notice(blocking=True))
        brief = OR.build_revision_brief(self.rep, self.p, self.r, 0)
        with self.assertRaises(K.ContractError) as e:
            OR.commit_decision(base_decision(revision_brief=brief), self.rep, self.p, self.r, self.loop)
        self.assertIn('blocking consult', str(e.exception))

    def test_non_blocking_consult_does_not(self):
        OR.raise_consult(self.loop, self.notice(blocking=False))
        brief = OR.build_revision_brief(self.rep, self.p, self.r, 0)
        d, _, _ = OR.commit_decision(base_decision(revision_brief=brief), self.rep, self.p, self.r, self.loop)
        self.assertEqual(d['type'], 'revise_protocol')

    def test_consults_always_forces_the_question_to_be_addressed(self):
        # the mode has to be set before loop-init: changing the request afterwards is refused
        r = load('request.cart_d10.json')
        r['human_in_the_loop'] = {'mode': 'checkpoints', 'consults': 'always'}
        loop = self.tmp / 'always'
        OR.loop_init(r, loop)
        p = approved(0, r)
        rep = AN.analyze(run_for(p), p, r)
        bio = BT.annotate(['EXH1'], 'knockout', 'CAR-T cell')
        brief = OR.build_revision_brief(rep, p, r, 0)
        with self.assertRaises(K.ContractError) as e:
            OR.commit_decision(base_decision(revision_brief=brief), rep, p, r, loop, [bio])
        self.assertIn('consults always', str(e.exception))
        d, _, _ = OR.commit_decision(base_decision(
            revision_brief=brief,
            considered=[{'type': 'consult_human', 'why_not': 'nobody in-house has run this assay'}]),
            rep, p, r, loop, [bio])
        self.assertEqual(d['type'], 'revise_protocol')


class BioinformaticsTests(unittest.TestCase):
    def test_known_gene_reports_effects_with_confidence(self):
        r = BT.annotate(['EXH1'], 'knockout', 'CAR-T cell')
        g = r['genes'][0]
        self.assertTrue(g['found'])
        self.assertTrue(all(e['confidence'] == 'synthetic_fixture' for e in g['effects']))
        self.assertTrue(any('SYNTHETIC placeholder' in x for x in r['limitations']))

    def test_unknown_gene_returns_a_lookup_plan_not_a_guess(self):
        r = BT.annotate(['NOSUCHGENE'], 'knockout')
        g = r['genes'][0]
        self.assertFalse(g['found'])
        self.assertEqual(g['effects'], [])
        self.assertEqual([p['source'] for p in g['live_lookup_plan']],
                         ['Ensembl', 'UniProt', 'Open Targets', 'STRING'])
        self.assertTrue(any('do not assume absence of effect' in x for x in r['limitations']))

    def test_other_perturbation_yields_no_effects(self):
        r = BT.annotate(['EXH1'], 'overexpression')
        self.assertEqual(r['genes'][0]['effects'], [])
        self.assertTrue(any('records no' in x for x in r['limitations']))

    def test_levers_and_untestable_assays_are_reported(self):
        r = BT.annotate(['EXH1'], 'knockout')
        self.assertEqual({l['parameter'] for l in r['lever_suggestions']}, {'activation duration', 'IL-15 dose'})
        self.assertTrue(r['untestable_here'])
        self.assertEqual(BT.consult_candidates(r), r['untestable_here'])

    def test_cross_check_sees_whether_the_arm_already_differs(self):
        r = BT.annotate(['EXH1'], 'knockout')
        before = BT.cross_check(r, load('protocol.it1.synthetic.json'))
        after = BT.cross_check(r, load('protocol.it2.synthetic.json'))
        self.assertEqual(before['control_arm_id'], 'WT')
        self.assertFalse(any(d['lever_already_addressed'] for lv in before['levers'] for d in lv['per_arm']))
        self.assertTrue(all(d['lever_already_addressed'] for lv in after['levers'] for d in lv['per_arm']))

    def test_bad_symbol_and_unknown_set_refused(self):
        with self.assertRaises(K.ContractError):
            BT.annotate(['not a symbol!'])
        with self.assertRaises(K.ContractError):
            BT.annotate(['EXH1'], knowledge_sets=['nope'])

    def test_malformed_knowledge_entry_is_refused_loudly(self):
        entry = {'symbol': 'X', 'perturbations': [{'perturbation': 'knockout', 'affected_process': 'p',
                                                   'direction': 'sideways', 'readout_metric': 'viability_pct',
                                                   'confidence': 'local_annotation', 'source': 's'}]}
        with self.assertRaises(K.ContractError):
            KB.effects_for(entry, {'knowledge_set_id': 'test'})

    def test_report_renders(self):
        md = BT.render_report(BT.annotate(['EXH1', 'NOSUCHGENE'], 'knockout'))
        self.assertIn('EXH1', md)
        self.assertIn('not found', md)


class TCellStandinTests(unittest.TestCase):
    def test_maps_protocol_values_onto_the_model(self):
        inp, notes = tcell.protocol_to_inputs(load('protocol.it1.synthetic.json'), 'WT')
        self.assertEqual(inp['activation_h'], 48.0)
        self.assertEqual(inp['seed_per_ml'], 1000000.0)
        self.assertEqual(inp['harvest_day'], 10)
        self.assertEqual(tcell._dose_at(inp['cytokines']['il7'], 5), 10.0)

    def test_arm_adjustment_shortens_activation_for_the_knockout_only(self):
        p = load('protocol.it2.synthetic.json')
        self.assertEqual(tcell.protocol_to_inputs(p, 'WT')[0]['activation_h'], 48.0)
        ko = tcell.protocol_to_inputs(p, 'EXH1_KO')[0]
        self.assertEqual(ko['activation_h'], 24.0)
        self.assertEqual(tcell._dose_at(ko['cytokines']['il15'], 5), 15.0)

    def test_output_is_valid_labelled_and_deterministic(self):
        p = load('protocol.it1.synthetic.json')
        a, b = run_for(p), run_for(p)
        self.assertEqual(K.schema_errors('bioreactor_run', a), [])
        self.assertEqual(a['source'], 'synthetic_standin')
        self.assertIn('SYNTHETIC', a['label'])
        self.assertEqual(a['arms'][0]['harvest'], b['arms'][0]['harvest'])

    def test_hidden_truth_changes_only_the_engineered_arm(self):
        p = load('protocol.it0.synthetic.json')
        with_truth = run_for(p)
        without = tcell.simulate_protocol(p, PR.protocol_sha256(p), truth=None)
        by = lambda r, a: next(x for x in r['arms'] if x['arm_id'] == a)['harvest']['viable_cells_total']
        self.assertEqual(by(with_truth, 'WT'), by(without, 'WT'))
        self.assertLess(by(with_truth, 'EXH1_KO'), by(without, 'EXH1_KO'))

    def test_max_mode_and_wrong_stage_count_refused(self):
        p = load('protocol.it1.synthetic.json')
        with self.assertRaises(ValueError):
            tcell.simulate_protocol(p, 'x', mode='max')
        broken = copy.deepcopy(p)
        broken['stages'] = broken['stages'][:2]
        with self.assertRaises(ValueError):
            tcell.protocol_to_inputs(broken, 'WT')

    def test_registry_resolves_both_standins(self):
        self.assertIs(get_standin('tcell'), tcell)
        self.assertTrue(hasattr(get_standin('monocyte'), 'simulate_protocol'))
        with self.assertRaises(ValueError):
            get_standin('nope')


class CartFixtureTests(unittest.TestCase):
    def setUp(self):
        self.r = load('request.cart_d10.json')

    def test_request_is_valid_and_declares_its_autonomy(self):
        K.require_valid('production_request', self.r)
        self.assertEqual(self.r['human_in_the_loop']['mode'], 'checkpoints')
        self.assertEqual(self.r['bioreactor_source'], 'synthetic_standin')

    def test_all_three_protocols_validate_against_their_handoffs(self):
        for it in (0, 1, 2):
            v = PR.validate_protocol(load(f'protocol.it{it}.synthetic.json'),
                                     [load(f'handoff.it{it}.synthetic.json')], self.r)
            self.assertEqual(v['status'], 'needs_approval', (it, v['errors'][:4]))

    def test_evidence_level_rises_once_the_knockout_claims_exist(self):
        v0 = PR.validate_protocol(load('protocol.it0.synthetic.json'), [load('handoff.it0.synthetic.json')], self.r)
        v2 = PR.validate_protocol(load('protocol.it2.synthetic.json'), [load('handoff.it2.synthetic.json')], self.r)
        self.assertEqual([e['effect_id'] for e in v0['hypothesis_effects']], ['E1'])
        self.assertEqual(v2['hypothesis_effects'], [])

    def test_qc_profile_marks_pluripotency_not_applicable(self):
        from biosense.production import qc as QC
        rows = {c['id']: c for c in QC.load_profile('cart_research')['criteria']}
        self.assertEqual(rows['qc-residual-pluripotency']['modes'], [])
        self.assertEqual(rows['qc-identity-purity']['value'], 20.0)

    def test_the_arc_holds(self):
        """it0 both short, it1 control only, it2 both met."""
        want = [(False, False), (True, False), (True, True)]
        for it, (wt_met, ko_met) in zip((0, 1, 2), want):
            p = approved(it, self.r)
            rep = AN.analyze(run_for(p), p, self.r)
            got = {a['arm_id']: a['target']['status'] == 'MET' for a in rep['arms']}
            self.assertEqual((got['WT'], got['EXH1_KO']), (wt_met, ko_met), f'iteration {it}')

    def test_release_tests_turn_the_last_run_into_success(self):
        p = approved(2, self.r)
        self.assertEqual(AN.analyze(run_for(p), p, self.r)['verdict']['status'], 'TARGET_MET_QC_INCOMPLETE')
        self.assertEqual(AN.analyze(run_for(p, release_tests=True), p, self.r)['verdict']['status'], 'SUCCESS')


class DemoCartTests(unittest.TestCase):
    def test_offline_agent_loop_reaches_success_within_budget(self):
        from biosense.production.demo_cart import run_demo
        tmp = Path(tempfile.mkdtemp())
        try:
            s = run_demo(tmp / 'loop')
            types = [r['type'] for r in s['ledger']]
            self.assertEqual(types, ['request_bioinformatics', 'consult_human', 'revise_protocol',
                                     'revise_protocol', 'complete_qc', 'protocol_succeeded'])
            self.assertEqual(s['iterations_used'], 2)
            self.assertLessEqual(s['iterations_used'], s['max_iterations'])
            self.assertEqual(s['consults'][0]['status'], 'answered')
            self.assertTrue(all(r['status'] == 'committed' for r in s['ledger']))
        finally:
            shutil.rmtree(tmp)


if __name__ == '__main__':
    unittest.main()
