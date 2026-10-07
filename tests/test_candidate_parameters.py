"""A lever the project does not have yet, and the protocol drawn as a production chain.

One real run's best idea was a cytokine its project had no parameter for. The
hypothesis was kept and labelled, and then nothing could be done with it: the
protocol could not carry it and the reactor could not play it. These tests hold
the three ways out — see it on the timeline, register it into your own project,
and simulate it as an assumed effect in the stage it is given — and that each
says plainly what it is.
"""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from biosense import contracts as K
from biosense import parameters as PR
from biosense import projects as PJ
from biosense import workspace as WS
from biosense.data import roots as DR
from biosense.evidence import cli as EVCLI
from biosense.production import project_builder as PB
from biosense.production import protocol_summary as PS
from biosense.production import sim_mode as SM


def il34_hypothesis(value=100.0, pid='IL-34'):
    h = EVCLI.build_hypothesis(EVCLI.TEMPLATE | {'effects': EVCLI.TEMPLATE['effects'][:1]},
                               project_id='ipsc_macrophage')
    h = json.loads(json.dumps(h))
    h['hypothesis_id'] = 'H-il34'
    h['statement'] = 'Adding IL-34 during myeloid differentiation may raise macrophage yield.'
    h['parameter'].update(parameter_id=pid, label=None, proposed_label='IL-34', unit='ng/mL',
                          stage='myeloid', candidate_value=value, registered=False,
                          simulator_coverage='not_in_project', direction='increase')
    return h


class TimelineTests(unittest.TestCase):
    def test_stages_are_laid_out_in_days_and_an_unregistered_lever_is_a_candidate(self):
        project = PJ.load('ipsc_macrophage')
        doc = PS.build(project=project, objective='o', hypotheses=[il34_hypothesis()],
                       runtime_mode='synthetic_demo')
        K.require_valid('protocol_summary', doc)
        tl = doc['timeline']
        self.assertEqual(['expansion', 'mesoderm', 'hemato', 'myeloid'],
                         [s['stage_id'] for s in tl['stages']])
        self.assertEqual(25.0, tl['total_days'])
        self.assertEqual(11.0, tl['stages'][3]['day_start'])
        cand = tl['candidates'][0]
        self.assertEqual(('IL-34', 'myeloid', 100.0),
                         (cand['label'], cand['stage_id'], cand['candidate_value']))
        row = next(r for r in doc['hypothesis_ledger'] if r['hypothesis_id'] == 'H-il34')
        self.assertFalse(row['adopted'])
        self.assertIn('not a parameter of this project yet', row['reason'])

    def test_the_markdown_has_one_row_per_stage_with_factors_apart_from_setpoints(self):
        doc = PS.build(project=PJ.load('ipsc_macrophage'), objective='o',
                       hypotheses=[il34_hypothesis()], runtime_mode='synthetic_demo')
        md = PS.markdown(doc)
        self.assertIn('## Production timeline', md)
        self.assertIn('| Stage | Days | Factors | Setpoints |', md)
        mes = next(l for l in md.splitlines() if l.startswith('| Mesoderm'))
        self.assertIn('BMP4', mes.split('|')[3], 'a concentration is a factor')
        exp = next(l for l in md.splitlines() if l.startswith('| Expansion'))
        self.assertIn('Seed density', exp.split('|')[4], 'cells/mL is a setpoint, not a factor')
        self.assertIn('IL-34', md.split('## Production timeline')[1].split('## ')[0])


