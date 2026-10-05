"""Simulator mode: the sandbox a person drives by hand.

The interesting tests here are not "does it produce numbers" but the three
properties that make a sandbox safe to put on a public URL: the hidden state
stays hidden, a sandbox reading cannot pass itself off as evidence, and the
condition read is arithmetic a reader can check rather than a model's opinion.
"""
import json
import unittest

from biosense import contracts as K
from biosense.production import sim_mode as SM


class ConditionParsingTests(unittest.TestCase):
    def test_an_empty_payload_runs_every_knob_at_its_default(self):
        sp, days, meta = SM.parse_condition({})
        for knob in SM.KNOBS:
            self.assertEqual(knob['default'], getattr(sp, knob['id']), knob['id'])
        self.assertEqual({s['id']: s['default_days'] for s in SM.STAGES}, days)
        self.assertEqual([], meta['clamped'])

    def test_every_knob_is_a_real_field_of_the_shared_simulator(self):
        """The interface is generated from KNOBS, so a knob the model does not
        have would be a slider that silently does nothing."""
        from analysis_agent.simulator import Setpoints
        fields = set(vars(Setpoints()))
        for knob in SM.KNOBS:
            self.assertIn(knob['id'], fields)
            self.assertGreater(knob['max'], knob['min'])
            self.assertLessEqual(knob['default'], knob['max'])
            self.assertGreaterEqual(knob['default'], knob['min'])

    def test_an_out_of_range_value_is_clamped_and_the_clamp_is_reported(self):
        sp, _, meta = SM.parse_condition({'setpoints': {'agitation_rpm': 9000}})
        self.assertEqual(SM.KNOB_BY_ID['agitation_rpm']['max'], sp.agitation_rpm)
        self.assertEqual(['agitation_rpm'], [c['knob'] for c in meta['clamped']])
        self.assertEqual(9000.0, meta['clamped'][0]['sent'])

    def test_a_knob_the_sandbox_does_not_expose_is_refused_by_name(self):
        with self.assertRaises(K.ContractError) as e:
            SM.parse_condition({'setpoints': {'passage_diameter_um': 250}})
        self.assertIn('passage_diameter_um', str(e.exception))

    def test_junk_and_infinities_are_refused_rather_than_clamped(self):
        for bad in ('warm', None, float('inf'), float('nan')):
            with self.assertRaises(K.ContractError):
                SM.parse_condition({'setpoints': {'bmp4': bad}})

    def test_the_total_run_length_is_capped(self):
        long = {s['id']: s['max'] for s in SM.STAGES}
        with self.assertRaises(K.ContractError) as e:
            SM.parse_condition({'stage_days': long})
        self.assertIn('cap', str(e.exception))

    def test_an_unknown_challenge_is_refused(self):
        with self.assertRaises(K.ContractError):
            SM.parse_condition({'challenge': 'make_it_work'})


class HiddenStateTests(unittest.TestCase):
    """The loop's agents infer the line from its instruments. So does a person
    in simulator mode: the sandbox is not a back door to the answer."""

    def test_no_payload_carries_the_lines_hidden_truth(self):
        blob = json.dumps(SM.simulate({'challenge': 'variant'}))
        for leaked in ('ground_truth', 'mu_max', 'death_base', 'variant_fraction',
                       'factor_optima', 'genotype_mu_ratio', 'opt_shift'):
            self.assertNotIn(leaked, blob, leaked)

    def test_a_frame_carries_observation_channels_only(self):
        frame = SM.simulate({})['frames'][0]
        allowed = set(SM.FRAME_CHANNELS) | {'day', 'stage', 'read', 'cd14_pct',
                                            'tra_1_60_pct'}
        self.assertEqual(set(), set(frame) - allowed)

    def test_the_comparison_payload_leaks_nothing_either(self):
        blob = json.dumps(SM.compare({'conditions': [{}, {'challenge': 'variant'}]}))
        self.assertNotIn('ground_truth', blob)
        self.assertNotIn('death_base', blob)


