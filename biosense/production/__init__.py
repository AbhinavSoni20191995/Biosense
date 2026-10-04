"""Production discovery loop: literature protocol -> bioreactor -> analysis -> orchestrator.

protocol      validate a ProductionProtocol (provenance, schedule, genotype arms),
              expand per-arm schedules, render the operator run sheet, record approval
qc            load QC profiles and score release criteria
runs          validate BioreactorRun records (minimal / max mode) against a protocol
analysis      target + QC verdict, genotype comparison, max-mode integrity, diagnosis
orchestrator  typed loop decision and the revision brief for the literature agent

Everything here is deterministic. LLM agents choose and explain tool calls; they do
not compute verdicts. Nothing here actuates laboratory equipment.
"""
