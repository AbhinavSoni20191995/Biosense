"""Plate, tube, tank: the vessel a protocol is actually run in.

Nobody starts in a bioreactor. The work starts in a 6-well plate, moves to a
rotating tube when suspension aggregates are the point, and reaches a stirred
tank when the number of cells is. A protocol that only exists in stirred-tank
units is one most people cannot start.

What these tests hold is the honesty of the translation, because the tempting
version of this feature is the wrong one: a conversion table that turns 60 rpm
in a tank into some number of rpm in a tube. There is no such conversion. A
concentration carries, a time carries, a fraction carries; a mixing rate and a
dissolved-oxygen setpoint do not, in either direction, and the vessel that
does not have the equipment says so instead of being given a number.
"""
import unittest

from biosense import contracts as K
from biosense import formats as F

PROTOCOL = {
    'culture_format': 'stirred_tank',
    'first_pass': {
        'stages': [{'stage_id': 'myeloid', 'label': 'Myeloid harvest', 'days': 14,
                    'day_start': 4, 'inducers': [
                        {'parameter_id': 'mcsf_ng_ml', 'label': 'M-CSF', 'unit': 'ng/mL',
                         'value': 100, 'provenance': 'reported'},
                        {'parameter_id': 'rocki_hours', 'label': 'ROCK inhibitor',
                         'unit': 'h', 'value': 24, 'provenance': 'reported'},
                        {'parameter_id': 'gmcsf', 'label': 'GM-CSF', 'unit': 'ng/mL',
                         'value': None, 'provenance': 'gap'},
                    ]}],
        'physical': [
            {'parameter_id': 'agitation_rpm', 'label': 'Agitation', 'unit': 'rpm',
             'value': 60, 'provenance': 'design_choice', 'stage_id': 'all'},
            {'parameter_id': 'do_setpoint', 'label': 'Dissolved oxygen', 'unit': 'fraction',
             'value': 0.2, 'provenance': 'reported', 'stage_id': 'all'},
            {'parameter_id': 'temperature_c', 'label': 'Temperature', 'unit': 'degC',
             'value': 37, 'provenance': 'reported', 'stage_id': 'all'},
            {'parameter_id': 'seed_density', 'label': 'Seed density', 'unit': '1e6 cells/mL',
             'value': 0.5, 'provenance': 'design_choice', 'stage_id': 'expansion'},
        ],
    },
}


def rows(t):
    return {r['parameter_id']: r for st in t['stages'] for r in st['items']} | {
        r['parameter_id']: r for r in t['physical']}


class RegistryTests(unittest.TestCase):
    def test_the_three_formats_are_the_path_a_process_actually_takes(self):
        self.assertEqual(('six_well', 'cero', 'stirred_tank'), F.ORDER)
        for fid in F.ORDER:
            f = F.get(fid)
            self.assertTrue(f.good_for and f.not_for, f'{fid} says what it is and is not for')
            self.assertTrue(f.simulator_note)

    def test_every_number_about_a_vessel_says_where_it_came_from(self):
        """A working volume taken from a catalogue and one somebody chose are
        different kinds of fact, and a protocol built on the second should
        say so."""
        for fid in F.ORDER:
            f = F.get(fid)
            self.assertIn(f.working_volume.basis, F.BASIS, fid)
            if f.growth_area:
                self.assertIn(f.growth_area.basis, F.BASIS, fid)

    def test_a_volume_nobody_can_know_is_not_invented(self):
        """A CERO tube is nominally 50 mL and is never run full. How full is
        the person's decision, so there is no default pretending otherwise."""
        cero = F.get('cero')
        self.assertIsNone(cero.working_volume.value)
        self.assertEqual('design_choice', cero.working_volume.basis)
        self.assertEqual((10.0, 30.0), (cero.working_volume.lower, cero.working_volume.upper))
        self.assertTrue(any('working volume' in d['what'] for d in cero.decide))

    def test_an_unknown_vessel_is_refused_by_name(self):
        with self.assertRaisesRegex(K.ContractError, 'six_well, cero, stirred_tank'):
            F.get('t175_flask')
        self.assertIsNone(F.get('t175_flask', required=False))

    def test_every_rule_names_a_kind_the_translation_knows(self):
        for table in F.BY_FAMILY.values():
            for pid, (kind, why) in table.items():
                self.assertIn(kind, F.KINDS, pid)
                self.assertTrue(why)


