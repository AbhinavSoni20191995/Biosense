# Dataset fixtures

Committed, synthetic, and labelled as such in their own manifests. They exist to
exercise the dataset contracts — ingest, checksum, plan, analysis, provenance —
without a network call and without anybody's real data in git.

| File | What it is |
|---|---|
| `facs_mcsf_synthetic.csv` | A processed flow-cytometry population table: population frequencies per sample under two M-CSF conditions. **Invented.** No cytometer produced these numbers. |
| `facs_il7_tcell_synthetic.csv` | A second processed population table, for iPSC-derived T-cell expansion under two IL-7 doses. **Invented.** It exists so the worked example proves the perturbation-to-parameter mapping is a table and not a special case for one factor. |
| `bulk_counts_synthetic.csv` | A small normalised bulk-expression matrix, long format, for the bulk comparison tool. **Invented.** |
| `geo_index.json` | The offline index the GEO adapter searches. Every record is **invented** and every accession begins with `SYNTHETIC-GSE`, because presenting a made-up accession as a real one would be fabricating a citation. It exists to exercise the adapter's scoring and candidate contract. A live GEO search returns real accessions and is gated behind an explicit network permission. |

Nothing here is biological evidence. A run that uses these fixtures produces a
result labelled `derived_analysis` over a source the manifest marks synthetic,
and the analysis carries that in its limitations.

Private data goes in `private_data/`, which is git-ignored and never served.
