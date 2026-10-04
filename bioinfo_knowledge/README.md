# Bioinformatics knowledge sets

Each `*.json` file here is an annotation set the bioinformatics tools can load.
Nothing in this directory is inferred at run time: a tool reports what a file
says, with the file's own provenance and confidence, or it reports `not_found`
and hands back the public queries to run instead. A gene with no entry never
gets a guessed effect.

| Field | Meaning |
|---|---|
| `knowledge_set_id` | File stem; what a request names in `bioinformatics.knowledge_sets` |
| `live` | `false` for a committed file; `true` only for a set written by a live lookup |
| `confidence` on an effect | `synthetic_fixture` (invented for tests), `local_annotation` (curated here, with a real citation), `live_annotation` (fetched from a public API) |
| `source` on an effect | Where the statement comes from, precisely enough to check |
| `experiments_not_possible_in_machine` | Assays that would resolve a question but cannot run in the closed path. These become human consults. |

`synthetic_fixture_genes.json` holds placeholder genes used by the shipped demo
fixtures (`EXH1`, `GENEX`). They are invented. Do not cite them as biology.

## Adding real annotations

Two routes, and they stay separate on purpose:

1. **Curated, committed.** Add a set with `live: false` and
   `confidence: "local_annotation"` on each effect, and give every effect a
   `source` a reviewer can check (DOI, accession, database release). Review it
   like any other evidence.
2. **Live lookup.** Permitted only when the request sets
   `bioinformatics.live_lookups: true`, which CI never does. The tools emit a
   `live_lookup_plan` naming the public endpoints
   (Ensembl REST, UniProt, Open Targets, STRING) and
   `biosense.bioinformatics.tools.live_lookup` performs them, writing a new set
   with `live: true` and the retrieval timestamp. A fetched annotation is still
   an annotation, not a result for your cell type and perturbation.

Database annotations describe genes in general. Whether an effect transfers to
your cell type, differentiation stage and culture format is a judgement the
loop records as a hypothesis, not a fact.
