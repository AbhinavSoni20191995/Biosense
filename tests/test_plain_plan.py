"""The recommendation in plain words: the recipe once, then what each arm changes.

A scientist in the lab read the recommended protocol and could not tell what
to actually do: which factor goes in at which stage, and what each arm was
testing. Two things made it unreadable, and both are tested here.

**A lever has to find its stage.** A project names a parameter
`il12_maturation_ng_ml` and the published route names the factor `IL-12`. They
are one lever, and matching them by string equality placed no arm change at
all — so the stage went on showing the reference dose while the arm's real
change was printed at the bottom of the page as an unplaced parameter id.

**An arm shows what it changes and nothing else.** The old page printed the
whole recipe in every arm's column, so five identical columns hid the one line
that differed, and it read as though each arm set every value in it.
"""
import copy
import json
import unittest

from biosense.production import plain_plan as PP

# The shape that produced the unreadable export: the stage lines come from the
# published route, which names factors ("IL-12"); the arms set the project's
# own stage-qualified parameter ids ("il12_maturation_ng_ml").
NK = {
    'title': 'NK cells', 'objective': 'Generate NK cells from iPSC',
    'timeline': {'total_days': 22, 'candidates': [], 'stages': [
        {'stage_id': 'expansion', 'label': 'Expansion', 'day_start': 0, 'days': 7},
        {'stage_id': 'diff', 'label': 'Differentiation', 'day_start': 7, 'days': 10},
        {'stage_id': 'maturation', 'label': 'Maturation / harvest', 'day_start': 17,
         'days': 5}]},
    'stages': [], 'hypothesis_ledger': [], 'readouts': [], 'limitations': [],
    'blocks_wet_lab': False,
    'gaps': [{'parameter_id': 'vitronectin_coating', 'why': 'no source gives one'}],
    'first_pass': {
        'stages': [
            {'stage_id': 'expansion', 'label': 'Expansion', 'day_start': 0, 'days': 7,
             'inducers': [
                 {'parameter_id': 'y27632', 'label': 'Y-27632 (ROCK inhibitor)', 'unit': 'uM',
                  'value': 10, 'letter': 'PUB', 'provenance': 'reported',
                  'from_baseline': True}]},
            {'stage_id': 'diff', 'label': 'Differentiation', 'day_start': 7, 'days': 10,
             'inducers': [
                 {'parameter_id': 'il15', 'label': 'IL-15', 'unit': 'ng/mL', 'value': 5,
                  'letter': 'PUB', 'provenance': 'reported', 'from_baseline': True}]},
            {'stage_id': 'maturation', 'label': 'Maturation / harvest', 'day_start': 17,
             'days': 5, 'inducers': [
                 {'parameter_id': 'il15', 'label': 'IL-15', 'unit': 'ng/mL', 'value': 10,
                  'letter': 'PUB', 'provenance': 'reported', 'from_baseline': True},
                 {'parameter_id': 'il12', 'label': 'IL-12', 'unit': 'ng/mL', 'value': 0.2,
                  'letter': 'PUB', 'provenance': 'reported', 'from_baseline': True},
                 {'parameter_id': 'n803', 'label': 'IL-15 superagonist (N-803)', 'unit': None,
                  'value': None, 'letter': 'GAP', 'provenance': 'gap',
                  'from_baseline': True}]}],
        'unstaged': [],
        'physical': [
            {'parameter_id': 'feed_interval_h', 'label': 'Feed interval', 'unit': 'h',
             'value': 72, 'letter': 'D', 'provenance': 'design_choice', 'stage_id': 'all'},
            {'parameter_id': 'stage_duration_days', 'label': 'Stage duration', 'unit': 'days',
             'value': 22, 'letter': 'D', 'provenance': 'design_choice', 'stage_id': 'all'}]},
    'round_plan': {
        'round': 1, 'replicates': 3,
        'readouts': [{'name': 'CD16 positive fraction', 'unit': '%', 'when': 'harvest'}],
        'decision_rules': [{'unknown': 'U1', 'if': 'A1 raises CD16 over A0',
                            'then': 'carry it forward'}],
        'unknowns': [],
        'arms': [
            {'arm_id': 'A0', 'label': 'control: current process', 'control': True,
             'setpoints': {}},
            {'arm_id': 'A1', 'label': 'NK stage 21 days, no IL-12',
             'basis': 'the shorter end of the reported window',
             'setpoints': {'stage_duration_days': 21, 'il12_maturation_ng_ml': 0,
                           'il15_maturation_ng_ml': 10}},
            {'arm_id': 'A3', 'label': '21 days plus IL-12',
             'setpoints': {'stage_duration_days': 21, 'il12_maturation_ng_ml': 0.2,
                           'il15_maturation_ng_ml': 10}}]}}


