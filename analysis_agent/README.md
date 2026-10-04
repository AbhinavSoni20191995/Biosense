# iPSC → macrophage agentic process-development lab

Simulator + Analysis Agent for the Hack-Nation Challenge 03 build.

```
# from the repo root (numpy comes with `uv sync --locked`)
uv run --frozen python analysis_agent/run_demo.py                  # clonal drift arm (clone appears at passage 3)
uv run --frozen python analysis_agent/run_demo.py --clean          # control arm
uv run --frozen python analysis_agent/run_demo.py --sensor-fault   # instrument fault arm (fouled biomass probe)
uv run --frozen python analysis_agent/run_demo.py --stain-fault    # reagent fault arm (failed antibody cartridge)
```

## Role in the BioSense-AI production loop

This folder supplies two things to the loop described in
[docs/PRODUCTION_LOOP.md](../docs/PRODUCTION_LOOP.md):

1. **Max-mode analysis tools.** When a bioreactor run is returned in `max`
   mode, `biosense/production/analysis.py` calls `analysis_tools.py` on every
   replicate: `derive_kinetics`, `fuse_purity`, `check_staining_integrity`,
   `check_measurement_coherence`, `reconcile_mass_balance`,
   `check_sensor_health`, `attribute_cause` and `recommend_next_action`. A
   staining failure, sensor drift, incoherent channels or an open mass balance
   makes the run `DATA_UNRELIABLE` (re-measure, do not redesign); likely clonal
   drift makes it a `SAFETY_HOLD`. Release criteria come from the request's QC
   profile in `qc_profiles/`, not from `CQA_SPEC` (whose values seed the
   `monocyte_macrophage_research` demo profile).
2. **A synthetic stand-in for the wet lab.** `protocol_runner.py` maps a
   ProductionProtocol 2.0 onto `simulator.py` (seeding density, agitation, DO,
   feeding, BMP4/VEGF/M-CSF/IL-3, the four stage lengths) and emits a
   BioreactorRun 2.0 record labelled `synthetic_standin`, in minimal or max
   mode, with optional fault scenarios (`clean`, `clonal`, `sensor_fault`,
   `stain_fault`). Engineered genotypes are hidden truth in a separate file
   (`LineState.factor_optima`, `genotype_mu_ratio`, `genotype_diff_ratio`), so a
   knockout can need a different cytokine dose than wild type. Agents never
   read the truth file. Stand-in output is a workflow demonstration, never
   biological evidence.

```
uv run --frozen python -m biosense.production.cli simulate-standin \
  --protocol <approved protocol> --mode max --scenario sensor_fault --out runs/x/run.json
```

The runnable Omnigent agent is `discovery_loop/agents/analysis/`; its prompt
carries the interpretation rules from `analysis_agent.yaml`.

## The machine: which module produces which value

No flow cytometer in the loop. Every in-loop signal comes from one module.

| Module | Hardware | Produces | Rate | Cost |
|---|---|---|---|---|
| **M1** vessel sensors | pH + DO optical patches (read through the wall, no sterile breach), capacitance biomass probe, gas mixer, impeller torque | pH, DO, capacitance, oxygen uptake, rpm, temperature | seconds | 0 |
| **M2** fluidics | Peristaltic pumps, pinch valves, gravimetric load cells on every bottle | base addition, feed and harvest volumes | continuous | 0 |
| **M3a** metabolite analyser | Enzymatic biosensor cassette on an auto-sampling loop (or in-line Raman) | glucose, lactate, ammonia, osmolality, LDH | 1–2×/day | 1 |
| **M3b** in-situ microscope | Immersible focus-stacking probe, or a flow-through imaging channel | aggregate diameter mean/SD, count, % >300 µm | hourly | 0 |
| **M3c** image cytometer | Automated brightfield + 2-channel fluorescence (AO/PI) off the sterile sampling valve | viable cell density, viability | daily | 1 |
| **M3d** impedance cytometer | Microfluidic impedance flow cytometer on a disposable chip, in-line on the harvest stream, multi-frequency coplanar electrodes | electrical diameter, opacity, monocyte-cluster fraction, label-free viability | continuous | 0 |
| **M3e** mini cytometer | Compact 3-channel fluorescence cytometer, no-wash staining loop, volumetric counting (no sheath, no beads) | CD14-PE, 7-AAD, TRA-1-60-APC, events | every ~4 days | 2 |
| **M4** harvest counter | In-line counter + cell-diameter histogram on the harvest stream | harvest rate, cumulative yield, cell diameter, monocyte-gate fraction | continuous | 0 |
| **M5** genomic cartridge | Automated DNA extraction + droplet digital PCR | 20q11.21 copy number, variant allele fraction | on demand | 25 |

Offline and optional: full multi-colour flow (cost 8) and functional assays
(cost 15), for periodic re-calibration and release testing only.

### Two-tier cytometry

