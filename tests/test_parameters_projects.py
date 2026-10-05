"""Canonical parameter identity, and the knobs a particular project actually has.

Phase 1 ended with three names for the same quantity and no way to tell that
`mcsf` and `mcsf_ng_ml` were one parameter. These tests pin the fix: one
canonical id, resolution that refuses to guess, and projects that narrow a
shared vocabulary instead of inventing private ones.
"""
import unittest

from biosense import contracts as K
from biosense import parameters as PR
from biosense import projects as PJ

MAC = 'ipsc_macrophage'
CART = 'cart_expansion'


class RegistryTests(unittest.TestCase):
    def test_every_spelling_of_a_parameter_resolves_to_one_id(self):
        for name in ('mcsf', 'M-CSF', 'mcsf_ng_ml', 'm-csf', 'CSF1',
                     'macrophage colony stimulating factor'):
            self.assertEqual('mcsf_ng_ml', PR.resolve(name), name)

    def test_the_three_phase_one_vocabularies_now_meet(self):
        """The actual bug: a simulator knob, a candidate parameter and an
        analysis-report lever naming one quantity three ways."""
        self.assertEqual(PR.resolve('mcsf'), PR.resolve('mcsf_ng_ml'))
        self.assertEqual(PR.resolve('seeding density'), PR.resolve('seed_density'))
        self.assertEqual(PR.resolve('IL-7 dose/window'), PR.resolve('il7_ng_ml'))
        self.assertEqual(PR.resolve('expansion duration'), PR.resolve('stage_duration_days'))

    def test_an_unknown_name_is_refused_with_the_nearest_candidates(self):
        with self.assertRaises(K.ContractError) as e:
            PR.resolve('mcsff')
        msg = str(e.exception)
        self.assertIn('mcsf_ng_ml', msg)
        self.assertIn('Guessing', msg)

    def test_resolution_can_be_asked_without_raising(self):
        self.assertIsNone(PR.resolve('not_a_parameter', required=False))

    def test_no_alias_is_claimed_by_two_different_parameters(self):
        """Import-time guard; this test states the invariant it protects. A
        parameter repeating its own label as an alias is harmless; two different
        parameters claiming one spelling is the failure that matters."""
        seen = {}
        for p in PR.PARAMETERS:
            for a in (p.parameter_id, p.label, *p.aliases):
                k = a.lower()
                self.assertEqual(seen.setdefault(k, p.parameter_id), p.parameter_id,
                                 f'{a!r} claimed by {seen[k]} and {p.parameter_id}')

    def test_every_parameter_carries_a_unit_and_a_meaning(self):
        for p in PR.PARAMETERS:
            self.assertTrue(p.unit, p.parameter_id)
            self.assertGreater(len(p.meaning), 20, p.parameter_id)
            self.assertIn(p.value_type, PR.VALUE_TYPES)

    def test_global_bounds_are_ordered_and_clamp(self):
        p = PR.get('do_setpoint')
        self.assertLess(p.global_min, p.global_max)
        v, clamped = p.clamp(9.0)
        self.assertEqual(p.global_max, v)
        self.assertTrue(clamped)
        v, clamped = p.clamp(0.2)
        self.assertFalse(clamped)

    def test_stage_applicability_is_declared(self):
        self.assertTrue(PR.applies_to_stage('agitation_rpm', 'myeloid'))     # 'all'
        self.assertTrue(PR.applies_to_stage('bmp4_ng_ml', 'mesoderm'))
        self.assertFalse(PR.applies_to_stage('bmp4_ng_ml', 'expansion'))


