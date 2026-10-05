# Benchmarks and demonstrations

> **Status.** Phase 2. The public benchmark below runs today from committed
> synthetic fixtures with no network and no model API.

A benchmark runs the real BioSense flow over declared inputs and exports the
**structured artifacts it actually produced**. It is not a set of screenshots,
and nothing in it is written by hand: the scorecard, the figures, the tables and
the report are all rendered from `benchmark.json`.

```bash
uv run --frozen python -m biosense.benchmark.cli list
uv run --frozen python -m biosense.benchmark.cli run --config benchmarks/configs/macrophage_mcsf_demo.json
uv run --frozen python -m biosense.benchmark.cli validate --benchmark benchmarks/public/macrophage_mcsf_demo/benchmark.json
```

---

## Projects decide what a benchmark is about

A benchmark names a **project**, and the project decides the parameters, the
readouts, the stages and the simulator. There is no universal control panel:

| | `ipsc_macrophage` | `cart_expansion` |
|---|---|---|
| Parameters | 12, including M-CSF, IL-3, BMP4, VEGF | 8, including IL-7, IL-15, activation duration, MOI |
| Shared | `agitation_rpm`, `seed_density`, `feed_interval_h` | same canonical ids, different ranges |
| Readouts | monocyte gate, aggregate diameter, harvest | CAR-positive fraction, exhaustion, CD4:CD8 |
| Simulator | `ipsc_monocyte_v1`, status **current** | **none** |

A CAR-T benchmark draws CAR-T parameters. It never inherits an M-CSF slider
because a macrophage project needed one — that is not cosmetic, it implies the
model knows something about it.

The same canonical `agitation_rpm` is 20–140 rpm in the stirred tank and 2–12 on
the rocking platform, and each profile records **why** it narrowed the range:
equipment, literature, an internal experiment or a design choice. A bound with
no origin is a guess wearing the clothes of a constraint, and the loader refuses
one.

---

## Simulator coverage is the row that matters

Every candidate parameter is accounted for in exactly one of two lists:

```
MODELLED     mcsf_ng_ml (M-CSF) -> knob mcsf
NOT MODELLED temperature_c (Temperature): a real design variable in
             ipsc_macrophage, but its simulator has no term for it, so no
             prediction is produced.
```

**The benchmark passes when an unsupported parameter is honestly labelled.** It
fails if BioSense silently drops it or invents an effect for it, and
`benchmark validate` checks both.

IL-7 is the worked case. It is a canonical parameter and a real CAR-T knob. It is
not a knob of the macrophage project, and the CAR-T project has no model at all —
so neither produces a prediction for it, and `cart_expansion` returns
`prediction: none` with a pointer to a real experiment rather than borrowing
another project's model.

---

## Public and private

| | `public_safe` | `private` |
|---|---|---|
| Inputs | public and fixture datasets only | private datasets, expert knowledge |
| Written to | `benchmarks/public/<id>/` | the private data root, outside the repository |
| If private lineage exists | **refuses, writes nothing** | normal |

A public-safe export **refuses rather than anonymising**. Scientifically
sensitive material is not made safe by removing a name: a population frequency
from an unpublished experiment is still that experiment's result. The refusal
names the lineage it found and tells you to re-run against public data or set
the policy to private.

A second scanner runs over the written bundle as a backstop, looking for
absolute home paths, paths inside the private root, usernames and private
dataset ids — but passing the scan does not excuse private lineage. Both checks
must be clean.

---

## What a bundle contains

```
benchmarks/public/macrophage_mcsf_demo/
  benchmark.json          the canonical artifact everything else is rendered from
  summary.md              the concise report: 1-3 pages, decision-oriented
  figures/                workflow, evidence summary, hypothesis effect,
                          simulator comparison, parameter coverage — light + dark SVG
  tables/                 statistics, hypothesis effects, candidates, scorecard (CSV)
  provenance/             execution, datasets, tools
  detailed/               the audit package the summary deliberately omits
```

The **concise report is the default**. A twenty-page technical dump is not a
report; it is a refusal to decide what matters. Everything needed to audit the
run is one directory away in `detailed/` and `provenance/`.

---

## The capability scorecard

Seventeen rows, from `objective_interpreted` to `privacy_validated`. Every result
carries this note:

> This is a SYSTEM CAPABILITY scorecard. It records whether BioSense identified
> an uncertainty, planned an analysis, executed it deterministically, quantified
> what it could, checked simulator coverage and labelled every number. It does
> NOT measure biological truth, and a run can pass every row while being
> biologically wrong.

`benchmark validate` fails a bundle whose scorecard note has lost that sentence.

---

## Reproducibility

Recorded on every run: BioSense version, git commit and dirty flag, package
hash, project profile version, simulator model and version, dataset ids and
checksums, tool versions, and seeds.

`mode: offline` is deterministic and is what CI runs. `mode: ai` is reserved for
a run driven by a live agent; it records the harness, model and spec revision
and **does not imply bitwise determinism**.

---

## What CI checks

No model API, no credentials, no network, no large datasets. CI validates the
**committed** artifacts: schemas, referenced files, numeric provenance, id
resolution, tool versions, simulator identity, coverage honesty, privacy status,
figure integrity and README links.

---

## After a live Omnigent run

The benchmark system consumes BioSense's structured artifacts, not the Omnigent
UI, so:

```bash
omnigent run discovery_loop
uv run --frozen python -m biosense.benchmark.cli export --benchmark runs/<loop>/benchmark.json
```

---

## Exporting one hypothesis

A whole benchmark bundle is more than anyone needs when the question is about a
single claim. A hypothesis is the unit people argue about — "raise M-CSF from 25
to 50 ng/mL" is what gets taken to a meeting — and the question it has to survive
is always *where did that number come from?*

```bash
# what would be exported, and whether it is permitted, writing nothing
uv run --frozen python -m biosense.benchmark.cli export-hypothesis \
  --benchmark benchmarks/public/macrophage_mcsf_demo/benchmark.json --dry-run

# write it
uv run --frozen python -m biosense.benchmark.cli export-hypothesis \
  --benchmark benchmarks/public/macrophage_mcsf_demo/benchmark.json
```

The workspace holds the claim and its parameter, the evidence rows with their
classes, every number flattened beside the estimate type that governs it, the
plain-language account together with the facts it was validated against, the
datasets **by reference and checksum**, any prediction residuals, and the
limitations. It copies no dataset contents: that is what lets the export be
checked without becoming a second, uncontrolled copy of somebody's experiment.

The same privacy rule applies as to a whole bundle. A `public_safe` export of a
hypothesis resting on private lineage refuses and writes nothing at all, rather
than attempting to anonymise it; `--policy private` writes it under the private
root instead.

---

## Limitations

- The public benchmark runs on **invented fixtures**. No number in it is a
  measurement of any real cell.
- The simulator is a mechanistic stand-in, not a validated digital twin.
- The scorecard measures capability, not biology.
- There is **no multi-user isolation**. "Private" means "does not leave this
  machine"; two people sharing a checkout share a private root.
