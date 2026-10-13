"""Grouping a GEO series by what its samples actually say, and keeping its time axis.

A run lost three decision-relevant series to plumbing, not biology: a label
that lived only in the per-sample title grouped into one sample per side, a
Day 2 / Day 4 series was registered pooled, and a `.tab.gz` table was "no
table-like file". These hold the fixes, offline.
"""
import csv
import json
import unittest

from biosense import contracts as K
from biosense.data import ingest as ING
from biosense.data.sources import geo_fetch as GF
from tests.test_geo_fetch import Sandbox


def s(gsm, title, **chars):
    return {'gsm': gsm, 'title': title, 'source': 'hPSC', 'characteristics': chars}


# Like GSE125761: the contrast lives only in the title.
TITLED = [s('GSM1', 'CD43- rep1'), s('GSM2', 'CD43- rep2'), s('GSM3', 'CD43- rep3'),
          s('GSM4', 'CD43+ rep1'), s('GSM5', 'CD43+ rep2'), s('GSM6', 'CD43+ rep3')]
# Like a treatment series sampled at two days.
TIMED = [s(f'GSM{i}', f'{t} D{d} rep{r}', treatment=t)
         for i, (t, d, r) in enumerate(((t, d, r) for t in ('vehicle', 'GM-CSF')
                                        for d in (2, 4) for r in (1, 2)), 1)]
GENES = ['PPARG', 'MARCO']


def matrix(n):
    return [[10.0 + j for j in range(n)], [5.0 + 2 * j for j in range(n)]]


class FieldTests(unittest.TestCase):
    def test_the_title_without_its_replicate_number_is_a_field(self):
        self.assertEqual('CD43-', GF._value_of(TITLED[1], 'title_label'))
        self.assertEqual('CD43+', GF._value_of(TITLED[4], 'title_label'))
        self.assertEqual('GFP+', GF.title_label('GFP+_3'))
        self.assertEqual('MoCul', GF.title_label('MoCul_n1'))
        self.assertEqual('IL-4 treated', GF.title_label('IL-4 treated rep2'))

    def test_a_pattern_over_a_field_reads_the_label(self):
        self.assertEqual('CD43+', GF._value_of(TITLED[3], 'title~(CD43[+-])'))
        self.assertEqual('D4', GF._value_of(TIMED[2], 'title~D\\d'))
        self.assertIsNone(GF._value_of(TITLED[0], 'title~(day \\d)'))
        with self.assertRaisesRegex(K.ContractError, 'not a regular expression'):
            GF._value_of(TITLED[0], 'title~(unclosed')
        with self.assertRaisesRegex(K.ContractError, 'give one group'):
            GF._value_of(TITLED[0], 'title~(a)(b)')

    def test_several_control_values_are_one_group_labelled_by_the_first(self):
        rows = [s('GSM1', 'UT 1', treatment='UT'), s('GSM2', 'Ctrl 1', treatment='Ctrl'),
                s('GSM3', 'drug 1', treatment='drug')]
        got = GF.select_samples(rows, condition_key='treatment', control=['UT', 'Ctrl'],
                                treatments=['drug'])
        self.assertEqual([('GSM1', 'UT'), ('GSM2', 'UT'), ('GSM3', 'drug')], got)


class TimeTests(unittest.TestCase):
    def test_a_time_is_a_number_joined_to_a_unit(self):
        for text, want in (('D2', 'day 2'), ('Day_4', 'day 4'), ('48h', 'hour 48'),
                           ('day 0.5', 'day 0.5'), ('2 weeks', 'week 2'), ('30 min', 'minute 30')):
            self.assertEqual(want, GF.parse_time(text), text)
        for text in ('CD43+', 'rep2', 'IL-4', '42', 'Hdac1'):
            self.assertIsNone(GF.parse_time(text), text)
        self.assertEqual(['hour 6', 'day 2', 'day 10', 'week 1'],
                         sorted(['day 10', 'week 1', 'hour 6', 'day 2'], key=GF.time_order))

    def test_with_a_time_key_the_table_keeps_the_time_axis(self):
        gsms = [x['gsm'] for x in TIMED]
        rows, stats = GF.build_long_table(TIMED, GENES, gsms, matrix(len(gsms)), {},
                                          condition_key='treatment', control='vehicle',
                                          treatments=['GM-CSF'], time_key='title~D\\d',
                                          transform='log')
        self.assertEqual(['day 2', 'day 4'], stats['timepoints'])
        self.assertEqual({'vehicle': 2, 'GM-CSF': 2}, stats['samples_per_group_and_time']['day 4'])
        self.assertEqual({'day 2', 'day 4'}, {r['timepoint'] for r in rows})
        self.assertEqual([], stats['time_pooled'])

    def test_without_one_the_pooling_is_named_not_silent(self):
        gsms = [x['gsm'] for x in TIMED]
        _rows, stats = GF.build_long_table(TIMED, GENES, gsms, matrix(len(gsms)), {},
                                           condition_key='treatment', control='vehicle',
                                           treatments=['GM-CSF'], transform='log')
        self.assertEqual([{'field': 'title_label', 'values': ['day 2', 'day 4']}],
                         stats['time_pooled'])

    def test_a_sample_with_no_time_is_refused_by_name(self):
        rows = TIMED + [s('GSM99', 'GM-CSF rep9', treatment='GM-CSF')]
        with self.assertRaisesRegex(K.ContractError, 'GSM99'):
            GF.build_long_table(rows, GENES, [x['gsm'] for x in rows], matrix(len(rows)), {},
                                condition_key='treatment', control='vehicle',
                                treatments=['GM-CSF'], time_key='title~D\\d', transform='log')


