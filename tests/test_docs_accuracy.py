"""The README and the diagrams must not claim what the code does not do.

A diagram that draws a planned capability like a working one is a claim the
repository cannot support, and it is the kind of claim nobody notices going
stale. These tests check the documents against the registries they describe.
"""
import json
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

    # each box in the capabilities diagram, and the tool that has to be registered
    # for that box to be drawn as running. Keyed on the tool rather than the
    # modality because the planned boxes are tool gaps, not modality gaps: raw FCS
    # is the flow_cytometry modality, which a registered tool already accepts, so a
    # modality check would pass that row without ever looking at it.
    CAPABILITY_BOXES = (
        ('Flow cytometry / FACS', 'cytometry.population_comparison'),
        ('Bulk RNA', 'bulk.expression_comparison'),
        ('Single cell RNA', 'single_cell.pseudobulk_comparison'),
        ('ChIP-seq / ATAC-seq', 'chromatin.peak_overlap'),
        ('Raw FCS / FlowSOM / UMAP', 'cytometry.gating'),
        ('External R / DESeq2', 'external.deseq2'),
    )

    def test_every_capability_box_names_a_tool_the_registry_knows(self):
        """Guards the guard below: a box keyed on a tool name that is in neither
        registry would be checked against nothing."""
        known = set(TREG.TOOLS) | {p['name'] for p in TREG.PLANNED}
        for label, tool in self.CAPABILITY_BOXES:
            self.assertIn(tool, known, label)

    def test_the_capabilities_diagram_marks_unimplemented_tools_as_planned(self):
        """The check that would catch the diagram going stale in either direction:
        a box drawn as running whose tool is not registered would promise an
        analysis that is refused, and a box still drawn as PLANNED after the tool
        landed would hide one that works."""
        svg = (ASSETS / 'arch-capabilities-light.svg').read_text()
        self.assertIn('PLANNED', svg)
        for label, tool in self.CAPABILITY_BOXES:
            status = self._box_status(svg, label)
            # the diagram is a committed artifact describing the project, so it is
            # checked against whether the code exists, not against whether this
            # machine happens to have the optional extra installed
            if TREG.is_implemented(tool):
                self.assertNotEqual('PLANNED', status,
                                    f'{label} is drawn as PLANNED but {tool} is implemented')
            else:
                self.assertEqual('PLANNED', status,
                                 f'{label} is drawn as {status} but {tool} does not exist')

    @staticmethod
    def _box_status(svg, label):
        """The status badge of the box carrying this label.

        box() writes the badge immediately before the title, so the nearest
        preceding badge is this box's own.
        """
        i = svg.index(f'>{label}<')
        badges = re.findall(r'letter-spacing="0.9">([A-Z0-9 ]+)</text>', svg[:i])
        assert badges, f'no status badge precedes {label!r}'
        return badges[-1]

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


class BenchmarkReadmeTests(unittest.TestCase):
    """The README's benchmark numbers are generated, never typed.

    A hand-copied number goes stale silently and a reader cannot tell."""

    def setUp(self):
        self.readme = README.read_text()

    def test_the_section_is_delimited_by_markers(self):
        self.assertIn('<!-- BENCHMARK:START -->', self.readme)
        self.assertIn('<!-- BENCHMARK:END -->', self.readme)

    def test_the_section_is_current(self):
        import subprocess
        r = subprocess.run(['python', str(K.ROOT / 'scripts' / 'update_benchmark_readme.py'),
                            '--check'], capture_output=True, text=True, cwd=K.ROOT)
        self.assertEqual(0, r.returncode,
                         f'{r.stdout}{r.stderr} — run scripts/update_benchmark_readme.py')

    def test_the_generator_rewrites_only_the_marked_block(self):
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            copy = Path(d) / 'README.md'
            copy.write_text(self.readme.replace('<!-- BENCHMARK:START -->',
                                                '<!-- BENCHMARK:START -->\nSTALE'))
            subprocess.run(['python', str(K.ROOT / 'scripts' / 'update_benchmark_readme.py'),
                            '--readme', str(copy)], capture_output=True, cwd=K.ROOT)
            after = copy.read_text()
        self.assertNotIn('STALE', after)
        head = self.readme.split('<!-- BENCHMARK:START -->')[0]
        self.assertEqual(head, after.split('<!-- BENCHMARK:START -->')[0])

    def test_the_section_says_the_demonstration_is_synthetic(self):
        block = self.readme.split('<!-- BENCHMARK:START -->')[1].split('<!-- BENCHMARK:END -->')[0]
        self.assertIn('SYNTHETIC DEMONSTRATION', block)
        self.assertIn('measurement of any real cell', block)
        self.assertIn('invented fixture', block)

    def test_every_number_in_the_section_comes_from_the_benchmark(self):
        """A figure in the README must exist in the artifact it was generated from.

        Rounding is allowed -- the README shows 4.762 where the artifact holds
        4.7619 -- so this reuses the same decimal-place tolerance the narrative
        validator applies. Inventing a number is not allowed.
        """
        from biosense.evidence import narrative as N
        block = self.readme.split('<!-- BENCHMARK:START -->')[1].split('<!-- BENCHMARK:END -->')[0]
        bench = K.read_json(K.ROOT / 'benchmarks' / 'public' / 'macrophage_mcsf_demo'
                            / 'benchmark.json')

        known = set()

        def collect(o):
            if isinstance(o, dict):
                for v in o.values():
                    collect(v)
            elif isinstance(o, list):
                for v in o:
                    collect(v)
            elif isinstance(o, (int, float)) and not isinstance(o, bool):
                known.add(f'{float(o):.6g}')

        collect(bench)
        suspicious = []
        for m in re.finditer(r'(?<![\w.$/-])(\d+\.\d+)(?![\w.%-])', block):
            if not N._matches(m.group(1), known):
                suspicious.append(m.group(1))
        self.assertEqual([], suspicious,
                         f'number(s) in the README benchmark section are not in benchmark.json: '
                         f'{suspicious}')

    def test_the_section_links_resolve(self):
        import re
        block = self.readme.split('<!-- BENCHMARK:START -->')[1].split('<!-- BENCHMARK:END -->')[0]
        for target in re.findall(r'\]\(([^)#][^)]*)\)', block):
            if target.startswith(('http', 'mailto')):
                continue
            self.assertTrue((K.ROOT / target).exists(), target)
        for src in re.findall(r'src(?:set)?="([^"]+)"', block):
            self.assertTrue((K.ROOT / src).exists(), src)


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