class FactorSimulationTests(unittest.TestCase):
    def run_factor(self, **kw):
        g = dict(label='IL-34', kind='factor', stage='myeloid', growth_ratio=1.0, diff_ratio=1.0)
        g.update(kw)
        return SM.genotype_compare({'seed': 7, 'genotype': g})

    def test_no_assumed_effect_is_no_difference(self):
        d = self.run_factor()
        self.assertEqual(d['curves']['wild_type'], d['curves']['edited'])
        self.assertEqual('without it', d['control_label'])

    def test_the_effect_acts_only_in_the_stage_it_is_given(self):
        myeloid = self.run_factor(diff_ratio=1.3)
        mesoderm = self.run_factor(diff_ratio=1.3, stage='mesoderm')
        h = lambda d: d['deltas']['harvest_per_input_ipsc']['b']
        self.assertGreater(h(myeloid), myeloid['deltas']['harvest_per_input_ipsc']['a'])
        self.assertNotAlmostEqual(h(myeloid), h(mesoderm))
        # Growth acts in every stage, so a whole-process edit and a myeloid-only
        # factor with the same ratio are different runs.
        grow_m = self.run_factor(growth_ratio=1.2)
        grow_all = SM.genotype_compare({'seed': 7, 'genotype': {
            'label': 'edit', 'growth_ratio': 1.2, 'diff_ratio': 1.0}})
        self.assertNotAlmostEqual(h(grow_m), h(grow_all))
        self.assertIn('during myeloid', myeloid['verdict'])
        self.assertIn('not a prediction about the factor', myeloid['verdict'])
        self.assertIn('assumed change', myeloid['note'])

    def test_an_unknown_stage_or_kind_is_refused(self):
        with self.assertRaisesRegex(K.ContractError, "reactor's stages"):
            self.run_factor(stage='activation')
        with self.assertRaisesRegex(K.ContractError, 'kind'):
            self.run_factor(kind='drug')

    def test_the_cli_plays_a_factor_and_labels_it(self):
        import contextlib
        import io
        tmp = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, tmp, True)
        with contextlib.redirect_stdout(io.StringIO()):
            code = EVCLI.main(['simulate', '--project', 'ipsc_macrophage', '--factor',
                               'IL-34:stage=myeloid,diff=1.2', '--out', str(tmp / 's.json')])
        self.assertEqual(0, code)
        g = K.read_json(tmp / 's.json')['genotype_simulation']
        self.assertEqual(('factor', 'myeloid'), (g['genotype']['kind'], g['genotype']['stage']))
        self.assertIn('not a measured effect of the factor', g['assumption'])


class RegisterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patcher = mock.patch.dict(os.environ, {DR.PRIVATE_ENV: str(self.tmp / 'private')})
        patcher.start(); self.addCleanup(patcher.stop)
        self.addCleanup(PR.forget_local, 'il34_ng_ml')
        self.me = WS.WorkspaceIdentity('scientist@example.org', authenticated=True)

    def register(self, **kw):
        spec = dict(label='IL-34', parameter_id='IL-34', unit='ng/mL', stage='myeloid',
                    minimum=0, maximum=200, hypothesis_id='H-il34')
        spec.update(kw)
        return PB.add_parameter(self.me, 'ipsc_macrophage', spec, registered_by='Dr A',
                                from_run='abcdef12')

    def test_a_new_lever_becomes_a_parameter_of_my_copy_and_the_protocol_adopts_it(self):
        doc, pid, how = self.register()
        self.assertEqual(('il34_ng_ml', 'local'), (pid, how))
        self.assertTrue((WS.projects_dir(self.me) / 'ipsc_macrophage.json').is_file())
        self.assertFalse(PJ.load('ipsc_macrophage').has('il34_ng_ml'), 'the template is untouched')
        mine = PB.load_for(self.me, 'ipsc_macrophage')
        self.assertEqual('myeloid', mine.parameter('il34_ng_ml').stage)
        self.assertEqual('not_modelled', mine.coverage('il34_ng_ml'))
        self.assertEqual('Dr A', mine.doc['local_parameters'][0]['registered_by'])
        # The hypothesis named it "IL-34" before it existed; it is adopted now.
        out = PS.build(project=mine, objective='o', hypotheses=[il34_hypothesis()],
                       runtime_mode='synthetic_demo')
        row = next(p for s in out['stages'] for p in s['parameters']
                   if p['parameter_id'] == 'il34_ng_ml')
        self.assertEqual(100.0, row['recommended_value'])
        self.assertEqual([], out['timeline']['candidates'])
        self.assertTrue(next(r for r in out['hypothesis_ledger']
                             if r['hypothesis_id'] == 'H-il34')['adopted'])

    def test_a_name_that_is_already_canonical_adds_that_parameter(self):
        PB.add_parameter(self.me, 'cart_expansion',
                         dict(label='IL-3', unit='ng/mL', stage='expansion', minimum=0,
                              maximum=100), registered_by='Dr A')
        self.assertTrue(PB.load_for(self.me, 'cart_expansion').has('il3_ng_ml'))
        self.assertFalse(PR.is_local('il3_ng_ml'))

    def test_what_cannot_be_registered_is_refused_and_nothing_is_written(self):
        with self.assertRaisesRegex(K.ContractError, 'minimum must be below'):
            self.register(minimum=5, maximum=1)
        with self.assertRaisesRegex(K.ContractError, 'stage must be one of'):
            self.register(stage='nowhere')
        with self.assertRaisesRegex(K.ContractError, 'already has'):
            self.register(label='M-CSF', parameter_id='mcsf', maximum=100)
        self.assertFalse((WS.projects_dir(self.me) / 'ipsc_macrophage.json').exists())
        self.register()
        with self.assertRaisesRegex(K.ContractError, 'unit'):
            PB.add_parameter(self.me, 'cart_expansion',
                             dict(label='IL-34', parameter_id='il34_ng_ml', unit='pg/mL',
                                  stage='expansion', minimum=0, maximum=1), registered_by='x')



