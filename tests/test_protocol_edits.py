"""What a person asks to change in the written protocol, after the run.

The run wrote one protocol for one vessel; the person wants the bench copy
laid out for their plate, with their seeding density and a note of their own,
without the alternates appendix. That is editing a document, not science: the
edits are checked like a design choice, laid over every export, named as the
person's, and the run's own files are untouched.
"""
import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from biosense import contracts as K
from biosense import projects as PJ
from biosense.evidence import cli as EVCLI
from biosense.evidence import protocol_edits as PE
from biosense.production import ask as ASK
from biosense.production import lab_protocol as LP
from biosense.production import pdf as PDF
from tests.test_lab_protocol import PROTOCOL, DownloadTests, text_of

VALUE = {'parameter_id': 'seed_density', 'stage_id': 'expansion', 'value': 0.3,
         'unit': '1e6 cells/mL', 'timing': 'at seeding',
         'rationale': 'the density this lab seeds at in a 6-well well',
         'derived_from': ["the person's own practice"], 'confidence': 'low'}
DRAFT = {'culture_format': 'six_well', 'working_volume_ml': 2,
         'values': [VALUE, {'parameter_id': 'mcsf_ng_ml', 'stage_id': 'myeloid', 'value': 80,
                            'unit': 'ng/mL', 'rationale': 'what the lab has run before',
                            'derived_from': ['lab practice'], 'confidence': 'low'}],
         'notes': [{'stage_id': 'myeloid', 'text': 'Record the X-VIVO 15 lot on the run sheet.'}],
         'omit': ['alternates']}


def build(draft=DRAFT, **kw):
    return PE.build(draft, project=PJ.load('ipsc_macrophage'), request='lay it out for my plate',
                    run_id='ai-x', requested_by='Ana', **kw)


class BuildTests(unittest.TestCase):
    def test_a_checked_document_names_the_person_and_the_vessel(self):
        d = build()
        self.assertEqual(('protocol_edits', 'Ana', 'six_well', 2.0),
                         (d['kind'], d['requested_by'], d['culture_format'],
                          d['working_volume_ml']))
        v = d['values'][0]
        self.assertEqual(('seed_density', 'Seed density', 0.3, 'Expansion', 'Ana'),
                         (v['parameter_id'], v['label'], v['value'], v['stage_label'],
                          v['requested_by']))
        self.assertEqual('at seeding', v['timing'])
        self.assertEqual('Myeloid harvest' if 'harvest' in d['values'][1]['stage_label']
                         else d['values'][1]['stage_label'], d['values'][1]['stage_label'])
        self.assertEqual('Ana', d['notes'][0]['requested_by'])
        self.assertEqual(['alternates'], d['omit'])
        self.assertIn('Nothing here is approved', d['does_not_change'])

    def test_a_value_is_checked_like_a_design_choice(self):
        with self.assertRaisesRegex(K.ContractError, 'give the value in that unit'):
            build(dict(DRAFT, values=[dict(VALUE, unit='1e6 cells/cm2')]))
        with self.assertRaisesRegex(K.ContractError, 'does not expose'):
            build(dict(DRAFT, values=[dict(VALUE, parameter_id='il7_ng_ml')]))
        with self.assertRaisesRegex(K.ContractError, 'needs derived_from'):
            build(dict(DRAFT, values=[dict(VALUE, derived_from=[])]))
        with self.assertRaisesRegex(K.ContractError, 'project maximum'):
            build(dict(DRAFT, values=[dict(VALUE, value=500)]))
        with self.assertRaisesRegex(K.ContractError, "stage 'harvest' is not"):
            build(dict(DRAFT, values=[dict(VALUE, stage_id='harvest')]))
        with self.assertRaisesRegex(K.ContractError, 'set twice'):
            build(dict(DRAFT, values=[VALUE, VALUE]))

    def test_what_is_not_a_vessel_a_value_or_a_section_is_refused(self):
        with self.assertRaisesRegex(K.ContractError, 'not a culture format'):
            build(dict(DRAFT, culture_format='petri'))
        with self.assertRaisesRegex(K.ContractError, 'sections that do not exist: steps'):
            build(dict(DRAFT, omit=['steps']))
        with self.assertRaisesRegex(K.ContractError, 'may not claim approval'):
            build(dict(DRAFT, notes=[{'text': 'This protocol is approved by the PI.'}]))
        with self.assertRaisesRegex(K.ContractError, 'changes nothing'):
            build({})
        with self.assertRaisesRegex(K.ContractError, 'out of range'):
            build(dict(DRAFT, working_volume_ml=0))

    def test_the_summary_is_one_line_per_thing_changed(self):
        s = PE.summary(build())
        self.assertEqual('six_well', s['culture_format'])
        self.assertEqual('seed_density = 0.3 1e6 cells/mL (Expansion)', s['values'][0])
        self.assertEqual((1, ['alternates']), (s['notes'], s['omit']))