class ProfileLoadingTests(unittest.TestCase):
    def test_the_shipped_profiles_validate(self):
        self.assertTrue(PJ.available())
        for pid in PJ.available():
            p = PJ.load(pid)
            self.assertEqual([], K.schema_errors('project_profile', p.doc))

    def test_every_project_parameter_is_a_canonical_id(self):
        for pid in PJ.available():
            p = PJ.load(pid)
            for q in p.parameter_ids:
                self.assertEqual(q, PR.resolve(q))

    def test_an_unknown_project_is_refused_and_lists_what_exists(self):
        with self.assertRaises(K.ContractError) as e:
            PJ.load('does_not_exist')
        self.assertIn(MAC, str(e.exception))


class ProjectSpecificKnobTests(unittest.TestCase):
    """The requirement: a project does not inherit a knob because the registry
    defines it."""

    def setUp(self):
        self.mac = PJ.load(MAC)
        self.cart = PJ.load(CART)

    def test_cart_does_not_receive_macrophage_knobs(self):
        for macrophage_only in ('mcsf_ng_ml', 'il3_ng_ml', 'bmp4_ng_ml', 'vegf_ng_ml',
                                'rocki_hours', 'do_setpoint'):
            self.assertIn(macrophage_only, self.mac.parameter_ids)
            self.assertNotIn(macrophage_only, self.cart.parameter_ids,
                             f'{macrophage_only} leaked into the CAR-T project')

    def test_macrophage_does_not_receive_t_cell_knobs(self):
        for t_only in ('il7_ng_ml', 'il15_ng_ml', 'il2_ng_ml', 'activation_duration_h',
                       'transduction_moi'):
            self.assertIn(t_only, self.cart.parameter_ids)
            self.assertNotIn(t_only, self.mac.parameter_ids)

    def test_asking_a_project_for_a_knob_it_does_not_have_is_refused_by_name(self):
        with self.assertRaises(K.ContractError) as e:
            self.cart.parameter('mcsf_ng_ml')
        msg = str(e.exception)
        self.assertIn('mcsf_ng_ml', msg)
        self.assertIn('not available here merely', msg)

    def test_the_same_parameter_has_different_ranges_in_different_projects(self):
        a = self.mac.parameter('agitation_rpm')
        b = self.cart.parameter('agitation_rpm')
        self.assertEqual(a.parameter_id, b.parameter_id)
        self.assertNotEqual((a.minimum, a.maximum), (b.minimum, b.maximum))
        self.assertGreater(a.minimum, b.maximum)   # stirred tank vs rocking platform

    def test_readouts_are_project_specific(self):
        mac_r = {r['readout_id'] for r in self.mac.readouts}
        cart_r = {r['readout_id'] for r in self.cart.readouts}
        self.assertIn('agg_diameter_mean_um', mac_r)
        self.assertNotIn('agg_diameter_mean_um', cart_r)
        self.assertIn('car_positive_pct', cart_r)
        self.assertNotIn('car_positive_pct', mac_r)

    def test_units_and_labels_come_from_the_canonical_registry(self):
        p = self.mac.parameter('mcsf_ng_ml')
        self.assertEqual('ng/mL', p.unit)
        self.assertEqual('M-CSF', p.label)


class BoundOriginTests(unittest.TestCase):
    def test_every_narrowed_bound_records_where_it_came_from(self):
        for pid in PJ.available():
            p = PJ.load(pid)
            for q in p._params.values():
                c = q.canonical
                narrowed = ((q.minimum is not None and c.global_min is not None
                             and q.minimum > c.global_min)
                            or (q.maximum is not None and c.global_max is not None
                                and q.maximum < c.global_max))
                if narrowed:
                    self.assertIsNotNone(q.bound_origin, f'{pid}/{q.parameter_id}')
                    self.assertIn(q.bound_origin['source'], PJ.BOUND_SOURCES)
                    self.assertTrue(q.bound_origin['basis'])

    def test_a_narrowed_bound_without_an_origin_is_refused(self):
        doc = PJ.load(MAC).doc
        bad = dict(doc, project_id='bad_bounds',
                   parameters=[dict(p) for p in doc['parameters']])
        for p in bad['parameters']:
            if p['parameter_id'] == 'agitation_rpm':
                p.pop('bound_origin', None)
        with self.assertRaises(K.ContractError) as e:
            PJ.Project(bad)
        self.assertIn('bound_origin', str(e.exception))
        self.assertIn('guess wearing the clothes of a constraint', str(e.exception))

    def test_a_project_cannot_widen_a_canonical_bound(self):
        doc = PJ.load(MAC).doc
        bad = dict(doc, project_id='too_wide',
                   parameters=[dict(p) for p in doc['parameters']])
        for p in bad['parameters']:
            if p['parameter_id'] == 'do_setpoint':
                p['maximum'] = 5.0
        with self.assertRaises(K.ContractError) as e:
            PJ.Project(bad)
        self.assertIn('narrows a global bound', str(e.exception))


