"""The chain from a prompt to a protocol, and the tools that check it.

Every real hosted run so far failed on something that was already true before it
started: no sandbox, a brief naming a command that does not exist, and no command
that wrote a hypothesis at all. These tests pin each of those, and the self-check
that now finds them in seconds instead of a paid run.
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from biosense import contracts as K
from biosense.evidence import cli as EVCLI
from biosense.production import activity as ACT
from biosense.production import discovery as DISC
from biosense.production import ingest as IN
from biosense.production import runtime as RT
from biosense.production import sandbox as SB
from biosense.production import selfcheck as SC
from biosense.production import stages as ST


class SandboxDiagnosisTests(unittest.TestCase):
    """Three causes, three remedies; the diagnosis has to tell them apart."""

    def diagnose(self, results, which='/usr/bin/bwrap'):
        calls = []

        def run(argv, cwd=None, env=None):
            calls.append(argv)
            for key, outcome in results:
                if key(argv):
                    return outcome
            return True, ''
        with mock.patch.object(SB.sys, 'platform', 'linux'), \
                mock.patch.object(SB.shutil, 'which', lambda _n: which), \
                mock.patch.object(SB, '_omnigent_argv', lambda ws, bind_proc: None):
            return SB.diagnose('/tmp', run=run), calls

    def test_missing_bwrap_is_named(self):
        found, _ = self.diagnose([], which=None)
        self.assertFalse(found['ok'])
        self.assertEqual('missing', found['problem'])

    def test_a_host_without_user_namespaces_is_named_and_nothing_is_relaxed(self):
        no_ns = (False, 'bwrap: No permissions to create new namespace')
        found, _ = self.diagnose([(lambda a: True, no_ns)])
        self.assertFalse(found['ok'])
        self.assertEqual('user_namespaces', found['problem'])
        self.assertEqual({}, found['env'])

    def test_a_refused_fresh_proc_is_fixed_by_binding_the_container_proc(self):
        """The case a Docker host with user namespaces allowed produces."""
        fresh = (lambda a: '--proc' in a, (False, "bwrap: Can't mount proc on /newroot/proc"))
        found, _ = self.diagnose([fresh])
        self.assertTrue(found['ok'])
        self.assertEqual('bind_proc', found['mode'])
        self.assertEqual({SB.PROC_BIND_ENV: SB.PROC_BIND_VALUE}, found['env'])
        lines = SB.env_lines(found, env={})
        self.assertIn(f'{SB.PROC_BIND_ENV}={SB.PROC_BIND_VALUE}', lines)
        # The runner's environment is stripped; the name must be passed through.
        self.assertIn(f'{SB.PASSTHROUGH_ENV}={SB.PROC_BIND_ENV}', lines)
        # And the boot script exports exactly these names and nothing else.
        boot = (K.ROOT / 'deploy' / 'start-ai.sh').read_text()
        for name in SB.EXPORTABLE:
            self.assertIn(name, boot)

    def test_an_existing_passthrough_list_is_extended_not_replaced(self):
        found = {'env': {SB.PROC_BIND_ENV: SB.PROC_BIND_VALUE}}
        lines = SB.env_lines(found, env={SB.PASSTHROUGH_ENV: 'FOO'})
        self.assertIn(f'{SB.PASSTHROUGH_ENV}=FOO,{SB.PROC_BIND_ENV}', lines)

    def test_a_working_default_sandbox_needs_nothing(self):
        found, _ = self.diagnose([])
        self.assertTrue(found['ok'])
        self.assertEqual('fresh_proc', found['mode'])
        self.assertEqual([], SB.env_lines(found, env={}))

    def test_a_bind_proc_host_started_without_the_switch_is_refused(self):
        found = {'ok': True, 'mode': 'bind_proc', 'problem': None, 'detail': None, 'env': {}}
        with mock.patch.object(SB, 'diagnose', lambda ws=None: found), \
                mock.patch.dict(RT.os.environ, {SB.PROC_BIND_ENV: ''}):
            self.assertIn(SB.PROC_BIND_ENV, RT._try_bwrap('/tmp'))
        with mock.patch.object(SB, 'diagnose', lambda ws=None: found), \
                mock.patch.dict(RT.os.environ, {SB.PROC_BIND_ENV: SB.PROC_BIND_VALUE}):
            self.assertIsNone(RT._try_bwrap('/tmp'))


class EvidenceCliTests(unittest.TestCase):
    """The command the brief always promised and never had."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def draft(self, **over):
        d = json.loads(json.dumps(EVCLI.TEMPLATE))
        d['effects'] = d['effects'][:1]
        d.update(over)
        return d

    def test_the_template_builds_a_valid_candidate_hypothesis(self):
        h = EVCLI.build_hypothesis(self.draft(), project_id='ipsc_macrophage')
        K.require_valid('quantified_hypothesis', h)
        self.assertEqual('candidate', h['claim_level'])
        self.assertFalse(h['expected_effects'][0]['magnitude_estimated'])

    def test_derived_fields_are_computed_not_taken_from_the_draft(self):
        h = EVCLI.build_hypothesis(self.draft(claim_level='simulated', confidence='high'),
                                   project_id='ipsc_macrophage')
        self.assertEqual('candidate', h['claim_level'])
        self.assertNotEqual('high', h['confidence'])

    def test_a_draft_without_an_uncertainty_or_next_experiment_is_refused(self):
        with self.assertRaisesRegex(K.ContractError, 'uncertainty'):
            EVCLI.build_hypothesis(self.draft(uncertainty=None), project_id='ipsc_macrophage')
        with self.assertRaisesRegex(K.ContractError, 'next_experiment'):
            EVCLI.build_hypothesis(self.draft(next_experiment=None),
                                   project_id='ipsc_macrophage')

    def test_a_draft_best_guess_builds_a_labelled_judgement(self):
        import copy
        guess = copy.deepcopy(EVCLI.TEMPLATE['effects'][1])
        h = EVCLI.build_hypothesis(self.draft(effects=[guess]), project_id='ipsc_macrophage')
        e = h['expected_effects'][0]
        self.assertEqual('judgement', e['estimate_type'])
        self.assertEqual('low', e['judgement']['confidence'])
        self.assertEqual('candidate', h['claim_level'])

    def test_a_magnitude_needs_typed_sourced_values(self):
        eff = {'metric': 'yield', 'unit': 'cells', 'baseline': 10, 'candidate': 12}
        with self.assertRaisesRegex(K.ContractError, 'estimate_type'):
            EVCLI.build_hypothesis(self.draft(effects=[eff]), project_id='ipsc_macrophage')

    def test_the_simulator_and_a_simulated_hypothesis_come_from_the_model(self):
        sim = EVCLI.simulate('ipsc_macrophage', {'mcsf_ng_ml': 50})
        self.assertEqual('simulated', sim['prediction'])
        self.assertTrue(sim['effects'])
        self.assertTrue(all(e['estimate_type'] == 'simulated' for e in sim['effects']))
        K.write_json_atomic(self.tmp / 'simulation.json', sim)
        h = EVCLI.build_hypothesis(
            self.draft(effects=[{'from_simulation': 'simulation.json'}]),
            project_id='ipsc_macrophage', base_dir=self.tmp)
        self.assertEqual('simulated', h['claim_level'])

    def test_a_parameter_the_model_does_not_cover_gets_no_simulated_effect(self):
        sim = EVCLI.simulate('ipsc_macrophage', {'temperature_c': 38})
        self.assertEqual([], sim['effects'])
        self.assertIn('temperature_c', [r.get('parameter_id') for r in sim['skipped']])

    def test_the_cli_writes_the_file_and_refuses_with_a_reason(self):
        K.write_json_atomic(self.tmp / 'H01.draft.json', self.draft())
        code = EVCLI.main(['hypothesis', '--draft', str(self.tmp / 'H01.draft.json'),
                           '--project', 'ipsc_macrophage',
                           '--out', str(self.tmp / 'quantified_hypothesis.json')])
        self.assertEqual(0, code)
        K.require_valid('quantified_hypothesis',
                        K.read_json(self.tmp / 'quantified_hypothesis.json'))
        K.write_json_atomic(self.tmp / 'bad.draft.json', self.draft(uncertainty=None))
        code = EVCLI.main(['hypothesis', '--draft', str(self.tmp / 'bad.draft.json'),
                           '--project', 'ipsc_macrophage', '--out', str(self.tmp / 'x.json')])
        self.assertEqual(2, code)
        self.assertFalse((self.tmp / 'x.json').exists())

    def test_a_context_is_written_valid(self):
        self.assertEqual(0, EVCLI.main(['context', '--species', 'human', '--cell-types',
                                        'macrophage', '--out', str(self.tmp / 'rc.json')]))
        K.require_valid('research_context', K.read_json(self.tmp / 'rc.json'))

    def test_a_draft_beside_the_artifacts_is_not_read_as_one(self):
        K.write_json_atomic(self.tmp / 'H01.draft.json', self.draft())
        bundle = IN.read_run(self.tmp)
        self.assertEqual([], bundle['rejected'])
        self.assertEqual({}, bundle['by_kind'])


