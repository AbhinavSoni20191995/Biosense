# How the biosimulator computes an output

This answers one question: **you moved a slider — what actually happened?**

Everything below is in `analysis_agent/simulator.py` (the calibrated iPSC →
monocyte model) and `biosense/production/response_model.py` (responses somebody
proposed). Nothing in either file is an LLM. A model never writes a number here;
it may only choose which number to ask for.

> **What this model is.** A mechanistic stand-in, anchored to published protocol
> ranges, written to behave like a stirred-tank iPSC differentiation. It is **not
> a validated digital twin**, nothing in it was fitted to your cells, and no
> number it produces is a measurement. It is useful because it is *consistent*:
> the same setpoints always give the same answer, and the trade-offs it contains
> are real ones.

---

## The shape of it: five layers, each derived from the one above

The design rule is that **every observable is derived, never drawn independently**.
You cannot get a high cell count and a low capacitance reading, because the
capacitance reading is computed *from* the cell count. That is what makes the
model worth reasoning against.

```
L0  Setpoints            what you choose
      ↓
L1  Hidden line state    the cells' own biology — you never see this
      ↓
L2  Physics              shear, oxygen transfer, aggregate size
      ↓
L3  Biology              growth, death, metabolism, stage transitions
      ↓
L4  Observables          what the instruments report, with their noise
```

The simulation steps forward in **2-hour increments** through four stages, and
each stage has its own idea of what "good" looks like.

---

## L0 — what you set

| Setpoint | Unit | What it is |
|---|---|---|
| `seed_density` | 1e6 cells/mL | cells per mL at the start |
| `agitation_rpm` | rpm | impeller speed |
| `do_setpoint` | fraction | dissolved oxygen target |
| `feed_fraction` | fraction | how much volume is exchanged per feed |
| `feed_interval_h` | h | hours between feeds |
| `rocki_hours` | h | ROCK-inhibitor exposure after seeding |
| `bmp4`, `vegf` | ng/mL | mesoderm specification |
| `mcsf`, `il3` | ng/mL | myeloid commitment |

Plus the length of each stage.

---

## L1 — the part you cannot see

Each iPSC line gets a hidden state: a maximum growth rate `mu_max`, a baseline
death rate, a glucose consumption rate, a lactate yield, per-stage optimum
offsets, and a clonal `variant_fraction` that can take the culture over.

**The agent never reads this.** It is the reason the loop is a real inference
problem: you infer the culture from its instruments, exactly as in a lab. The
sensors have their own hidden state too — a pH offset, a photobleached DO patch,
a fouled capacitance probe — so a drifting probe and a changing culture have to
be told apart from the readings alone.

---

## L2 — physics: what agitation actually does

Two formulas, and one trade-off that runs through the whole model.

**Shear rises steeply with stirring:**

```
shear(rpm) = 0.06 × (rpm / 75)^1.6        dyne/cm²
```

**Oxygen transfer also rises with stirring:**

```
kLa(rpm) = 4.0 × (rpm / 60)^1.4           1/h
```

**Aggregates grow by division and are broken by shear:**

```
dD/dt = D·μ/3  −  0.242 · shear · D² / 300
```

Because breakage scales with **D²** and growth with **D**, this settles at a
stable equilibrium diameter *set by the rpm you chose*. Faster stirring → smaller
aggregates. That is the lever.

**And size decides oxygen:**

```
penetration = 110 × √(DO / 0.20)          µm of viable shell
necrotic    = ((r − penetration) / r)²    when r > penetration, else 0
```

An aggregate bigger than oxygen can reach goes hypoxic in the middle. So:

> **The central trade-off.** Stirring harder breaks aggregates down to the size a
> stage wants and improves oxygen transfer — *and the same shear kills cells*.
> There is no setting that wins both. This is why the simulator is worth having.

---

## L3 — biology: the growth equation

This is the heart of it. Growth rate per 2-hour step is a **product of
limitations**, each between 0 and 1:

```
μ = mu_max
    × glucose/(0.4 + glucose)        Monod: starve and you stop
    × 22/(22 + lactate)              lactate inhibition
    × 4.5/(4.5 + ammonia)            ammonia inhibition
    × bell(DO, stage optimum)        too little OR too much oxygen hurts
    × (1 − necrotic_fraction)        dead cores do not divide
    × (1 − X/6.0)                    the vessel fills up
    × rocki                          ROCK inhibitor, first hours only
```

Multiplying means **any single term near zero stops growth**. You cannot
compensate for starvation with perfect oxygen.

**Death is a sum**, because causes of death add:

```
death = 0.0035                                  baseline
      + 0.055 × max(shear − 0.07, 0) / 0.07     shear above threshold
      + 0.090 × necrotic_fraction               hypoxic cores
      + 0.030 × max(ammonia − 3.0, 0)           ammonia toxicity
```

Then `X += (μ − death) · X · dt`, and viability relaxes toward a target set by
the death rate.

**The bell curve** is the shape that appears everywhere:

```
bell(x, optimum, tolerance) = exp(−0.5 · ((x − optimum)/tolerance)²)
```

It is 1 at the optimum and falls away either side. This is how the model says
"a dose can be too high" — the single most important thing it knows that a
monotonic model would not.

**Each stage wants something different:**

| Stage | Wants diameter | Wants DO |
|---|---|---|
| Expansion | 150 µm | 0.20 |
| Mesoderm | 200 µm | 0.10 |
| Hemogenic | 300 µm | 0.12 |
| Myeloid | 400 µm | 0.18 |

So one agitation setting cannot be right for the whole run — which is exactly
the kind of thing the loop is meant to discover.

**Stage transition efficiency** — how many cells actually become the next thing —
is its own product:

