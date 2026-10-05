# Simulator mode

> `uv run --frozen python -m biosense.production.app --runs runs --static webapp`
> → <http://127.0.0.1:8000/simulator.html>, or the **Simulator** tab in the console.

The console asks the loop to find a condition. Simulator mode hands you the same
reactor and lets you look for one yourself.

It is the same model underneath — `analysis_agent/simulator.py`, the iPSC →
monocyte process — so what you learn by hand is about the object the loop
optimises, not a separate cartoon of it.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/simulator-dark.png">
  <img alt="Simulator mode: setpoint sliders grouped by stage on the left, a side and top view of the vessel with aggregates drawn from the imaging channels in the middle, a day timeline under it, and the day's instrument readings with sparklines on the right." src="assets/simulator-light.png" width="100%">
</picture>

## What you are looking at

| | |
|---|---|
| **Left** | ten setpoints, grouped by the stage each one bites in. Click a knob's name for what it does. |
| **Middle** | the vessel, side and top. Every circle, colour and label is drawn from an observation channel — nothing is decorative. |
| **Timeline** | one frame per day, coloured by stage. Scrub it or press play. |
| **Right** | the instruments for the day on screen, a sparkline per channel, and the flags the read raised. |

What the vessel drawing means:

- **circle size** — `agg_diameter_mean_um`, on one scale in both views;
- **a dark core** — `agg_frac_over_300um` says that fraction of aggregates is
  past the diameter where the centre goes hypoxic, so that fraction is drawn
  with one;
- **red specks** — `ldh_u_per_l`, released intracellular enzyme, so lysis;
- **the liquid tint** — the condition score;
- **the outlet stream** — `harvest_cells_e6_per_ml_day`, drawn only when the
  myeloid stage is actually releasing cells;
- **where a circle sits** — nothing. Position is arrangement, from a fixed hash,
  so the field does not jitter when the day advances.

## The condition read

Each day gets `good` / `strained` / `failing` and a score out of 100, computed in
`read_condition` from the thresholds in `sim_mode.LIMITS` — which the interface
fetches and the session export carries, so the score is checkable rather than
asserted. A breach's penalty scales with its size: one channel far past its limit
can carry the read on its own.

**A flag names what a channel says, not what is wrong with the culture.** The
difference matters, and the *Starting culture* menu exists to make it concrete.
Pick `Capacitance probe fouling` and biomass reads low while the cell count and
the oxygen uptake rate disagree with it. Pick `Antibody lot failure` and every
fluorescence channel drops together while the label-free impedance channels do
not. Neither is biology. Telling them apart from a culture that is genuinely
thinning is the exercise the loop's analysis agent faces on every iteration.

One trap is worth naming: a collapsed culture consumes nothing, so glucose,
lactate and ammonia all read comfortable. Viable cell density is what catches it.

## Comparing two conditions

*Hold as A*, change something, *hold as B*, *compare*. Both run on the same line
and the same seed, so a difference is attributable to the setpoints that differ.

A percentage against a collapsed baseline is withheld rather than printed: 5%
viability to 75% is one dead culture and one living one, not a 1400%
improvement.

**One replicate each.** Every difference is directional, and that is all the
verdict claims.

## Carrying a condition into a loop run

*Carry into a loop run* turns the condition into a brief and hands the console a
prompt. Every quantity leaves as a **`design_choice`** whose basis says, in
words, that a person turned a knob against a synthetic model and no literature
value supports it.

That is not a formality. `design_choice` is the provenance class the production
loop blocks the wet lab on, so a sandbox condition cannot launder itself into a
protocol by passing through the interface. The loop still has to earn each
value; the brief only says where the search starts.

## What it refuses

- **The line's hidden truth never leaves the server.** Its true growth rate,
  death rate and clonal fraction are what the loop's agents have to infer from
  instruments, and so do you. Tested in `tests/test_sim_mode.py`.
- **A knob the model does not have is refused by name** rather than ignored, so
  a slider can never silently do nothing.
- **Nothing here decides anything.** There is no verdict, no allowed-action set
  and no iteration budget, because there is no agent proposing a move.

## What it does not model

- **T-lineage differentiation.** The T-cell stand-ins the loop runs are
  phenomenological: no DO, pH, shear or aggregate physics, so they cannot be
  driven from these knobs. A mechanistic T-cell reactor is the obvious next
  piece and does not exist yet.
- Medium composition beyond glucose, lactate and ammonia.
- A real vessel of any make. The model is calibrated to published protocol
  ranges and is **not** a validated digital twin. It rewards the levers it was
  built to reward, and a real culture may not.

## The API, if you want it without the page

```bash
curl -s localhost:8000/api/sim/config                       # the knobs and limits
curl -s localhost:8000/api/sim/run     -H 'Content-Type: application/json' \
  -d '{"setpoints":{"agitation_rpm":70},"challenge":"variant"}'
curl -s localhost:8000/api/sim/compare -H 'Content-Type: application/json' \
  -d '{"conditions":[{},{"setpoints":{"mcsf":90}}]}'
curl -s localhost:8000/api/sim/brief   -H 'Content-Type: application/json' -d '{}'
```

Out-of-range numbers are clamped to the range the model is valid over and the
clamp is reported in `clamped`; unknown knobs are refused.
