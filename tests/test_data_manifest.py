"""Datasets: describing them, hashing them, and keeping their lineage.

The properties under test are the ones that make a derived number traceable:
a manifest hashes the bytes it describes, a derived dataset can always name its
parent, and nothing about an experimental design is guessed.
"""
import csv
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from biosense import contracts as K
from biosense.data import ingest as ING
from biosense.data import manifest as MF
from biosense.data import registry as DREG
from biosense.data import roots as DR
from biosense.data import tables as TB

FACS = K.ROOT / 'examples' / 'datasets' / 'facs_mcsf_synthetic.csv'
DESIGN = {'condition_column': 'condition', 'control': 'control', 'treatments': ['mcsf_high'],
          'replicate_column': 'replicate', 'donor_column': 'donor',
          'sample_id_column': 'sample_id'}


class PrivateRootCase(unittest.TestCase):
    """Every test gets its own private root, so nothing touches a real one."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._env = os.environ.get(DR.PRIVATE_ENV)
        os.environ[DR.PRIVATE_ENV] = str(self.tmp / 'private')
        os.environ[DR.CACHE_ENV] = str(self.tmp / 'cache')
        DR.ensure_roots()

    def tearDown(self):
        if self._env is None:
            os.environ.pop(DR.PRIVATE_ENV, None)
        else:
            os.environ[DR.PRIVATE_ENV] = self._env
        os.environ.pop(DR.CACHE_ENV, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ingest_facs(self, dataset_id='facs-1', **kw):
        kw.setdefault('experimental_design', DESIGN)
        return ING.ingest_local(FACS, dataset_id=dataset_id, title='M-CSF comparison',
                                modality='cytometry_summary', perturbation='M-CSF concentration',
                                cell_type='iPSC-derived monocyte', **kw)


class ManifestValidationTests(PrivateRootCase):
    def test_a_manifest_validates_against_its_schema(self):
        m, _ = self.ingest_facs()
        self.assertEqual([], K.schema_errors('dataset_manifest', m))

    def test_the_manifest_hashes_the_bytes_it_describes(self):
        m, _ = self.ingest_facs()
        f = m['files'][0]
        self.assertEqual(K.sha256_file(FACS), f['checksum_sha256'])
        self.assertEqual(FACS.stat().st_size, f['bytes'])

    def test_a_changed_file_changes_the_checksum(self):
        copy = self.tmp / 'copy.csv'
        shutil.copy(FACS, copy)
        a, _ = self.ingest_facs('d-a', register=False)
        with copy.open('a') as fh:
            fh.write('extra,control,r9,D9,1,1,1,1,1\n')
        b = MF.file_entry(copy, 'population_table', 'csv')
        self.assertNotEqual(a['files'][0]['checksum_sha256'], b['checksum_sha256'])

    def test_the_source_decides_the_class_and_the_visibility(self):
        m, _ = self.ingest_facs()
        self.assertEqual('user_upload', m['source'])
        self.assertEqual('private', m['visibility'])
        self.assertEqual('private_user_dataset', m['evidence_class'])

    def test_a_user_upload_cannot_be_registered_as_public(self):
        with self.assertRaises(K.ContractError) as e:
            MF.build('x', 'title', 'user_upload', 'generic_table', 'Homo sapiens',
                     [MF.file_entry(FACS, 'other', 'csv')], visibility='public')
        self.assertIn('private', str(e.exception))

    def test_a_public_dataset_must_carry_its_accession(self):
        with self.assertRaises(K.ContractError) as e:
            MF.build('x', 'title', 'geo', 'bulk_rna', 'Homo sapiens',
                     [MF.file_entry(FACS, 'counts', 'csv')])
        self.assertIn('accession', str(e.exception))

    def test_a_derived_dataset_must_name_its_parent(self):
        with self.assertRaises(K.ContractError) as e:
            MF.build('x', 'title', 'derived', 'generic_table', 'Homo sapiens',
                     [MF.file_entry(FACS, 'summary', 'csv')])
        self.assertIn('derived_from', str(e.exception))

    def test_no_dataset_is_ever_citable(self):
        m, _ = self.ingest_facs()
        self.assertIs(False, m['citable'])


class MissingMetadataTests(PrivateRootCase):
    def test_a_design_field_that_was_not_supplied_is_reported_not_guessed(self):
        m, _ = self.ingest_facs('d-nodesign', experimental_design={})
        missing = {x['field'] for x in m['missing_metadata']}
        self.assertIn('condition_column', missing)
        self.assertIn('control', missing)
        for x in m['missing_metadata']:
            self.assertTrue(x['why_it_matters'])

    def test_a_column_name_that_is_not_in_the_table_is_refused_at_ingest(self):
        with self.assertRaises(K.ContractError) as e:
            self.ingest_facs('d-bad', experimental_design=dict(DESIGN, condition_column='treatment'))
        self.assertIn('treatment', str(e.exception))
        self.assertIn('condition', str(e.exception))      # names the real columns

    def test_a_control_level_that_does_not_occur_is_refused(self):
        with self.assertRaises(K.ContractError) as e:
            self.ingest_facs('d-bad2', experimental_design=dict(DESIGN, control='untreated'))
        self.assertIn('untreated', str(e.exception))

    def test_an_ambiguous_modality_is_refused_rather_than_inferred(self):
        p = self.tmp / 'plain.csv'
        with p.open('w', newline='') as fh:
            w = csv.writer(fh)
            w.writerow(['a', 'b'])
            w.writerow(['1', '2'])
            w.writerow(['3', '4'])
        with self.assertRaises(K.ContractError) as e:
            ING.ingest_local(p, dataset_id='amb', title='t')
        self.assertIn('declare the modality', str(e.exception))

    def test_a_file_type_with_no_reader_is_refused_at_ingest(self):
        p = self.tmp / 'cells.h5ad'
        p.write_bytes(b'not really an h5ad')
        with self.assertRaises(K.ContractError) as e:
            ING.ingest_local(p, dataset_id='h5', title='t', modality='single_cell_rna')
        self.assertIn('h5ad', str(e.exception).lower())


class LineageTests(PrivateRootCase):
    def _derived(self, parent_id, child_id, **kw):
        return MF.build(child_id, 'derived summary', 'derived', 'generic_table', 'Homo sapiens',
                        [MF.file_entry(FACS, 'summary', 'csv')],
                        derived_from={'parent_dataset_id': parent_id, 'tool': 'demo',
                                      'tool_version': '1.0', 'parameters': {}},
                        **kw)

    def test_a_derived_dataset_keeps_a_path_back_to_its_parent(self):
        parent, _ = self.ingest_facs('parent-1')
        child = self._derived('parent-1', 'child-1', visibility='private')
        DREG.register(child)
        lin = DREG.lineage_of('child-1')
        self.assertEqual(['child-1', 'parent-1'], lin['chain'])
        self.assertEqual('private_user_dataset', lin['source_evidence_class'])
        self.assertEqual('private', lin['source_visibility'])

    def test_lineage_survives_more_than_one_hop(self):
        self.ingest_facs('p00')
        DREG.register(self._derived('p00', 'p01', visibility='private'))
        DREG.register(self._derived('p01', 'p02', visibility='private'))
        lin = DREG.lineage_of('p02')
        self.assertEqual(['p02', 'p01', 'p00'], lin['chain'])
        self.assertEqual('private', lin['source_visibility'])

    def test_a_derived_dataset_whose_parent_is_unregistered_is_refused(self):
        DREG.register(self._derived('nowhere', 'orphan', visibility='private'))
        with self.assertRaises(K.ContractError) as e:
            DREG.lineage_of('orphan')
        self.assertIn('no provenance', str(e.exception))

    def test_one_private_ancestor_makes_the_whole_chain_private_to_handle(self):
        pub = MF.build('pub-1', 'public set', 'geo', 'bulk_rna', 'Homo sapiens',
                       [MF.file_entry(FACS, 'counts', 'csv')], accession='SYNTHETIC-GSE1')
        priv, _ = self.ingest_facs('priv-1')
        cls, vis = MF.combined_source([pub, priv])
        self.assertEqual('mixed', cls)
        self.assertEqual('private', vis)


class StoredPathTests(PrivateRootCase):
    """A committed manifest must work on a machine that is not this one."""

    def test_a_file_inside_the_repository_is_stored_relative(self):
        e = MF.file_entry(FACS, 'population_table', 'csv')
        self.assertFalse(Path(e['path']).is_absolute())
        self.assertEqual('examples/datasets/facs_mcsf_synthetic.csv', e['path'])
        self.assertEqual(FACS, MF.resolve_path(e['path']))

    def test_a_file_outside_the_repository_keeps_its_absolute_path(self):
        outside = self.tmp / 'elsewhere.csv'
        shutil.copy(FACS, outside)
        e = MF.file_entry(outside, 'population_table', 'csv')
        self.assertTrue(Path(e['path']).is_absolute())
        self.assertEqual(outside.resolve(), MF.resolve_path(e['path']))

    def test_no_committed_fixture_manifest_holds_an_absolute_path(self):
        for p in sorted(DR.FIXTURES.glob('*/manifest.json')):
            m = K.read_json(p)
            for f in m['files']:
                self.assertFalse(Path(f['path']).is_absolute(),
                                 f'{p.parent.name} stores an absolute path; it would not '
                                 f'resolve on another clone')
                self.assertTrue(MF.resolve_path(f['path']).is_file())


class RegistryTests(PrivateRootCase):
    def test_a_private_manifest_lands_under_the_private_root(self):
        m, path = self.ingest_facs('priv-x')
        self.assertTrue(DR.is_private_path(path))

    def test_registering_a_private_dataset_outside_the_private_root_is_refused(self):
        m, _ = self.ingest_facs('priv-y', register=False)
        with self.assertRaises(K.ContractError) as e:
            DREG.register(m, root=self.tmp / 'somewhere_servable')
        self.assertIn('private root', str(e.exception))

    def test_a_manifest_is_immutable_once_written(self):
        self.ingest_facs('once')
        with self.assertRaises(K.ContractError):
            self.ingest_facs('once')

    def test_an_unregistered_dataset_is_refused_by_name(self):
        with self.assertRaises(K.ContractError) as e:
            DREG.require('no-such-dataset')
        self.assertIn('no-such-dataset', str(e.exception))

    def test_the_summary_row_carries_no_file_path(self):
        m, _ = self.ingest_facs('sum-1')
        row = DREG.summary(m)
        self.assertNotIn('files', row)
        self.assertIs(False, row['citable'])
        self.assertEqual('private', row['visibility'])


class TableReaderTests(unittest.TestCase):
    def test_a_partially_numeric_column_is_not_treated_as_numeric(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 't.csv'
            p.write_text('a,b\n1,2\n3,\n')
            t = TB.read_table(p)
            self.assertIn('a', t.numeric)
            self.assertNotIn('b', t.numeric)
            with self.assertRaises(K.ContractError) as e:
                t.numeric_column('b')
            self.assertIn('not fully numeric', str(e.exception))

    def test_duplicate_columns_are_refused(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 't.csv'
            p.write_text('a,a\n1,2\n')
            with self.assertRaises(K.ContractError):
                TB.read_table(p)

    def test_an_unreadable_format_names_what_is_readable(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 't.bed'
            p.write_text('chr1\t1\t2\n')
            with self.assertRaises(K.ContractError) as e:
                TB.read_table(p)
            self.assertIn('csv', str(e.exception))


if __name__ == '__main__':
    unittest.main()