```
efficiency = bell(diameter, stage optimum)
           × bell(DO, stage optimum)
           × lactate inhibition
           × cytokine term
           × viability
```

where the cytokine term is `bell(BMP4) × bell(VEGF)` in mesoderm and
`bell(M-CSF) × bell(IL-3)` in myeloid. **This is the chain that makes M-CSF
matter**: M-CSF → bell → efficiency → monocyte release → harvest.

**Harvest** accumulates continuously in the myeloid stage:

```
release per step = 0.028 × efficiency × X × dt
```

---

## L4 — what the instruments report

Nothing is read perfectly. Each observable is the true value times the sensor's
hidden fault times lognormal noise:

| Reading | Noise (CV) | Module |
|---|---|---|
| Online sensors (pH, DO, capacitance, OUR) | 2% | M1 vessel suite |
| Metabolites | 5% | M3a analyser |
| In-situ imaging (aggregate size) | 8% | M3b microscope |
| Image cytometer (count, viability) | 6% | M3c AO/PI |
| Harvest counter | 7% | M4 in-line |
| Mini cytometer (markers) | 4% | M3e 3-colour |
| Offline flow | 12% | not in the machine |
| Offline function assay | 18% | not in the machine |

Assays also carry a **cost** and a **latency** — a flow panel takes 24 hours and
a function assay 48 — so "measure everything" is not a strategy the model
rewards.

---

## Worked example: what +25 ng/mL of M-CSF does

Raising M-CSF from 25 to 50 ng/mL, with everything else held:

1. The line's hidden M-CSF optimum is near 50 ng/mL, so `bell(50, 50, 25)` goes
   from **0.61 → 1.00**.
2. The myeloid cytokine term rises by the same factor.
3. Stage transition efficiency rises with it.
4. Monocyte release per step is proportional to efficiency, so harvest rises.
5. Growth and death are **untouched** — M-CSF is in the efficiency term, not the
   growth equation — so final viability barely moves.

Which is why the demonstration reports a large yield gain and an almost
unchanged viability. The model is not being generous; the lever genuinely sits
in one term and not the other.

---

## Responses nobody fitted: `de_novo_ai` and `expert_declared`

The model above has terms for ten parameters. Real processes have more, and a
new project may have no calibrated model at all. So a project parameter can carry
a **response model** — a shape and its constants — supplied either by the
discovery agents from cited evidence (`de_novo_ai`) or by you from experience
(`expert_declared`).

Four shapes, evaluated by `biosense/production/response_model.py`:

| Shape | Formula | Use it when |
|---|---|---|
| `bell` | `exp(−0.5·((x−optimum)/tolerance)²)` | a dose with an optimum — too much is as bad as too little |
| `saturating` | `x / (half_max + x)` | more helps, with diminishing returns to a plateau |
| `threshold` | `0` below, `1` above | nothing happens until a level is reached |
| `linear` | `slope · x` | proportional within the working range |

The shape returns 0…1. That is scaled by `max_effect` and turned into a ratio
between your control value and your candidate:

```
f(x)       = 1 + max_effect × shape(x)
multiplier = f(candidate) / f(control)          (inverted for a death target)
```

The multiplier then multiplies whatever the term's `target` names —
`growth`, `death`, `viability`, `transition_efficiency` or `harvest` — in the
calibrated signature.

**What is bounded, and why.** `max_effect` is capped by the contract at ±1…4 and
the multiplier is clamped again in code to 0.2…5.0. A proposed term adjusts the
calibrated biology; it cannot overwhelm it. A term claiming a cytokine multiplies
yield forty-fold is not a prediction, and the code will not express one.

**What is never hidden.** Every number a proposed term touches carries
`model_basis` (`ai_proposed` or `expert_declared`), the parameters responsible,
and `calibrated_value` — the number the calibrated model gave *before* the term
was applied. The interface shows both. An expert-declared term is private expert
knowledge, so a protocol value resting on it is a **design choice**, never a
cited one.

**Where there is no calibrated model at all**, a proposed term still says which
way and roughly how far, as a *relative* change with no absolute value and a
sentence saying why there is none. That is deliberately not an Estimate: an
Estimate carries a baseline and a candidate value, and here there are neither.

---

## What the model does not know

- **Your line.** Hidden state is sampled, not measured from your cells.
- **Your medium.** Glucose, lactate and ammonia are the only metabolites.
- **Anything outside its ten terms.** Temperature, pH setpoint, surface coating,
  passage method and vessel geometry have no equation. They are real variables;
  the model is silent on them, and `not_modelled` is what it says.
- **Genotype, except as a ratio.** A knockout enters as a multiplier on growth,
  death or differentiation. It is a stand-in for a mechanism, not a mechanism.
- **Whether any of this is true of a real culture.** It reproduces published
  *ranges* and known trade-offs. That is not the same as predicting your run.

A simulated number is labelled `SIMULATED` everywhere it appears, for this
reason. Use it to compare conditions and find the shape of a trade-off. Do not
use it as evidence that a protocol will work.

---

## Running it yourself

```bash
# the sandbox, in a browser: move a setpoint, watch the vessel respond
uv run --frozen python -m biosense.production.app --runs runs --static webapp
# then open http://127.0.0.1:8000 and choose Simulator

# one condition, from the command line
uv run --frozen python -c "
from biosense.production import sim_mode as SM
r = SM.simulate({'setpoints': {'mcsf': 50, 'agitation_rpm': 60}})
print(r['signature'])"
```

Both run in about a tenth of a second and need no credentials.

See also: [`docs/SIMULATOR_MODE.md`](SIMULATOR_MODE.md) for the sandbox
interface, and [`docs/BIOSIMULATOR.md`](BIOSIMULATOR.md) for the older cardiac
compartment model, which is a different and simpler object.