class RegisterApiTests(RegisterTests):
    """From a finished run's timeline: register the lever, and the run's protocol carries it."""

    def setUp(self):
        super().setUp()
        import threading
        from http.server import ThreadingHTTPServer
        from biosense.production import app as APP
        from biosense.production import authz as AZ
        from biosense.production import budget as BU
        from biosense.production import discovery as DISC
        from biosense.production import run_store as RS
        from biosense.production import runtime as RT
        (self.tmp / 'runs').mkdir()
        cfg = RT.from_env(runs_dir=self.tmp / 'runs', env={})
        H = APP.Handler
        saved = {k: getattr(H, k, None) for k in (
            'runs_dir', 'static_dir', 'registry', 'runtime_cfg', 'limits', 'policy',
            'discovery', 'sessions', 'default_identity')}
        self.addCleanup(lambda: [setattr(H, k, v) for k, v in saved.items()])
        H.runs_dir, H.static_dir = self.tmp / 'runs', K.ROOT / 'webapp'
        H.registry, H.runtime_cfg = APP.Registry(self.tmp / 'runs'), cfg
        H.limits, H.policy = BU.Limits(), AZ.Policy()
        H.discovery = APP.DiscoveryRegistry(self.tmp / 'runs', cfg)
        H.sessions, H.default_identity = WS.SessionStore(), WS.local_identity()
        self.srv = ThreadingHTTPServer(('127.0.0.1', 0), H)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.rid = '0123456789abcdef'
        d = self.tmp / 'runs' / f'ai-20260101-{self.rid[:6]}'
        d.mkdir()
        req = DISC.build(project_id='ipsc_macrophage',
                         objective='Increase viable macrophage production while keeping identity.',
                         runtime_mode='synthetic_demo')
        K.write_json_atomic(d / 'discovery_request.json', req)
        K.write_json_atomic(d / 'quantified_hypothesis.json', il34_hypothesis())
        RS.write_state(d, {'run_id': self.rid, 'status': 'succeeded', 'runtime_mode':
                           'synthetic_demo', 'owner': WS.local_identity().owner})
        self.run_dir = d

    def post(self, path, body):
        import urllib.error
        import urllib.request
        r = urllib.request.Request(f'http://127.0.0.1:{self.srv.server_address[1]}{path}',
                                   method='POST', data=json.dumps(body).encode(),
                                   headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(r, timeout=60) as resp:
                return resp.status, json.loads(resp.read() or b'{}')
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b'{}')

    def test_registering_from_a_run_rebuilds_its_protocol(self):
        from biosense.production import run_store as RS
        code, d = self.post('/api/projects/ipsc_macrophage/parameters', {
            'label': 'IL-34', 'parameter_id': 'IL-34', 'unit': 'ng/mL', 'stage': 'myeloid',
            'minimum': 0, 'maximum': 200, 'hypothesis_id': 'H-il34', 'run_id': self.rid})
        self.assertEqual(200, code, d)
        self.assertEqual(('il34_ng_ml', 'local', True),
                         (d['parameter_id'], d['how'], d['protocol_rebuilt']))
        state = RS._read_state(RS.state_path(self.run_dir))
        proto = state['result']['protocol']
        row = next(p for s in proto['stages'] for p in s['parameters']
                   if p['parameter_id'] == 'il34_ng_ml')
        self.assertEqual(100.0, row['recommended_value'])
        code, d = self.post('/api/projects/ipsc_macrophage/parameters', {
            'label': 'IL-34', 'unit': 'ng/mL', 'stage': 'myeloid', 'minimum': 0, 'maximum': 200})
        self.assertEqual(400, code)
        self.assertIn('already has', d['error'])

    # The parent's tests run once, not again over HTTP.
    test_a_new_lever_becomes_a_parameter_of_my_copy_and_the_protocol_adopts_it = None
    test_a_name_that_is_already_canonical_adds_that_parameter = None
    test_what_cannot_be_registered_is_refused_and_nothing_is_written = None


if __name__ == '__main__':
    unittest.main()
