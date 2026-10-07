"""Reasoned starting values, and limitations a person can read.

Both exist because of one real run. Its science was strong — it found an
iPSC-derived alveolar-like macrophage validated against primary BAL RNA-seq,
caught that CD206 and CD169 were saturated readouts, and noticed the one time
course peaked at its last sampled day — and its protocol table was seven GAPs
under a flat list of forty-two limitations, three of them "no dataset".
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from biosense import contracts as K
from biosense import projects as PJ
from biosense.evidence import cli as EVCLI
from biosense.evidence import design as DC
from biosense.evidence import limitations as LIM
from biosense.production import protocol_summary as PS

REAL_RUN = [
    'NO DATASET. No dataset ids were named in the request and no reachable real dataset was found: all registered datasets are synthetic_fixture and citable:false.',
    'NO DATASET. The request named no dataset ids, and the bioinformatics specialist found no reachable real dataset: all 3 registered datasets are synthetic_fixture.',
    'NO SIMULATED PREDICTION IS POSSIBLE. The macrophage project declares simulator status none.',
    'NO SIMULATED PREDICTION IS POSSIBLE. The macrophage project declares simulator status none and every parameter as no_simulator. The simulate tool was run.',
    'NO ANNOTATION. There is no macrophage or myeloid knowledge set in this installation; PPARG returned found:false.',
    'THE ONE REAL TIME COURSE PEAKED AT ITS LAST SAMPLED DAY: MARCO was highest at day 6 of a 6-day experiment.',
    'NO DOSE-RESPONSE EXISTS (gap G1), so no optimum can be stated.',
    'THE LEVER IS NOT A PROJECT PARAMETER. The project has no growth-factor term at all, so no protocol can adopt this value.',
    'CANDIDATE PARAMETER — NOT YET REGISTERED: GM-CSF concentration is not in the canonical parameter registry.',
    'PROVENANCE WARNING ON THE LITERATURE ARTIFACTS. The retry OVERWROTE sources.json WITHOUT A BACKUP.',
    'THE BEST REMAINING TIME-COURSE SOURCE WAS NOT READ. PMC13228567 was identified and not fetched.',
    'CANNOT BE SETTLED IN THIS MACHINE AT ALL, per the bioinformatics specialist: surfactant handling; in vivo engraftment.',
    'Nothing here approves a protocol or runs a bioreactor; a wet-lab run needs a named human approver.',
    'Nothing in this run approves a protocol or runs a bioreactor, and no part of this session can be the named human approver.',
]


class LimitationTests(unittest.TestCase):
    def test_two_findings_that_share_a_template_are_never_folded(self):
        gm = ("CANDIDATE PARAMETER — NOT YET REGISTERED: 'GM-CSF (CSF2) concentration during "
              "maturation' is not in the canonical parameter registry, so BioSense has no units.")
        tgf = ("CANDIDATE PARAMETER — NOT YET REGISTERED: 'Exogenous TGF-beta1 concentration "
               "during maturation' is not in the canonical parameter registry, so BioSense has no units.")
        self.assertEqual(2, len(LIM.fold([gm, tgf])))
        self.assertEqual(2, len(LIM.fold(['NO DOSE-RESPONSE EXISTS for GM-CSF dose in iPSC.',
                                          'NO DOSE-RESPONSE EXISTS. Plateau day for MARCO under '
                                          'an alveolar regime is untested anywhere.'])))

    def test_restatements_fold_into_the_fullest_wording(self):
        kept = LIM.fold(REAL_RUN)
        self.assertEqual(1, sum(x.startswith('NO DATASET') for x in kept))
        self.assertEqual(1, sum(x.startswith('NO SIMULATED') for x in kept))
        self.assertIn('every parameter as no_simulator', ' '.join(kept), 'the longer one is kept')
        self.assertLess(len(kept), len(REAL_RUN))

    def test_the_structural_finding_leads_and_machine_limits_follow(self):
        groups = LIM.digest(REAL_RUN)
        kinds = [g['kind'] for g in groups]
        self.assertEqual('blocks', kinds[0], 'what stops the wet lab is read first')
        by = {g['kind']: g['items'] for g in groups}
        self.assertTrue(any('NOT A PROJECT PARAMETER' in x for x in by['blocks']))
        self.assertTrue(any('PEAKED AT ITS LAST SAMPLED DAY' in x for x in by['unsettled']))
        self.assertTrue(all(any(w in x for w in ('DATASET', 'SIMULATED', 'ANNOTATION'))
                            for x in by['installation']))
        self.assertTrue(any('WITHOUT A BACKUP' in x for x in by['provenance']))
        self.assertTrue(any('IN THIS MACHINE' in x for x in by['untestable']))
        self.assertEqual('standing', kinds[-1])
        self.assertLess(kinds.index('unsettled'), kinds.index('installation'))

    def test_nothing_is_lost_only_folded(self):
        groups = LIM.digest(REAL_RUN)
        shown = [x for g in groups for x in g['items']]
        self.assertEqual(sorted(LIM.fold(REAL_RUN)), sorted(shown))


class DesignChoiceTests(unittest.TestCase):
    def setUp(self):
        self.project = PJ.load('ipsc_macrophage')
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def row(self, **kw):
        r = {'parameter_id': 'agitation_rpm', 'value': 60,
             'rationale': 'Low shear for suspension aggregates in a small stirred vessel.',
             'derived_from': ['PMID:0 (iPSC aggregates, same vessel class)'],
             'confidence': 'low', 'would_settle_it': 'An agitation series measuring aggregate size.'}
        r.update(kw)
        return r

    def test_a_reasoned_value_is_accepted_with_its_basis(self):
        doc = DC.build({'choices': [self.row()]}, project=self.project)
        K.require_valid('design_choices', doc)
        c = doc['choices'][0]
        self.assertEqual(('agitation_rpm', 60.0, 'low'), (c['parameter_id'], c['value'], c['confidence']))

    def test_a_guess_with_nothing_behind_it_is_a_gap_not_a_choice(self):
        with self.assertRaisesRegex(K.ContractError, 'derived_from'):
            DC.build({'choices': [self.row(derived_from=[])]}, project=self.project)
        with self.assertRaisesRegex(K.ContractError, 'rationale'):
            DC.build({'choices': [self.row(rationale=' ')]}, project=self.project)
        with self.assertRaisesRegex(K.ContractError, 'confidence'):
            DC.build({'choices': [self.row(confidence='certain')]}, project=self.project)

    def test_a_knob_the_project_does_not_have_or_a_value_outside_its_bounds_is_refused(self):
        with self.assertRaisesRegex(K.ContractError, 'does not expose'):
            DC.build({'choices': [self.row(parameter_id='temperature_c', value=37)]},
                     project=self._without('temperature_c'))
        q = self.project.parameter('agitation_rpm')
        if q.maximum is not None:
            with self.assertRaisesRegex(K.ContractError, 'project maximum'):
                DC.build({'choices': [self.row(value=q.maximum + 1)]}, project=self.project)
        with self.assertRaisesRegex(K.ContractError, 'given twice'):
            DC.build({'choices': [self.row(), self.row()]}, project=self.project)

    def _without(self, pid):
        class P:
            project_id = 'stub'
            parameter_ids = set(self.project.parameter_ids) - {pid}
            parameter = self.project.parameter
        return P()

    def test_the_protocol_shows_a_design_choice_where_it_showed_a_gap(self):
        # A person's own project (the one in the real run) records no defaults;
        # the committed template does. Read it as the person's would be.
        from unittest import mock
        pid = 'agitation_rpm'
        real = PS._current_value
        patcher = mock.patch.object(PS, '_current_value',
                                    lambda pr, p, q, c: None if p == pid else real(pr, p, q, c))
        patcher.start(); self.addCleanup(patcher.stop)
        q = self.project.parameter(pid)
        val = q.minimum if q.minimum is not None else 1
        doc = DC.build({'choices': [self.row(parameter_id=pid, value=val)]}, project=self.project)
        hyp = EVCLI.build_hypothesis(EVCLI.TEMPLATE | {'effects': EVCLI.TEMPLATE['effects'][:1]},
                                     project_id='ipsc_macrophage')
        before = PS.build(project=self.project, objective='o', hypotheses=[hyp],
                          runtime_mode='synthetic_demo')
        after = PS.build(project=self.project, objective='o', hypotheses=[hyp],
                         runtime_mode='synthetic_demo', design_choices=DC.by_parameter(doc))
        def row(d):
            return next(p for s in d['stages'] for p in s['parameters'] if p['parameter_id'] == pid)
        self.assertEqual('gap', row(before)['provenance'])
        self.assertEqual('design_choice', row(after)['provenance'])
        self.assertEqual(val, row(after)['recommended_value'])
        self.assertEqual('low', row(after)['design_choice']['confidence'])
        self.assertNotIn(pid, [g['parameter_id'] for g in after['gaps']])
        self.assertIn(pid, [g['parameter_id'] for g in before['gaps']])
        self.assertEqual(1, len(after['design_choices']))
        self.assertTrue(any('reasoned starting values, not measurements' in x
                            for x in after['limitations']))
        md = PS.markdown(after)
        self.assertIn('## S2. Reasoned starting values', md, 'in the supplement')
        self.assertIn('### ', md, 'limitations are grouped under headings')
        K.require_valid('protocol_summary', after)

    def test_the_cli_builds_the_file_and_the_template_prints(self):
        import contextlib
        import io
        draft = self.tmp / 'choices.draft.json'
        draft.write_text(json.dumps({'choices': [self.row()]}))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = EVCLI.main(['design-choices', '--project', 'ipsc_macrophage',
                               '--draft', str(draft), '--out', str(self.tmp / 'design_choices.json')])
        self.assertEqual(0, code)
        K.require_valid('design_choices', K.read_json(self.tmp / 'design_choices.json'))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            EVCLI.main(['template', 'design-choices'])
        self.assertIn('derived_from', out.getvalue())

    def test_the_brief_names_the_third_option(self):
        from biosense.production import discovery as DISC
        req = DISC.build(project_id='ipsc_macrophage',
                         objective='Increase viable macrophage production while keeping identity.',
                         runtime_mode='synthetic_demo')
        brief = DISC.render_brief(req, loop_dir='ai-x')
        self.assertIn('design choice', brief)
        self.assertIn('evidence.cli design-choices', brief)
        self.assertIn('not caution', brief)


class FixTests(unittest.TestCase):
    """Two failures from one real run, kept from coming back."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_design_choices_in_a_run_directory_reach_the_protocol(self):
        """Every proposed starting value had silently become a GAP: the runner
        looked the file up in an index the display bundle does not have."""
        from biosense.production import discovery as DISC
        from biosense.production import discovery_runner as DR
        from unittest import mock
        req = DISC.build(project_id='ipsc_macrophage',
                         objective='Increase viable macrophage production while keeping identity.',
                         runtime_mode='synthetic_demo')
        draft = dict(EVCLI.TEMPLATE, effects=EVCLI.TEMPLATE['effects'][:1])
        K.write_json_atomic(self.tmp / 'quantified_hypothesis.json',
                            EVCLI.build_hypothesis(draft, project_id='ipsc_macrophage'))
        K.write_json_atomic(self.tmp / 'discovery_request.json', req)
        # Hand-written, in the shape an agent writes: not the full contract.
        (self.tmp / 'design_choices.json').write_text(json.dumps({'choices': [
            {'parameter_id': 'agitation_rpm', 'value': 55, 'confidence': 'moderate',
             'rationale': 'iPSC aggregate spinner practice', 'derived_from': ['PMID:0']}]}))
        real = PS._current_value
        with mock.patch.object(PS, '_current_value',
                               lambda pr, p, q, c: None if p == 'agitation_rpm' else real(pr, p, q, c)):
            final = DR.finish(req, self.tmp, runtime_mode='synthetic_demo')
        row = next(p for s in final['protocol']['stages'] for p in s['parameters']
                   if p['parameter_id'] == 'agitation_rpm')
        self.assertEqual(('design_choice', 55.0), (row['provenance'], row['recommended_value']))
        self.assertNotIn('agitation_rpm', [g['parameter_id'] for g in final['protocol']['gaps']])

    def test_a_design_choices_file_that_cannot_be_used_says_why(self):
        from biosense.production import discovery_runner as DR
        (self.tmp / 'design_choices.json').write_text(json.dumps({'choices': [
            {'parameter_id': 'agitation_rpm', 'value': 55, 'confidence': 'certain',
             'rationale': 'x', 'derived_from': ['y']}]}))
        choices, why = DR.read_design_choices(self.tmp, PJ.load('ipsc_macrophage'))
        self.assertEqual({}, choices)
        self.assertIn('design_choices.json was not used', why[0])
        self.assertIn('confidence', why[0])

    def test_direction_only_is_a_shape_and_is_accepted_as_one(self):
        eff = {'metric': 'identity_purity', 'unit': '%', 'direction': 'increase',
               'estimate_type': 'direction_only', 'reason': 'no magnitude in any source'}
        e = EVCLI._effect(eff, self.tmp)
        self.assertEqual(('derived', False), (e['estimate_type'], e['magnitude_estimated']))
        self.assertEqual('judgement', EVCLI._effect(dict(eff, estimate_type='best guess'), self.tmp)['estimate_type'])
        with self.assertRaisesRegex(K.ContractError, 'leave it out'):
            EVCLI._effect(dict(eff, estimate_type='vibes'), self.tmp)
