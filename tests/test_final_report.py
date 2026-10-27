"""The final report, and the narrative the communication agent may write for it.

What matters here is the honesty of the assembled document, not its looks: the
narrative is refused when it carries a number the protocol does not, the
report renders with and without one, says PROPOSED — NOT APPROVED at both
ends, and the process figure draws a gap as a gap. The CLI writes real files
from a run directory's own artifacts.
"""
import json
import shutil
import tempfile
import unittest
import xml.dom.minidom
from pathlib import Path

from biosense import contracts as K
from biosense.evidence import communication as CM
from biosense.production import ask as ASK
from biosense.production import final_report as FR
from biosense.production import pdf as PDF
from biosense.production.cli import main as production_cli
from tests.test_lab_protocol import PROTOCOL, text_of
from tests.test_section_exports import docx_ok

DRAFT = {
    'audience': 'the lab meeting deciding whether to run round 1',
    'executive_summary': [
        'This run asked how to raise viable macrophage yield from iPSC and recommends '
        'raising M-CSF to 100 ng/mL through the harvest phase, an adapted value resting '
        'on cited claims; the GM-CSF dose is still a gap.'],
    'stages': [
        {'stage_id': 'expansion',
         'headline': 'Grow the iPSC to the number the round needs.',
         'at_the_bench': 'Aggregates in suspension; feed on schedule and check the medium '
                         'stays clear. Nothing changes in this stage: it runs as the '
                         'process runs now.'},
        {'stage_id': 'myeloid',
         'headline': 'The stage this round changes: the cytokine dose goes up.',
         'at_the_bench': 'From day 4 the vessels split into 2 arms of 3 replicates; the '
                         'changed arm gets M-CSF at 100 ng/mL against the control at 50. '
                         'Harvest the floating monocytes at each feed.',
         'watch_for': 'A yield fall across both arms points at the process, not the arm.'}],
    'closing': 'Round 1 can say whether the raised dose moves yield in this vessel; it '
               'cannot say the mechanism, and the GM-CSF gap stays open until somebody '
               'decides it.',
}


def narrative(protocol=PROTOCOL, draft=DRAFT):
    return CM.build(draft, protocol=protocol, project_id='ipsc_macrophage',
                    run_id='ai-x', created_by='test')


