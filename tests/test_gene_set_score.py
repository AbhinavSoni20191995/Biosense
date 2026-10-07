"""A question about a module is answered by scoring the module, not a top-genes list."""
import json
import unittest
from unittest import mock

from biosense import contracts as K
from biosense.bioinformatics import registry as TREG
from biosense.data.sources import geo_fetch as GF
from tests.test_geo_fetch import ACC, SERVED, Sandbox, fake_get


class GeneSetScoreTests(Sandbox):
    def fetch(self):
        with mock.patch.object(GF, '_get', fake_get(SERVED)):
            code, d = self.cli('datasets', 'fetch-geo', '--accession', ACC, '--condition-key',
                               'treatment', '--control', 'vehicle', '--treatment', 'ATRA 100 nM',
                               '--genes', 'GATA6', 'ACTB', 'TFRC', '--i-have-network-permission')
        self.assertEqual(0, code, d)
        return d['dataset_id']

    def plan(self, did, *genes):
        return self.cli(
            'analyse', 'plan', '--plan-id', 'plan-set', '--question', 'Does ATRA shift the set?',
            '--evidence-gap', 'U1', '--uncertainty', 'whether the module moves as a whole',
            '--why', 'it decides whether the cue changes identity, not one gene',
            '--dataset-ids', did, '--analysis-type', 'gene_set_score',
            '--tool', 'bulk.gene_set_score', '--decision-relevance', 'maturation cue',
            '--group-column', 'condition', '--control', 'vehicle',
            '--treatment-level', 'ATRA 100 nM', '--readouts', *genes,
            '--out', str(self.tmp / 'plan.json'))

    def test_the_tool_is_registered_for_its_analysis_type(self):
        self.assertIn('bulk.gene_set_score', TREG.TOOLS)
        self.assertIn('gene_set_score', TREG.TOOLS['bulk.gene_set_score'].analysis_types)

    def test_a_set_is_scored_with_its_members_and_what_was_missing(self):
        did = self.fetch()
        code, plan = self.plan(did, 'GATA6', 'TFRC', 'NOTAGENE')
        self.assertEqual(0, code, plan)
        code, res = self.cli('analyse', 'run', '--plan', str(self.tmp / 'plan.json'),
                             '--out', str(self.tmp / 'result.json'))
        self.assertEqual(0, code, res)
        res = json.loads((self.tmp / 'result.json').read_text())
        head = res['statistics'][0]
        self.assertIn('gene set score (2 genes)', head['readout'])
        self.assertEqual('difference_in_mean_gene_z_score', head['effect_type'])
        self.assertEqual({'GATA6', 'TFRC'}, {r['readout'] for r in res['statistics'][1:]})
        qc = {c['check']: c for c in res['quality_control']['checks']}
        self.assertIn('missing: NOTAGENE', qc['genes found']['detail'])
        self.assertIn('1 of 2 genes higher', qc['agreement within the set']['detail'],
                      'GATA6 rises and TFRC falls: the split is shown, not averaged away')

    def test_a_set_the_table_does_not_carry_is_refused(self):
        did = self.fetch()
        code, plan = self.plan(did, 'GATA6', 'NOTAGENE')
        self.assertEqual(0, code, plan)
        code, res = self.cli('analyse', 'run', '--plan', str(self.tmp / 'plan.json'),
                             '--out', str(self.tmp / 'result.json'))
        self.assertNotEqual(0, code)
        self.assertIn('only 1 of the 2 genes', str(res))


if __name__ == '__main__':
    unittest.main()
