# BioSense-AI iPSC discovery loop: literature, biosimulator and outcome agents

Three Omnigent specialists and a coordinator form a computational discovery
loop: literature evidence -> explicit scenario -> toy-model simulation ->
outcome evaluation -> typed next action (run another experiment, request
evidence, request calibration, or stop). The biosimulator and outcome agents
are documented in [docs/BIOSIMULATOR.md](docs/BIOSIMULATOR.md); launch the
whole loop with `omnigent run discovery_loop`. The rest of this README covers
the literature agent.


This specialist agent turns a constrained literature question into candidate,
evidence-backed simulator inputs. The example is human iPSC expansion followed
by cardiomyocyte differentiation. It is not a complete autonomous application:
Omnigent supplies the LLM/harness; this package supplies its prompt and executable
retrieval/validation tools. Live Omnigent orchestration has not been tested here.

## What is included

- `agent_prompt.md`: ready-to-use instructions for the specialist.
- `request.example.json`: editable input and proposed simulator requirements.
- `agent_tools.py`: search, open-access full-text retrieval, unit conversion,
  provenance checks, conflict detection, constraint checks and handoff assembly.
- `output.schema.json`: machine-readable handoff contract (JSON Schema).
- `example.extraction.json`: manually checked extraction from a real paper.
- `example.handoff.json`: compiler output for that extraction. This is an example
  integration run, not a comprehensive automated literature review.
- `test_agent_tools.py`: synthetic regression tests for rejection and unit cases.

## Start with the input

An iPSC is an induced pluripotent stem cell: the starting cell population.
A cardiomyocyte is a heart-muscle cell: the target of differentiation. Expansion
in this example refers to increasing the starting iPSC population; differentiation
refers to conversion toward the target. Keep any proliferation during later
stages separately labeled.

Set the objective, species, source/target cell, allowed sources, search budget,
hard constraints and simulator's required parameters. Null culture format/cell
line means unknown, not unrestricted transferability. Ask the simulator developer
to confirm required parameter names, units, meaning and model equations.
The defaults are illustrative requirements; they may not fit your simulator.

"Dimensions" can mean two different things: variables such as temperature or
reagent concentration, and physical dimensions/units such as time or amount per
volume. The output needs both. Also distinguish controllable variables from
constants in the model and measurable outcomes.

| Category | Examples | Simulator use |
|---|---|---|
| Initial conditions | Cell density, initial nutrient concentration | Starting state |
| Operating conditions | Temperature, pH, oxygen, medium identity | Fixed settings or candidate controls |
| Schedules | Reagent timing, medium exchanges, stage duration | Time-dependent inputs |
| Geometry | Vessel working volume, area, aggregate size metric | Context or model geometry |
| Kinetic constants | Growth, death, nutrient uptake, state transitions | Equations; often need fitting |
| Outcomes | Final cell count, marker-positive fraction, viability | Calibration/validation observations |

An outcome is not automatically a kinetic constant. A reported recipe is not
automatically an optimized recipe. A range tested in a paper is not necessarily
a validated optimization domain. Percent marker positivity is not potency or
cardiomyocyte-subtype purity.

## Retrieval and extraction workflow

1. Search Europe PMC, using separate query families for each stage and model
   parameter. PubMed metadata helps find papers; full text and supplements are
   needed for settings. Publisher protocols and supplied PDFs can supplement
   evidence after lawful access. This Python backend currently implements Europe
   PMC only; other sources require adapters. It does not scrape Google Scholar.
2. Read primary Methods, Results, tables and timelines. Preserve independent
   protocols by cell line, medium, format and arm. Do not combine 2D monolayer,
   suspension aggregate and microcarrier settings into one recipe.
3. The Omnigent LLM extracts claim JSON according to the prompt. The supplied
   example was checked manually. The tools do not automatically understand all
   biology or discover numerical parameters without the LLM.
4. Compile candidate evidence; reject unsupported passages, keep constraints
   and conflicts visible, report gaps. Search failure is different from absence
   of published evidence. Unsupported/missing values are never invented.
5. Review applicability and choose a coherent protocol. Map approved claims to
   the simulator. `selected_parameters` is initially empty and readiness false:
   a sentence-match check alone cannot approve a biological parameter.

## Run the tools (Python 3.12 via uv, stdlib only)

```bash
uv sync --locked
uv run --frozen python -m unittest -v
uv run --frozen python agent_tools.py search '(hiPSC OR "induced pluripotent stem cell") AND cardiomyocyte AND (bioreactor OR expansion)' --page-size 5 --out runs/search.json
uv run --frozen python agent_tools.py fetch PMC7076930 --out runs/source.json
uv run --frozen python agent_tools.py compile --request request.example.json --extraction example.extraction.json --sources runs/source.json --out runs/handoff.json
```

Research outputs go under `runs/` (git-ignored). Omnigent setup and launch:
see [OMNIGENT_SETUP.md](OMNIGENT_SETUP.md).

Fetch requires internet access to www.ebi.ac.uk. Full texts are restricted to
what the Europe PMC open-access endpoint provides. Preserve source licenses and
respect service limits. Article supplements are not fetched by this version;
their presence is flagged. API metadata article-status checks are preliminary,
not a complete retraction/correction database audit.

In Omnigent, configure a specialist with `agent_prompt.md` and expose the Python
functions listed there as tools, using the current official agent configuration
schema. Alternatively, the harness can run the CLI. Set read-only access to
evidence and write access only to research outputs; cap searches/full-text
requests and model spend. The search budget is currently prompt-enforced, not a
hard backend counter. Add a supervisor counter before unattended use.

This package is not a tested Omnigent YAML deployment. The official project
documents Python-function tools and specialist/sub-agent definitions:
https://github.com/omnigent-ai/omnigent

## Example that exposes a real extraction problem

Laco et al. (2020), DOI 10.1186/s13287-020-01618-6, PMCID PMC7076930, describes an
integrated FR202 microcarrier process. The extracted phase durations are 5 days
for starting-iPSC expansion and 9 days for differentiation. Methods reports
10 uM CHIR99021; Results reports 12 uM in the bioreactor description. Keep both
and request clarification. Do not interpret the discrepancy as a recommended
10-12 uM interval. The example contains only four claims, so its missing-input
list is a partial-extraction gap list, not proof that the paper lacks those data.

Source: https://link.springer.com/article/10.1186/s13287-020-01618-6

The example's short excerpts are matched against live-retrieved paragraphs.
Full article text is not redistributed in this package. The source's cell line,
medium, culture format and time origin must remain attached to each datum.

## Useful next extension

Add numeric intervals with an explicit type (tested doses, observed spread,
standard deviation, confidence interval, or proposed prior), intervention
objects with verified time origins, and dimensional units through a dedicated
library. Add automated numeric transcription checks and semantic review. Add
source adapters and supplement retrieval. Decide the model contract with the
simulator author before selecting any parameter set.

A production-ready handoff must distinguish:
reported values; derived estimates with formula and parent observations;
model assumptions; user-imposed bounds; and reviewed simulator selections.
The first version deliberately compiles directly reported scalar claims only.

Official source/API documentation:
https://europepmc.org/RestfulWebService
https://pmc.ncbi.nlm.nih.gov/tools/textmining/

## Validation completed on 2026-10-04

Live Europe PMC search and open-access XML retrieval succeeded. Four manually
extracted claims were matched to the retrieved source; the concentration
discrepancy was flagged. All 12 synthetic regression tests passed. Autonomous
LLM extraction, semantic accuracy across many papers, formal JSON Schema
validation, and live Omnigent configuration have not yet been evaluated.
