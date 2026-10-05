# Phase 3 deliverables — real AI online, with zero terminal commands

Branch `claude/biosense-primary-discovery-interface-cbdtoi`. 848 tests, green on
the base install (9 skips). `scripts/check.sh` exits 0, and its Omnigent
agent-bundle rung now runs from the project's own extra instead of being skipped.

The goal: a public user opens the hosted BioSense, presses **Run AI discovery**,
and the real agentic workflow executes online — without installing Omnigent,
starting a server, registering an agent, opening a terminal, or depending on
anybody's laptop being switched on.

| # | Deliverable | State | Where |
|---|---|---|---|
| 1 | `omnigent_client` declared as a project extra | done | `pyproject.toml` |
| 2 | …and resolved in the lockfile, so no `uv add` after cloning | done | `uv.lock` (113 packages) |
| 3 | The extra also provides the `omnigent` command — nothing global | done | `.venv/bin/omnigent` |
| 4 | One-command local real AI | done | `scripts/start_local_ai.sh` |
| 5 | One-command readiness answer | done | `scripts/check_local_ai.sh`, `scripts/_probe_runtime.py` |
| 6 | Hosted image: app + Omnigent server + executor, one filesystem | written, **not built** | `deploy/Dockerfile.ai` |
| 7 | Boot sequence and runtime supervisor | done, exercised from a checkout | `deploy/start-ai.sh` |
| 8 | Railway configuration for the AI service | done | `deploy/railway.ai.json` |
| 9 | Artifact transport: no boundary, so no transport | done | volume at `/app/runs` inside workspace `/app` |
| 10 | The volume path is asserted, not remembered | done | `test_cloud_runtime.DeploymentTests` |
| 11 | `/readyz`: four named parts, no secrets | done | `biosense/production/health.py` |
| 12 | `/healthz` stays liveness, so a cold runtime cannot cause a restart loop | done | `deploy/Dockerfile.ai`, `railway.ai.json` |
| 13 | Run caps: per caller, per deployment, cooldown, wall clock | done | `biosense/production/budget.py` |
| 14 | A cap answers 429 naming the cap, the retry and the uncapped paths | done | `app.do_POST` |
| 15 | Stop a run, interrupting the Omnigent session not just the stream | done | `POST /api/discovery/<id>/cancel` |
| 16 | A stopped or timed-out run is recorded as stopped, never as an answer | done | `app.STOP_MESSAGES`, `DiscoveryRun.should_stop` |
| 17 | Run recovery by id after a reload, a lost network or a redeploy | done | `biosense/production/run_store.py` |
| 18 | An interrupted run says so; never finished, never still running | done | `run_store.restore` |
| 19 | Hosted posture: `REAL AI — ONLINE`, and remedies a visitor can act on | done | `runtime.HOSTED_LABEL`, `HOSTED_STEPS` |
| 20 | Real runtime first in the picker; caps visible before they refuse | done | `webapp/discovery.js`, `console.html` |
| 21 | A model-provider 401 is reported as `model_auth_missing`, not "failed" | done | `app._work` + `omnigent_runtime.model_auth_reason` |
| 22 | Deployment diagram, light and dark | done | `scripts/make_architecture_diagrams.py` |
| 23 | README leads with the hosted product; install is supplementary | done | `README.md`, `docs/RUN_ON_YOUR_PC.md`, `deploy/README.md` |

## Verified by running it

Not by reading the code:

- `scripts/start_local_ai.sh` from a clean state: extra installed, server up with
  the agent registered at boot, host registered, and **BioSense's own probe
  returning `ok` with `executor: host`** — which is the acceptance condition.
- `deploy/start-ai.sh` (the container's boot sequence) run from a checkout: the
  same three processes, `/readyz` 200 with all four parts true, the badge reading
  `REAL AI — ONLINE`, and the caps in force — a second real run inside the
  cooldown answered **429**.
- A real session started against that runtime with a deliberately invalid model
  key: it reached the agents and **failed honestly** with `model_auth_missing`
  and the remedy. No fabricated result, no synthetic substitution.
- A finished run read back from its own record after the process was replaced,
  with its protocol still exportable, and an in-flight record reported as
  `interrupted`.
- The browser: the hosted picker, the ONLINE badge, the DEMO LIMITS line, the
  stop button, and a result restored after a page reload.

## Not done, and why

**`deploy/Dockerfile.ai` has never been built.** This environment has no Docker
daemon. The boot script it runs was exercised directly against a checkout, and
the image's own invariants — the runs directory inside the workspace, the hosted
posture, the caps, the agent bundle, loopback binding, no baked credential — are
asserted in `tests/test_cloud_runtime.py`. The build itself is untested, and
`deploy/README.md` says so with the command to run first.

**No Railway deploy has been run from this repository.** Same as Phase 2.

**No live model run has completed.** There is still no valid `ANTHROPIC_API_KEY`
in this environment. What is proven is everything up to and including the model
call: the session is created, the brief is delivered, the agents are reached, and
the provider's rejection comes back and is classified. What a real key would
produce — a complete agent-driven discovery run — remains unverified here.

**One container means one crash takes three processes.** The supervisor restarts
the runtime and the platform restarts the container, but this is a single point
of failure accepted for a first production version, not designed away.

**The run-cap ledger is in memory.** A redeploy forgets the counts. Correct for a
cap that exists to stop waste; wrong if it ever needs to enforce an entitlement.

**A caller bucket is not an identity.** Rate limiting keys on the left-most
`X-Forwarded-For` hop, which is attacker-controlled. It stops casual waste and
nothing more; it is not an abuse-prevention system.

**Still no claim of biological improvement.** Unchanged from Phase 2, and
unchanged by any of this: the bioreactor is a synthetic stand-in, and a stand-in
never counts as evidence.
