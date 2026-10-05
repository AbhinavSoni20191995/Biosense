# DESeq2 behind the external-tool adapter.
#
# Run by biosense/bioinformatics/toolkit/bulk/deseq2.py, never by hand and never
# in-process. It is committed so the exact model that produced a number can be
# read without re-running anything.
#
#   Rscript deseq2.R <counts.tsv> <coldata.tsv> <condition> <control> <treatment> <out.tsv>
#
# counts.tsv : features in rows, samples in columns, raw integer counts.
# coldata.tsv: one row per sample, a sample column and the condition column.
#
# The control level is set explicitly with relevel(). Without it DESeq2 orders
# factor levels alphabetically, so the sign of every log2 fold change would
# depend on how the conditions happen to be spelled.
suppressPackageStartupMessages(library(DESeq2))

args <- commandArgs(trailingOnly = TRUE)
if (length(args) != 6) stop("expected 6 arguments: counts coldata condition control treatment out")
counts_f <- args[1]; coldata_f <- args[2]
condition <- args[3]; control <- args[4]; treatment <- args[5]; out_f <- args[6]

counts <- as.matrix(read.delim(counts_f, row.names = 1, check.names = FALSE))
coldata <- read.delim(coldata_f, row.names = 1, check.names = FALSE)

# Refuse rather than silently reorder: a mismatch here would attach every count
# column to the wrong sample's condition and still produce a plausible table.
if (!identical(colnames(counts), rownames(coldata))) {
  stop("counts columns and coldata rows are not the same samples in the same order")
}
if (!condition %in% colnames(coldata)) stop(paste("no", condition, "column in coldata"))

coldata[[condition]] <- relevel(factor(coldata[[condition]]), ref = control)

dds <- DESeqDataSetFromMatrix(countData = counts, colData = coldata,
                              design = as.formula(paste("~", condition)))
dds <- DESeq(dds, quiet = TRUE)
res <- results(dds, contrast = c(condition, treatment, control))

# apeglm shrinkage where available: unshrunk log2 fold changes for low-count
# features are large and meaningless, and this table is read by people.
shrunk <- tryCatch({
  lfcShrink(dds, coef = paste0(condition, "_", treatment, "_vs_", control), type = "apeglm",
            quiet = TRUE)
}, error = function(e) NULL)
lfc <- if (!is.null(shrunk)) shrunk$log2FoldChange else res$log2FoldChange
se  <- if (!is.null(shrunk)) shrunk$lfcSE else res$lfcSE

out <- data.frame(
  feature = rownames(res),
  base_mean = res$baseMean,
  log2_fold_change = lfc,
  lfc_se = se,
  stat = res$stat,
  p_value = res$pvalue,
  q_value = res$padj,
  shrinkage = if (!is.null(shrunk)) "apeglm" else "none",
  stringsAsFactors = FALSE
)
write.table(out, out_f, sep = "\t", quote = FALSE, row.names = FALSE, na = "NA")

cat("deseq2_version=", as.character(packageVersion("DESeq2")), "\n", sep = "")
cat("n_features=", nrow(out), "\n", sep = "")
cat("n_samples=", ncol(counts), "\n", sep = "")