class GroupingTests(unittest.TestCase):
    def test_a_title_encoded_contrast_groups_three_against_three(self):
        gsms = [x['gsm'] for x in TITLED]
        _rows, stats = GF.build_long_table(TITLED, GENES, gsms, matrix(6), {},
                                           condition_key='title_label', control='CD43-',
                                           treatments=['CD43+'], transform='log')
        self.assertEqual({'CD43-': 3, 'CD43+': 3}, stats['samples_per_group'])

    def test_grouping_by_the_raw_title_says_where_the_label_is(self):
        gsms = [x['gsm'] for x in TITLED]
        with self.assertRaisesRegex(K.ContractError, 'try --condition-key title_label'):
            GF.build_long_table(TITLED, GENES, gsms, matrix(6), {}, condition_key='title',
                                control='CD43- rep1', treatments=['CD43+ rep1'],
                                transform='log')


class FormatTests(unittest.TestCase):
    def test_a_tab_table_in_the_series_directory_is_table_like(self):
        listing = ('<a href="GSE287217_counts.tab.gz">x</a> <a href="GSE287217_RAW.tar">y</a>'
                   ' <a href="GSE1_peaks.narrowPeak.gz">z</a>')
        self.assertEqual(['GSE287217_counts.tab.gz'], GF.supplementary_files(listing))


class TimedAnalysisTests(Sandbox):
    """A dataset with a time axis is compared at a timepoint, or pooled on purpose."""

    def setUp(self):
        super().setUp()
        rows = []
        for i, (cond, day) in enumerate([(c, d) for c in ('vehicle', 'GM-CSF')
                                         for d in ('day 2', 'day 4') for _ in range(3)]):
            for gene, base in (('PPARG', 5.0), ('MARCO', 3.0)):
                lift = 2.0 if (cond == 'GM-CSF' and day == 'day 4' and gene == 'PPARG') else 0
                rows.append({'gene': gene, 'sample_id': f'GSM{i}', 'condition': cond,
                             'logcpm': f'{base + lift + 0.1 * (i % 3):.3f}', 'timepoint': day})
        table = self.tmp / 'timed.tsv'
        with open(table, 'w', newline='') as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]), delimiter='\t')
            w.writeheader(); w.writerows(rows)
        ING.ingest_public_table(
            table, dataset_id='gse000001_timed', accession='GSE000001', source='geo',
            title='timed', experimental_design={
                'condition_column': 'condition', 'control': 'vehicle', 'treatments': ['GM-CSF'],
                'sample_id_column': 'sample_id', 'timepoint_column': 'timepoint'})

    def plan(self, *extra):
        out = self.tmp / f'plan{len(extra)}.json'
        code, p = self.cli(
            'analyse', 'plan', '--plan-id', 'plan-01', '--question', 'Does GM-CSF raise PPARG?',
            '--evidence-gap', 'U1', '--uncertainty', 'when GM-CSF acts',
            '--why', 'the lever is a window', '--dataset-ids', 'gse000001_timed',
            '--analysis-type', 'bulk_expression_comparison', '--tool',
            'bulk.expression_comparison', '--decision-relevance', 'GM-CSF window',
            '--group-column', 'condition', '--control', 'vehicle', '--treatment-level',
            'GM-CSF', *extra, '--out', str(out))
        self.assertEqual(0, code, p)
        return self.cli('analyse', 'run', '--plan', str(out))

    def test_several_timepoints_are_not_pooled_by_default(self):
        code, res = self.plan()
        self.assertEqual(1, code)
        self.assertIn('holds 2 timepoints (day 2, day 4)', json.dumps(res))

    def test_a_named_timepoint_compares_that_time_only(self):
        code, res = self.plan('--timepoint', 'Day 4')
        self.assertEqual(0, code, res)
        pparg = next(r for r in res['statistics'] if 'PPARG' in json.dumps(r)[:120])
        self.assertAlmostEqual(2.0, pparg['effect'], places=6)
        self.assertTrue(any('Compared at day 4 only' in x for x in res['limitations']))
        code, res = self.plan('--timepoint', 'day 9')
        self.assertEqual(1, code)

    def test_pooling_on_purpose_is_stated(self):
        code, res = self.plan('--pool-timepoints')
        self.assertEqual(0, code, res)
        self.assertTrue(any('Pooled across 2 timepoints' in x for x in res['limitations']))


if __name__ == '__main__':
    unittest.main()
