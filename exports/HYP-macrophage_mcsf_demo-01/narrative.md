# HYP-macrophage_mcsf_demo-01 in plain language

BioSense is asking whether raising M-CSF from 25 to 50 ng/mL would improve the process. Changing M-CSF may improve the objective: Increase viable macrophage production while maintaining macrophage identity and viability. CD14 pos pct goes from 41.18% to 67.58%, a change of +26.4 percentage points (+64.1%) — calculated from measurements. CD16 pos pct goes from 16.93% to 31%, a change of +14.06 percentage points (+83.04%) — calculated from measurements. viability pct goes from 93.96% to 91.04%, a change of -2.922 percentage points (-3.11%) — calculated from measurements. CD206 pos pct goes from 33.5% to 57.52%, a change of +24.02 percentage points (+71.69%) — calculated from measurements. Monocytes per input iPSC goes from 17.99 cells/input_cell to 29.66 cells/input_cell, a change of +11.67 cells/input_cell (+64.87%) — predicted by the simulator. Harvested cells goes from 8.995 1e6 cells/mL to 14.83 1e6 cells/mL, a change of +5.835 1e6 cells/mL (+64.87%) — predicted by the simulator. Final viability goes from 80.78% to 80.78%, a change of +0 percentage points (+0%) — predicted by the simulator. Cells in the monocyte gate goes from 76.66% to 90.25%, a change of +13.59 percentage points (+17.73%) — predicted by the simulator. Peak viable cell density goes from 4.762 1e6 cells/mL to 4.762 1e6 cells/mL, a change of +0 1e6 cells/mL (+0%) — predicted by the simulator. Mean aggregate diameter goes from 273.9 um to 273.9 um, a change of +0 um (+0%) — predicted by the simulator. Mean condition score goes from 92.7 score to 92.7 score, a change of +0 score (+0%) — predicted by the simulator. This rests on simulation, synthetic fixture. Confidence is moderate. Test M-CSF at 20, 50, 80 ng/mL against the current process, measuring monocytes per input ipsc, harvested cells, final viability. This is evidence for a decision. It does not change the protocol by itself.

## The facts this was checked against

Every number above matches one of these. The prose is a rendering of these values, not a separate account of them.

