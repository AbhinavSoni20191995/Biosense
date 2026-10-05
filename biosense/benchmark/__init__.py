"""Benchmarks: a reproducible demonstration built from the artifacts a run produced.

Not screenshots. A benchmark runs the real flow, collects the structured objects
it emitted, and renders a scorecard, figures and a concise report from those.
If the run did not produce a hypothesis, the scorecard says so.

    config      what to run and under which export policy
    runner      runs it and assembles the BenchmarkResult
    figures     deterministic SVG from the structured result
    report      the concise scientific summary
    privacy     the validator that refuses an unsafe public export
"""
