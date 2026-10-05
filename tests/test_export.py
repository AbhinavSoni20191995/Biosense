"""The per-hypothesis export: one claim, and everything needed to check it.

A hypothesis is the unit people argue about, and the question it has to survive
is always "where did that number come from?". These tests are mostly about the
two ways an export could answer that question dishonestly: by copying private
data into a shareable directory, and by carrying prose whose numbers no longer
match the structured facts.
"""
import copy
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from biosense import contracts as K
from biosense.data import roots as DR
from biosense.evidence import estimates as E
from biosense.evidence import export as X
from biosense.evidence import narrative as NR
from biosense.evidence import residual as RS

BENCH = K.ROOT / 'benchmarks' / 'public' / 'macrophage_mcsf_demo' / 'benchmark.json'

PRIVATE_EVIDENCE = {'evidence_class': 'expert_knowledge', 'stance': 'supportive',
                    'summary': 'the process lead recalls 50 ng/mL as the practical ceiling',
                    'strength': 'weak', 'ref': 'EK-002', 'visibility': 'private'}


def a_hypothesis():
    return copy.deepcopy(K.read_json(BENCH)['hypotheses'][0])


def datasets():
    return copy.deepcopy(K.read_json(BENCH).get('datasets') or [])


class ExportCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._old = os.environ.get(DR.PRIVATE_ENV)
        os.environ[DR.PRIVATE_ENV] = str(self.tmp / 'private')
        DR.ensure_roots()
        self.out = self.tmp / 'ws'

    def tearDown(self):
        if self._old is None:
            os.environ.pop(DR.PRIVATE_ENV, None)
        else:
            os.environ[DR.PRIVATE_ENV] = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def read(self, name):
        return (self.out / name).read_text()

    def load(self, name):
        return json.loads(self.read(name))


class ContentsTests(ExportCase):
    def test_it_writes_the_claim_the_evidence_the_prose_and_the_numbers(self):
        info = X.write(a_hypothesis(), self.out, datasets=datasets())
        for name in ('hypothesis.json', 'evidence.md', 'narrative.md', 'numbers.json',
                     'datasets.json', 'limitations.md', 'MANIFEST.json'):
            self.assertTrue((self.out / name).is_file(), name)
        self.assertTrue(info['privacy']['safe_to_publish'])

    def test_a_dataset_appears_by_reference_and_never_as_a_copy(self):
        """The checksum is the point: a reader can confirm they are looking at
        the same file without the export becoming a second copy of it."""
        X.write(a_hypothesis(), self.out, datasets=datasets())
        rows = self.load('datasets.json')
        self.assertTrue(rows)
        for r in rows:
            self.assertIn('dataset_id', r)
            self.assertIn('reference only', r['note'])
        self.assertFalse(self.load('MANIFEST.json')['contains_dataset_contents'])

    def test_every_number_carries_its_estimate_type(self):
        """Flattened on purpose: a reader checking one figure should not have to
        know which nested structure it came from to learn what kind of number
        it is."""
        X.write(a_hypothesis(), self.out, datasets=datasets())
        rows = self.load('numbers.json')
        self.assertTrue(rows)
        for r in rows:
            self.assertTrue(r['estimate_type'], r)
        kinds = {r['estimate_type'] for r in rows}
        self.assertIn('simulated', kinds)
        self.assertIn('derived', kinds)

    def test_the_manifest_checksums_every_file_it_wrote(self):
        X.write(a_hypothesis(), self.out, datasets=datasets())
        man = self.load('MANIFEST.json')
        for name, digest in man['files'].items():
            self.assertEqual(digest, K.sha256_file(self.out / name), name)

    def test_the_narrative_ships_with_the_facts_it_was_checked_against(self):
        """Prose alone would be a claim; the fact table is what makes it an
        auditable rendering of structured values."""
        X.write(a_hypothesis(), self.out)
        text = self.read('narrative.md')
        self.assertIn('The facts this was checked against', text)
        self.assertIn('estimate type', text)

    def test_prose_that_does_not_match_its_facts_is_refused(self):
        h = a_hypothesis()
        h['statement'] = h['statement'] + ' Yield rose by 9999 percent.'
        with self.assertRaises(K.ContractError) as e:
            X.write(h, self.out)
        self.assertIn('does not match its facts', str(e.exception))

    def test_without_residuals_it_says_the_loop_has_not_closed(self):
        X.write(a_hypothesis(), self.out)
        self.assertIn('The loop has not closed', self.read('limitations.md'))
        self.assertNotIn('residuals.json', X.plan(a_hypothesis())['contents'])


