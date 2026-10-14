"""The round on one page, and each specialist's section as a document.

A scientist asked for the protocol as a picture — the arms, the vessel, the
stages — and for the literature and the bioinformatics as Word or PDF from
where they read them on the run page. These build each document from a run's
real files and check what it says, that it says nothing the files did not,
and that both formats are real files a reader opens.
"""
import io
import json
import shutil
import tempfile
import unittest
import xml.dom.minidom
import zipfile
from pathlib import Path

from biosense import contracts as K
from biosense.bioinformatics import analyst as AN
from biosense.bioinformatics import execute as EX
from biosense.evidence import cli as EVCLI
from biosense.production import pdf as PDF
from biosense.production import section_report as SR
from biosense.production import visual_protocol as VP
from tests.test_analysis_plan import PlanCase
from tests.test_lab_protocol import PROTOCOL, DownloadTests, text_of


def docx_ok(data):
    z = zipfile.ZipFile(io.BytesIO(data))
    for name in z.namelist():
        if name.endswith('.xml') or name.endswith('.rels'):
            xml.dom.minidom.parseString(z.read(name))
    return z


class VisualProtocolTests(unittest.TestCase):
    def test_each_arm_is_the_control_plus_what_it_sets(self):
        cols, rows = VP.grid(PROTOCOL)
        self.assertEqual(['Expansion', 'Myeloid harvest'], [c['label'] for c in cols])
        control, high = rows
        myeloid = control['cells'][1]
        self.assertIn(('M-CSF & GM-CSF <mix> 50 ng/mL [current]', False), myeloid['lines'],
                      'the control runs the process as it is now, not the new dose')
        self.assertIsNone(myeloid['fill'])
        changed = high['cells'][1]
        self.assertIn(('M-CSF & GM-CSF <mix> 100 ng/mL  (control 50) [D]', True), changed['lines'])
        self.assertEqual(VP.CHANGED, changed['fill'])
        self.assertEqual([('—', False)], control['cells'][0]['lines'],
                         'an empty stage is shown empty, not filled in')

    def test_a_gap_is_shaded_as_a_gap_and_a_new_lever_goes_under_whole_run(self):
        p = json.loads(json.dumps(PROTOCOL))
        p['stages'][0]['parameters'].append(
            {'parameter_id': 'il3_ng_ml', 'label': 'IL-3', 'unit': 'ng/mL',
             'recommended_value': None, 'control_value': None, 'changed': False,
             'provenance': 'gap'})
        p['round_plan']['arms'][1]['setpoints']['rocki_hours'] = 24
        cols, rows = VP.grid(p)
        self.assertEqual('Whole run', cols[-1]['label'])
        gap = rows[0]['cells'][1]
        self.assertIn(('IL-3: GAP — decide before the round', True), gap['lines'])
        self.assertEqual(VP.GAP, gap['fill'])
        self.assertTrue(any('rocki_hours 24' in t for t, _ in rows[1]['cells'][-1]['lines']))

    def test_without_a_round_the_page_is_the_protocol_as_written(self):
        cols, rows = VP.grid(dict(PROTOCOL, round_plan=None))
        self.assertEqual(1, len(rows))
        self.assertIn(('M-CSF & GM-CSF <mix> 100 ng/mL [A]', False), rows[0]['cells'][1]['lines'])

    def test_one_landscape_page_in_both_formats_that_never_reads_as_approved(self):
        pdf = VP.tobytes_pdf(PROTOCOL, run_id='ai-x')
        self.assertIn(b'/MediaBox [0 0 842 595]', pdf, 'landscape A4')
        t = '\n'.join(PDF.text_of(pdf))
        self.assertIn('round 1 at a glance', t)
        self.assertIn('The stages, to scale', t)
        self.assertIn('2 arm(s) × 3 replicate(s) = 6 vessels', t)
        self.assertEqual(2, t.count('PROPOSED — NOT APPROVED.'))
        self.assertIn('page 1 of 1', t)
        z = docx_ok(VP.tobytes(PROTOCOL, run_id='ai-x'))
        body = z.read('word/document.xml').decode()
        self.assertIn('w:orient="landscape"', body)
        self.assertIn(f'w:fill="{VP.CHANGED}"', body, 'the change is shaded')
        self.assertIn('M-CSF &amp; GM-CSF &lt;mix&gt; 100 ng/mL  (control 50) [D]', body)