class ApplyTests(unittest.TestCase):
    def test_a_value_replaces_the_line_and_keeps_what_it_was(self):
        p = PE.apply(PROTOCOL, build())
        myeloid = next(s for s in p['first_pass']['stages'] if s['stage_id'] == 'myeloid')
        mcsf = next(i for i in myeloid['inducers'] if i['parameter_id'] == 'mcsf_ng_ml')
        self.assertEqual((80, 100, 'design_choice', 'D', 'Ana', 'set'),
                         (mcsf['value'], mcsf['was'], mcsf['provenance'], mcsf['letter'],
                          mcsf['requested_by'], mcsf['action']))
        self.assertEqual(['lab practice'], mcsf['refs'])
        seed = next(i for i in p['first_pass']['physical'] if i['parameter_id'] == 'seed_density')
        self.assertEqual(('add', 'expansion', 0.3, None), (seed['action'], seed['stage_id'],
                                                          seed['value'], seed['was']))
        self.assertEqual('six_well', p['culture_format'])
        self.assertEqual('Ana', p['edits']['requested_by'])
        self.assertEqual('Record the X-VIVO 15 lot on the run sheet.', p['edits']['notes'][0]['text'])
        # The run's own payload is untouched.
        self.assertNotIn('edits', PROTOCOL)
        self.assertEqual(100, PROTOCOL['stages'][0]['parameters'][0]['recommended_value'])

    def test_nothing_to_apply_is_the_same_payload(self):
        self.assertIs(PROTOCOL, PE.apply(PROTOCOL, None))


class DocumentTests(unittest.TestCase):
    def setUp(self):
        self.p = PE.apply(dict(PROTOCOL, alternate_plans={'items': [{'alt_id': 'ALT-1'}]}),
                          build())

    def test_the_edits_are_on_the_page_as_the_person_s(self):
        t = text_of(LP.tobytes(self.p, run_id='ai-x'))
        joined = '\n'.join(t)
        self.assertIn('This copy carries changes Ana asked for after the run', joined)
        self.assertIn('80 ng/mL (was 100)', t)
        self.assertIn('  [D · lab practice · asked for by Ana]', t)
        self.assertIn('Record the X-VIVO 15 lot on the run sheet.', t)
        self.assertIn('  [P · Ana]', t)
        self.assertIn('Changes requested after the run', t)
        self.assertIn(PE.DOES_NOT_CHANGE, t)
        self.assertTrue(any(x.startswith('P — the person') for x in t), 'the legend has P')
        self.assertNotIn('Alternate and extended plans', t, 'omitted at the person\'s request')
        self.assertIn('Set Seed density', t)
        self.assertEqual(2, sum('NOT APPROVED' in x for x in t))

    def test_the_pdf_carries_the_same(self):
        t = '\n'.join(PDF.text_of(LP.tobytes_pdf(self.p, run_id='ai-x')))
        self.assertIn('asked for by Ana', t)
        self.assertIn('Changes requested after the run', t)

    def test_a_section_that_cannot_be_left_out_is_not(self):
        p = PE.apply(PROTOCOL, build(dict(DRAFT, omit=['testing', 'letters', 'round'])))
        t = '\n'.join(text_of(LP.tobytes(p)))
        self.assertNotIn('What this run is testing', t)
        self.assertNotIn('What the letters mean', t)
        self.assertNotIn('V1', t)
        self.assertIn('Before you run this: what is not decided', t)
        self.assertIn('The protocol, stage by stage', t)
        self.assertIn('Reviewed by', t)