class PrivacyTests(ExportCase):
    def test_a_public_export_with_private_lineage_refuses_and_writes_nothing(self):
        """Refuses rather than anonymising: a frequency from an unpublished
        experiment is still that experiment's result."""
        h = a_hypothesis()
        h['evidence'].append(PRIVATE_EVIDENCE)
        plan = X.plan(h, policy='public_safe')
        self.assertFalse(plan['may_write'])
        self.assertIn('refuses rather than attempting to anonymise',
                      plan['privacy']['refusal_reason'])
        with self.assertRaises(K.ContractError):
            X.write(h, self.out, policy='public_safe')
        self.assertFalse(self.out.exists(), 'a refused export must leave nothing behind')

    def test_the_same_hypothesis_exports_under_the_private_policy(self):
        h = a_hypothesis()
        h['evidence'].append(PRIVATE_EVIDENCE)
        info = X.write(h, self.out, policy='private')
        self.assertTrue((self.out / 'MANIFEST.json').is_file())
        self.assertFalse(info['privacy']['safe_to_publish'])
        self.assertIn('Private lineage', self.read('limitations.md'))
        self.assertIn('EK-002', self.read('limitations.md'))

    def test_a_private_workspace_defaults_outside_the_published_tree(self):
        d = X.workspace_dir('HYP-1', 'private')
        self.assertTrue(str(d).startswith(str(DR.private_root())))
        self.assertFalse(str(d).startswith(str(X.PUBLIC_DIR)))

    def test_planning_does_not_create_anything(self):
        """A refusal should be a value to show someone, not a directory to clean
        up afterwards."""
        X.plan(a_hypothesis(), policy='public_safe')
        self.assertFalse(self.out.exists())


class WithResidualsTests(ExportCase):
    def residual(self, source='wet_lab', n=3):
        pred = E.value(68.0, 'simulated')
        c = RS.commit(source='biosimulator', readout='monocyte_gate_pct', value=pred, unit='%',
                      model_id='ipsc_monocyte_v1', committed_at='2026-10-01T09:00:00Z')
        m = RS.measurement(run_id='RUN-014', source=source, measured_at='2026-10-04T16:00:00Z',
                           n=n)
        return RS.residual('RES-001', 'monocyte_gate_pct', '%', commitment=c, measurement_=m,
                           predicted=pred, measured=E.value(64.9, 'measured', n=n))

    def test_residuals_are_written_with_their_summary(self):
        r = self.residual()
        X.write(a_hypothesis(), self.out, residuals=[r])
        d = self.load('residuals.json')
        self.assertEqual(1, len(d['residuals']))
        self.assertIn('summary', d)
        self.assertIn('Against measurement', self.read('limitations.md'))

    def test_a_standin_residual_is_exported_but_never_as_model_evidence(self):
        r = self.residual(source='synthetic_standin')
        X.write(a_hypothesis(), self.out, residuals=[r])
        lims = self.read('limitations.md')
        self.assertIn('none of which says anything about the model', lims)
        row = [x for x in self.load('numbers.json') if x['where'] == 'residual'][0]
        self.assertFalse(row['counts_as_model_evidence'])

    def test_the_summary_never_claims_validation(self):
        X.write(a_hypothesis(), self.out, residuals=[self.residual()])
        self.assertNotIn('validated', self.read('limitations.md'))


class NarrativeGuardTests(unittest.TestCase):
    """The two validator fixes this export surfaced."""

    def test_a_number_inside_a_unit_name_is_not_a_claim(self):
        """'1e6 cells/mL' spells its unit with a number in it. Flagging that
        numeral would force every renderer to avoid the unit."""
        facts = [NR.Fact('f.1', 'harvest', 8.995, unit='1e6 cells/mL')]
        self.assertEqual([], NR.validate('Harvest was 8.995 1e6 cells/mL.', facts))

    def test_a_bare_number_matching_a_unit_token_is_still_caught(self):
        """Only the unit's exact spelling is blanked, not the numeral anywhere."""
        facts = [NR.Fact('f.1', 'harvest', 8.995, unit='1e6 cells/mL')]
        problems = NR.validate('Harvest was 8.995 1e6 cells/mL, from 1e6 donors.', facts)
        self.assertEqual(1, len(problems))
        self.assertIn('1e6', problems[0])

    def test_the_recommended_test_points_are_facts(self):
        """A level nobody recorded is a level nobody agreed to run, so the prose
        stating one has to be backed by a fact."""
        h = a_hypothesis()
        ids = {f.fact_id for f in NR.facts_from_hypothesis(h)}
        self.assertTrue(any(i.startswith('hyp.test_point.') for i in ids))
        self.assertEqual([], NR.validate(NR.say_hypothesis(h), NR.facts_from_hypothesis(h)))


if __name__ == '__main__':
    unittest.main()


class CliTests(ExportCase):
    def run_cli(self, *argv):
        from biosense.benchmark import cli
        return cli.main(['export-hypothesis', '--benchmark', str(BENCH), *argv])

    def test_dry_run_reports_permission_and_writes_nothing(self):
        self.assertEqual(0, self.run_cli('--dry-run', '--out', str(self.out)))
        self.assertFalse(self.out.exists())

    def test_it_writes_one_folder_per_hypothesis(self):
        self.assertEqual(0, self.run_cli('--out', str(self.out)))
        made = sorted(p.name for p in self.out.iterdir())
        self.assertEqual(['HYP-macrophage_mcsf_demo-01'], made)
        self.assertTrue((self.out / made[0] / 'MANIFEST.json').is_file())

    def test_an_unknown_hypothesis_id_is_refused_and_lists_what_is_there(self):
        self.assertEqual(1, self.run_cli('--hypothesis', 'HYP-nope', '--out', str(self.out)))
