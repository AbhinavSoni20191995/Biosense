"""Deterministic analysis tools.

Named `toolkit` rather than `tools` because `biosense/bioinformatics/tools.py`
already holds the annotation functions, and a package of the same name would
shadow it silently.

    statistics             two-group comparison, Fisher, correlation, BH-FDR
    cytometry/             processed flow-cytometry population comparison
    bulk/                  minimal bulk-expression comparison

Each is selected through `biosense.bioinformatics.registry`, which declares what
it accepts and what metadata it requires, so a plan is checked before anything
runs.
"""