class AmountTests(unittest.TestCase):
    """The one calculation everybody does by hand and occasionally gets wrong
    by a factor of a thousand."""

    def test_a_concentration_times_a_volume_is_an_amount(self):
        self.assertEqual({'amount': 200.0, 'unit': 'ng'},
                         F.amount_per_vessel(100, 'ng/mL', 2.0))
        self.assertEqual({'amount': 2000.0, 'unit': 'ng'},
                         F.amount_per_vessel(100, 'ng/mL', 20.0))

    def test_molar_units_become_moles_not_a_guess(self):
        self.assertEqual({'amount': 0.02, 'unit': 'µmol'}, F.amount_per_vessel(10, 'uM', 2.0))
        self.assertEqual({'amount': 2.0, 'unit': 'nmol'}, F.amount_per_vessel(1000, 'nM', 2.0))
        self.assertEqual({'amount': 4.0, 'unit': 'µmol'}, F.amount_per_vessel(2, 'mM', 2.0))

    def test_a_unit_it_cannot_do_exactly_gets_no_number(self):
        self.assertIsNone(F.amount_per_vessel(60, 'rpm', 2.0))
        self.assertIsNone(F.amount_per_vessel(0.2, 'fraction', 2.0))
        self.assertIsNone(F.amount_per_vessel(100, 'ng/mL', None), 'no volume, no amount')
        self.assertIsNone(F.amount_per_vessel(None, 'ng/mL', 2.0))