class CommunicationContractTests(unittest.TestCase):
    def test_a_valid_draft_builds_and_passes_its_schema(self):
        doc = narrative()
        self.assertEqual('communication', doc['kind'])
        self.assertEqual(['expansion', 'myeloid'], [s['stage_id'] for s in doc['stages']])
        self.assertEqual([], K.schema_errors('communication', doc))

    def test_a_number_the_protocol_does_not_carry_is_refused_by_name(self):
        bad = dict(DRAFT, closing='Raise the dose to 77 ng/mL next time and yield doubles.')
        with self.assertRaises(K.ContractError) as ctx:
            narrative(draft=bad)
        self.assertIn('77', str(ctx.exception))
        self.assertIn('does not carry', str(ctx.exception))

    def test_numbers_the_protocol_carries_pass_including_derived_ones(self):
        """50 and 100 are protocol values, 25 the total days, 6 the vessels
        (2 arms x 3 replicates), and small counting numbers are counting."""
        ok = dict(DRAFT, closing='Over 25 days the 6 vessels (2 arms of 3) compare 100 '
                                 'against 50 ng/mL; round 1 settles direction only.')
        self.assertTrue(narrative(draft=ok))

    def test_citations_and_dois_keep_their_digits(self):
        ok = dict(DRAFT, closing='The dose rests on PMC12345 and 10.1038/s41586-xyz; '
                                 'H01 carries the claim and what it stands on.')
        self.assertTrue(narrative(draft=ok))

    def test_overclaiming_and_approval_claims_are_refused(self):
        for text, word in (('This protocol guarantees a higher yield for every later run '
                            'of this process at any scale.', 'overclaims'),
                           ('This protocol is approved and goes to the bench on Monday '
                            'morning with the usual team.', 'approval')):
            with self.assertRaises(K.ContractError) as ctx:
                narrative(draft=dict(DRAFT, executive_summary=[text]))
            self.assertIn(word, str(ctx.exception))

    def test_a_caption_for_a_stage_the_protocol_does_not_have_is_refused(self):
        bad = dict(DRAFT, stages=[dict(DRAFT['stages'][0], stage_id='freezer')])
        with self.assertRaises(K.ContractError) as ctx:
            narrative(draft=bad)
        self.assertIn('freezer', str(ctx.exception))
        self.assertIn('expansion', str(ctx.exception))

    def test_the_key_points_are_optional_and_checked_like_every_other_field(self):
        """They lead the recommendation on the page, so they are the sentences
        most likely to be read and the ones an invented number would do most
        damage in. Same rule: a number must already stand in the protocol."""
        self.assertIsNone(narrative()['key_points'], 'a write-up without them validates')
        ok = dict(DRAFT, key_points=[
            'Raise M-CSF to 100 ng/mL through the myeloid harvest stage.',
            'Everything else runs as the process runs now.',
            'The GM-CSF dose is still undecided and blocks the wet lab.'])
        doc = narrative(draft=ok)
        self.assertEqual(3, len(doc['key_points']))
        self.assertEqual([], K.schema_errors('communication', doc))
        bad = dict(DRAFT, key_points=['Raise M-CSF to 77 ng/mL in the harvest stage.'])
        with self.assertRaises(K.ContractError) as ctx:
            narrative(draft=bad)
        self.assertIn('key_points[0]', str(ctx.exception))
        self.assertIn('77', str(ctx.exception))

    def test_the_agents_key_points_lead_the_recommendation_in_place_of_ours(self):
        from biosense.production import plain_plan as PP
        doc = narrative(draft=dict(DRAFT, key_points=[
            'Raise M-CSF to 100 ng/mL through the myeloid harvest stage.',
            'Everything else runs as the process runs now.']))
        points = PP.key_points(PROTOCOL, doc)
        self.assertEqual('communication_agent', points['source'])
        self.assertEqual(doc['key_points'], points['points'])
        # And the report prints them where a reader who stops early sees them.
        t = '\n'.join(text_of(FR.tobytes(PROTOCOL, narrative=doc)))
        self.assertIn('In short', t)
        self.assertLess(t.index('Raise M-CSF to 100 ng/mL through the myeloid harvest stage.'),
                        t.index('This run asked how to raise viable macrophage yield'))

    def test_the_write_up_is_the_fifth_kind_of_ask_turn(self):
        self.assertIn('report', ASK.KINDS)