class Fixture(PlanCase):
    """A run directory holding what the specialists wrote."""

    def setUp(self):
        super().setUp()
        self.run = self.tmp / 'runs' / 'ai-20261012-abcdef'
        lit, bio = self.run / 'literature', self.run / 'bioinformatics'
        (lit / 'myeloid').mkdir(parents=True)
        bio.mkdir(parents=True)
        K.write_json_atomic(self.run / 'discovery_request.json',
                            {'project_id': 'ipsc_macrophage', 'title': 'GM-CSF swap',
                             'objective': 'More alveolar-like macrophages from iPSC.'})
        K.write_json_atomic(lit / 'discover.json', {'papers': [
            {'pmcid': 'PMC111', 'title': 'Alveolar macrophages need GM-CSF', 'pubYear': 2014,
             'isOpenAccess': 'Y', 'source_file': 'f.xml', 'found_by': ['europepmc']},
            {'pmid': '222', 'title': 'TGF-beta and lung macrophage identity', 'pubYear': 2017,
             'isOpenAccess': 'N'}]})
        K.write_json_atomic(lit / 'myeloid' / 'discover.json', {'papers': [
            {'pmcid': 'PMC111', 'title': 'Alveolar macrophages need GM-CSF', 'pubYear': 2014}]})
        (lit / 'insights.md').write_text(
            '# Leads\n\n- **GM-CSF** is required for alveolar identity (PMC111).\n'
            '- TGF-beta acts with it.\n\n| Factor | Dose |\n|---|---|\n| GM-CSF | 10 ng/mL |\n')
        (lit / 'myeloid' / 'insights.md').write_text('M-CSF at 100 ng/mL in the harvest stage.\n')
        K.write_json_atomic(self.run / 'quantified_hypothesis.json',
                            EVCLI.build_hypothesis(dict(EVCLI.TEMPLATE,
                                                        effects=EVCLI.TEMPLATE['effects'][:1]),
                                                   project_id='ipsc_macrophage'))
        plan = self.a_plan(plan_id='plan-01')
        K.write_json_atomic(self.run / 'analysis_plan.json', plan)
        result = EX.execute(plan)
        K.write_json_atomic(self.run / 'analysis_result.json', result)
        draft = dict(AN.TEMPLATE, analysis_ref=str(self.run / 'analysis_result.json'),
                     question=plan['question'])
        K.write_json_atomic(bio / 'interpretation.json', AN.interpret(draft, result=result))
        (self.run / 'analysis_result_broken.json').write_text('{"analysis_id": "x"}')
        (bio / 'insights.md').write_text('## Datasets\n\nThe FACS table is the only one in reach.\n')
        K.write_json_atomic(bio / 'gene_info.json', {'genes': [
            {'symbol': 'PPARG', 'ensembl_id': 'ENSG00000132170', 'description': 'nuclear receptor',
             'interaction_partners': ['RXRA']}]})
        K.write_json_atomic(bio / 'paper_datasets.json', {'papers': [
            {'paper': 'PMID:222', 'datasets': [{'accession': 'GSE46080', 'title': 'Pparg AM',
                                                'via': ['GEO links this series to the paper']}]}]})