class ConditionReadTests(unittest.TestCase):
    def test_a_clean_frame_reads_good_with_no_flags(self):
        r = SM.read_condition(dict(viability_pct=95.0, lactate_mM=8.0, ammonia_mM=1.0,
                                   glucose_mM=14.0, agg_frac_over_300um=0.05,
                                   do_measured=0.19))
        self.assertEqual('good', r['state'])
        self.assertEqual(100.0, r['score'])
        self.assertEqual([], r['flags'])

    def test_each_flag_names_the_channel_and_the_number_that_raised_it(self):
        r = SM.read_condition(dict(viability_pct=60.0, lactate_mM=34.0, ammonia_mM=6.0,
                                   glucose_mM=0.1, agg_frac_over_300um=0.8,
                                   do_measured=0.02))
        self.assertEqual('failing', r['state'])
        channels = {f['channel'] for f in r['flags']}
        self.assertEqual({'viability_pct', 'lactate_mM', 'ammonia_mM', 'glucose_mM',
                          'agg_frac_over_300um', 'do_measured'}, channels)
        for f in r['flags']:
            self.assertIn(f['severity'], ('warn', 'bad'))
            self.assertTrue(any(ch.isdigit() for ch in f['detail']), f['detail'])

    def test_a_collapsed_culture_reads_failing_even_when_the_medium_looks_calm(self):
        """The trap this test exists for: a dead vessel consumes nothing, so
        glucose, lactate and ammonia all read comfortable. Density is what
        catches it, and a breach that large has to carry the read on its own."""
        r = SM.read_condition(dict(viability_pct=5.0, vcd_e6_per_ml=0.0,
                                   lactate_mM=0.1, ammonia_mM=0.01, glucose_mM=19.0,
                                   agg_frac_over_300um=0.0, do_measured=0.19))
        self.assertEqual('failing', r['state'])
        self.assertIn('vcd_e6_per_ml', {f['channel'] for f in r['flags']})

    def test_a_breach_records_how_far_past_the_threshold_it_sits(self):
        mild = SM.read_condition(dict(viability_pct=74.0, lactate_mM=8.0, ammonia_mM=1.0,
                                      glucose_mM=14.0, agg_frac_over_300um=0.05,
                                      do_measured=0.19))
        severe = SM.read_condition(dict(viability_pct=20.0, lactate_mM=8.0, ammonia_mM=1.0,
                                        glucose_mM=14.0, agg_frac_over_300um=0.05,
                                        do_measured=0.19))
        self.assertLess(mild['flags'][0]['excess'], severe['flags'][0]['excess'])
        self.assertLess(severe['score'], mild['score'])

    def test_the_score_is_bounded_and_the_state_follows_it(self):
        awful = SM.read_condition(dict(viability_pct=1.0, lactate_mM=99.0, ammonia_mM=99.0,
                                       glucose_mM=0.0, agg_frac_over_300um=1.0,
                                       do_measured=0.0))
        self.assertEqual(0.0, awful['score'])
        self.assertEqual('failing', awful['state'])

    def test_a_missing_channel_does_not_raise_a_flag_about_it(self):
        """A day with no cytometer panel must not read as a dying culture."""
        r = SM.read_condition(dict(viability_pct=95.0, lactate_mM=8.0, ammonia_mM=1.0,
                                   glucose_mM=14.0, agg_frac_over_300um=0.05,
                                   do_measured=0.19, imp_opacity=None))
        self.assertEqual([], r['flags'])


class SimulateTests(unittest.TestCase):
    def test_a_default_run_returns_one_frame_per_day_with_a_read_on_each(self):
        r = SM.simulate({})
        self.assertEqual('synthetic_demonstration', r['evidence_status'])
        self.assertEqual('synthetic_standin', r['bioreactor_source'])
        self.assertTrue(r['frames'])
        days = [f['day'] for f in r['frames']]
        self.assertEqual(days, sorted(days))
        for f in r['frames']:
            self.assertIn(f['read']['state'], ('good', 'strained', 'failing'))
        self.assertEqual({'expansion', 'mesoderm', 'hemato', 'myeloid'},
                         {f['stage'] for f in r['frames']})

    def test_the_same_condition_twice_gives_the_same_answer(self):
        """Two conditions must differ by what the person changed and nothing
        else, so the line is fixed rather than sampled."""
        a, b = SM.simulate({'seed': 11}), SM.simulate({'seed': 11})
        self.assertEqual(a['frames'], b['frames'])
        self.assertEqual(a['outcome'], b['outcome'])

    def test_shear_that_breaks_the_culture_shows_up_in_the_outcome(self):
        """Not a biological claim -- a check that the knob reaches the model at
        all, which is the one thing a sandbox must get right."""
        calm = SM.simulate({'setpoints': {'agitation_rpm': 60}})
        violent = SM.simulate({'setpoints': {'agitation_rpm': 140}})
        self.assertLess(violent['outcome']['final_viability_pct'],
                        calm['outcome']['final_viability_pct'])
        self.assertLess(violent['signature']['harvest_per_input_ipsc'],
                        calm['signature']['harvest_per_input_ipsc'])

    def test_a_stage_specific_cytokine_does_not_move_a_stage_it_does_not_touch(self):
        base = SM.simulate({})
        off = SM.simulate({'setpoints': {'mcsf': 0.0}})
        expansion = [f for f in base['frames'] if f['stage'] == 'expansion']
        expansion_off = [f for f in off['frames'] if f['stage'] == 'expansion']
        self.assertEqual([f['vcd_e6_per_ml'] for f in expansion],
                         [f['vcd_e6_per_ml'] for f in expansion_off])
        self.assertLess(off['signature']['harvest_per_input_ipsc'],
                        base['signature']['harvest_per_input_ipsc'])

    def test_the_signature_counts_the_days_its_own_frames_flagged(self):
        r = SM.simulate({'setpoints': {'agitation_rpm': 130, 'feed_interval_h': 72}})
        sig, frames = r['signature'], r['frames']
        self.assertEqual(sum(1 for f in frames if f['read']['state'] == 'failing'),
                         sig['days_failing'])
        self.assertEqual(min(f['read']['score'] for f in frames), sig['worst_score'])