**Tier 1 — M3d, label-free, continuous, free.** Electrical diameter tracks cell
volume and opacity (high-frequency over low-frequency impedance) tracks the
membrane, so differentiation state and viability are readable with no antibody:

```
purity% ≈ 12.0 + 62.0·imp_frac_monocyte_cluster  (M3d)
               + 14.0·imp_opacity                (M3d)
               +  0.6·harvest_cell_diam_mean_um  (M4)
               -  3.0·y_lac_per_glc              (M3a)      σ = 5.5
```

**Tier 2 — M3e, direct, scheduled, cost 2.** A real surface-antigen measurement,
σ = 2.2 % at ~10k events, but sparse and it ages.

**Fusion.** Inverse-variance weighted, with the direct panel's precision decaying
as `σ·√(1 + age_h/48)`. The direct value dominates while fresh and hands back to
the label-free channel as it ages. The residual `direct − label_free` is the
**online calibration offset** — which is why the loop needs progressively fewer
panels the longer it runs.

### The constraint that shapes the panel

**Surface markers only.** Intracellular OCT4 needs fixation and permeabilisation,
which cannot be automated in a closed single-use flow path, so **TRA-1-60** is
the surface substitute for residual pluripotency. No-wash protocols are
mandatory: a centrifugation step would break the closed path. Volumetric
counting removes the need for counting beads.

### Staining failure is the cleanest fault of all

A bad antibody lot or a failed no-wash step drops **every** fluorescence channel
together, viability dye included, while M3d label-free viability is untouched.
That asymmetry identifies the fault unambiguously: if fluorescence viability
sits >12 points below impedance viability, the panel is void, its antigen CQAs
are not scored, and the cartridge is replaced.

Files:

| File | What it is |
|---|---|
| `simulator.py` | Mechanistic bioreactor model, 4 stages (configurable lengths), clonal drift, sensor faults and engineered genotypes injectable |
| `analysis_tools.py` | Deterministic tools: kinetics, soft sensor, sensor health, attribution |
| `protocol_runner.py` | ProductionProtocol 2.0 → simulator → BioreactorRun 2.0 (synthetic stand-in) |
| `analysis_agent.yaml` | Design spec: device map, prompt, tools, policies, handoffs |
| `run_demo.py` | 9-run campaign → the agent's structured verdict |

---

## Which values come from which

Nothing is sampled independently. Every observable is derived, so a change in
one setpoint propagates coherently through every channel — which is exactly
what lets the Analysis Agent catch an *incoherent* channel as an artifact.

### L0 → L2  Setpoints drive physics

| Derived | Formula | Depends on |
|---|---|---|
| `shear_stress τ` | `6e-2·(rpm/75)^1.6` dyne/cm² | agitation_rpm |
| `kLa` | `4·(rpm/60)^1.4` 1/h | agitation_rpm |
| `DO_actual` | `DO_setpoint − OUR/kLa·0.05` | DO setpoint, rpm, VCD |
| `OUR` | `0.42 · VCD` | VCD |
| `aggregate diameter D` | `dD/dt = D·μ_agg/3 − k·τ·D²/300` | rpm (breakage), μ (growth), glucose |
| `necrotic fraction` | shell model, `pen = 110·√(DO/0.20)` | D, DO |

Breakage scales with D², so diameter has a **stable equilibrium set by rpm**.
That is the mechanism behind the whole optimization problem.

### L2 → L3  Physics drives biology

| Derived | Depends on |
|---|---|
| `μ` (growth) | μ_max · Monod(glucose) · inhib(lactate) · inhib(ammonia) · bell(DO) · (1−necrotic) · (1−X/X_max) |
| `death` | base + shear(τ>0.07) + 0.09·necrotic + ammonia, **then × clonal factor** |
| `VCD` | integrates (μ − death)·X |
| `viability` | relaxes toward `1 − 25·death` |
| `glucose` | −q_glc·VCD, plus feed |
| `lactate` | `Y_lac/glc` × glucose consumed, diluted by feed |
| `stage efficiency` | bell(D, **stage-specific** optimum) · bell(DO) · inhib(lactate) · cytokine bells · viability · **clonal factor** |
| `cumulative efficiency` | product of the 4 stage efficiencies |
| `yield per input iPSC` | cumulative efficiency × final VCD / seed density |

The diameter optimum changes per stage — ~150 µm in expansion, ~400 µm in
myeloid priming — so a fixed diameter target is a built-in trap.

### L3 → L4  Biology drives observables