class FinalReportTests(unittest.TestCase):
    def test_without_a_narrative_the_summary_is_assembled_not_phrased(self):
        t = '\n'.join(text_of(FR.tobytes(PROTOCOL, run_id='ai-x')))
        self.assertIn('final report', t)
        self.assertIn('Executive summary', t)
        self.assertIn('1 hypothesis(es) weighed, 1 adopted', t)
        self.assertIn('Stage by stage, at the bench', t)
        self.assertIn('M-CSF & GM-CSF <mix> 100 ng/mL [A] (was 50)', t)
        self.assertNotIn('communication agent', t.split('How to read')[0])

    def test_the_narrative_rides_beside_the_protocol_rows_never_instead(self):
        t = '\n'.join(text_of(FR.tobytes(PROTOCOL, run_id='ai-x', narrative=narrative())))
        self.assertIn('Written for the lab meeting', t)
        self.assertIn('Harvest the floating monocytes at each feed.', t)
        self.assertIn('Watch for: A yield fall across both arms', t)
        # The validated row still prints beside the prose.
        self.assertIn('M-CSF & GM-CSF <mix> 100 ng/mL [A] (was 50)', t)
        self.assertIn('Where this leaves the project', t)

    def test_both_formats_are_real_files_that_never_read_as_approved(self):
        data = FR.tobytes(PROTOCOL, run_id='ai-x', narrative=narrative())
        t = '\n'.join(text_of(data))
        docx_ok(data)
        self.assertGreaterEqual(t.count('PROPOSED — NOT APPROVED.'), 2)
        self.assertIn('Approval', t)
        pdf = FR.tobytes_pdf(PROTOCOL, run_id='ai-x', narrative=narrative())
        pt = '\n'.join(PDF.text_of(pdf))
        self.assertIn('Executive summary', pt)
        self.assertIn('Stage by stage, at the bench', pt)
        self.assertGreaterEqual(pt.count('PROPOSED'), 2)

    def test_the_chapters_come_from_the_runs_own_files(self):
        run = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, run, ignore_errors=True)
        K.write_json_atomic(run / 'discovery_request.json',
                            {'project_id': 'ipsc_macrophage', 'title': 'yield',
                             'objective': 'more macrophages'})
        (run / 'literature').mkdir()
        K.write_json_atomic(run / 'literature' / 'discover.json', {'papers': [
            {'pmcid': 'PMC111', 'title': 'Macrophages need M-CSF', 'pubYear': 2014}]})
        t = '\n'.join(text_of(FR.tobytes(PROTOCOL, run_dir=run, run_id='ai-x')))
        self.assertIn('Macrophages need M-CSF', t)
        self.assertIn('Bioinformatics', t)


class FlowSvgTests(unittest.TestCase):
    def test_the_same_protocol_twice_is_the_same_bytes_and_valid_xml(self):
        svg = FR.flow_svg(PROTOCOL)
        self.assertEqual(svg, FR.flow_svg(PROTOCOL))
        xml.dom.minidom.parseString(svg)
        xml.dom.minidom.parseString(FR.flow_svg(PROTOCOL, dark=True))

    def test_it_draws_what_the_protocol_carries_and_nothing_it_does_not(self):
        p = json.loads(json.dumps(PROTOCOL))
        p['stages'][0]['parameters'].append(
            {'parameter_id': 'il3_ng_ml', 'label': 'IL-3', 'unit': 'ng/mL',
             'recommended_value': None, 'control_value': None, 'changed': False,
             'provenance': 'gap'})
        svg = FR.flow_svg(p)
        self.assertIn('Expansion', svg)
        self.assertIn('Myeloid harvest', svg)
        self.assertIn('NOT APPROVED', svg)
        self.assertIn('GAP', svg)
        self.assertIn('nothing set by this run', svg)


class CliTests(unittest.TestCase):
    def test_final_report_writes_the_files_from_a_run_directory(self):
        run = Path(tempfile.mkdtemp()) / 'ai-20261014-abcdef'
        run.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, run.parent, ignore_errors=True)
        K.write_json_atomic(run / 'protocol_summary.json', PROTOCOL)
        K.write_json_atomic(run / 'communication.json', narrative())
        rc = production_cli(['final-report', '--run', str(run)])
        self.assertEqual(0, rc)
        docx_ok((run / 'final_report.docx').read_bytes())
        self.assertTrue((run / 'final_report.pdf').read_bytes().startswith(b'%PDF'))
        svg = (run / 'process_flow.svg').read_text(encoding='utf-8')
        self.assertIn('NOT APPROVED', svg)
        self.assertTrue((run / 'process_flow.dark.svg').is_file())
        t = '\n'.join(text_of((run / 'final_report.docx').read_bytes()))
        self.assertIn('Harvest the floating monocytes at each feed.', t)

    def test_without_a_protocol_summary_it_refuses_with_the_reason(self):
        empty = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, empty, ignore_errors=True)
        rc = production_cli(['final-report', '--run', str(empty)])
        self.assertNotEqual(0, rc)


if __name__ == '__main__':
    unittest.main()