def plan(**over):
    doc = copy.deepcopy(NK)
    doc.update(over)
    return doc


class StageMatchingTests(unittest.TestCase):
    def test_a_stage_qualified_id_finds_the_factor_the_route_names(self):
        stages = PP.stages_of(NK)
        self.assertEqual(('maturation', 'il12'),
                         PP.split_stage('il12_maturation_ng_ml', stages))
        placed = PP.place(['il12_maturation_ng_ml', 'il15_maturation_ng_ml',
                           'stage_duration_days'], stages)
        self.assertEqual('maturation', placed['il12_maturation_ng_ml'][0])
        self.assertEqual('maturation', placed['il15_maturation_ng_ml'][0],
                         'IL-15 is in two stages; the id names which one')
        self.assertEqual('all', placed['stage_duration_days'][0])

    def test_a_lever_that_names_no_stage_still_matches_by_name(self):
        stages = PP.stages_of(NK)
        self.assertEqual((None, 'feedinterval'),
                         PP.split_stage('feed_interval_h', stages))
        self.assertIn('feed_interval_h', PP.place(['feed_interval_h'], stages))

    def test_a_lever_nothing_carries_is_placed_nowhere_rather_than_guessed(self):
        self.assertEqual({}, PP.place(['some_unknown_lever'], PP.stages_of(NK)))


class RecipeTests(unittest.TestCase):
    def test_the_recipe_is_the_control_written_out_once_per_stage(self):
        rec = PP.recipe(NK)
        self.assertEqual(['Expansion', 'Differentiation', 'Maturation / harvest',
                          'Every stage'], [s['label'] for s in rec])
        maturation = next(s for s in rec if s['stage_id'] == 'maturation')
        self.assertEqual('day 17–22 (5 d)', maturation['window'])
        self.assertIn('IL-12 0.2 ng/mL', [line['text'] for line in maturation['lines']])

    def test_a_gap_is_said_to_be_undecided_and_never_filled_in(self):
        maturation = next(s for s in PP.recipe(NK) if s['stage_id'] == 'maturation')
        gap = next(line for line in maturation['lines'] if line['gap'])
        self.assertIsNone(gap['value'])
        self.assertIn('not decided yet', gap['text'])

    def test_a_control_arm_that_sets_a_value_is_the_reference_for_that_line(self):
        doc = plan()
        doc['round_plan']['arms'][0]['setpoints'] = {'il12_maturation_ng_ml': 0.5}
        maturation = next(s for s in PP.recipe(doc) if s['stage_id'] == 'maturation')
        il12 = next(line for line in maturation['lines'] if line['label'] == 'IL-12')
        self.assertEqual(0.5, il12['value'])


