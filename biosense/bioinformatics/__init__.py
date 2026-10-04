"""Deterministic bioinformatics tools for the orchestrator and its analysis agent.

What these tools do: report what a loaded annotation set says about a gene and a
perturbation, name the protocol parameters those statements implicate, and name
the assays that would settle a question but cannot run in this machine.

What they never do: invent an effect. A gene absent from every loaded set comes
back `found: false` with the exact public queries to run instead. A direction is
reported only when a knowledge entry states it, and every statement carries its
source and a confidence label that distinguishes a synthetic fixture from a
curated annotation from a live database lookup.

Network access is off unless a request sets `bioinformatics.live_lookups: true`;
ordinary CI never needs it.
"""
from . import knowledge, tools
