# Task to give local Codex

Read AGENTS.md, README.md, agent_prompt.md, the existing Python tools, example
request, JSON schema, tests and example extraction/handoff. Preserve work already
present in my local repository. Implement a usable literature-to-simulator
specialist for human iPSC expansion followed by cardiomyocyte differentiation.

The first version already implements Europe PMC search/full-text retrieval and
basic evidence/constraint checks. It does not implement autonomous LLM extraction
or a tested Omnigent deployment. Inspect the current Omnigent documentation and
installed CLI help before wiring the runtime; do not invent API names or flags.

Complete these deliverables:

1. Keep tools deterministic and provider-independent. Make the literature
   specialist run through Omnigent using the existing prompt and project tools.
   First establish a local CLI path; use typed Python tools or MCP only where
   needed. The model must use actual tool results, not pretend searches.
2. Validate input and output with the formal schemas. Reconcile schema/code
   differences and implement malformed-input tests. Confirm simulator-required
   parameter names, units, stage populations and schedule semantics with the
   supplied project specification; unresolved fields stay explicit.
3. Enforce search/full-text/model-call budgets in code. Add request timeouts,
   bounded retries, caching, DOI/PMCID deduplication, atomic run output, error
   records, and a configurable source allowlist. Distinguish retrieval failures,
   inaccessible text and searched-but-not-found parameters.
4. Validate extracted numbers/units against source passages in addition to
   checking excerpt existence. Preserve original and normalized values. Add
   structured intervals with their actual semantics, uncertainty/replicate
   metadata and intervention timing with verified time origins.
5. Keep coherent candidate protocols by species, origin, line, subtype, format,
   medium and arm. Flag source contradictions and incompatible evidence. Do not
   combine different protocols into one universal recipe. Separate observations,
   reported settings, derived estimates, assumptions and selected model inputs.
6. Extend the example workflow to produce extraction JSON and a reviewed
   candidate handoff from live tool outputs. In the current compiler readiness
   is always false. Design any later readiness rule around resolved required
   fields and explicit protocol/model mappings, not just JSON validity.
7. Add an offline reproducible fixture demo, model-independent regression tests,
   and extraction evaluation against manually annotated claims. A manually
   checked four-claim example is not evidence of broad automated accuracy.
8. Verify the Omnigent YAML by actually launching it and completing a small
   research run. Save tool calls, selected model, settings, code/config hashes,
   source identifiers, extraction, handoff and limitations. Mark unavailable
   checks honestly rather than reporting them as passed.
9. Keep regular CI offline and free of model credentials. Document install,
   test, live-run and team-host setup. Follow official Omnigent server deployment
   templates if later hosting is needed; do not replace them with an invented
   generic web-server command.

Acceptance criteria: a supplied request produces a real search, accessible full
text evidence, structured extraction, constraint/conflict checks and a saved
handoff. Missing evidence stays missing. Tests pass from a fresh clone using the
lockfile. One live Omnigent session shows the actual tool calls and resulting
research artifacts. Summarize what changed, tests run, and remaining limitations.