class LiteratureReportTests(Fixture):
    def test_papers_once_each_with_who_found_them(self):
        rows = SR.papers(self.run)
        self.assertEqual(['PMC111', 'PMID:222'], [r['id'] for r in rows])
        self.assertEqual({'europepmc', 'myeloid'}, rows[0]['found_by'])
        self.assertTrue(rows[0]['read'])

    def test_the_document_says_what_each_part_is(self):
        t = '\n'.join(text_of(SR.tobytes('literature', self.run, 'docx', run_id='abcdef')))
        self.assertIn('Literature — GM-CSF swap', t)
        self.assertIn('2 paper(s) found; 1 read in full', t)
        self.assertIn('The evidence the hypotheses rest on', t)
        self.assertIn('Leads, not findings', t)
        # The emphasis is kept as weight: **GM-CSF** is its own bold run.
        self.assertIn('GM-CSF\n is required for alveolar identity (PMC111).', t,
                      'markdown emphasis as bold, words kept')
        body = zipfile.ZipFile(io.BytesIO(SR.tobytes('literature', self.run, 'docx',
                                                     run_id='abcdef'))).read(
            'word/document.xml').decode()
        self.assertRegex(body, r'<w:b/>.{0,80}>GM-CSF</w:t>', 'the bold run is bold')
        self.assertIn('Stage: myeloid', t)
        self.assertIn('10 ng/mL', t, 'a table in the notes stays a table')
        self.assertNotIn('**', t)
        pdf = ' '.join(PDF.text_of(SR.tobytes('literature', self.run, 'pdf', run_id='abcdef')))
        self.assertIn('GM-CSF is required for alveolar identity (PMC111).', pdf,
                      'in the PDF the bold run sits inline in its sentence')


class BioinformaticsReportTests(Fixture):
    def test_a_plan_with_its_result_and_interpretation(self):
        data = SR.tobytes('bioinformatics', self.run, 'pdf', run_id='abcdef')
        t = ' '.join(PDF.text_of(data))      # a phrase may wrap across lines
        self.assertIn('plan-01 — Does the higher M-CSF condition raise CD14+', t)
        self.assertIn('Tool: cytometry.population_comparison (population_comparison)', t)
        self.assertIn('Result.', t)
        self.assertIn('What it means here.', t, 'the interpretation is paired by its path')
        self.assertIn('Datasets analysed', t)
        self.assertIn('facs-demo', t)
        self.assertIn('Datasets found from the papers', t)
        self.assertIn('GSE46080', t)
        self.assertIn('Genes looked up', t)
        self.assertIn('never a direction of effect', t)
        self.assertIn('analysis_result_broken.json', t, 'what does not parse is named, not mended')

    def test_an_empty_run_says_so(self):
        empty = self.tmp / 'runs' / 'ai-empty'
        empty.mkdir(parents=True)
        t = '\n'.join(text_of(SR.tobytes('literature', empty)))
        self.assertIn('wrote nothing under literature/', t)
        t = '\n'.join(text_of(SR.tobytes('bioinformatics', empty)))
        self.assertIn('No analysis was planned or run', t)
        with self.assertRaisesRegex(K.ContractError, 'section must be one of'):
            SR.tobytes('protocol', empty)


class ExportDownloadTests(DownloadTests):
    def get(self, path):
        import urllib.request
        import urllib.error
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{self.port}{path}', timeout=30) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def test_the_round_at_a_glance_and_each_section_download(self):
        code, h, body = self.get(f'/api/discovery/{self.rid}/protocol?format=pdf&view=plan')
        self.assertEqual(200, code)
        self.assertIn('round-at-a-glance.pdf', h['Content-Disposition'])
        self.assertIn('round 1 at a glance', '\n'.join(PDF.text_of(body)))
        for section in ('literature', 'bioinformatics'):
            for fmt in ('pdf', 'docx'):
                code, h, body = self.get(
                    f'/api/discovery/{self.rid}/export?section={section}&format={fmt}')
                self.assertEqual(200, code, (section, fmt, body[:200]))
                self.assertIn(f'{section}-', h['Content-Disposition'])
        code, _, _ = self.get(f'/api/discovery/{self.rid}/export?section=secrets&format=pdf')
        self.assertEqual(400, code)
        code, _, _ = self.get('/api/discovery/0000000000000000/export?section=literature')
        self.assertEqual(404, code)

    def test_the_page_offers_each_export_where_it_is_read(self):
        html = (K.ROOT / 'webapp' / 'console.html').read_text()
        self.assertEqual(1, html.count('data-section="literature"'))
        self.assertEqual(2, html.count('data-section="bioinformatics"'))
        js = (K.ROOT / 'webapp' / 'discovery.js').read_text()
        self.assertIn('/export?section=${sec}&format=${fmt}', js)
        self.assertIn("'Round at a glance (PDF)', '&view=plan'", js)


if __name__ == '__main__':
    unittest.main()