class TranslationTests(unittest.TestCase):
    def test_a_concentration_carries_and_brings_the_amount_per_well(self):
        t = F.translate(PROTOCOL, 'six_well')
        r = rows(t)['mcsf_ng_ml']
        self.assertEqual(F.AMOUNT, r['kind'])
        self.assertEqual(100, r['value'], 'the concentration itself does not change')
        self.assertEqual({'amount': 200.0, 'unit': 'ng'}, r['amount'])
        self.assertEqual({'vessels': 6, 'label': 'plate of 6 wells'}, t['per_unit'])

    def test_a_mixing_rate_is_never_converted_in_either_direction(self):
        """The wrong version of this feature is a table that turns 60 impeller
        rpm into some number of tube rpm. They are different physical
        quantities and there is no such number."""
        for target in ('six_well', 'cero'):
            r = rows(F.translate(PROTOCOL, target))['agitation_rpm']
            self.assertEqual(F.SET_HERE, r['kind'], target)
            self.assertEqual(60, r['value'], 'the old value is shown, not converted')
            self.assertIn('not', r['why'].lower())
        back = F.translate(dict(PROTOCOL, culture_format='cero'), 'stirred_tank')
        self.assertEqual(F.SET_HERE, rows(back)['agitation_rpm']['kind'],
                         'a tube rotation does not become an impeller speed either')

    def test_a_control_the_vessel_does_not_have_is_dropped_and_says_why(self):
        for target in ('six_well', 'cero'):
            r = rows(F.translate(PROTOCOL, target))['do_setpoint']
            self.assertEqual(F.DROPPED, r['kind'], target)
            self.assertIn('no dissolved-oxygen', r['why'])

    def test_times_temperatures_and_fractions_carry_untouched(self):
        r = rows(F.translate(PROTOCOL, 'six_well'))
        self.assertEqual(F.CARRIED, r['temperature_c']['kind'])
        self.assertEqual(F.CARRIED, r['rocki_hours']['kind'])
        self.assertEqual(37, r['temperature_c']['value'])

    def test_a_seeding_density_says_that_per_area_is_a_different_number(self):
        """cells/mL and cells/cm2 are not two ways of saying one thing, and
        which matters depends on biology the vessel does not know."""
        r = rows(F.translate(PROTOCOL, 'six_well'))['seed_density']
        self.assertEqual(F.AMOUNT, r['kind'])
        self.assertEqual({'amount': 1.0, 'unit': '1e6 cells'}, r['amount'])
        self.assertIn('per cm2', r['why'])
        self.assertIn('nothing here can convert', r['why'])

    def test_a_gap_stays_a_gap_in_the_new_vessel(self):
        """Nothing was established in the old vessel, so there is nothing to
        carry and nothing to convert — and changing format does not fill it."""
        r = rows(F.translate(PROTOCOL, 'six_well'))['gmcsf']
        self.assertIsNone(r['value'])
        self.assertIsNone(r['amount'], 'no dose, no amount to weigh out')
        self.assertEqual(F.CARRIED, r['kind'])
        self.assertIn('It is a gap here too', r['why'])

    def test_a_tube_gets_no_amounts_until_a_working_volume_is_given(self):
        """Because the tube's working volume is a choice, not a specification."""
        t = F.translate(PROTOCOL, 'cero')
        self.assertIsNone(t['working_volume_ml'])
        self.assertIsNone(rows(t)['mcsf_ng_ml']['amount'])
        t2 = F.translate(PROTOCOL, 'cero', working_volume_ml=20)
        self.assertEqual({'amount': 2000.0, 'unit': 'ng'}, rows(t2)['mcsf_ng_ml']['amount'])
        self.assertEqual('given for this translation', t2['working_volume_basis'])

    def test_it_says_the_translation_is_a_starting_point_and_not_the_process(self):
        t = F.translate(PROTOCOL, 'six_well')
        self.assertIn('not the same process', t['note'])
        self.assertIn('comparison against the old one', t['note'])
        self.assertIn('never evidence that the protocol transferred', t['note'])

    def test_the_counts_add_up_to_everything_in_the_protocol(self):
        t = F.translate(PROTOCOL, 'six_well')
        self.assertEqual(7, sum(t['counts'].values()))
        self.assertEqual({'carried': 3, 'amount': 2, 'set_here': 1, 'dropped': 1, 'unknown': 0},
                         t['counts'],
                         'the GM-CSF gap carries as a gap rather than needing a rule')

    def test_the_written_layout_carries_the_amounts_and_the_warnings(self):
        md = F.markdown(F.translate(PROTOCOL, 'six_well'))
        self.assertIn('This protocol in a 6-well plate', md)
        self.assertIn('200 ng per vessel', md)
        self.assertIn('What this format needs you to decide', md)
        self.assertIn('adherent or suspension', md)


class SimulatorTests(unittest.TestCase):
    def test_the_model_keeps_its_vessel_terms_in_the_vessel_it_was_written_for(self):
        usable, dropped, note = F.simulatable('stirred_tank',
                                              {'agitation_rpm': 60, 'mcsf_ng_ml': 100})
        self.assertEqual({'agitation_rpm': 60, 'mcsf_ng_ml': 100}, usable)
        self.assertEqual([], dropped)
        self.assertIn('written for this format', note)

    def test_in_a_plate_the_vessel_terms_are_dropped_and_named(self):
        usable, dropped, note = F.simulatable(
            'six_well', {'agitation_rpm': 60, 'do_setpoint': 0.2, 'feed_fraction': 0.5,
                         'mcsf_ng_ml': 100})
        self.assertEqual({'mcsf_ng_ml': 100}, usable)
        self.assertEqual(['agitation_rpm', 'do_setpoint', 'feed_fraction'],
                         [d['parameter_id'] for d in dropped])
        for d in dropped:
            self.assertTrue(d['why'])
        self.assertIn('not a prediction about this format', note)

    def test_a_simulation_with_nothing_left_to_ask_is_refused_rather_than_run(self):
        from biosense.evidence import cli as EC
        with self.assertRaises(K.ContractError) as e:
            EC.simulate('ipsc_macrophage', {'agitation_rpm': 70}, culture_format='six_well')
        self.assertIn('every setting given is a stirred-tank control', str(e.exception))

    def test_a_plate_simulation_keeps_the_dose_and_says_what_it_is_worth(self):
        from biosense.evidence import cli as EC
        out = EC.simulate('ipsc_macrophage', {'mcsf_ng_ml': 120, 'agitation_rpm': 70},
                          culture_format='six_well')
        self.assertEqual({'mcsf_ng_ml': 120.0}, out['requested'])
        self.assertEqual(['agitation_rpm'],
                         [d['parameter_id'] for d in out['dropped_for_format']])
        self.assertIn('its numbers do not describe a plate', out['note'])


