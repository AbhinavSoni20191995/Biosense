# Bioinformatics: data, analysis and evidence

> **Status.** Phase 1. What runs today is listed below; what is declared but not
> implemented is marked **Planned** and calling it is refused with a message
> saying why.

BioSense does not ask *"what analysis can I run?"*. It asks:

> **What evidence is missing to make the next cell-production decision?**

Everything in this module exists to serve that question, and the structure that
keeps it honest is one required field.

---

## The rule: no uncertainty, no analysis

Every `AnalysisPlan` requires `uncertainty_ref`, which must resolve to either

- a **hypothesis** the analysis agent already raised (a `hypothesis_id` in the
  analysis report's `diagnosis`), or
- an **evidence gap** the loop explicitly recorded.

A plan without one cannot be constructed, and a plan citing a hypothesis the
report does not raise is refused *by name*:

```
uncertainty_ref names hypothesis 'H99', which is not in the analysis report
'a1'. Known: H01, H02. An analysis is planned against an uncertainty the loop
actually has.
```

This is enforced in `biosense/bioinformatics/plan.py` and again in
`orchestrator.data_analysis_errors`, not described in a prompt. It is the
difference between a bioinformatics capability and a dashboard.

---

## Three facts, never collapsed

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/arch-evidence-dark.svg">
  <img alt="Five evidence sources feed one evidence layer that records evidence_class, source_evidence_class, source_visibility, parent_dataset_id and checksums. A derived analysis keeps what it was computed from separate from what it is. A literature claim requires a source, paragraph and verbatim quote, so no dataset can become one." src="assets/arch-evidence-light.svg" width="100%">
</picture>

| Field | Question it answers |
|---|---|
| `evidence_class` | what this object **is** |
| `source_evidence_class` | what it was computed **from** |
| `source_visibility` | who may **see** it |
| `parent_dataset_id` | which dataset it came from, one hop at a time |

So an analysis over somebody's own FACS table is:

```
evidence_class        : derived_analysis
source_evidence_class : private_user_dataset
source_visibility     : private
parent_dataset_ids    : ['my-il7-run']
```

**not** `evidence_class: private_user_dataset`. The analysis is derived evidence;
its parent is private evidence. Any private ancestor makes the whole chain
private to handle — the only safe direction for that rule to fail in.

---

## One agent, or several specialists

The run form offers three ways to gather bioinformatics, as it does for the
literature (`bioinformatics_mode` on the request):

| Mode | Who works | Each writes in |
| --- | --- | --- |
| `single` (default) | one agent across every kind of data | `bioinformatics/` |
| `by_modality` | one specialist per kind of data, in parallel | `bioinformatics/<specialist>/` |
| `by_stage` | one specialist per process stage, in parallel (up to four) | `bioinformatics/<stage>/` |

The specialists are the same agent sent scoped tasks (`SPECIALIST: <name>` or
`STAGE: <stage>`), so the rules about evidence are the same for all of them.
What differs is the data each owns and the analyses it plans:

| Specialist | Modalities | Analyses | Public data fetched here |
| --- | --- | --- | --- |
| transcriptomics | bulk_rna | expression comparison, gene-set score, pathway enrichment, combine | GEO series, end to end |
| single_cell | single_cell_rna | identity and purity, composition, pseudobulk | found on GEO; analysed once registered |
| cytometry (FACS) | flow_cytometry, cytometry_summary | populations, marker intensity, viability, over time | none; gated tables you upload |
| proteomics | proteomics, secretome | protein abundance | none; PRIDE deposits are named, not fetched |
| epigenomics | atac_seq, chip_seq | peak overlap | found on GEO; called peaks once registered |

With no specialists chosen, a by-modality run sends the first four, and adds
the specialist of any dataset the request names. They share the dataset
registry, so a series one registers the others can use. Each keeps its own
running notes, shown under its name on the run page. The orchestrator
reconciles them: agreement between kinds of data raises confidence, and
disagreement is recorded with the context that may explain it, never averaged.
Each specialist is another agent session, so it uses more of the model allowance.

## Datasets

One `DatasetManifest` describes public and private data alike. It records the
source, the accession where there is one, every file's **sha256**, the sample
metadata actually present, and the experimental design — plus, critically,
**what is missing**:

```json
"missing_metadata": [
  {"field": "batch_column",
   "why_it_matters": "whether a batch effect could explain a difference"}
]
```

Nothing is inferred. A plan that needs `condition_column` on a dataset that
does not record one is refused and says so. Choosing the condition column for
you is exactly the kind of help that produces a confident comparison of the
wrong thing.

### Registering your own data

```bash
uv run --frozen python -m biosense.bioinformatics.cli datasets register \
  --file my_facs.csv --dataset-id my-il7-run --title "My IL-7 titration, March" \
  --modality cytometry_summary --cell-type "iPSC-derived T cell" \
  --perturbation "IL-7 concentration" \
  --condition-column condition --control il7_standard --treatment il7_high \
  --replicate-column replicate --donor-column donor
```

It lands under `private_data/` — git-ignored, never served by either HTTP
server, and `citable: false` forever.

### Finding public data

```bash
uv run --frozen python -m biosense.bioinformatics.cli datasets search \
  --query "BACH2 knockout T cell exhaustion" --modality bulk_rna
```

Offline by default. It searches a **committed fixture index** whose accessions
all begin with `SYNTHETIC-GSE`, because presenting an invented accession as a
real one would be fabricating a citation. When it finds nothing it still returns
the exact queries it would have sent:

```json
{"step": "esearch",
 "url": "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=gds&term=..."}
```

so "this index has no match" never reads as "no such data exists".

> **Live search is written but unverified from this repository.** Outbound access
> to NCBI is blocked in the environment it was developed in, so the live branch
> has not been exercised against the real service. It needs two explicit flags
> and is never used by CI.

---

## Analysis

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/arch-capabilities-dark.svg">
  <img alt="Generic statistics, flow cytometry and bulk RNA run today. Single cell, ChIP-seq, ATAC-seq and raw FCS are declared but not implemented. An external-tool adapter ships its contract and a mock." src="assets/arch-capabilities-light.svg" width="100%">
</picture>

Tools are selected from a registry, not hard-coded in a prompt. Each declares
its modalities, analysis types, required metadata and software, so a plan is
checked *before* anything executes.

```bash
uv run --frozen python -m biosense.bioinformatics.cli tools
```

Two things the statistics layer does that a bare t-test does not:

- **It counts independent units.** If the manifest names a donor column, wells
  from one donor are averaged before any test runs, and the row says so:
  *"n counted as 3 donors (donor); 6 rows averaged within each donor first"*.
  Testing the wells inflates n for a reason that has nothing to do with biology.
- **It separates a gain from its cost.** Identity, frequency and yield readouts
  speak to the product; viability and stress readouts bound how far a parameter
  may be pushed. A result where both moved reports the counterweight beside the
  gain.

Multiplicity is corrected with Benjamini–Hochberg across the readouts in a
result, and the q-value is reported *beside* the p-value, never instead of it.

### Which tool answers which question

| Question | Tool | Notes |
|---|---|---|
| Did the gated populations change? | `cytometry.population_comparison` | processed population tables |
| Which genes responded? | `bulk.expression_comparison` | a screen; DESeq2 behind the external adapter for counts |
| Did a named module (identity genes, a pathway) move together? | `bulk.gene_set_score` | the set is the planner's, named in the plan |
| Which programmes moved? | `bulk.pathway_enrichment` | Reactome (CC0) or GO biological process (CC BY), fetched with `datasets fetch-genesets`; or a person's own GMT. Background = the measured genes |
| Did a protein or secreted cytokine change? | `proteomics.abundance_comparison` | processed proteomics or cytokine-panel tables; missing values never imputed |
| What fraction of the product is on-identity? | `single_cell.identity_purity` | per-cell marker rule, compared per sample (n = samples) |
| How does a single-cell culture differ? | `single_cell.pseudobulk_comparison` | collapsed per sample first |
| Do chromatin peak sets differ? | `chromatin.peak_overlap` | called peaks only |
| Do several series agree? | `analyse combine` | Hedges' g, random effects, I² reported; confidence no higher than the best series |

Tool settings are passed as `--option KEY=VALUE` and recorded with the result;
a tool refuses an option it does not know.

---

## From a finding to a parameter

The mapping is a table in `implications.py`, not a judgement made per result:

**The parameter comes from what the dataset says it varied**, not from the
readout that moved. A table whose declared perturbation is "M-CSF concentration"
can implicate `mcsf_ng_ml`; the same readouts in a dataset that varied agitation
implicate `agitation_rpm`. A perturbation that maps to nothing suggests nothing:

```
the dataset declares its perturbation as 'lunar phase', which this version does
not map to any protocol parameter. The comparison still stands as evidence;
naming a parameter for it would be inventing the link.
```

Candidates use the loop's **existing lever vocabulary** —
`parameter · direction · arm_scope · basis · confidence` — so there is no second
competing recommendation structure.

Confidence is **capped at moderate** from a single dataset, whatever the
p-value. The quantity a p-value does not measure — whether this transfers to
your cells, your vessel and your stage — is the one that decides whether a
parameter should move.

---

## What an analysis cannot do

**It cannot change a protocol.** An `AnalysisResult` has no field through which
a protocol change could travel. The path is:

```
AnalysisResult → candidate lever → orchestrator weighs every source
               → proposes a decision → decision envelope validates → commit or refuse
```

A dataset analysis is committed as a `request_bioinformatics` decision, which is
an **information action**: it never spends an iteration of the loop budget. When
it carries `instruction.analysis`, four fields are required — the uncertainty,
the datasets, the plan and the process decision it could inform — and the
orchestrator refuses the decision without them.

`tests/test_data_loop.py` asserts the separation directly, including a test that
`revise.py` contains no reference to analysis results at all.

---

## The external-tool adapter

DESeq2, edgeR, Scanpy and MACS are **declared**; Phase 1 ships the contract and
a mock. The base install needs no R, no Bioconductor and no container runtime,
and ordinary CI runs none of it.

The contract matters more than the execution. A record is refused unless it
carries its command, tool version, parameters, input and output checksums, exit
status, duration and environment:

```
refusing to record a run of 'deseq2' with no tool version. A number whose
software version is unknown cannot be reproduced or disputed, which makes it an
assertion rather than a measurement.
```

A mock record is marked `mock: true`, and **the executor refuses to build an
AnalysisResult from one**: the adapter contract may be exercised with a mock;
evidence may not be produced from one.

---

## Privacy

| Guarantee | How |
|---|---|
| Private data is never served | `roots.is_private_path` — path containment, not a filename rule |
| …not even by misconfiguration | `assert_disjoint` refuses a server rooted inside the private root |
| Private data is never committed | `.gitignore` covers `private_data/` and `data_cache/` |
| Private data is never listed | `/api/datasets` filters the **roots it reads**, not the rows it returns |
| A prompt is not consent | a web-console run gets `datasets: true, private_data: false` |
| Private data is never a citation | a literature claim needs source + paragraph + verbatim quote |

`tests/test_data_privacy.py` asserts each one against the failure, including a
live HTTP server and four URL shapes aimed at a registered private manifest.

---

## Planned, not implemented

Single-cell clustering and annotation (Scanpy) · ChIP-seq and ATAC-seq peak
calling and motif enrichment · raw FCS parsing, automated gating, FlowSOM, UMAP,
CyTOF, spectral · peptide-level proteomics rollup and imputation · calibrating
simulator terms from bioreactor data (waits for an installed bioreactor) · live
adapters beyond GEO and the gene-set libraries · multi-user isolation and
consent capture · multi-omics integration.

The `DatasetManifest` and `ToolSpec` contracts already accept all of them: a
manifest written today for an h5ad file stays valid when its reader lands, and
ingest refuses the file now rather than pretending.