class ArmChangeTests(unittest.TestCase):
    def test_an_arm_lists_only_what_it_changes_and_names_the_stage(self):
        arms = {a['arm_id']: a for a in PP.changes(NK)}
        a1 = arms['A1']
        self.assertEqual(
            [('IL-12', 'Maturation / harvest', 0.2, 0),
             ('Stage duration', 'Every stage', 22, 21)],
            sorted((c['label'], c['stage_label'], c['from'], c['to']) for c in a1['changes']))
        self.assertEqual([], a1['unplaced'], 'both levers found their line')

    def test_a_setpoint_equal_to_the_reference_is_not_a_change(self):
        """A1 sets IL-15 to 10, which is what the reference already uses. An
        arm that is holding a value is not testing it, and listing it made
        every arm look as though it changed everything."""
        a1 = next(a for a in PP.changes(NK) if a['arm_id'] == 'A1')
        self.assertNotIn('IL-15', [c['label'] for c in a1['changes']])
        a3 = next(a for a in PP.changes(NK) if a['arm_id'] == 'A3')
        self.assertEqual(['Stage duration'], [c['label'] for c in a3['changes']],
                         'A3 sets IL-12 to the reference dose, so only the duration differs')

    def test_the_control_is_the_reference_and_changes_nothing(self):
        a0 = next(a for a in PP.changes(NK) if a['control'])
        self.assertEqual([], a0['changes'])
        self.assertIn('reference', a0['summary'])

    def test_an_arm_whose_every_setpoint_matches_says_so_rather_than_showing_blank(self):
        doc = plan()
        doc['round_plan']['arms'][2]['setpoints'] = {'il12_maturation_ng_ml': 0.2}
        a3 = next(a for a in PP.changes(doc) if a['arm_id'] == 'A3')
        self.assertEqual([], a3['changes'])
        self.assertIn('changes nothing', a3['summary'])

    def test_a_lever_no_stage_carries_is_reported_as_unplaced_not_as_absent(self):
        doc = plan()
        doc['round_plan']['arms'][1]['setpoints']['some_unknown_lever'] = 3
        a1 = next(a for a in PP.changes(doc) if a['arm_id'] == 'A1')
        self.assertEqual(['some_unknown_lever'],
                         [u['parameter_id'] for u in a1['unplaced']])

    def test_which_unknown_each_arm_tests_comes_from_the_fixed_rules(self):
        a1 = next(a for a in PP.changes(NK) if a['arm_id'] == 'A1')
        self.assertEqual(['U1'], a1['tests'])


class KeyPointTests(unittest.TestCase):
    def test_the_points_say_the_shape_of_the_round_in_sentences(self):
        points = PP.key_points(NK)
        self.assertEqual('computed', points['source'])
        text = ' '.join(points['points'])
        self.assertIn('3 arms × 3 replicates = 9 vessels', text)
        self.assertIn('One value is not decided yet', text)
        self.assertIn('Nothing here is approved', text)
        self.assertLessEqual(len(points['points']), 6, 'key points stay few enough to read')

    def test_the_communication_agents_own_points_win_where_it_wrote_them(self):
        """The agent whose job is plain language, and whose numbers were
        checked against the protocol, writes better sentences than a template."""
        points = PP.key_points(NK, {'key_points': ['Shorten the NK stage and drop IL-12.']})
        self.assertEqual('communication_agent', points['source'])
        self.assertEqual(['Shorten the NK stage and drop IL-12.'], points['points'])


class BuildTests(unittest.TestCase):
    def test_the_whole_plan_is_one_json_shape_the_page_and_the_export_share(self):
        d = PP.build(NK)
        self.assertEqual('plain_plan', d['kind'])
        self.assertEqual((3, 3, 9), (d['arms_count'], d['replicates'], d['vessels']))
        self.assertEqual('A0', d['reference_arm'])
        self.assertEqual(['CD16 positive fraction'], [m['name'] for m in d['measure']])
        self.assertEqual(['vitronectin_coating'],
                         [g['parameter_id'] for g in d['decide_first']])
        json.dumps(d)      # it travels to the browser as it stands

    def test_a_protocol_with_no_round_still_has_a_recipe(self):
        d = PP.build(plan(round_plan=None))
        self.assertEqual([], d['arms'])
        self.assertTrue(d['recipe'])
        self.assertIsNone(d['vessels'])

    def test_an_empty_protocol_does_not_crash_the_plan(self):
        d = PP.build({})
        self.assertEqual('plain_plan', d['kind'])
        self.assertEqual([], d['arms'])


class OlderRunTests(unittest.TestCase):
    """A run that finished before the plain plan existed opens in the new layout."""

    def test_a_record_without_a_plain_plan_gets_one_when_read_back(self):
        import shutil
        import tempfile
        from pathlib import Path
        from biosense.production import run_store as RS
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        d = tmp / 'ai-20260901-oldrun'
        d.mkdir()
        protocol = {k: v for k, v in NK.items()}       # as display() stored it, minus plain_plan
        RS.write_state(d, {'run_id': 'a' * 16, 'status': 'done', 'is_real': True,
                           'runtime_mode': 'local_real_ai', 'owner': 'ivy@lab.example',
                           'project_id': 'p', 'objective': 'o', 'run_dir': d.name,
                           'started_at': 1.0, 'finished_at': 2.0,
                           'result': {'protocol': protocol}})
        snap = RS.restore(tmp, 'a' * 16)
        plan = snap['result']['protocol']['plain_plan']
        self.assertEqual('plain_plan', plan['kind'])
        self.assertEqual(['A0', 'A1', 'A3'], [a['arm_id'] for a in plan['arms']])
        # Nothing was rewritten on disk: the record is read, not migrated.
        self.assertNotIn('plain_plan', RS.state_path(d).read_text())


