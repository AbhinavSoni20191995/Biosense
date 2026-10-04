# The worked iPSC → T-lineage example

Two loops, from the two halves of one question:

> Optimise the conditions for growing wild-type T cells from iPSC and output the
> best protocol. Then compare with what happens when a T cell carrying a gene
> knockout — BACH2 — has to be expanded to the same target.

```bash
uv run --frozen python scripts/run_ipsc_tcell_example.py --out reports
```

No model, no network, no credentials. The committed output is in
[`reports/`](../reports): a reasoning report per loop and a comparative one, each
as HTML and PDF.

## What came out

| Loop | Arms | Iterations used | Ended in | Best per arm |
|---|---|---|---|---|
| `wt` | WT only | **3 of 30** | `protocol_succeeded` | WT 25.72 (met) |
| `bach2` | WT + BACH2 KO | **25 of 30** | `protocol_succeeded` | WT 25.74 (met), BACH2_KO 25.95 (met) |

Both reach the same target of 25 T cells per input iPSC at day 40. The number
worth looking at is not either yield — it is **3 against 25**. Adding one
knocked-out arm multiplied the search by roughly eight, and the reason is in the
loop's own words:

> IL-7 cannot be set to one shared value: moving it 16 → 25.6 ng/mL improved
> BACH2_KO and degraded WT. The arms are being given separate values of this
> parameter from here on.

That is the finding. One schedule could not serve both genotypes, the search
established it from measurements rather than assuming it, and it then split the
parameter per arm. Where the two arms ended up:

| Arm | Parameter | Stage | Shared value | This arm | Unit |
|---|---|---|---|---|---|
| BACH2_KO | IL-7 | t_commitment | 10 | 16 | ng/mL |
| BACH2_KO | DLL4 | t_commitment | 500 | 800 | ng/mL |
| BACH2_KO | IL-7 | maturation | 10 | 25.6 | ng/mL |
| BACH2_KO | SCF | maturation | 50 | 80 | ng/mL |
| BACH2_KO | anti-CD3 | maturation | 13.31 | 11.713 | ng/mL |
| BACH2_KO | IL-7 | expansion | 16 | 25.6 | ng/mL |
| BACH2_KO | IL-15 | expansion | 16 | 14.08 | ng/mL |

More IL-7 at every stage, and a weaker TCR stimulus. Those are the two levers
the curated BACH2 annotation implicated — and that is precisely why this proves
nothing biological, which the next section is about.

## What this does not show

**The stand-in was built to reward those levers.** `standins/ipsc_tcell.py` is a
phenomenological model, not a digital twin, and
`examples/ipsc_tcell/standin_truth.synthetic.json` — which no agent and no report
ever reads — gives the knockout arm a faster differentiation index and a lower
TCR optimum *because that is what the annotation's inference levers predict*. The
loop recovering them demonstrates that the search works. A real experiment could
reward the opposite, and the loop would have to discover that instead.

**No number in either protocol is attributed to a publication.** The evidence
handoff holds directional claims only. Every quantity in both final protocols is
a `design_choice` — 29 in the wild-type loop, 36 in the comparison — and the
reports show that count rather than burying it.

**The cited BACH2 work is not in iPSC-derived cells.** It is in mouse and human
peripheral or engineered T cells. The curated knowledge set marks what a paper
reports as `local_annotation` and marks the transfer to an iPSC-derived
expansion readout as `inference`, with the transfer stated in full. The
distinction survives into the reports.

**One replicate per arm.** Every difference is directional only, and the search
was climbing a surface it could not distinguish from noise below its own step
size.

## How the search works

`biosense/production/revise.py` is a bounded coordinate search with backtracking:

- it moves **one lever per arm per iteration**, because arms are separate
  cultures and several simultaneous moves make one observation unattributable;
- it scores each move against that arm's **best reading so far**, not the
  previous one, and reverts anything that does not beat it — so the protocol in
  hand is always the best configuration found;
- when a shared move helps one arm and hurts another it records a **conflict**,
  reverts, and re-issues the lever per arm, each in the direction its own result
  asked for;
- it keeps stepping a lever while it keeps paying off;
- when every evidence-implicated lever is exhausted it explores the protocol's
  own quantities, and labels those moves `unimplicated_search` so blind
  exploration is never mistaken for an evidence-led step.

It has no model of the process and cannot see interactions between levers. It
finds a better recipe or it does not; it never establishes why one works.

## The documents

| File | What it holds |
|---|---|
| `examples/ipsc_tcell/request.wt_d40.json` | the wild-type-only request |
| `examples/ipsc_tcell/request.bach2_d40.json` | the WT vs BACH2-KO request |
| `examples/ipsc_tcell/standin_truth.synthetic.json` | hidden truth; invented, never read by any agent, never served |
| `bioinfo_knowledge/tcell_curated_genes.json` | BACH2 annotations with real citations, split into `local_annotation` and `inference` |
| `reports/*.reasoning_report.{html,pdf}` | the committed reports |

## Reading a reasoning report

Each one carries, for the loop it describes:

- **every decision** with its reasoning, the alternatives the envelope refused
  and the refusal reason for each, the hypotheses raised with their basis, the
  tool results cited, and the action set it was validated against;
- **every search move**: what it moved, by how much, for which arm, what
  happened to each arm's reading, and whether it survived or was reverted;
- **every quantity** in the final protocol with its provenance, so the line
  between a cited value and a chosen one is visible at a glance;
- **references** with DOIs, what each was used for, and whether its full text was
  ever retrieved — which for this example is always *no*;
- **limitations**, stated rather than implied.

Decisions here are authored by the deterministic policy, so they carry
`authored_by: "policy"` and the report labels their reasoning as a description of
a rule rather than a model's reasoning. A live Omnigent session replaces exactly
that part and writes `authored_by: "orchestrator_agent"`; the report then shows
both the agent's choice and what the policy would have done, side by side.

## Why Europe PMC was not used

`agent_tools.py` retrieves open-access full texts from Europe PMC, and that is
the intended evidence path. It could not run in the environment where these
reports were generated: outbound access to `www.ebi.ac.uk` was refused by the
network policy (403 on CONNECT). The citations in
`biosense/production/design.py` were therefore located from publication records
instead, no full text was retrieved, and **no numeric value is attributed to any
of them** — which is why every protocol quantity is a design choice.

That is a real limitation of this run, not a design decision. On a machine with
Europe PMC access, the literature agent can extract numeric claims and those
quantities become `reported` or `adapted`, with the claim IDs to check them
against. The reports will show the change in the provenance counts.
