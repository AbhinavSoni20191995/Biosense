# Increase viable macrophage production while maintaining viability.

> **SYNTHETIC DEMONSTRATION.**
> Project `ipsc_macrophage` v1.0.0 · BioSense 0.2.0 · commit 448ba82204

## What we were trying to improve

Increase viable macrophage production while maintaining viability.

## Research context

Species: human · Cell type: monocyte, macrophage, iPSC-derived macrophage · State: differentiating, mature · Excluded: mouse-only evidence. In-scope evidence ranks first; out-of-scope evidence is shown separately and labelled a context mismatch.

## The question that was blocking a decision

**`GAP-mcsf_ng_ml`** — Nothing in this project says whether the current M-CSF setting is limiting the objective, so a revision would move it blind. This question was chosen by BioSense, not stated in the request.

## Evidence considered

| Class | Count |
|---|---|
| SYNTHETIC FIXTURE | 1 |
| DERIVED ANALYSIS | 1 |
| SIMULATION | 1 |

- `facs-mcsf-fixture` SYNTHETIC: processed FACS populations under two M-CSF conditions (SYNTHETIC FIXTURE, PUBLIC)

## Analysis performed

`cytometry.population_comparison` v1.0.0 — `analysis-disc_a682bc475a31-facs-mcsf-fixture` (scipy 1.18.1, numpy 2.5.3)
- CD14_pos_pct: 41.18 in control vs 67.58 in mcsf_high (+26.4)  
  *Welch's t-test, p=0.00132, BH-q=0.00176, n=3 vs 3, 95% CI [21.5, 31.5]*
- CD206_pos_pct: 33.5 in control vs 57.52 in mcsf_high (+24.02)  
  *Welch's t-test, p=0.000335, BH-q=0.000671, n=3 vs 3, 95% CI [21.4, 26.7]*
- CD16_pos_pct: 16.93 in control vs 31 in mcsf_high (+14.06)  
  *Welch's t-test, p=0.000149, BH-q=0.000596, n=3 vs 3, 95% CI [12.8, 15.4]*

## Current hypothesis

Changing M-CSF may improve the objective: Increase viable macrophage production while maintaining viability.

**Confidence: moderate.** supported by 2 source(s) across 2 evidence class(es) capped below high: no real experimental measurement of this process supports it yet

### Quantified effects

| Outcome | Baseline → Candidate | Change | Provenance |
|---|---|---|---|
| CD14 pos pct | 41.18% → 67.58% | +26.4 pp (+64.1%) | DERIVED |
| CD16 pos pct | 16.93% → 31% | +14.06 pp (+83.04%) | DERIVED |
| viability pct | 93.96% → 91.04% | -2.922 pp (-3.11%) | DERIVED |
| CD206 pos pct | 33.5% → 57.52% | +24.02 pp (+71.69%) | DERIVED |
| Monocytes per input iPSC | 17.99 cells/input_cell → 29.66 cells/input_cell | +11.67 cells/input_cell (+64.87%) | SIMULATED |
| Harvested cells | 8.995 1e6 cells/mL → 14.83 1e6 cells/mL | +5.835 1e6 cells/mL (+64.87%) | SIMULATED |
| Final viability | 80.78% → 80.78% | +0 pp (+0%) | SIMULATED |
| Cells in the monocyte gate | 76.66% → 90.25% | +13.59 pp (+17.73%) | SIMULATED |
| Peak viable cell density | 4.762 1e6 cells/mL → 4.762 1e6 cells/mL | +0 1e6 cells/mL (+0%) | SIMULATED |
| Mean aggregate diameter | 273.9 um → 273.9 um | +0 um (+0%) | SIMULATED |
| Mean condition score | 92.7 score → 92.7 score | +0 score (+0%) | SIMULATED |

**Trade-off.** 6 outcome(s) improve and 1 worsen. This is a trade-off, not an improvement.

### Candidate parameter

`mcsf_ng_ml` (M-CSF) — **increase**
· 25 → 50 ng/mL
· search range 0–150 ng/mL

