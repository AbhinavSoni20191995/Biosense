# Four claim levels, and why generating a hypothesis is not adopting one

During real-AI testing BioSense repeatedly returned **nothing** for reasonable
questions — "improve neutrophil production" — not because the science was absent
but because requirements belonging to a much later claim were being applied to
the first one. This document is the rule that replaced that, and the audit of
where the over-constraint actually was.

## The levels

| | Level | What it needs | What it may not do |
|---|---|---|---|
| 1 | **CANDIDATE HYPOTHESIS** | a plausible relationship worth testing, from evidence that may be indirect | change a protocol |
| 2 | **QUANTIFIED HYPOTHESIS** | at least one effect with a magnitude from measurement or derivation | — |
| 3 | **SIMULATOR PREDICTION** | a model with a term for the parameter; labelled SIMULATED | be read as a measurement |
| 4 | **ADOPTABLE PROCESS CHANGE** | the full gates: provenance, registered parameter, bounds, approval | — |

**The requirements of level 4 must never suppress level 1.** A missing dataset, a
refused analysis, absent simulator coverage and an unestablished magnitude lower
the claim level, lower the confidence and constrain the wording. They do not make
a hypothesis impossible.

`claim_level` is computed from a hypothesis's own contents and never asserted:
any simulated effect with coverage makes it `simulated`; any measured or derived
magnitude makes it `quantified`; otherwise it is `candidate`, with the reason the
magnitude was withheld.

## What a candidate looks like

```
CANDIDATE HYPOTHESIS

Increasing G-CSF during granulocytic commitment may increase mature
neutrophil output.

Candidate variable     G-CSF concentration
                       CANDIDATE PARAMETER — NOT YET REGISTERED
Direction              increase
Effect magnitude       NOT ESTABLISHED
Evidence class         published literature · related differentiation system
Confidence             low
Simulator              NOT MODELLED
Main uncertainty       optimal concentration and timing in this cell system
Next experiment        test several G-CSF levels while measuring viable yield,
                       neutrophil identity and maturation
```

That is a valid hypothesis. It is not a validated protocol, not a guaranteed
prediction, and not a parameter BioSense may adopt — `may_change_protocol` is
false and stays false.

## The audit: what was blocking, and where each guard belongs

| Guard | Was | Now |
|---|---|---|
| `expected_effects` must be non-empty | blocked a hypothesis with no number | unchanged — but `direction_only` **is** the answer, and the refusal now says so. `effect_estimate = null` with a stated reason is a valid claim |
| `PR.resolve(parameter_id)` raised for an unregistered lever | **discarded the hypothesis** | the hypothesis is kept, `registered: false`, labelled CANDIDATE PARAMETER — NOT YET REGISTERED, and barred from a protocol |
| simulated/predicted effect requires coverage | — | unchanged, and correct: it gates **level 3**, not level 1 |
| candidates came only from executed analyses and declared values | **no analysis and no declared value → no hypothesis at all** | expert knowledge that names a parameter is a lever too; and when nothing names one, the reason is recorded instead of silence |
| "no hypothesis" | an empty panel | `hypothesis_withheld_reason`, shown where the hypothesis would be |

Classified by the level each guard belongs to:

* **hypothesis creation** — needs a lever, a direction, at least one expected
  effect (magnitude optional), evidence of some class, and a next experiment;
* **quantification** — needs a magnitude from measurement or derivation;
* **simulation** — needs coverage for the parameter in this project's model;
* **protocol commitment** — needs all of the above plus provenance, a registered
  parameter, bounds and a named human approver.

## What the deterministic layer still enforces

Unchanged, and none of it is a veto on reasonable hypothesis generation: schema
validity, units, the parameter vocabulary, provenance, evidence class,
confidence constraints, allowed ranges, privacy, and the protocol and action
safety gates.

## When nothing is proposed

Withholding a hypothesis is a scientific decision and is reported as one.
"No hypothesis" with no reason is indistinguishable from a fault. It is reserved
for the case where nothing — not an analysis, not expert knowledge, not a value
the person asked about — names a lever worth testing, and the reason then says
what would change that.

## Language

The wording follows the strength of the claim: *may*, *suggests*, *worth
testing*, *candidate*, *estimated*, *not established*. Never *will*, *proven*,
*validated* or *optimal* unless a measurement supports it. Allowing more
hypotheses is not permission to assert more.

## Tests

`tests/test_hypothesis_policy.py` covers the five cases:

1. literature and no dataset → candidate allowed
2. a dataset whose analysis refused → candidate allowed, with the limitation
3. an analysis result and no simulator → quantified, simulator NOT MODELLED
4. coverage and a model effect → simulated
5. no effect at all → refused, and the refusal says what shape to use instead

plus the **neutrophil regression**: it asserts no cytokine and no biological
answer, only that when relevant evidence exists, no dataset is executable, no
neutrophil simulator exists and no magnitude is available, BioSense still returns
a candidate hypothesis with `effect_estimate = null`, an explicit evidence class,
a confidence, its limitations and a next experiment.