class WiringTests(unittest.TestCase):
    def test_a_run_can_be_asked_for_in_a_format_and_the_brief_says_which(self):
        from biosense.production import discovery as DISC
        req = DISC.build(project_id='ipsc_macrophage', runtime_mode='synthetic_demo',
                         objective='increase viable macrophage yield from iPSC',
                         culture_format='six_well')
        self.assertEqual('six_well', req['culture_format'])
        K.require_valid('discovery_request', req)
        brief = DISC.render_brief(req, loop_dir='runs/ai-x')
        self.assertIn('The vessel this run is for: 6-well plate', brief)
        self.assertIn('A concentration is the same concentration in any vessel', brief)
        self.assertIn('say which format each source', brief)

    def test_a_run_that_names_no_format_says_nothing_about_one(self):
        from biosense.production import discovery as DISC
        req = DISC.build(project_id='ipsc_macrophage', runtime_mode='synthetic_demo',
                         objective='increase viable macrophage yield from iPSC')
        self.assertIsNone(req['culture_format'])
        self.assertNotIn('The vessel this run is for',
                         DISC.render_brief(req, loop_dir='runs/ai-x'))

    def test_an_unknown_format_is_refused_when_the_run_is_built(self):
        from biosense.production import discovery as DISC
        with self.assertRaisesRegex(K.ContractError, 'not a culture format'):
            DISC.build(project_id='ipsc_macrophage', runtime_mode='synthetic_demo',
                       objective='increase viable macrophage yield from iPSC',
                       culture_format='shake_flask')

    def test_the_page_offers_the_choice_and_the_layout(self):
        html = (K.ROOT / 'webapp' / 'console.html').read_text()
        self.assertIn('id="cultureFormat"', html)
        self.assertIn('CERO', html)
        js = (K.ROOT / 'webapp' / 'discovery.js').read_text()
        self.assertIn('function formatPanel', js)
        self.assertIn('/translate?', js)
        self.assertIn('culture_format:', js)


