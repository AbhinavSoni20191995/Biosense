"""The answer is what the page is for, once the run has ended.

While a run is going the live tracking is the page. When it ends, the tracking
folds itself away, the page goes to the recommendation, and the plan can be put
on screen on its own. The plan is also drawn: the stages as vessels in a line
with what goes into each, from the same first_pass the boxes use, so the
picture and the list can never disagree.
"""
import unittest

from biosense import contracts as K


class LivePanelTests(unittest.TestCase):
    def test_the_live_panel_folds_like_the_others(self):
        html = (K.ROOT / 'webapp' / 'console.html').read_text()
        live = html[html.index('id="progressPanel"') - 120:html.index('id="progressPanel"') + 60]
        self.assertIn('fold', live, 'the live panel is foldable')
        self.assertIn('data-open', live)

    def test_an_ended_run_folds_the_tracking_and_goes_to_the_answer(self):
        js = (K.ROOT / 'webapp' / 'discovery.js').read_text()
        self.assertIn('function focusTheAnswer', js)
        self.assertIn('focusTheAnswer(snap)', js)
        fn = js[js.index('function focusTheAnswer'):js.index('async function stopRun')]
        # Open while live, folded once, and only once per run so a person who
        # opens it again keeps it open.
        self.assertIn("if (live) { panel.dataset.open = '1'; return; }", fn)
        self.assertIn('state.focusedRun === snap.run_id', fn)
        self.assertIn("panel.dataset.open = '0'", fn)
        self.assertIn('scrollIntoView', fn)


class FlowTests(unittest.TestCase):
    def test_the_plan_is_drawn_from_the_same_first_pass_the_boxes_use(self):
        js = (K.ROOT / 'webapp' / 'discovery.js').read_text()
        self.assertIn('function renderFlow', js)
        self.assertIn('host.append(renderFlow(p.first_pass));', js)
        self.assertIn('host.append(renderPlan(p.first_pass));', js)
        flow = js[js.index('function renderFlow'):js.index('/* The plan on its own')]
        self.assertIn('fp.stages', flow)
        self.assertIn('s.inducers', flow)
        self.assertIn('vessel(', flow, 'a bioreactor, not a box')
        self.assertNotIn('innerHTML', flow)

    def test_the_plan_can_fill_the_screen_and_escape_leaves(self):
        js = (K.ROOT / 'webapp' / 'discovery.js').read_text()
        self.assertIn('function toggleFocus', js)
        self.assertIn("e.key === 'Escape'", js[js.index('function toggleFocus'):])
        self.assertIn("'Full screen'", js)
        css = (K.ROOT / 'webapp' / 'brand.css').read_text()
        self.assertIn('#protocolPanel.focus', css)
        self.assertIn('position:fixed', css[css.index('#protocolPanel.focus'):][:200])
        self.assertIn('.fl-tank', css)


if __name__ == '__main__':
    unittest.main()