class BriefCommandTests(unittest.TestCase):
    """The brief named `bioinformatics.cli execute`, which never existed."""

    def brief(self):
        req = DISC.build(project_id='ipsc_macrophage', objective='Is M-CSF limiting yield?',
                         runtime_mode='local_real_ai')
        return DISC.render_brief(req, loop_dir='data/runs/ai-x')

    def test_the_brief_names_the_real_analysis_command_and_the_evidence_cli(self):
        b = self.brief()
        self.assertNotIn('cli execute', b)
        self.assertIn('bioinformatics.cli analyse run', b)
        self.assertIn('--evidence-gap', b)
        self.assertIn('biosense.evidence.cli hypothesis', b)
        self.assertIn('biosense.evidence.cli simulate', b)

    def test_the_real_analysis_command_ticks_the_running_analysis_stage(self):
        self.assertEqual('running_analysis', ST.stage_for_tool(
            'sys_os_shell', 'python -m biosense.bioinformatics.cli analyse run --plan p.json'))
        self.assertEqual('planning_analysis', ST.stage_for_tool(
            'sys_os_shell', 'python -m biosense.bioinformatics.cli analyse plan --plan-id x'))
        # Reading a file named after a stage is not doing that stage.
        self.assertIsNone(ST.stage_for_tool('sys_os_shell', 'cat analysis_plan.json'))

    def test_the_self_check_finds_a_command_that_does_not_exist(self):
        c = SC.Check(K.ROOT, K.ROOT, sandboxed=False, bind_proc=False,
                     python=SC.sys.executable)
        with mock.patch.object(SC.K, 'ROOT', Path(tempfile.mkdtemp())):
            SC.check_commands(c, '`python -m biosense.bioinformatics.cli execute --plan x`')
        self.assertEqual('fail', c.rows[0]['status'])
        self.assertIn('execute', c.rows[0]['detail'])

    def test_every_command_the_agents_are_told_to_run_exists(self):
        c = SC.Check(K.ROOT, K.ROOT, sandboxed=False, bind_proc=False,
                     python=SC.sys.executable)
        SC.check_commands(c, self.brief())
        self.assertEqual('ok', c.rows[0]['status'], c.rows[0]['detail'])