class ChatTests(unittest.TestCase):
    def test_the_kind_is_a_turn_in_the_same_conversation(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        q = ASK.start(d, 'lay it out for a 6-well plate', asked_by='Ana', kind='protocol')
        self.assertEqual('protocol', q['kind'])

    def test_the_orchestrator_is_told_to_write_one_file_and_change_no_science(self):
        text = PE.framed('6-well at 2 mL, seed at 0.3e6/mL', loop_dir='runs/ai-x',
                         project_id='ipsc_macrophage', python='py', fresh=True)
        self.assertIn('runs/ai-x/protocol_edits.draft.json', text)
        self.assertIn('py -m biosense.evidence.cli protocol-edits --project ipsc_macrophage', text)
        self.assertIn('--run-id ai-x', text)
        self.assertIn('do not change the protocol summary', text)
        self.assertIn('six_well, cero, stirred_tank', text)
        self.assertIn('Never convert a per-cm2 density', text)
        self.assertIn('<<<6-well at 2 mL, seed at 0.3e6/mL>>>', text)
        again = PE.framed('also add a note', loop_dir='runs/ai-x', project_id='p',
                          existing=build())
        self.assertIn('Edits already written for this run', again)
        self.assertIn('seed_density = 0.3', again)


class CliTests(unittest.TestCase):
    def cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = EVCLI.main(list(argv))
        return code, out.getvalue()

    def test_the_template_and_the_command(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        code, out = self.cli('template', 'protocol-edits')
        self.assertEqual(0, code)
        shape = json.loads(out)
        self.assertEqual('seed_density', shape['values'][0]['parameter_id'])
        (d / 'draft.json').write_text(json.dumps(DRAFT))
        code, out = self.cli('protocol-edits', '--project', 'ipsc_macrophage', '--draft',
                             str(d / 'draft.json'), '--out', str(d / PE.NAME), '--run-id', 'ai-x',
                             '--request', 'my plate', '--requested-by', 'Ana')
        self.assertEqual(0, code, out)
        self.assertIn('seed_density = 0.3 1e6 cells/mL (Expansion)', out)
        self.assertEqual('Ana', PE.load(d)['requested_by'])
        (d / 'bad.json').write_text(json.dumps(dict(DRAFT, values=[dict(VALUE, unit='cells/cm2')])))
        code, out = self.cli('protocol-edits', '--project', 'ipsc_macrophage', '--draft',
                             str(d / 'bad.json'), '--out', str(d / 'x.json'))
        self.assertEqual(2, code)


class ServerTests(DownloadTests):
    """Given directly on the run page, then carried by every export."""

    def post(self, path, body):
        import urllib.request
        req = urllib.request.Request(f'http://127.0.0.1:{self.port}{path}',
                                     data=json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json'}, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read())
        except __import__('urllib.error').error.HTTPError as e:
            return e.code, json.loads(e.read())

    def get(self, path):
        import urllib.request
        with urllib.request.urlopen(f'http://127.0.0.1:{self.port}{path}', timeout=30) as r:
            return r.headers, r.read()

    def test_the_edits_are_checked_written_exported_and_removable(self):
        code, d = self.post(f'/api/discovery/{self.rid}/protocol-edits',
                            dict(DRAFT, values=[dict(VALUE, unit='cells/cm2')]))
        self.assertEqual(400, code)
        self.assertIn('surface density', d['error'])
        code, d = self.post(f'/api/discovery/{self.rid}/protocol-edits',
                            dict(DRAFT, request='my plate'))
        self.assertEqual(200, code, d)
        self.assertEqual('seed_density = 0.3 1e6 cells/mL (Expansion)', d['summary']['values'][0])
        _, snap = self.get(f'/api/discovery/{self.rid}')
        self.assertEqual('six_well', json.loads(snap)['protocol_edits']['culture_format'])
        h, body = self.get(f'/api/discovery/{self.rid}/protocol?format=pdf')
        t = '\n'.join(PDF.text_of(body))
        self.assertIn('asked for by', t)
        self.assertIn('In a 6-well plate', t, 'laid out for the vessel asked for in the edits')
        self.assertIn('Working volume', t)
        self.assertIn('2 mL', t)
        h, body = self.get(f'/api/discovery/{self.rid}/protocol?format=docx&edits=none')
        self.assertNotIn('asked for by', '\n'.join(text_of(body)))
        _, body = self.get(f'/api/discovery/{self.rid}/translate?to=six_well')
        self.assertEqual(2.0, json.loads(body)['working_volume_ml'],
                         'the volume from the edits, when the vessel is theirs')
        code, d = self.post(f'/api/discovery/{self.rid}/protocol-edits', {'clear': True})
        self.assertEqual((200, None), (code, d['protocol_edits']))
        h, body = self.get(f'/api/discovery/{self.rid}/protocol?format=pdf')
        self.assertNotIn('asked for by', '\n'.join(PDF.text_of(body)))


if __name__ == '__main__':
    unittest.main()
