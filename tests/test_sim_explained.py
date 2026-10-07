"""Every knob says how it reaches growth and harvest, and the README shows the same words."""
import unittest

from biosense import contracts as K
from biosense.production import sim_mode as SM


class ExplainedTests(unittest.TestCase):
    def test_every_knob_has_its_mechanism_and_the_config_serves_it(self):
        cfg = SM.config()
        for k in cfg['knobs']:
            self.assertTrue(k.get('acts_on') and len(k.get('how') or '') > 40, k['id'])
        self.assertIn('new_parameters', cfg['model_summary'])

    def test_the_readme_table_matches_the_model(self):
        readme = (K.ROOT / 'README.md').read_text()
        for k in SM.KNOBS:
            self.assertIn(k['how'].replace('|', '/'), readme,
                          f'{k["id"]}: README is stale; regenerate the table from sim_mode')


if __name__ == '__main__':
    unittest.main()
