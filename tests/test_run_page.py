"""The run page is one reading order, and a table row shows what it cut short.

The page once offered "Arrange as a canvas": every box free to drag, widen and
minimise. A scientist found the controls in the way and the arrangement buggy,
and asked for the reading order alone. These hold that the feature is gone for
good — no script, no styles, no toolbar — so it cannot creep back in a
stylesheet merge.
"""
import unittest

from biosense import contracts as K

WEB = K.ROOT / 'webapp'


class NoCanvasTests(unittest.TestCase):
    def test_the_canvas_script_and_its_toolbar_are_gone(self):
        self.assertFalse((WEB / 'canvas.js').exists())
        html = (WEB / 'console.html').read_text()
        self.assertNotIn('canvas.js', html)
        for text in ('Arrange as a canvas', 'Reset layout', 'Back to the page layout'):
            for name in ('console.html', 'discovery.js', 'shell.js', 'runview.js'):
                self.assertNotIn(text, (WEB / name).read_text(), f'{text} in {name}')

    def test_no_style_waits_for_a_box_to_be_freed(self):
        for name in ('brand.css', 'shell.css'):
            css = (WEB / name).read_text()
            self.assertNotIn('.cols.canvas', css, name)
            self.assertNotIn(':not(.canvas)', css, name)
            self.assertNotIn('.cv-', css, name)

    def test_the_tabs_and_the_docked_live_view_still_have_their_rules(self):
        css = (WEB / 'brand.css').read_text()
        self.assertIn('.cols > .stack > section.card.tab-off{display:none}', css)
        self.assertIn('.cols > .stack > section.card#progressPanel.docked{position:fixed;', css)


class TableRowTests(unittest.TestCase):
    def test_an_open_row_shows_the_whole_of_what_its_closed_line_cut(self):
        css = (WEB / 'brand.css').read_text()
        self.assertIn('.ft tr.open .clip{display:inline;-webkit-line-clamp:unset', css)
        js = (WEB / 'discovery.js').read_text()
        self.assertIn("td.querySelectorAll('.clip').forEach(n => { if (!n.title) n.title = n.textContent; })",
                      js, 'and the whole text is on hover while it is closed')


if __name__ == '__main__':
    unittest.main()