| fact | value | unit | estimate type |
| --- | --- | --- | --- |
| hyp.id | None |  |  |
| hyp.statement | None |  |  |
| hyp.confidence | None |  |  |
| hyp.parameter | None |  |  |
| hyp.direction | None |  |  |
| hyp.coverage | None |  |  |
| hyp.current_value | 25.0 | ng/mL |  |
| hyp.candidate_value | 50 | ng/mL |  |
| hyp.range_low | 0.0 | ng/mL |  |
| hyp.range_high | 150.0 | ng/mL |  |
| hyp.effect.CD14_pos_pct.baseline | 41.181666666666665 | % | measured |
| hyp.effect.CD14_pos_pct.candidate | 67.58 | % | measured |
| hyp.effect.CD14_pos_pct.absolute | 26.398333 | percentage_points | derived |
| hyp.effect.CD14_pos_pct.relative | 64.1 | % | derived |
| hyp.effect.CD14_pos_pct.ci_low | 21.471666666666657 | % | derived |
| hyp.effect.CD14_pos_pct.ci_high | 31.488333333333337 | % | derived |
| hyp.effect.CD14_pos_pct.n_baseline | 3 |  |  |
| hyp.effect.CD14_pos_pct.n_candidate | 3 |  |  |
| hyp.effect.CD14_pos_pct.q_value | 0.00176 |  |  |
| hyp.effect.CD16_pos_pct.baseline | 16.933333333333334 | % | measured |
| hyp.effect.CD16_pos_pct.candidate | 30.995000000000005 | % | measured |
| hyp.effect.CD16_pos_pct.absolute | 14.061667 | percentage_points | derived |
| hyp.effect.CD16_pos_pct.relative | 83.04 | % | derived |
| hyp.effect.CD16_pos_pct.ci_low | 12.761666666666667 | % | derived |
| hyp.effect.CD16_pos_pct.ci_high | 15.361666666666668 | % | derived |
| hyp.effect.CD16_pos_pct.n_baseline | 3 |  |  |
| hyp.effect.CD16_pos_pct.n_candidate | 3 |  |  |
| hyp.effect.CD16_pos_pct.q_value | 0.000596 |  |  |
| hyp.effect.viability_pct.baseline | 93.96333333333332 | % | measured |
| hyp.effect.viability_pct.candidate | 91.04166666666667 | % | measured |
| hyp.effect.viability_pct.absolute | -2.921667 | percentage_points | derived |
| hyp.effect.viability_pct.relative | -3.11 | % | derived |
| hyp.effect.viability_pct.ci_low | -3.518333333333331 | % | derived |
| hyp.effect.viability_pct.ci_high | -2.34999999999998 | % | derived |
| hyp.effect.viability_pct.n_baseline | 3 |  |  |
| hyp.effect.viability_pct.n_candidate | 3 |  |  |
| hyp.effect.viability_pct.q_value | 0.00686 |  |  |
| hyp.effect.CD206_pos_pct.baseline | 33.501666666666665 | % | measured |
| hyp.effect.CD206_pos_pct.candidate | 57.51833333333334 | % | measured |
| hyp.effect.CD206_pos_pct.absolute | 24.016667 | percentage_points | derived |
| hyp.effect.CD206_pos_pct.relative | 71.69 | % | derived |
| hyp.effect.CD206_pos_pct.ci_low | 21.35333333333333 | % | derived |
| hyp.effect.CD206_pos_pct.ci_high | 26.72833333333333 | % | derived |
| hyp.effect.CD206_pos_pct.n_baseline | 3 |  |  |
| hyp.effect.CD206_pos_pct.n_candidate | 3 |  |  |
| hyp.effect.CD206_pos_pct.q_value | 0.000671 |  |  |
| hyp.effect.harvest_yield_per_input_ipsc.baseline | 17.9908 | cells/input_cell | simulated |
| hyp.effect.harvest_yield_per_input_ipsc.candidate | 29.6618 | cells/input_cell | simulated |
| hyp.effect.harvest_yield_per_input_ipsc.absolute | 11.671 | cells/input_cell | simulated |
| hyp.effect.harvest_yield_per_input_ipsc.relative | 64.87 | % | simulated |
| hyp.effect.harvest_total_e6_per_ml.baseline | 8.9954 | 1e6 cells/mL | simulated |
| hyp.effect.harvest_total_e6_per_ml.candidate | 14.8309 | 1e6 cells/mL | simulated |
| hyp.effect.harvest_total_e6_per_ml.absolute | 5.8355 | 1e6 cells/mL | simulated |
| hyp.effect.harvest_total_e6_per_ml.relative | 64.87 | % | simulated |
| hyp.effect.final_viability_pct.baseline | 80.78 | % | simulated |
| hyp.effect.final_viability_pct.candidate | 80.78 | % | simulated |
| hyp.effect.final_viability_pct.absolute | 0.0 | percentage_points | simulated |
| hyp.effect.final_viability_pct.relative | 0.0 | % | simulated |
| hyp.effect.monocyte_gate_pct.baseline | 76.66 | % | simulated |
| hyp.effect.monocyte_gate_pct.candidate | 90.25 | % | simulated |
| hyp.effect.monocyte_gate_pct.absolute | 13.59 | percentage_points | simulated |
| hyp.effect.monocyte_gate_pct.relative | 17.73 | % | simulated |
| hyp.effect.peak_vcd_e6_per_ml.baseline | 4.7619 | 1e6 cells/mL | simulated |
| hyp.effect.peak_vcd_e6_per_ml.candidate | 4.7619 | 1e6 cells/mL | simulated |
| hyp.effect.peak_vcd_e6_per_ml.absolute | 0.0 | 1e6 cells/mL | simulated |
| hyp.effect.peak_vcd_e6_per_ml.relative | 0.0 | % | simulated |
| hyp.effect.agg_diameter_mean_um.baseline | 273.9 | um | simulated |
| hyp.effect.agg_diameter_mean_um.candidate | 273.9 | um | simulated |
| hyp.effect.agg_diameter_mean_um.absolute | 0.0 | um | simulated |
| hyp.effect.agg_diameter_mean_um.relative | 0.0 | % | simulated |
| hyp.effect.mean_condition_score.baseline | 92.7 | score | simulated |
| hyp.effect.mean_condition_score.candidate | 92.7 | score | simulated |
| hyp.effect.mean_condition_score.absolute | 0.0 | score | simulated |
| hyp.effect.mean_condition_score.relative | 0.0 | % | simulated |
| hyp.test_point.0 | 20.0 | ng/mL |  |
| hyp.test_point.1 | 50.0 | ng/mL |  |
| hyp.test_point.2 | 80.0 | ng/mL |  |
| hyp.evidence_count | 2 |  |  |
