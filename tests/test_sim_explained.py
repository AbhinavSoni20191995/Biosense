"""Every knob says how it reaches growth and harvest, and the model doc shows the same words."""
import unittest

from biosense import contracts as K
from biosense.production import sim_mode as SM


class ExplainedTests(unittest.TestCase):
    def test_every_knob_has_its_mechanism_and_the_config_serves_it(self):
        cfg = SM.config()
        for k in cfg['knobs']:
            self.assertTrue(k.get('acts_on') and len(k.get('how') or '') > 40, k['id'])
        self.assertIn('new_parameters', cfg['model_summary'])

    def test_the_model_doc_table_matches_the_model(self):
        # The knob-by-knob table lives with the model's own documentation, not
        # the README, which only points at it.
        doc = (K.ROOT / 'docs' / 'BIOSIMULATOR_MODEL.md').read_text()
        for k in SM.KNOBS:
            self.assertIn(k['how'].replace('|', '/'), doc,
                          f'{k["id"]}: BIOSIMULATOR_MODEL.md is stale; '
                          f'regenerate the table from sim_mode')


if __name__ == '__main__':
    unittest.main()
