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
        fn = JS[JS.index('function toggleFocus'):JS.index('/* The plan a person takes')]
        self.assertIn("classList.toggle('wide', want)", fn)
        self.assertIn("document.body.classList.toggle('plan-wide', want)", fn)
        self.assertNotIn('has-focus', JS, 'the overlay is gone')
        # One column, not an overlay: nothing is hidden or covered.
        self.assertIn('body.plan-wide .cols{grid-template-columns:minmax(0,1fr)}', CSS)
        self.assertNotIn('#protocolPanel.focus', CSS)
        self.assertIn("'Widen'", JS)


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