class LongLabelTests(unittest.TestCase):
    """A lever the project does not carry arrives with its rationale as its label.

    The recipe is one line per factor, and a paragraph set as a pill is
    unreadable — so the name is the name, and what it cannot hold is the
    description, shown where there is room for it. Nothing is thrown away.
    """

    PARA = ('Activin A concentration across days 1-4 of mesoderm induction (ng/mL). The CERO '
            'anchor protocol adds no activin A and no activin/nodal inhibitor in this window, '
            'so the current value is effectively zero and unset rather than chosen. This is '
            'not a parameter the project has registered.')

    def test_a_paragraph_label_becomes_a_name_and_a_description(self):
        name, described = PP.split_name(self.PARA)
        self.assertEqual('Activin A concentration across days 1-4 of mesoderm induction', name)
        self.assertTrue(described.startswith('The CERO anchor protocol adds no activin A'))
        self.assertNotIn('ng/mL', described, 'the unit is its own field, not prose')

    def test_a_short_label_is_left_exactly_as_it_is(self):
        for label in ('IL-15', 'Feed interval', 'Agitation',
                      'Aggregate size at handover to differentiation (um diameter)'):
            name, described = PP.split_name(label)
            self.assertIsNone(described, label)
            self.assertLess(len(name), 60, label)
        self.assertEqual('IL-15', PP.split_name('IL-15')[0])

    def test_a_gloss_that_is_not_a_unit_is_kept_as_the_description(self):
        name, described = PP.split_name(
            'Day of IL-3 withdrawal relative to the start of GM-CSF in the maturation stage '
            '(0 = withdrawn on the day GM-CSF begins; negative = withdrawn before). Not a '
            'parameter this project has registered.')
        self.assertEqual('Day of IL-3 withdrawal relative to the start of GM-CSF in the '
                         'maturation stage', name)
        self.assertIn('0 = withdrawn on the day GM-CSF begins', described)

    def test_the_recipe_line_carries_both_and_its_text_is_the_short_one(self):
        doc = plan()
        doc['first_pass']['stages'][2]['inducers'].append(
            {'parameter_id': 'activin_a', 'label': self.PARA, 'unit': 'ng/mL', 'value': 0,
             'letter': 'NEW', 'provenance': 'candidate'})
        line = next(l for s in PP.recipe(doc) for l in s['lines']
                    if l['parameter_id'] == 'activin_a')
        self.assertEqual('Activin A concentration across days 1-4 of mesoderm induction',
                         line['name'])
        self.assertEqual('Activin A concentration across days 1-4 of mesoderm induction 0 ng/mL',
                         line['text'])
        self.assertEqual(self.PARA, line['label'], 'the whole label is still carried')
        self.assertTrue(line['description'])

    def test_an_arms_change_names_the_lever_not_its_rationale(self):
        doc = plan()
        doc['first_pass']['stages'][2]['inducers'].append(
            {'parameter_id': 'activin_a', 'label': self.PARA, 'unit': 'ng/mL', 'value': 0,
             'letter': 'NEW', 'provenance': 'candidate'})
        doc['round_plan']['arms'][1]['setpoints']['activin_a'] = 25
        change = next(c for a in PP.changes(doc) if a['arm_id'] == 'A1'
                      for c in a['changes'] if c['parameter_id'] == 'activin_a')
        self.assertEqual('Activin A concentration across days 1-4 of mesoderm induction',
                         change['label'])
        self.assertNotIn('CERO anchor protocol', change['text'])


