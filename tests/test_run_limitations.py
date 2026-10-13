"""The cards a live run showed under "analysis limitation", each pinned to its cause.

Five of them were not limits of the analysis: a plan refused for a modality
prefix, a cross-check that died on a KeyError and showed only the key, a shell
result cut so short its reason was lost, and a search NCBI throttled that read
as "nothing found". Each test below is one card, and what it should have been.
"""
import contextlib
import io
import json
import unittest
import urllib.error
from unittest import mock

from biosense.bioinformatics import cli as BCLI
from biosense.bioinformatics import plan as PLAN
from biosense.bioinformatics import registry as TREG
from biosense.bioinformatics import tools as BT
from biosense import contracts as K
from biosense.data.sources import geo
from biosense.data.sources import http as HTTP
from biosense.production import activity as ACT
from biosense.production import stages as ST


class AnalysisTypeTests(unittest.TestCase):
    """Card: "bulk.expression_comparison does not perform 'expression_comparison'
    (it performs bulk_expression_comparison)"."""

    def test_a_spelling_that_names_one_of_the_tool_s_types_is_that_type(self):
        spec = TREG.get('bulk.expression_comparison')
        for said in ('expression_comparison', 'Bulk Expression Comparison',
                     'bulk-expression-comparison', 'bulk_expression_comparison'):
            self.assertEqual('bulk_expression_comparison',
                             PLAN.resolve_analysis_type(said, spec), said)

    def test_what_names_none_or_several_is_left_for_the_refusal(self):
        spec = TREG.get('bulk.expression_comparison')
        self.assertEqual('pathway_enrichment', PLAN.resolve_analysis_type('pathway_enrichment', spec))
        class Two:
            analysis_types = ('bulk_expression_comparison', 'protein_expression_comparison')
        self.assertEqual('expression_comparison',
                         PLAN.resolve_analysis_type('expression_comparison', Two),
                         'could mean either: not guessed')


class CrossCheckTests(unittest.TestCase):
    """Card: "'genotype_arms'" — a KeyError's whole text."""

    REPORT = {'lever_suggestions': [{'parameter': 'mcsf_ng_ml', 'direction': 'increase',
                                     'confidence': 'low', 'basis': 'x'}]}

    def test_a_protocol_without_arms_is_not_cross_checked_and_says_why(self):
        out = BT.cross_check(self.REPORT, {'stages': [], 'title': 'a discovery summary'})
        self.assertEqual([], out['levers'])
        self.assertIsNone(out['control_arm_id'])
        self.assertIn('no genotype arms', out['note'])
        self.assertIn('discovery protocol summary', out['note'])
        K.require_valid  # the shape is the schema's: an object with these fields
        from biosense.bioinformatics import tools as T  # noqa: F401
        schema = json.loads((K.ROOT / 'schemas' / 'bioinformatics_report.schema.json').read_text())
        allowed = set(schema['properties']['protocol_cross_check']['properties'])
        self.assertLessEqual(set(out), allowed)

    def test_a_missing_field_is_said_to_be_a_missing_field(self):
        err = io.StringIO()
        with contextlib.redirect_stdout(err), \
                mock.patch.object(BT, 'annotate', side_effect=KeyError('genotype_arms')):
            code = BCLI.main(['annotate', '--genes', 'PPARG'])
        self.assertEqual(1, code)
        msg = json.loads(err.getvalue())['message']
        self.assertIn("missing the field 'genotype_arms'", msg)