| Observable | Derived from | Tier | Cost | CV |
|---|---|---|---|---|
| pH | `7.35 − 0.013·lactate` | online | 0 | 0.4% |
| DO measured | DO_actual | online | 0 | 3% |
| capacitance | `1.9 · VCD` | online | 0 | 3% |
| oxygen uptake | `0.42 · VCD` | online | 0 | 4% |
| base addition | `0.05 · lactate` | online | 0 | 8% |
| VCD, viability | state | at-line | 1 | 6% |
| glucose, lactate, ammonia | state | at-line | 1 | 5–7% |
| osmolality | `285 + 2.1·lactate` | at-line | 1 | 1% |
| LDH | `120 + 900·death` | at-line | 1 | 10% |
| diameter mean / SD / %>300 µm | D, lognormal spread | imaging | 1 | 5–8% |
| marker % (OCT4/KDR/CD34/CD14…) | stage efficiency | **flow** | 8 | 12% |
| phagocytosis, TNFα, IL-10 | clonal factor | **function** | 15 | 18% |
| 20q11.21 copy number, VAF | `variant_fraction` | **genomic** | 25 | 5% |

Capacitance, OUR and VCD share one source, so they must co-move. LDH is the
inverse of viability. Those built-in couplings are what
`check_measurement_coherence` tests: if VCD drops and capacitance does not,
it is the sample, not the cells.

### L1  The hidden layer the agents can never read

`variant_fraction` models a culture-adaptation clone on the
20q11.21 / *BCL2L1* pattern:

```
death      × 0.55   (BCL-xL is anti-apoptotic under all stress)
μ_max      × 1.04   (mild proliferative edge)
differentiation × 0.45   (impaired commitment)
selection  s = 0.9 per passage  (rapid culture takeover)
```

This produces the signature that makes the Analysis Agent's job non-trivial:

```
passage  variantF   VCD   viab%   cumEff   yield
   0       0.000    3.23   76.0   0.0329   0.212
   3       0.056    3.28   76.0   0.0279   0.183   <- clone appears
   6       0.242    3.44   77.8   0.0204   0.140
   8       0.547    3.71   80.4   0.0121   0.090
```

**The culture looks healthier while the product gets worse.** No process
deviation does that — process problems degrade growth and differentiation
together. The decoupling is the discriminator.

Crucially, this test survives removing the flow cytometer: the growth side comes
from **M3c** (VCD, viability) and the product side from **M4** (harvest yield),
both in-line.

## Four causes, checked in order

| # | Cause | Detected by | Signature |
|---|---|---|---|
| 1 | **Instrument or reagent fault** | redundant ratios vs the post-calibration window; M3e vs M3d viability | capacitance/VCD, OUR/VCD or pH-at-zero-lactate shifts (needs \|z\|>4 **and** >8% relative); or all fluorescence channels low while label-free viability holds |
| 2 | **Measurement artifact** | cross-channel coherence | viability falls but LDH is flat; VCD falls but capacitance holds |
| 3 | **Process** | setpoint regression | the residual is explained by the setpoints, and reverses |
| 4 | **Clonal / genetic drift** | passage-trend decoupling | growth up, product down, unexplained by setpoints |

The order matters: a drifting probe makes every downstream inference wrong,
including the clonal test, so the agent is required to clear the instrument
before it is allowed to blame the genome.

Verified behaviour:

| Arm | top cause | confidence | recommended action |
|---|---|---|---|
| clonal drift | `clonal_genetic_drift` 0.85 | high | M5 ddPCR, escalate to human |
| control | `process_parameters` 0.72 | moderate | periodic soft-sensor recalibration |
| fouled probe | `sensor_fault` 0.70 (z = −12.8 on capacitance/VCD) | moderate | recalibrate M1 probe |
| failed antibody lot | `sensor_fault` 0.80 (fluorescence viability 27 points below label-free) | moderate | replace M3e cartridge; antigen CQAs void |

---

## What the Analysis Agent emits

```jsonc
{
  "derived_kinetics": { "mu_h": ..., "y_lac_per_glc": ..., "diameter_cv_pct": ... },
  "cqa_verdict": { "status": "FAIL", "failed": [...] },
  "attribution": {
    "weights": { "process_parameters": 0.10,
                 "measurement_artifact": 0.05,
                 "clonal_genetic_drift": 0.85 },
    "top_cause": "clonal_genetic_drift"
  },
  "evidence": [ "...each line traces to a tool result..." ],
  "assumption_challenged": { "flag": true, "statement": "..." },
  "confidence": "high",
  "recommended_measurement": { "assay": "karyotype_ddpcr", "cost": 25.0 },
  "next_decision_for_planner": "HOLD parameter optimization...",
  "escalate_to_human": true
}
```

`assumption_challenged` is the hook the Planner and Literature agents listen
on. It is what turns a result into a changed next decision.

---

## Honest limitations

- Mechanistic model calibrated to published protocol ranges, **not a validated
  digital twin**. Absolute yields are arbitrary; only relative comparisons
  between conditions are meaningful.
- Clonal drift is modelled as one variant with fixed effect sizes. Real
  karyotypic drift is heterogeneous.
- `run_stage(setpoints) → observations` is the hardware boundary. Replacing the
  simulator with a real bioreactor means replacing that one function.
- Validation still needed: a real differentiation time course to fit growth and
  metabolic rates, and documented failure runs to calibrate the failure modes.
