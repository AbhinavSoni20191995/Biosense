"""Reading the plan, and building one from what the run proposed.

Three things a person hit. The "Why" fold shut itself a second after they
opened it, because every snapshot rebuilt the result. "Full screen" covered
the page instead of widening the section. And a stage with no inducer said
"None set", which reads as "nothing belongs here" when it means "this project
has no parameter that could carry a dose here" — with the lever the run
proposed sitting one click away from fixing it.
"""
import unittest

from biosense import contracts as K

JS = (K.ROOT / 'webapp' / 'discovery.js').read_text()
CSS = (K.ROOT / 'webapp' / 'brand.css').read_text()


class RerenderTests(unittest.TestCase):
    def test_the_result_is_built_once_so_what_a_reader_opened_stays_open(self):
        self.assertIn('let resultSeen', JS)
        fn = JS[JS.index('function renderResult'):JS.index('function renderEvidence')]
        self.assertIn('if (key === resultSeen)', fn)
        self.assertIn("$('#resultPanel').hidden = false; return;", fn)


class WidenTests(unittest.TestCase):
    def test_widening_keeps_the_rest_of_the_page(self):
        fn = JS[JS.index('function toggleFocus'):JS.index('function wireWiden')]
        self.assertIn("classList.toggle('wide', want)", fn)
        # One column while any section is wide; nothing is hidden or covered.
        self.assertIn("document.body.classList.toggle('plan-wide', any)", fn)
        self.assertNotIn('has-focus', JS, 'the overlay is gone')
        self.assertIn('body.plan-wide .cols{grid-template-columns:minmax(0,1fr)}', CSS)
        self.assertNotIn('#protocolPanel.focus', CSS)
        self.assertIn("'Widen'", JS)

    def test_every_section_can_be_widened_not_only_the_parameters(self):
        """The one box that could take the page's width was the parameters;
        a person reading the round or the comparison wanted the same."""
        fn = JS[JS.index('function wireWiden'):JS.index('const TABS')]
        self.assertIn("section.card[data-tab], #plainPanel", fn)
        self.assertIn("'btn sm widen'", fn)
        self.assertIn("if (e.key === 'Escape') narrowAll()", JS)
        self.assertIn('section.card.wide{grid-column:1 / -1}', CSS)


HTML = (K.ROOT / 'webapp' / 'console.html').read_text()


