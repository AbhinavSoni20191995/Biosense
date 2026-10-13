"""The run page as a canvas, and a table row that shows what it cut short.

A scientist asked to take the boxes out of the page's column order and lay
them out themselves, and pointed at a round's unknown cut off mid-sentence.
The behaviour is checked in a browser; these hold the pieces it rests on so a
later edit cannot quietly drop one.
"""
import re
import unittest

from biosense import contracts as K

WEB = K.ROOT / 'webapp'


class CanvasTests(unittest.TestCase):
    def setUp(self):
        self.js = (WEB / 'canvas.js').read_text()
        self.html = (WEB / 'console.html').read_text()
        self.css = (WEB / 'brand.css').read_text()

    def test_the_run_page_loads_it_after_the_page_s_own_script(self):
        self.assertLess(self.html.index('src="discovery.js"'), self.html.index('src="canvas.js"'))

    def test_the_layout_is_the_viewer_s_and_survives_without_storage(self):
        self.assertIn("'bs-canvas-layout-v2'", self.js)
        self.assertIn("OLD_KEY = 'bs-canvas-layout-v1'", self.js,
                      'a first canvas is carried over, not thrown away')
        # Every read and write of browser storage is guarded: private mode
        # and blocked storage leave the page working.
        reads = len(re.findall(r'localStorage\.\w+Item', self.js))
        self.assertEqual(reads, len(re.findall(r'try \{ (?:const v = )?localStorage', self.js)))

    def test_a_drag_does_not_also_fold_a_box(self):
        self.assertIn('justMoved', self.js)
        self.assertIn("cols.addEventListener('click'", self.js)
        self.assertIn('stopImmediatePropagation', self.js)

    def test_boxes_move_and_widen_from_either_side_and_heighten_from_the_bottom(self):
        for piece in ("'pointerdown'", "'pointermove'", "['e', 'w', 's', 'se', 'sw']",
                      '.cv-h-e{', '.cv-h-w{', '.cv-h-s{', 'cursor:ew-resize',
                      'Reset layout', 'Back to the page layout'):
            self.assertIn(piece, self.js + self.css, piece)

    def test_one_box_detaches_into_a_window_and_docks_back(self):
        self.assertIn('function detach(c, saved)', self.js)
        self.assertIn('function dock(c)', self.js)
        self.assertIn('section.card.cv-float{position:fixed', self.css)
        self.assertIn('wholly on screen', self.js)

    def test_a_closed_box_stays_as_its_title_and_the_title_brings_it_back(self):
        self.assertIn('section.card.cv-min{opacity:.6', self.css)
        self.assertIn("section.card.cv-min > :not(.head):not(.cv-ctl):not(.cv-h){display:none", self.css)
        self.assertIn('restore(c);', self.js)
        self.assertIn('Open ${mins} minimised', self.js)

    def test_a_free_box_scrolls_its_body_under_a_fixed_title(self):
        self.assertIn('.cv-body{display:contents}', self.css, 'the page lays out as before')
        self.assertIn('.cv-free > .cv-body{display:block;flex:1 1 auto;min-height:0;overflow:auto}',
                      self.css)

    def test_a_phone_keeps_the_reading_order(self):
        self.assertIn("matchMedia('(max-width: 900px)')", self.js)
        self.assertIn('@media (max-width:900px){.cv-bar{display:none}.cv-det{display:none}}', self.css)

    def test_a_card_the_run_reveals_later_finds_a_free_spot(self):
        self.assertIn("attributeFilter: ['hidden']", self.js)
        self.assertIn('function freeSpot', self.js)


class TableRowTests(unittest.TestCase):
    def test_an_open_row_shows_the_whole_of_what_its_closed_line_cut(self):
        css = (WEB / 'brand.css').read_text()
        self.assertIn('.ft tr.open .clip{display:inline;-webkit-line-clamp:unset', css)
        js = (WEB / 'discovery.js').read_text()
        self.assertIn("td.querySelectorAll('.clip').forEach(n => { if (!n.title) n.title = n.textContent; })",
                      js, 'and the whole text is on hover while it is closed')


if __name__ == '__main__':
    unittest.main()
