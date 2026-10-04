# Biosimulator and outcome agents

This page covers the two agents that sit downstream of the literature agent,
the numerical model behind them, the file formats they exchange, and how to run
them. The design comes from `BioSenseAI_Biosimulator_Outcome_Blueprint.md`.

The loop runs in this order:

1. The literature handoff (version 0.1) is read but never modified.
2. The biosimulator builds an explicit scenario from it (version 1.0).
3. The numerical simulator produces a result for each experiment.
4. The outcome agent evaluates the results.
5. The evaluation ends in one typed next action, which the coordinator
   dispatches.

The LLM agents choose tools and explain what the tools return. Every
trajectory, metric, comparison, sensitivity value and next-test decision is
computed by deterministic Python in `biosense/`.

## Two modes

| Mode | Where the inputs come from | What an output means |
|---|---|---|
| `evidence_based` | Only reported claims, documented derivations and fitted records | A model prediction, conditional on that evidence. If any required input is missing, the scenario is blocked and the gaps are listed. |
| `synthetic_demo` | Selected reported claims plus an explicit file of synthetic assumptions (`examples/assumptions.synthetic.json`) | A demonstration of the workflow and of the toy model. It is not biological evidence. |

Every scenario reports two flags independently:
- `numerically_complete`: every model input has a value.
- `biologically_calibrated`: always `false` in this version.

A synthetic assumption can never become a reported claim. The adapter, the
scenario validator and the tests all enforce this.

## Model: `compartment_growth_v1`

This is a phenomenological demonstration model, not a validated mechanism of
cardiomyocyte differentiation. It tracks four cell counts:

| Compartment | Meaning |
|---|---|
| R | Remaining undifferentiated cells (one coarse compartment that hides the intermediate states) |
| C | Modeled target cardiomyocytes |
| O | Modeled off-target cells |
| D | Dead cells, which stay in the accounting |

With N = R + C + O and f(N) = max(0, 1 − N/K):

```
dR/dt = [mu_R f(N) − delta_R − k_C − k_O] R
dC/dt = k_C R + [mu_C f(N) − delta_C] C
dO/dt = k_O R + [mu_O f(N) − delta_O] O
dD/dt = delta_R R + delta_C C + delta_O O
```

Units:
- Time is in hours.
- The rates mu, delta and k are per hour.
- K and all four states are cell counts.

Further rules:
- **Structural zeros.** During `ipsc_expansion`, k_C and k_O are zero by model
  declaration, so supplying them is refused.
- **Stage boundaries.** At the boundary the state carries forward unchanged.
  Cells are not converted automatically.
- **Accounting check.** A fifth bookkeeping state, B (cumulative births), makes
  the accounting check exact: (R + C + O + D)(t) − (R + C + O + D)(0) − B(t) = 0.
  The residual is recorded for every run. A run fails if the relative residual
  exceeds 1e-6.
- **Solver.** Each stage is integrated separately with SciPy `solve_ivp`
  (LSODA, rtol 1e-8, atol 1e-6). A run fails if the solver does not complete
  the interval, if a state becomes nonfinite, or if a state goes negative by
  more than the declared tolerance. Small negative corrections within that
  tolerance are recorded.
- **Required inputs.** 21 in total, listed in `biosense/models/compartment.py`:
  - 14 rates,
  - K,
  - the 4 initial counts,
  - the 2 reference stage durations.
- **Not modeled:**
  - reagent dose-response (CHIR99021 is an *unsupported control*), Wnt timing;
  - metabolism, oxygen, geometry;
  - purification and recovery;
  - marker expression and maturity.

Claim names are mapped to model inputs through a registry. For example, an
`effective_net_growth_rate` claim is refused as `mu_R`, and so is a surface
density used as a count without a declared volume or area.

## Contracts (JSON Schemas in `schemas/`)

| Contract | Schema file | Notes |
|---|---|---|
| SimulationScenario 1.0 | `simulation_scenario.schema.json` | Records for every input: original and normalized value and unit, provenance type, claim or assumption IDs, transformation, and any conflict resolution. Also records gaps, unsupported controls and the model contract. |
| Experiment 1.0 | `experiment.schema.json` | Stage durations, nominal or labelled parameter draw, harvest at the end of the schedule. A computational experiment only. |
| Objective 1.0 | `objective.schema.json` | The metric to maximize, the constraints, and an optional target. Thresholds are user or demo settings. |
| SimulationResult 1.0 | `simulation_result.schema.json` | Status (`completed`, `blocked` or `failed`), hashes of scenario, experiment, code and solver, event log, solver checks, trajectory CSV with units, cost accounting. Blocked or failed results carry no endpoint or metrics. |
| OutcomeEvaluation 1.0 | `outcome_evaluation.schema.json` | Metrics, feasibility, ranking rule, paired comparisons, Pareto table, sensitivity, uncertainty semantics, interpretation, limitations, and one next action. |
| NextAction | `next_action.schema.json` | One of `run_experiment`, `request_evidence`, `request_calibration`, `stop_target`, `stop_budget` or `stop_invalid_model`. Each action carries the runs that support it and at least one alternative. |

## Metrics

| Metric | Definition |
|---|---|
| total viable cells | R + C + O |
| target-cell yield | C at harvest |
| target purity | C / (R + C + O) |
| viability proxy | (R + C + O) / (R + C + O + D) |
| yield per initial cell | C / (R0 + C0 + O0) |
| time to target | First time all target conditions hold. If the target is never reached it is censored, not extrapolated. |
| resource cost | Unknown, because no cost model is supplied |