class CompareTests(unittest.TestCase):
    def test_a_comparison_names_the_setpoints_that_differ(self):
        c = SM.compare({'conditions': [{}, {'setpoints': {'mcsf': 90, 'il3': 40}}]})
        self.assertEqual({'mcsf', 'il3'}, {x['knob'] for x in c['changed']})
        self.assertIn('harvest_per_input_ipsc', c['deltas'])

    def test_identical_conditions_are_called_identical_rather_than_scored(self):
        c = SM.compare({'conditions': [{}, {}]})
        self.assertEqual([], c['changed'])
        self.assertIn('identical', c['verdict'])

    def test_the_verdict_says_the_comparison_is_directional_only(self):
        c = SM.compare({'conditions': [{}, {'setpoints': {'mcsf': 90}}]})
        self.assertIn('One replicate', c['verdict'])

    def test_a_percentage_against_a_collapsed_baseline_is_withheld(self):
        """5% viability to 75% is one dead culture and one living one, not a
        1400% improvement. The ratio is arithmetic without meaning, so the
        comparison declines to print it and says why."""
        dead = {'setpoints': {'agitation_rpm': 140, 'seed_density': 2.0}}
        c = SM.compare({'conditions': [dead, {}]})
        d = c['deltas']['harvest_per_input_ipsc']
        self.assertIsNone(d['pct'])
        self.assertIn('too small', d['pct_withheld'])
        live = SM.compare({'conditions': [{}, {'setpoints': {'mcsf': 60}}]})
        self.assertIsNotNone(live['deltas']['harvest_per_input_ipsc']['pct'])

    def test_a_comparison_needs_exactly_two_conditions(self):
        for bad in ({}, {'conditions': []}, {'conditions': [{}]},
                    {'conditions': [{}, {}, {}]}):
            with self.assertRaises(K.ContractError):
                SM.compare(bad)


class SeedBriefTests(unittest.TestCase):
    """Carrying a sandbox condition into a loop run must carry its provenance
    with it. A hand-turned knob against a toy model is a design choice, and the
    production loop blocks the wet lab on exactly that class."""

    def test_every_quantity_leaves_as_a_design_choice(self):
        b = SM.seed_brief(SM.simulate({}))
        self.assertEqual('simulator_sandbox', b['origin'])
        self.assertTrue(b['quantities'])
        for q in b['quantities']:
            self.assertEqual('design_choice', q['provenance'])
            self.assertIn('synthetic', q['basis'])
        self.assertIn('design_choice', b['caveat'])

    def test_the_brief_covers_every_knob_the_sandbox_exposes(self):
        b = SM.seed_brief(SM.simulate({}))
        self.assertEqual(set(SM.KNOB_BY_ID), {q['knob'] for q in b['quantities']})

    def test_the_prompt_it_hands_the_console_parses_into_a_valid_request(self):
        from biosense.production import prompt as PR
        b = SM.seed_brief(SM.simulate({}))
        req, prov = PR.parse(b['prompt'])
        self.assertEqual([], K.schema_errors('production_request', req))
        self.assertEqual('synthetic_standin', req['bioreactor_source'])

    def test_the_brief_never_claims_a_literature_basis(self):
        blob = json.dumps(SM.seed_brief(SM.simulate({}))).lower()
        for word in ('reported', 'adapted', 'citation', 'doi'):
            self.assertNotIn(word, blob, word)


class ConfigTests(unittest.TestCase):
    def test_the_config_describes_the_knobs_the_server_will_accept(self):
        c = SM.config()
        self.assertEqual({k['id'] for k in SM.KNOBS}, {k['id'] for k in c['knobs']})
        for k in c['knobs']:
            self.assertTrue(k['note'])
        self.assertEqual('synthetic_demonstration', c['evidence_status'])

    def test_the_config_states_what_is_refused_and_what_is_not_modelled(self):
        c = SM.config()
        self.assertTrue(c['refuses'] and c['not_modelled'])
        joined = ' '.join(c['not_modelled']).lower()
        self.assertIn('t-lineage', joined)

    def test_the_thresholds_the_read_uses_are_published(self):
        """A score nobody can check is not a measurement, so the limits the
        read applies ship with the interface that draws it."""
        self.assertEqual(set(SM.LIMITS), set(SM.config()['limits']))


if __name__ == '__main__':
    unittest.main()