## Simulator prediction

Model `ipsc_monocyte_v1` v1.0.0 — M-CSF 25 → 50 ng/mL

| Outcome | Control → Candidate | Change | Provenance |
|---|---|---|---|
| Monocytes per input iPSC | 17.99 cells/input_cell → 29.66 cells/input_cell | +11.67 cells/input_cell (+64.87%) | SIMULATED |
| Harvested cells | 8.995 1e6 cells/mL → 14.83 1e6 cells/mL | +5.835 1e6 cells/mL (+64.87%) | SIMULATED |
| Final viability | 80.78% → 80.78% | +0 pp (+0%) | SIMULATED |
| Cells in the monocyte gate | 76.66% → 90.25% | +13.59 pp (+17.73%) | SIMULATED |
| Peak viable cell density | 4.762 1e6 cells/mL → 4.762 1e6 cells/mL | +0 1e6 cells/mL (+0%) | SIMULATED |
| Mean aggregate diameter | 273.9 um → 273.9 um | +0 um (+0%) | SIMULATED |
| Mean condition score | 92.7 score → 92.7 score | +0 score (+0%) | SIMULATED |

*Every number in `effects` was produced by the project's mechanistic stand-in and is labelled SIMULATED. It is not a measurement and not a promise about a real culture.*


## Recommended next experiment

Test M-CSF at 20, 50, 80 ng/mL against the current process, measuring monocytes per input ipsc, harvested cells, final viability.

## How BioSense got here

1. BioSense was asked to increase viable macrophage production while maintaining viability.
2. It identified an unresolved question: Nothing in this project says whether the current M-CSF setting is limiting the objective, so a revision would move it blind. This question was chosen by BioSense, not stated in the request.
3. It found 1 relevant dataset(s) already registered.
4. Comparing the conditions in that data, cd14 pos pct goes from 41.18% to 67.58%, a change of +26.4 percentage points (+64.1%) — calculated from measurements.
5. BioSense proposes that M-CSF is worth testing.
6. Testing that in the project simulator, monocytes per input ipsc goes from 17.99 cells/input_cell to 29.66 cells/input_cell, a change of +11.67 cells/input_cell (+64.87%) — predicted by the simulator.
7. Test M-CSF at 20, 50, 80 ng/mL against the current process, measuring monocytes per input ipsc, harvested cells, final viability.

## Main limitations

- 4 readout(s) at q < 0.05 with consistent direction
- One dataset, one comparison. A difference here is directional evidence about this experiment, not a demonstration that the same change helps your process.
- Processed tables only. Raw FCS, automated gating, FlowSOM and UMAP are Phase 2 and are refused rather than approximated.
- The model rewards the levers it was built to reward; a real culture may not.
- The simulator is a mechanistic stand-in calibrated to published protocol ranges. It is not a validated digital twin and no number it produces is a measurement of any real cell.
- capped at moderate: one dataset, one comparison, and transfer to this cell type, stage and culture format is untested

## Capability scorecard

**17 PASS / 0 FAIL**

| Capability | Status |
|---|---|
| objective interpreted | PASS |
| project selected | PASS |
| research context applied | PASS |
| uncertainty identified | PASS |
| evidence collected | PASS |
| analysis plan produced | PASS |
| deterministic analysis executed | PASS |
| provenance complete | PASS |
| candidate parameter generated | PASS |
| simulator coverage checked | PASS |
| simulator handoff | PASS |
| predicted improvement generated | PASS |
| hypothesis generated | PASS |
| quantitative effect supported | PASS |
| next experiment generated | PASS |
| plain language generated | PASS |
| privacy validated | PASS |

*This is a SYSTEM CAPABILITY scorecard. It records whether BioSense identified an uncertainty, planned an analysis, executed it deterministically, quantified what it could, checked simulator coverage and labelled every number. It does NOT measure biological truth, and a run can pass every row while being biologically wrong.*

## Privacy

- export policy: **public_safe**
- private lineage: none
- safe to publish: **yes**