Further rules:
- A zero denominator gives `null` with a flag.
- Constraints are applied before ranking.
- Ranking is by the objective metric alone. There is no weighted score.
- A zero baseline gives an absolute difference only; the ratio stays `null`.
- If no candidate is feasible, the result is reported as `infeasible`.
  Thresholds are never lowered.

Each evaluation also adds two diagnostics. They are counted as
`diagnostic_simulations`, outside the scientific run budget.
- **Local sensitivity.** One-at-a-time elasticities at the best allocation.
  They show which inputs drive the conclusion and what provenance each has.
- **Paired ensemble.** Optional. The best run and the baseline are rerun on the
  same seeded draws. The spread is an assumption-sensitivity interval, not a
  confidence interval.

## Campaign and next-test policy

The first experimental dimension is how a fixed total culture time is split
between expansion and differentiation. Initial cells, parameter draw and total
time are the same for every experiment.

`runs/<campaign>/campaign_state.json` is the ledger. Only the campaign writer
creates run IDs and decrements the budget. Each run is registered, with its
hashes, before it executes. Failed runs count toward the budget. Results and
evaluations are immutable: the code refuses to overwrite them.

The deterministic policy, in priority order:
1. **Scenario blocked:** `request_evidence`, with the gaps' literature
   parameter IDs, the context and suggested Europe PMC queries.
2. **A nominal run failed:** `stop_invalid_model`.
3. **The best feasible run meets the declared target:** `stop_target`.
4. **A feasible run exists:** `run_experiment` with the midpoints of the
   bracket around the best feasible allocation. If the optimum is already
   resolved to the design resolution, the action is `request_calibration`,
   naming the inputs that drive the result.
5. **Nothing is feasible:** probe more differentiation time. If that is not
   possible, `request_evidence` for the rates that determine purity.
6. **Budget exhausted before resolution:** `stop_budget`.

## Commands

Run all commands from the repository root.

```bash
uv sync --locked
uv run --frozen python -m unittest -v                          # every test (literature + package)
uv run --frozen python -m unittest test_agent_tools -v          # literature tools only
uv run --frozen python -m unittest discover -s tests -t . -v    # biosimulator/outcome package only

# literature handoff -> scenario (evidence-based: expected to be blocked with gaps)
uv run --frozen python -m biosense.cli assemble-scenario --handoff example.handoff.json \
  --selection examples/selection.evidence.json --out runs/scenario.evidence.json
# synthetic demo scenario (durations from the handoff, everything else declared synthetic)
uv run --frozen python -m biosense.cli assemble-scenario --handoff example.handoff.json \
  --selection examples/selection.synthetic.json --assumptions examples/assumptions.synthetic.json \
  --out runs/scenario.synthetic.json
uv run --frozen python -m biosense.cli validate-scenario --scenario runs/scenario.synthetic.json
uv run --frozen python -m biosense.cli simulate --scenario examples/scenario.synthetic.json \
  --experiment examples/baseline.json --out runs/demo-single
uv run --frozen python -m biosense.cli verify-convergence --scenario examples/scenario.synthetic.json \
  --experiment examples/baseline.json

# full offline campaign (fixed pipeline, for tests and demos without an LLM)
uv run --frozen python -m biosense.cli campaign --config examples/campaign.synthetic.json --out runs/demo

# the same steps one at a time (what the agents call)
uv run --frozen python -m biosense.cli campaign-init --config examples/campaign.synthetic.json --out runs/c1
uv run --frozen python -m biosense.cli campaign-propose --campaign runs/c1
uv run --frozen python -m biosense.cli campaign-run --campaign runs/c1 --proposal prop-00
uv run --frozen python -m biosense.cli campaign-evaluate --campaign runs/c1
uv run --frozen python -m biosense.cli check-action --campaign runs/c1 --action runs/c1/evaluations/eval-00.json
uv run --frozen python -m biosense.cli campaign-status --campaign runs/c1
```

## Live Omnigent run

```bash
omnigent run discovery_loop
```

`discovery_loop/config.yaml` defines the coordinator, and
`discovery_loop/agents/{literature,biosimulator,outcome}/` define the three
specialists, all on `claude-sdk`. Each runs in a bwrap sandbox:
- It can write only to `./runs`.
- It can see the project `.venv`.
- Only the literature agent has network access.

Agents call the tools as `.venv/bin/python -m biosense.cli ...`.

A short opening message that works:

> Run the discovery loop with request.example.json and the existing handoff
> example.handoff.json. Show me the evidence_based gaps, then continue in
> synthetic_demo mode with examples/assumptions.synthetic.json.

Watch the handoffs in the browser at the session URL that is printed. Open each
sub-agent session from the Subagents panel. Outputs go to `runs/loop-*/`.

## Limitations

- The model is uncalibrated, and its synthetic rates were chosen to make a
  trade-off visible. Correct numbers do not establish biological validity.
- With the committed example handoff, only the two stage durations are
  reported evidence. The other 19 inputs are synthetic.
- Purity is a modeled compartment fraction. It is not marker positivity,
  subtype purity, maturity or potency.
- No reagent response, metabolism or oxygen is simulated.
- Budgets are enforced in code for computational runs. The literature search
  budget is still enforced only by the prompt.
- No real improvement in performance is claimed. The optional comparison of
  this policy against grid or random allocation search has not been run.