class ApiTests(unittest.TestCase):
    """Through the server: pick a vessel, get the protocol laid out in it."""

    @classmethod
    def setUpClass(cls):
        import json
        import os
        import shutil
        import tempfile
        import threading
        from http.server import ThreadingHTTPServer
        from pathlib import Path
        from biosense import workspace as WS
        from biosense.production import app as APP
        from biosense.production import authz as AZ
        from biosense.production import budget as BU
        from biosense.production import run_store as RS
        from biosense.production import runtime as RT
        cls.tmp = Path(tempfile.mkdtemp())
        cls._rm = lambda: shutil.rmtree(cls.tmp, ignore_errors=True)
        (cls.tmp / 'runs').mkdir()
        cls._private = os.environ.get('BIOSENSE_PRIVATE_DATA')
        os.environ['BIOSENSE_PRIVATE_DATA'] = str(cls.tmp / 'private')
        cfg = RT.from_env(runs_dir=cls.tmp / 'runs', env={})
        APP.Handler.runs_dir = cls.tmp / 'runs'
        APP.Handler.static_dir = K.ROOT / 'webapp'
        APP.Handler.registry = APP.Registry(cls.tmp / 'runs')
        APP.Handler.runtime_cfg = cfg
        APP.Handler.limits = BU.Limits()
        APP.Handler.policy = AZ.Policy()
        APP.Handler.discovery = APP.DiscoveryRegistry(cls.tmp / 'runs', cfg)
        APP.Handler.sessions = WS.SessionStore()
        APP.Handler.default_identity = WS.local_identity()
        APP.Handler._ready = {'at': 0.0, 'body': None, 'code': 503}
        cls.rid = 'e' * 16
        d = cls.tmp / 'runs' / 'ai-20261012-eeeeee'
        d.mkdir()
        (d / 'discovery_request.json').write_text(json.dumps(
            {'project_id': 'ipsc_macrophage', 'runtime_mode': 'local_real_ai',
             'objective': 'o', 'request_id': 'r1', 'culture_format': 'stirred_tank'}))
        proto = dict(PROTOCOL, title='Macrophage yield', approval={
            'required': True, 'approved_by': None, 'how': 'approve-protocol'},
            stages=[], hypothesis_ledger=[], gaps=[], limitations=[],
            timeline={'stages': [], 'total_days': 25, 'candidates': []})
        RS.write_state(d, {'run_id': cls.rid, 'status': 'done', 'is_real': True,
                           'runtime_mode': 'local_real_ai', 'owner': None,
                           'project_id': 'ipsc_macrophage', 'objective': 'o',
                           'run_dir': d.name, 'started_at': 1.0, 'finished_at': 2.0,
                           'result': {'protocol': proto}})
        cls.srv = ThreadingHTTPServer(('127.0.0.1', 0), APP.Handler)
        cls.srv.daemon_threads = True
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        import os
        cls.srv.shutdown(); cls.srv.server_close()
        if cls._private is None:
            os.environ.pop('BIOSENSE_PRIVATE_DATA', None)
        else:
            os.environ['BIOSENSE_PRIVATE_DATA'] = cls._private
        cls._rm()

    def _get(self, path):
        import json
        import urllib.error
        import urllib.request
        try:
            with urllib.request.urlopen(
                    f'http://127.0.0.1:{self.port}{path}', timeout=30) as r:
                body = r.read()
                ctype = r.headers.get('Content-Type', '')
                if ctype.startswith('application/json'):
                    return r.status, json.loads(body)
                if 'wordprocessing' in ctype:
                    return r.status, body
                return r.status, body.decode()
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b'{}')

    def test_the_formats_are_listed_for_the_page(self):
        code, d = self._get('/api/formats')
        self.assertEqual(200, code)
        self.assertEqual(['six_well', 'cero', 'stirred_tank'],
                         [f['format_id'] for f in d['formats']])

    def test_a_protocol_lays_out_in_a_plate_over_http(self):
        code, d = self._get(f'/api/discovery/{self.rid}/translate?to=six_well')
        self.assertEqual(200, code, d)
        self.assertEqual('six_well', d['to']['format_id'])
        self.assertEqual('stirred_tank', d['from']['format_id'], 'it knows what it came from')
        got = {r['parameter_id']: r for r in d['physical']}
        self.assertEqual('dropped', got['do_setpoint']['kind'])

    def test_a_working_volume_given_in_the_url_is_what_the_amounts_use(self):
        code, d = self._get(
            f'/api/discovery/{self.rid}/translate?to=cero&working_volume_ml=20')
        self.assertEqual(200, code, d)
        mcsf = [r for st in d['stages'] for r in st['items']
                if r['parameter_id'] == 'mcsf_ng_ml'][0]
        self.assertEqual({'amount': 2000.0, 'unit': 'ng'}, mcsf['amount'])

    def test_a_vessel_that_does_not_exist_is_refused_rather_than_guessed(self):
        code, d = self._get(f'/api/discovery/{self.rid}/translate?to=roller_bottle')
        self.assertEqual(400, code)
        self.assertIn('not a culture format', d['error'])
        code, _ = self._get(f'/api/discovery/{self.rid}/translate?to=cero'
                            '&working_volume_ml=not-a-number')
        self.assertEqual(400, code)

    def test_the_bench_copy_can_be_asked_for_in_the_vessel_it_is_going_into(self):
        code, body = self._get(
            f'/api/discovery/{self.rid}/protocol?format=docx&as=six_well')
        self.assertEqual(200, code)
        self.assertTrue(body.startswith(b'PK'))
        from tests.test_lab_protocol import text_of
        text = '\n'.join(text_of(body))
        self.assertIn('In a 6-well plate', text)
        self.assertIn('200 ng', text, 'the amount per well, worked out')
        self.assertIn('adherent or suspension', text)
        self.assertIn('NOT APPROVED', text)


if __name__ == '__main__':
    unittest.main()