class TabsTests(unittest.TestCase):
    """The run page as a piece of software: the finding on top, one section at
    a time below it, chosen from a tab bar. The page was eight stacked cards
    of text, and the thing a person needed was lost among them."""

    def test_the_finding_stays_on_screen_and_the_rest_is_one_tab_at_a_time(self):
        self.assertLess(HTML.index('id="plainPanel"'), HTML.index('id="resultNav"'))
        self.assertNotIn('data-tab', HTML[HTML.index('id="plainPanel"'):HTML.index('id="resultNav"')],
                         'the finding is not a tab; it is always shown')
        self.assertIn('.cols:not(.canvas) > .stack > section.card.tab-off{display:none}', CSS)

    def test_the_highlighted_tabs_are_what_a_person_acts_on(self):
        tabs = JS[JS.index('const TABS = ['):JS.index('];', JS.index('const TABS = ['))]
        main = [line for line in tabs.splitlines() if 'main: true' in line]
        for key in ('hyp', 'compare', 'round', 'params', 'help', 'ask', 'run'):
            self.assertTrue(any(f"key: '{key}'" in line for line in main), key)
        for key in ('detail', 'other', 'alt', 'bt', 'evidence', 'bench'):
            self.assertIn(f"key: '{key}'", tabs)
            self.assertFalse(any(f"key: '{key}'" in line for line in main),
                             f'{key} is supplementary: a small tab, not a highlighted one')
        self.assertIn(".tabs-more .tab{font-size:10.5px", CSS)

    def test_a_tab_exists_only_where_its_section_has_something_to_show(self):
        fn = JS[JS.index('function renderTabs'):JS.index('function showTab')]
        self.assertIn("return n && !n.hidden;", fn, 'the renderer decides, the tab bar reads')
        # And on the canvas every box is free, so the tabs stand down.
        self.assertIn('.cols.canvas > #resultNav', CSS)

    def test_each_tabbed_section_is_marked_and_the_record_moved_in_with_them(self):
        for sid in ('resultPanel', 'comparePanel', 'roundPanel', 'protocolPanel', 'askDataPanel',
                    'askPanel', 'runInfoPanel', 'insightsPanel', 'otherPanel', 'altPanel',
                    'btPanel', 'evidencePanel', 'benchPanel'):
            self.assertRegex(HTML, rf'id="{sid}" data-tab="[a-z]+"', sid)
        self.assertNotIn('class="glass card fold" id="evidencePanel"', HTML,
                         'the evidence record is a supplementary tab, not a right-hand fold')

    def test_the_comparison_puts_the_field_in_one_table_with_this_recommendation_last(self):
        fn = JS[JS.index('function renderCompare'):JS.index('function renderRunInfo')]
        self.assertIn("['Protocol', 'Route', 'Yield', 'Purity', 'Timeline']", fn)
        self.assertIn("el('tr', 'ours')", fn)
        self.assertIn("'to be measured in round 1'", fn, 'an unrun protocol has no yield to claim')
        self.assertIn('never a measured gain', fn)
        self.assertIn("'The published protocols in detail'", fn, 'the detail is a dropdown')

    def test_the_runs_own_details_are_findable(self):
        """The prompt, the scope, the datasets, the runtime and the account: a
        person could not find them after the run. Now they are a tab."""
        fn = JS[JS.index('function renderRunInfo'):JS.index('/* The plan a person takes')]
        for field in ("'What was asked'", "kv('Project'", "kv('Runtime'", "kv('Datasets'",
                      "kv('Scope'", "kv('Account'", "'As the orchestrator was told it'",
                      "'The full request record'"):
            self.assertIn(field, fn, field)


class BuildTests(unittest.TestCase):
    def test_an_empty_stage_says_why_rather_than_none_set(self):
        fn = JS[JS.index('function renderPlan'):JS.index('function renderBenchmark')]
        self.assertIn('This project has no inducer registered for this ', fn)
        self.assertNotIn('None set for this stage in this run.', fn)

    def test_every_stage_offers_a_way_to_add_one(self):
        fn = JS[JS.index('function renderPlan'):JS.index('function renderBenchmark')]
        self.assertIn('addInducer(p, s.stage_id, stages)', fn)
        self.assertIn('function addInducer', JS)
        self.assertIn("'+ Add an inducer'", JS)

    def test_a_lever_the_run_proposed_is_one_click_from_being_carried(self):
        item = JS[JS.index('function planItem'):JS.index('function renderPlan')]
        self.assertIn('if (!i.registered && p)', item)
        self.assertIn('registerForm(p, {', item)

    def test_a_proposal_already_in_its_stage_is_not_listed_twice(self):
        fn = JS[JS.index('function renderPlan'):JS.index('function renderBenchmark')]
        self.assertIn('const here = new Set(s.inducers.map(i => i.parameter_id));', fn)
        self.assertIn('!here.has(c.parameter_id)', fn)

    def test_a_lever_with_no_candidate_behind_it_is_named_by_the_person(self):
        fn = JS[JS.index('function registerForm'):JS.index('function showBasis')]
        self.assertIn("const name = cand.label ? null : field('Name'", fn)
        self.assertIn('label: cand.label || (name && name.value.trim()),', fn)
        # It still goes through the one endpoint that rebuilds the protocol.
        self.assertIn('/parameters`', fn)
        self.assertIn('run_id: runId', fn)


if __name__ == '__main__':
    unittest.main()