class CompareRunTests(unittest.TestCase):
    """Adding the comparison to a run that has none.

    It is a run of its own, because the literature agent is the cost. What it
    must not do is turn into a second discovery run: one literature task, one
    file, and the file is handed back to the run it was asked for.
    """

    def _req(self, **over):
        from biosense.production import discovery as DISC
        req = DISC.build(project_id='ipsc_macrophage', objective='x' * 40,
                         runtime_mode='local_real_ai', purpose='compare')
        req.update(over)
        return req

    def test_a_compare_run_turns_the_comparison_on_and_nothing_else(self):
        from biosense.production import discovery as DISC
        self.assertIn('compare', DISC.PURPOSES)
        self.assertEqual(['state_of_the_art'], self._req()['addons'])

    def test_its_brief_compares_the_earlier_protocol_and_writes_one_file(self):
        from biosense.production import discovery as DISC
        req = self._req(continued_from={'kind': 'compare', 'run_id': 'abc123',
                                        'run_dir': 'ai-20260901-abc123'})
        brief = DISC.render_brief(req, loop_dir='runs/ai-x')
        self.assertIn('adds the state-of-the-art comparison to an earlier run', brief)
        self.assertIn('`abc123`', brief)
        self.assertIn('runs/ai-x/protocol_summary.json', brief)
        self.assertIn('runs/ai-x/sota_comparison.json', brief)
        self.assertIn('do not write a new protocol', brief)
        self.assertIn('literature-sota', brief)
        self.assertIn('send no bioinformatics task', brief)
        # Not a discovery run: it designs nothing of its own.
        self.assertNotIn('literature-it1', brief)
        self.assertNotIn('DISCOVERY EVIDENCE:', brief)

    def test_a_discovery_run_still_reads_as_it_did(self):
        from biosense.production import discovery as DISC
        brief = DISC.render_brief(DISC.build(
            project_id='ipsc_macrophage', objective='x' * 40,
            runtime_mode='local_real_ai'), loop_dir='runs/ai-x')
        self.assertIn('literature-it1', brief)
        self.assertIn('literature-sota', brief, 'the add-on is on by default')
        self.assertNotIn('adds the state-of-the-art comparison to an earlier run', brief)

    def test_the_comparison_is_handed_back_and_the_parents_own_is_never_overwritten(self):
        import shutil
        import tempfile
        from pathlib import Path
        from biosense import contracts as K
        from biosense.production import app as APP
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        child, parent = tmp / 'child', tmp / 'parent'
        (child / 'literature' / 'sota').mkdir(parents=True)
        parent.mkdir()
        doc = {'kind': 'sota_comparison', 'references': [], 'comparisons': [],
               'standing': {'verdict': 'variant_of_sota'}}
        K.write_json_atomic(child / 'sota_comparison.json', doc)
        (child / 'literature' / 'sota' / 'discover.json').write_text('{}')
        self.assertTrue(APP.hand_back_comparison(child, parent))
        self.assertEqual(doc['kind'], K.read_json(parent / 'sota_comparison.json')['kind'])
        self.assertTrue((parent / 'literature' / 'sota' / 'discover.json').is_file())
        self.assertTrue((child / 'sota_comparison.json').is_file(), 'copied, never moved')
        # A second hand-back leaves what a person has already read alone.
        self.assertFalse(APP.hand_back_comparison(child, parent))
        # And nothing is handed back when there is nothing to hand back.
        (child / 'sota_comparison.json').unlink()
        self.assertFalse(APP.hand_back_comparison(child, tmp / 'other'))

    def test_a_run_reads_back_a_comparison_written_after_it_ended(self):
        import shutil
        import tempfile
        from pathlib import Path
        from biosense import contracts as K
        from biosense.production import run_store as RS
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        d = tmp / 'ai-20260901-parent'
        d.mkdir()
        RS.write_state(d, {'run_id': 'b' * 16, 'status': 'done', 'is_real': True,
                           'runtime_mode': 'local_real_ai', 'owner': None,
                           'project_id': 'p', 'objective': 'o', 'run_dir': d.name,
                           'started_at': 1.0, 'finished_at': 2.0, 'result': {}})
        self.assertIsNone(RS.restore(tmp, 'b' * 16).get('sota_comparison'))
        K.write_json_atomic(d / 'sota_comparison.json', {
            'kind': 'sota_comparison', 'cell_type': 'macrophage', 'references': [],
            'comparisons': [], 'standing': {'verdict': 'variant_of_sota', 'summary': 's'},
            'limitations': [], 'note': 'n'})
        got = RS.restore(tmp, 'b' * 16)['sota_comparison']
        self.assertEqual('variant_of_sota', got['standing']['verdict'])
