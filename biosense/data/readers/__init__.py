"""Readers for formats beyond CSV and TSV.

Each reader declares the optional extra it needs and **refuses with the install
line** when it is absent. It never silently does something cheaper and calls it
the same analysis: a pseudobulk comparison that quietly became a row count is
worse than an error.

    h5ad     single-cell matrices, needs biosense[singlecell]
    peaks    BED / narrowPeak / broadPeak, stdlib only

Imported lazily by name rather than eagerly here, so that a missing extra never
breaks an unrelated import.
"""

__all__ = ['h5ad', 'peaks']