class SimulatorCoverageTests(unittest.TestCase):
    """Coverage is declared, never inferred, and never fabricated."""

    def setUp(self):
        self.mac = PJ.load(MAC)
        self.cart = PJ.load(CART)

    def test_the_macrophage_project_declares_its_mappings(self):
        self.assertEqual('current', self.mac.simulator['status'])
        self.assertEqual('mcsf', self.mac.parameter('mcsf_ng_ml').simulator_mapping)
        self.assertTrue(self.mac.parameter('mcsf_ng_ml').modelled)

    def test_a_real_parameter_the_model_cannot_predict_is_marked_not_modelled(self):
        t = self.mac.parameter('temperature_c')
        self.assertEqual('not_modelled', t.simulator_coverage)
        self.assertIsNone(t.simulator_mapping)
        self.assertIn('temperature_c', self.mac.summary()['not_modelled'])

    def test_a_project_with_no_model_reports_no_simulator_everywhere(self):
        self.assertEqual('none', self.cart.simulator['status'])
        self.assertIsNone(self.cart.simulator['model_id'])
        self.assertEqual([], self.cart.modelled_ids())
        for q in self.cart.parameter_ids:
            self.assertEqual('no_simulator', self.cart.coverage(q))

    def test_il7_is_a_real_parameter_that_the_macrophage_model_does_not_cover(self):
        """The review's worked example: IL-7 exists canonically, is a usable CAR-T
        knob, and must never acquire a macrophage-model prediction."""
        self.assertEqual('il7_ng_ml', PR.resolve('IL-7'))
        self.assertEqual('no_simulator', self.cart.coverage('il7_ng_ml'))
        self.assertEqual('not_in_project', self.mac.coverage('il7_ng_ml'))

    def test_claiming_modelled_without_a_mapping_is_refused(self):
        doc = PJ.load(MAC).doc
        bad = dict(doc, project_id='no_mapping',
                   parameters=[dict(p) for p in doc['parameters']])
        for p in bad['parameters']:
            if p['parameter_id'] == 'temperature_c':
                p['simulator_coverage'] = 'modelled'
        with self.assertRaises(K.ContractError) as e:
            PJ.Project(bad)
        self.assertIn('which term it is', str(e.exception))

    def test_a_project_without_a_simulator_cannot_declare_a_modelled_parameter(self):
        doc = PJ.load(CART).doc
        bad = dict(doc, project_id='borrowed_model',
                   parameters=[dict(p) for p in doc['parameters']])
        bad['parameters'][0]['simulator_coverage'] = 'modelled'
        bad['parameters'][0]['simulator_mapping'] = 'mcsf'
        with self.assertRaises(K.ContractError) as e:
            PJ.Project(bad)
        msg = str(e.exception)
        self.assertIn('modelled', msg)
        self.assertIn("'none'", msg)

    def test_a_template_is_not_marked_current_to_advertise_an_intention(self):
        for pid in PJ.available():
            p = PJ.load(pid)
            if p.simulator['status'] == 'current':
                self.assertTrue(p.simulator['model_id'])
                self.assertTrue(p.modelled_ids())


if __name__ == '__main__':
    unittest.main()