class ToolOutputTests(unittest.TestCase):
    """Card: "a command exited 1" — the reason cut off with the rest."""

    def event(self, output):
        return ST.map_event({'type': 'response.output_item.done',
                             'item': {'type': 'function_call_output', 'name': 'sys_os_shell',
                                      'output': output}})

    def test_a_refusal_survives_a_long_shell_result(self):
        reason = 'GSE287217 has no table this run can read, so it is a dataset a person can supply'
        out = json.dumps({'exit_code': 1, 'stderr': 'x' * 3000,
                          'stdout': json.dumps({'refused': True, 'reason': reason})
                          + '\n' + 'y' * 5000})
        technical = self.event(out)['technical']
        self.assertLess(len(technical), 1600)
        self.assertEqual(reason, ACT.refusal_in(technical).rstrip('…'))

    def test_the_error_at_the_end_of_stderr_survives(self):
        out = json.dumps({'exit_code': 1, 'stdout': 'a' * 4000,
                          'stderr': 'b' * 4000 + '\nConnectionError: Could not resolve host'})
        self.assertIn('Could not resolve host', ACT.refusal_in(self.event(out)['technical']))

    def test_a_result_that_is_not_a_shell_result_is_cut_as_before(self):
        self.assertEqual(ST.TOOL_OUTPUT_CAP, len(self.event('z' * 5000)['technical']))

    def test_a_result_cut_before_it_could_say_why_is_a_timeline_note(self):
        a = ACT.Activity(started_at=0)
        a.observe({'kind': 'tool_result', 'at': 1, 'tool': 'sys_os_shell',
                   'technical': '{"exit_code": 1, "stdout": "partial outp'})
        snap = a.snapshot()
        self.assertEqual([], snap['limitations'])
        self.assertTrue(any(r['kind'] == 'agent_slip' for r in snap['timeline']))


class ThrottledSearchTests(unittest.TestCase):
    """A throttled search is not an empty one."""

    def setUp(self):
        p = mock.patch.object(HTTP, '_sleep', lambda s: None)
        p.start()
        self.addCleanup(p.stop)

    def test_a_search_throttled_once_is_retried_and_answers(self):
        replies = iter([urllib.error.HTTPError('u', 429, 'slow', {}, None),
                        {'esearchresult': {'idlist': []}}])

        class R(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def urlopen(req, timeout=None):
            r = next(replies)
            if isinstance(r, Exception):
                raise r
            return R(json.dumps(r).encode())
        with mock.patch('urllib.request.urlopen', urlopen):
            res = geo.search('PPARG macrophage', live=True, i_have_network_permission=True)
        self.assertNotIn('failed', res.note)

    def test_a_search_still_throttled_says_so_and_what_to_do(self):
        def urlopen(req, timeout=None):
            raise urllib.error.HTTPError('u', 429, 'slow', {}, None)
        with mock.patch('urllib.request.urlopen', urlopen):
            res = geo.search('PPARG macrophage', live=True, i_have_network_permission=True)
        self.assertIn('No result is not the same as no such data', res.note)
        self.assertIn('NCBI is throttling (HTTP 429)', res.note)
        self.assertIn('one after another', res.note)


if __name__ == '__main__':
    unittest.main()


class BriefTests(unittest.TestCase):
    """The agents are told what to try before asking a person."""

    def test_a_request_comes_after_the_commands_that_could_have_fetched_it(self):
        from biosense.production import discovery as DISC
        req = DISC.build(project_id='ipsc_macrophage', runtime_mode='local_real_ai',
                         objective='Increase viable macrophage production.', public_data=True)
        brief = DISC.render_brief(req, loop_dir='runs/ai-x')
        self.assertIn('after the commands that could have fetched it', brief)
        self.assertIn('datasets from-papers --papers', brief)
        self.assertIn('should be run again (it maps them\n   now)', brief)
        self.assertIn('`UT_1`', brief)
        self.assertIn('not in a parallel burst', brief)


class HelpPanelTests(unittest.TestCase):
    """Five requests read as a wall to get through before doing anything else."""

    def test_each_request_is_a_line_with_its_details_folded_and_skipping_is_one_click(self):
        js = (K.ROOT / 'webapp' / 'discovery.js').read_text()
        self.assertIn('Each one is optional: the run did not wait for them', js)
        self.assertIn("'Details and answer'", js)
        self.assertIn('Continue without these', js)
        self.assertIn("note: 'Skipped by the person: carry on without it.'", js)
