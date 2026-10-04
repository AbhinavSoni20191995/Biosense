"""Integrated-machine analysis tools (max mode) and a synthetic bioreactor stand-in.

analysis_tools  deterministic max-mode checks: kinetics, two-tier purity,
                sensor health, staining integrity, coherence, mass balance,
                clonal drift and four-way cause attribution.
simulator       mechanistic iPSC -> monocyte bioreactor model (NOT a digital
                twin) used as a labelled synthetic stand-in for the wet lab.
protocol_runner maps a ProductionProtocol onto the simulator and emits a
                BioreactorRun 2.0 record (source = synthetic_standin).
"""
