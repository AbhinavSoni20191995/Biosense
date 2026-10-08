"""A resumed live run ended "The run hit an unexpected error" after its agents
had finished: one part of BioSense's final assembly raised, and the whole run
was lost. Each part is now assembled on its own, the error is named, and units
written in another spelling are the same unit.
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from biosense import contracts as K
from biosense import projects as PJ
from biosense.evidence import cli as EVCLI
from biosense.evidence import design as DC
from biosense.production import discovery as DISC
from biosense.production import discovery_runner as DR


def choice(**kw):
    row = {'parameter_id': 'agitation_rpm', 'value': 55, 'confidence': 'moderate',
           'rationale': 'iPSC aggregate spinner practice', 'derived_from': ['PMID:0']}
    row.update(kw)
    return DC.choice(row.pop('parameter_id'), row.pop('value'), project=PJ.load('ipsc_macrophage'),
                     **{('range_' if k == 'range' else k): v for k, v in row.items()})


class UnitAndRangeTests(unittest.TestCase):
    def test_a_unit_in_another_spelling_is_the_same_unit(self):
        self.assertEqual(DC._unit('h'), DC._unit('hours'))
        self.assertEqual(DC._unit('fraction of volume'), DC._unit('fraction of working volume'))
        self.assertNotEqual(DC._unit('fraction'), DC._unit('% O2'), 'a conversion is not a spelling')
        c = choice(parameter_id='feed_interval_h', value=24, unit='hours')
        self.assertEqual(24.0, c['value'], '"hours" is accepted where the unit is h')
        with self.assertRaisesRegex(K.ContractError, 'give the value in that unit'):
            choice(parameter_id='do_setpoint', value=0.2, unit='% O2')

    def test_a_range_in_the_shapes_agents_write(self):
        for r in ({'lower': 40, 'upper': 70}, {'low': 40, 'high': 70}, {'min': 70, 'max': 40}, [40, 70]):
            self.assertEqual({'lower': 40.0, 'upper': 70.0}, choice(range=r)['range'], r)
        with self.assertRaisesRegex(K.ContractError, 'a range is'):
            choice(range={'from_value': 40})


class FinishTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.req = DISC.build(project_id='ipsc_macrophage',
                              objective='Increase viable macrophage production while keeping identity.',
                              runtime_mode='synthetic_demo')
        draft = dict(EVCLI.TEMPLATE, effects=EVCLI.TEMPLATE['effects'][:1])
        K.write_json_atomic(self.tmp / 'quantified_hypothesis.json',
                            EVCLI.build_hypothesis(draft, project_id='ipsc_macrophage'))
        K.write_json_atomic(self.tmp / 'discovery_request.json', self.req)

    def test_a_malformed_design_choices_file_is_a_limitation_not_a_crash(self):
        (self.tmp / 'design_choices.json').write_text(json.dumps(
            [{'parameter_id': 'agitation_rpm', 'value': 55, 'range': {'from_value': 1}}]))
        final = DR.finish(self.req, self.tmp, runtime_mode='synthetic_demo')
        self.assertIsNotNone(final['protocol'])
        self.assertTrue(any('design_choices.json was not used' in x
                            for x in final['protocol']['limitations']))

    def test_a_part_that_raises_keeps_the_rest_of_the_run(self):
        with mock.patch.object(DR, 'ensure_round_plan', side_effect=TypeError('boom')):
            final = DR.finish(self.req, self.tmp, runtime_mode='synthetic_demo')
        self.assertIsNotNone(final['protocol'], 'the protocol is still the answer')
        self.assertTrue(any('could not assemble the round plan (TypeError: boom)' in x
                            for x in final['protocol']['limitations']))
        self.assertIn('TypeError: boom', (self.tmp / 'finish_errors.log').read_text())


class ErrorMessageTests(unittest.TestCase):
    def test_an_unexpected_error_names_itself(self):
        from biosense.production import app as APP
        src = Path(APP.__file__).read_text()
        self.assertIn("'simple': f'The run hit an unexpected error in BioSense: '", src)
        self.assertIn("'error.log'", src)


if __name__ == '__main__':
    unittest.main()
