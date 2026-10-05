"""The README and the diagrams must not claim what the code does not do.

A diagram that draws a planned capability like a working one is a claim the
repository cannot support, and it is the kind of claim nobody notices going
stale. These tests check the documents against the registries they describe.
"""
import re
import unittest
from pathlib import Path

from biosense import contracts as K
from biosense.bioinformatics import registry as TREG
from biosense.data.sources import geo

README = K.ROOT / 'README.md'
BIOINFO_DOC = K.ROOT / 'docs' / 'BIOINFORMATICS.md'
ASSETS = K.ROOT / 'docs' / 'assets'
DIAGRAMS = ('system', 'evidence', 'loop', 'capabilities')


class DiagramTests(unittest.TestCase):
    def test_every_diagram_ships_in_both_themes(self):
        for name in DIAGRAMS:
            for theme in ('light', 'dark'):
                p = ASSETS / f'arch-{name}-{theme}.svg'
                self.assertTrue(p.is_file(), f'{p} is missing')
                self.assertGreater(p.stat().st_size, 1000)

    def test_the_diagrams_are_version_controlled_text(self):
        for name in DIAGRAMS:
            svg = (ASSETS / f'arch-{name}-light.svg').read_text()
            self.assertTrue(svg.startswith('<svg'))
            self.assertNotIn('<image', svg, 'a diagram must not embed a raster')

    def test_every_diagram_carries_an_accessible_description(self):
        for name in DIAGRAMS:
            svg = (ASSETS / f'arch-{name}-light.svg').read_text()
            m = re.search(r'aria-label="([^"]+)"', svg)
            self.assertIsNotNone(m, name)
            self.assertGreater(len(m.group(1)), 80, name)

    def test_the_capabilities_diagram_marks_unimplemented_tools_as_planned(self):
        """The check that would catch a diagram going stale: anything drawn as
        running must actually be in the registry."""
        svg = (ASSETS / 'arch-capabilities-light.svg').read_text()
        self.assertIn('PLANNED', svg)
        self.assertIn('Single cell RNA', svg)
        self.assertIn('ChIP-seq', svg)
        # the modalities drawn as PHASE 1 are ones a registered tool accepts
        running = {m for spec in TREG.TOOLS.values() for m in spec.modalities}
        self.assertIn('flow_cytometry', running)
        self.assertIn('bulk_rna', running)
        self.assertNotIn('single_cell_rna', running)
        self.assertNotIn('chip_seq', running)

    def test_the_regenerator_is_committed_and_reproduces_the_files(self):
        import subprocess
        import tempfile
        script = K.ROOT / 'scripts' / 'make_architecture_diagrams.py'
        self.assertTrue(script.is_file())
        with tempfile.TemporaryDirectory() as d:
            r = subprocess.run(['python', str(script), '--out', d],
                               capture_output=True, text=True, cwd=K.ROOT)
            self.assertEqual(0, r.returncode, r.stderr)
            for name in DIAGRAMS:
                fresh = (Path(d) / f'arch-{name}-light.svg').read_text()
                committed = (ASSETS / f'arch-{name}-light.svg').read_text()
                self.assertEqual(committed, fresh,
                                 f'arch-{name}-light.svg is stale; regenerate it')


class ReadmeTests(unittest.TestCase):
    def setUp(self):
        self.readme = README.read_text()
        self.doc = BIOINFO_DOC.read_text()

    def test_every_local_link_resolves(self):
        for target in re.findall(r'\]\(([^)#][^)]*)\)', self.readme):
            if target.startswith(('http', 'mailto')):
                continue
            self.assertTrue((K.ROOT / target.split('#')[0]).exists(),
                            f'README links to a missing path: {target}')

    def test_every_image_the_readme_shows_exists(self):
        for src in re.findall(r'src(?:set)?="([^"]+)"', self.readme):
            self.assertTrue((K.ROOT / src).exists(), f'README shows a missing image: {src}')

    def test_the_readme_names_the_unimplemented_modalities_as_planned(self):
        limits = self.readme.split('## What this is **not**')[1]
        self.assertIn('declared, not implemented', limits)
        for word in ('Single cell', 'ChIP-seq', 'ATAC-seq', 'FCS'):
            self.assertIn(word, limits, f'{word} is not named among the limits')

    def test_the_readme_says_the_dataset_fixtures_are_invented(self):
        limits = self.readme.split('## What this is **not**')[1]
        self.assertIn('invented', limits)
        self.assertIn('SYNTHETIC-GSE', limits)

    def test_the_readme_says_live_search_is_unverified(self):
        limits = self.readme.split('## What this is **not**')[1].lower()
        self.assertIn('unverified', limits)

    def test_the_readme_does_not_claim_a_real_dataset_was_analysed(self):
        for phrase in ('downloaded from GEO', 'analysed GEO data', 'real omics dataset'):
            self.assertNotIn(phrase.lower(), self.readme.lower())

    def test_the_readme_still_describes_bioinformatics_beyond_annotation(self):
        """The old README described the agent as annotation-only. That claim is
        now wrong in the other direction."""
        self.assertIn('public and private datasets', self.readme)
        self.assertIn('uncertainty', self.readme.lower())

    def test_the_test_count_the_readme_quotes_is_not_wildly_stale(self):
        m = re.search(r'python -m unittest\s+#\s*(\d+) tests', self.readme)
        self.assertIsNotNone(m, 'the README no longer quotes a test count')
        import unittest as ut
        total = ut.defaultTestLoader.discover(str(K.ROOT / 'tests'),
                                              top_level_dir=str(K.ROOT)).countTestCases()
        self.assertAlmostEqual(int(m.group(1)), total, delta=15,
                               msg=f'README says {m.group(1)} tests; there are {total}')


class FixtureHonestyTests(unittest.TestCase):
    def test_no_fixture_accession_can_be_mistaken_for_a_real_one(self):
        for rec in geo.load_index():
            self.assertTrue(rec['accession'].startswith('SYNTHETIC-'), rec['accession'])
            self.assertIn('SYNTHETIC', rec['title'])

    def test_the_fixture_index_says_it_is_invented(self):
        d = K.read_json(geo.INDEX)
        self.assertIs(True, d['synthetic'])
        self.assertIn('INVENTED', d['note'])

    def test_committed_fixture_manifests_declare_themselves_synthetic(self):
        from biosense.data import registry as DREG
        from biosense.data import roots as DR
        rows = DREG.list_datasets(dirs=[DR.FIXTURES])
        self.assertTrue(rows)
        for m in rows:
            self.assertEqual('synthetic_fixture', m['evidence_class'])
            self.assertEqual('fixture', m['source'])
            self.assertTrue(any('invented' in x.lower() for x in m['limitations']), m['dataset_id'])


if __name__ == '__main__':
    unittest.main()
