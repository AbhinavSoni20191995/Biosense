# Phase 2 deliverables

Branch `claude/vigilant-edison-7yj2v4`, 13 commits. 677 tests, green on the base
install (9 skips) and with the `singlecell` extra (1 skip). `scripts/check.sh`
exits 0.

| # | Deliverable | State | Where |
|---|---|---|---|
| 1 | Canonical parameter registry | done | `biosense/parameters.py` |
| 2 | ProjectProfile / per-project knobs | done | `biosense/projects.py`, `projects/*.json` |
| 3 | Bounds narrow, never widen; `bound_origin` required | done | `Project._check_bounds` |
| 4 | `simulator_coverage` incl. `not_modelled`, `no_simulator` | done | `Project._check_coverage` |
| 5 | Estimate types with per-number provenance | done | `biosense/evidence/estimates.py` |
| 6 | Weakest-link combination; measured→measured is `derived` | done | `change_type()` |
| 7 | Percentage points vs relative change kept apart | done | `estimate()` |
| 8 | `magnitude_estimated: false` + required reason | done | `direction_only()` |
| 9 | Quantified hypothesis contract | done | `evidence/hypothesis.py` |
| 10 | Narrative validated against structured facts | done | `evidence/narrative.py` |
| 11 | ResearchContext, STRICT/PREFER/OPEN, never-generalise pairs | done | `evidence/context.py` |
| 12 | Expert knowledge as a private, non-citable class | done | `evidence/expert.py` |
| 13 | Real dataset ingest | done | `biosense/data/ingest.py` |
| 14 | Single-cell h5ad + pseudobulk | done | `data/readers/h5ad.py`, `toolkit/single_cell/` |
| 15 | Bulk comparison | done (Phase 1) | `toolkit/bulk/basic_de.py` |
| 16 | DESeq2 external adapter | code done, **never run against real R** | `toolkit/bulk/deseq2.py`, `bioinformatics/r/deseq2.R` |
| 17 | Expanded FACS | done (Phase 1) | `toolkit/cytometry/` |
| 18 | Chromatin peaks | done | `data/readers/peaks.py`, `toolkit/chromatin/` |
| 19 | Optional extras with capability probing | done | `registry._register_single_cell` |
| 20 | Candidate → simulator handoff | done | `production/sim_candidate.py` |
| 21 | Quantified simulator comparison | done | `compare_conditions()` |
| 22 | Prediction residuals | done | `evidence/residual.py` |
| 23 | Benchmark run/validate/export/list | done | `biosense/benchmark/` |
| 24 | Capability scorecard | done | `benchmark/runner.py` |
| 25 | Public-safe export refuses private lineage | done | `benchmark/privacy.py` |
| 26 | Per-hypothesis export workspace | done | `evidence/export.py` |
| 27 | Concise report + detailed export | done | `benchmark/report.py` |
| 28 | Four-way nav | done | `webapp/*.html` |
| 29 | SIMPLE / TECHNICAL views | done | console + data pages |
| 30 | Project-driven simulator controls | done | `webapp/simulator.js` |
| 31 | Hypothesis card + evidence badges | done | `webapp/console.html` |
| 32 | Diagrams + README marked by phase | done | `scripts/make_architecture_diagrams.py` |
| 33 | Live external data | code written, **not verified** | `data/sources/geo.py` |

## Not done, and why

**DESeq2 has never run.** No R in this container. The handoff and the parsing are
pure functions and are tested on every machine; `run()` is covered by a stub on
PATH that answers like Rscript. That tests the wiring and says nothing about the
statistics. The tool is not registered without Rscript, so it is refused at
planning rather than part-way through a run.

**Live GEO is unverified.** The egress proxy refuses CONNECT to NCBI by
organization policy (403). The response parsing is now covered by feeding
recorded eutils shapes through the real parser; whether NCBI is up, or whether
its schema still looks like that, is unknown from here.

**No real-AI demonstration.** There is no `ANTHROPIC_API_KEY` in this
environment, so P2.11 — a complete local run with a real model over real data —
did not happen. Everything below it is in place; the model call is not.

**No closed loop against a real bioreactor.** The residual contract exists and is
tested, but every measurement available here is synthetic, and a synthetic
stand-in never counts as model evidence by construction. Nothing in this branch
supports a claim of biological improvement.