class SelfCheckRunTests(unittest.TestCase):
    def test_the_offline_chain_passes_from_a_prompt_to_a_protocol(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        report = SC.run(runs_dir=tmp, cfg=None, hosted=False)
        rows = {r['id']: r for r in report['rows']}
        for cid in ('commands_named', 'bioinformatics', 'simulator', 'hypothesis',
                    'parameters', 'protocol', 'bioreactor_loop'):
            self.assertEqual('ok', rows[cid]['status'], f'{cid}: {rows[cid]["detail"]}')
        self.assertTrue(report['ok'], SC.render(report))
        self.assertEqual('skip', rows['agents_talk']['status'])
        # Nothing left behind unless asked.
        self.assertEqual([], list(tmp.iterdir()))


class RunEndTests(unittest.TestCase):
    def test_a_finished_run_shows_no_agent_still_running(self):
        """COMPLETE beside "Orchestrator: Running" was the reported screenshot."""
        a = ACT.Activity(started_at=0)
        a.observe({'kind': 'accepted', 'at': 1, 'simple': 'started'})
        a.observe({'kind': 'subagent', 'agent': 'literature', 'state': 'started', 'at': 2})
        a.close('complete', at=5)
        self.assertEqual([], a.snapshot(now=6)['active_agents'])
        self.assertEqual('complete', a.agents['orchestrator']['status'])
        b = ACT.Activity(started_at=0)
        b.observe({'kind': 'accepted', 'at': 1})
        b.close('cancelled', at=3)
        self.assertEqual('cancelled', b.agents['orchestrator']['status'])

    def test_the_discovery_page_has_a_stop_button_beside_run(self):
        html = (K.ROOT / 'webapp' / 'console.html').read_text()
        self.assertIn('id="stopBtn"', html)
        js = (K.ROOT / 'webapp' / 'discovery.js').read_text()
        self.assertIn('/cancel', js)
        self.assertIn("$('#stopBtn').addEventListener('click', stopFromButton)", js)


if __name__ == '__main__':
    unittest.main()
