# Hosting BioSense-AI

There are two ways to host this, and the difference between them is whether the
service holds a model key.

| | `deploy/Dockerfile.ai` | `deploy/Dockerfile` |
|---|---|---|
| What a visitor can run | **the real discovery agents**, plus the demonstration path | the demonstration path only |
| Holds model credentials | **yes**, one secret | no |
| Processes in the container | BioSense + Omnigent server + executor | BioSense |
| Needs run caps | **yes**, and it sets them | no |
| Badge in the browser | `REAL AI — ONLINE` | `SYNTHETIC DEMO` |
| Image size, boot time | larger, ~20s | small, ~3s |

Pick the first one if the point is that people can use the product. Pick the
second if the point is a public URL that provably holds nothing.

Both serve `webapp/console.html` at `/`, keep the read-only tracker at
`/index.html`, and write their artifacts to one volume.

---

## A. The real-AI service (the hosted product)

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="../docs/assets/arch-deployment-dark.svg">
  <img alt="Anyone, in a browser, reaches only the BioSense web app over https. Inside one Railway service the app creates sessions on an Omnigent server bound to loopback, which launches a runner through a registered host with /app as its workspace. The runner executes the discovery_loop agents, which write artifacts to the mounted volume at /app/runs; the app ingests from the same directory, so there is no transport because there is no boundary. The model key lives only in that container's environment, run caps bound what it can spend, and /readyz reports whether the runtime is reachable, the agent registered, an executor available and credentials present, without exposing any of them. The same three processes run on a laptop from one script, and the separate synthetic-only image holds no credentials at all." src="../docs/assets/arch-deployment-light.svg" width="100%">
</picture>

**One Railway service. Three processes. One filesystem.**

```
Browser
  │  https
  ▼
┌───────────────── Railway service ─────────────────┐
│  BioSense app       0.0.0.0:$PORT   ← exposed     │
│       │ BIOSENSE_RUNTIME_MODE=local               │
│  Omnigent server    127.0.0.1:6767  --agent …     │
│  Omnigent host      → loopback                    │
│                                                   │
│  /app        code, .venv, discovery_loop, tools   │
│  /app/runs   ← the volume. Agents write, app reads│
│  ANTHROPIC_API_KEY  in this service's env only    │
└───────────────────────────────────────────────────┘
```

### Why one container and not a service each

Because the artifact-transport problem disappears instead of being solved. The
agents write their artifacts to the filesystem and BioSense reads them from the
filesystem (`biosense/production/ingest.py`). Co-locating the processes means:

- there is no machine boundary, so there is no transport to invent, no new
  credential to mint and no object store to run;
- the runner has BioSense's code, dependencies, deterministic tools and
  simulator **by construction**, because it is the same image.

The alternatives were weighed and rejected for this version: an inbound artifact
POST needs a new write route, a token, and an LLM that reliably posts; shared
object storage and a database are new infrastructure for a problem that can be
deleted; and Omnigent's session-file events have no agent-side emission path in
0.16.0. A managed sandbox host (`host_type: managed`) would need a paid
third-party provider account **and** BioSense bootstrapped into a generic host
image on every launch.

**The honest cost.** This container holds a model key, which the synthetic image
does not — so the argument "defensible because it holds nothing" is replaced by
"defensible because what it can spend is bounded", which is why the caps below
are not optional. And one container means one crash takes all three processes;
`deploy/start-ai.sh` supervises and restarts the runtime, and the platform
restarts the container.

### Deploy it

```bash
railway init
railway up                                  # deploy/railway.ai.json -> Dockerfile.ai
railway volume add --mount-path /app/data     # NOT /data. See below.
railway variables set ANTHROPIC_API_KEY=sk-...
```

**The mount path is load-bearing, and it holds two things.** The volume at
`/app/data` carries `runs/` (artifacts and run journals) and `private/` (the
private data root, which is where the **projects accounts create** live). Both
must be on the volume: a project is a file, and an image filesystem is
ephemeral, so without `private/` on the volume every project a scientist makes is
lost on the next redeploy.

Both must also be inside the runner's workspace (`/app`): the agents write under
the run directory relative to that workspace, and `runtime.check_workspace`
**refuses** a real run when BioSense is reading anywhere else — because that run
would otherwise succeed and produce artifacts nobody ever sees. Mounting the
volume at `/data` instead makes the service refuse every real run, by design,
with that reason. `tests/test_cloud_runtime.py` asserts the image's own
`RUNS_DIR` is inside its workspace, so the image cannot drift out of agreement
with itself.

Migrating an existing deployment whose volume is at `/app/runs`: either move the
mount to `/app/data`, or keep it and set `RUNS_DIR=/app/runs` with
`BIOSENSE_PRIVATE_DATA=/app/runs/_private` — the two roots must stay disjoint,
which the server checks at startup and refuses to run without.

### Is it working? `GET /readyz`

`/healthz` is liveness: the web process answers. That is what the platform
healthcheck uses, and all it should ever use — pointing a healthcheck at
readiness restarts the container while the runtime is coming up, and keeps
restarting it if a key is missing, taking the demonstration path down with it.

`/readyz` is the question a person has when the button does not work, in four
named parts:

```json
{ "ok": true, "label": "REAL AI — ONLINE", "runs_dir_writable": true,
  "checks": { "local_real_ai": {
      "omnigent_reachable": true, "agent_registered": true,
      "executor_available": true, "model_credentials": true,
      "reason": "ok", "executor": "host" } } }
```

It returns 503 when the runtime this deployment offers cannot run, or when the
runs directory is not writable. It carries no token, no model key, no filesystem
path and no server URL beyond scheme and host — asserted in the tests, not
reviewed by eye. Each failing part carries a remedy written for whoever is
reading it: a visitor is never told to run `omnigent host`.

### Run caps

A public URL that can spend model credits needs an answer to "how much may a
stranger spend", and `biosense/production/budget.py` is it. Defaults with
`BIOSENSE_PUBLIC_DEMO=1`:

| Cap | Variable | Default |
|---|---|---|
| Real runs per caller per day | `BIOSENSE_MAX_REAL_RUNS_PER_CLIENT` | 3 |
| Real runs per deployment per day | `BIOSENSE_MAX_REAL_RUNS_PER_DAY` | 40 |
| One run's wall clock | `BIOSENSE_REAL_RUN_TIMEOUT_S` | 1800 |
| Between one caller's runs | `BIOSENSE_REAL_RUN_COOLDOWN_S` | 60 |
| Real runs at once | `MAX_CONCURRENT_REAL` in `app.py` | 1 |

Set any of them to `0` to remove that cap — the only way to remove one, so it
cannot happen by accident. Reaching a cap answers **429** naming the cap and when
to retry, and points at the two uncapped paths (the demonstration run, and
running BioSense yourself). It never substitutes a synthetic run.

Worth knowing:

- A caller is a **bucket, not an identity**: a hash of the left-most
  `X-Forwarded-For` hop, in memory, used for counting and nothing else. A shared
  NAT shares a bucket; that is the accepted cost of not tracking people.
- The ledger is in memory, so a redeploy forgets the counts. Correct for a cap
  that exists to stop waste rather than to enforce an entitlement.
- A run can be **stopped** from the page (`POST /api/discovery/<id>/cancel`),
  which interrupts the Omnigent session as well as the local stream — the cost
  is in the agents, not in the stream.
- A stopped or timed-out run is recorded as `stopped`, with whatever the agents
  had written. It is never reported as a finished answer.

### Designating an operator account

A deployment that you develop against will hit its own demo caps, which is
correct for strangers and tiresome for you. Two variables fix that, and both are
server-side configuration a request can never reach:

```bash
railway variables set BIOSENSE_AUTH_SERVER=https://your-omnigent-accounts-server
railway variables set BIOSENSE_ADMIN_USER_IDS=you@yourlab.example
```

`BIOSENSE_AUTH_SERVER` is the one Omnigent accounts server this deployment trusts
to say who somebody is; `BIOSENSE_ADMIN_USER_IDS` is a comma-separated list of
ids on that server. **Both are required**: naming administrators without naming
the issuer refuses to start, because anyone can run an Omnigent server and return
any id they like, so an id alone is not an identity.

Signed in as a listed account, the page reads `ADMIN · REAL AI — ONLINE` and the
demo caps do not apply — not the per-caller daily cap, not the deployment daily
cap, not the cooldown. The run timeout, the one-at-a-time gate, the size limits,
the fixed agent bundle and every data boundary still do, and **other accounts'
runs and projects remain invisible**: an exemption from spending caps is not a
key to anybody's science. Operator runs are counted as
`admin_real_runs_today`, separately from the public ones, because they are not
free.

Nothing needs an admin identifier in source, and nothing needs editing to add or
remove one: change the variable and redeploy. The full model, and the planned
user-provided-key design that is **not** implemented, are in
[docs/ACCOUNTS.md](../docs/ACCOUNTS.md).

### Running an accounts server for sign-in

The runtime server inside the container is a loopback single-user server, so it
has no accounts and no `/auth/login`. Sign-in therefore needs an Omnigent server
running in **accounts** mode, which is a separate process (a second small service,
or one you already run):

```bash
OMNIGENT_AUTH_ENABLED=1 OMNIGENT_AUTH_PROVIDER=accounts OMNIGENT_ACCOUNTS_ENABLED=1 OMNIGENT_ACCOUNTS_COOKIE_SECRET="$(openssl rand -hex 32)" OMNIGENT_ACCOUNTS_BASE_URL=https://your-omnigent-accounts-server OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD='<first-boot admin password>'   omnigent server --host 0.0.0.0 --port "$PORT" --no-open
```

On first boot that creates one admin account (named `root` unless the accounts
store says otherwise) and invites are issued from there. Point
`BIOSENSE_AUTH_SERVER` at it. Verified in this repository against a real accounts
server: `POST /auth/login` returns `{"token": ..., "user": {"id": ..., "is_admin": ...}}`
and `GET /auth/me` returns the same id, which is exactly the shape BioSense reads.

### What a visitor can and cannot do

Still enforced in code, exactly as in the synthetic image:

- **No bioreactor.** `engine.preflight` refuses any request whose
  `bioreactor_source` is not `synthetic_standin`; a wet-lab run always needs a
  named human approver.
- **No self-approval.** The approver string an app run records says in words
  that no human approved it.
- **No hidden truth.** No handler serves a file whose name contains `truth`.
- **No private data.** `/api/datasets` reads the public roots only, the static
  handler refuses any path inside the private data root, and the server refuses
  to start rooted inside it.
- **No agent of their choosing.** One agent bundle is registered at boot from a
  path inside the image. A request cannot name an agent, an executable or a
  command: the objective travels as a JSON string value in an HTTP body, and
  there is no subprocess, no argv and no shell anywhere on the discovery path.
- **No unbounded work.** Concurrency, queue depth, prompt length, body size,
  events per run and the caps above.

### Environment

| Category | Variable | Set by the image | Notes |
|---|---|---|---|
| App | `BIOSENSE_RUNTIME_MODE` | `local` | the container's own loopback |
| | `BIOSENSE_ALLOWED_RUNTIMES` | `synthetic,local` | what the picker may offer |
| | `BIOSENSE_OMNIGENT_WORKSPACE` | `/app` | the runner's working directory |
| | `RUNS_DIR` | `/app/data/runs` | must be inside the workspace |
| | `BIOSENSE_PRIVATE_DATA` | `/app/data/private` | accounts' projects: must be on the volume |
| | `BIOSENSE_HOSTED` | `1` | badge reads ONLINE, remedies are a visitor's |
| | `BIOSENSE_PUBLIC_DEMO` | `1` | turns the caps on |
| | `PORT` | `8000` | the platform overrides it |
| Omnigent | `BIOSENSE_OMNIGENT_PORT` | `6767` | loopback only |
| | `OMNIGENT_LOCAL_SINGLE_USER` | `1` | loopback, no login |
| Model | `ANTHROPIC_API_KEY` | **no — you set it** | a platform secret, never in the image |
| Remote | `BIOSENSE_OMNIGENT_TOKEN` / `_TOKEN_FILE` | no | only for `remote` mode |
| Accounts | `BIOSENSE_AUTH_SERVER` | **no — you set it** | the one issuer sign-in is accepted from |
| | `BIOSENSE_ADMIN_USER_IDS` | **no — you set it** | operator ids on that server |

`tests/test_cloud_runtime.py` asserts that no image or script bakes in a
credential.

---

## B. The synthetic-only service

Unchanged, and still the right choice when the claim you want to make is that
the service holds nothing:

```bash
railway init
railway up                      # deploy/railway.json -> deploy/Dockerfile
railway volume add --mount-path /data
```

The image installs no extras, holds no Omnigent and no model credentials, and
the runtime picker shows one option with the reason beside the others. `RUNS_DIR`
defaults to `/data/runs`; without a volume every redeploy discards the artifacts.

What it still is: a public URL that runs CPU work on demand. Put it behind
whatever rate limiting your platform offers, and treat `MAX_CONCURRENT` as the
real cost control.

---

## Which runtimes a deployment offers

BioSense reads its runtime from the environment at startup and offers the
browser only what the deployment can actually serve. A button that cannot work is
worse than one that is not there.

| Variable | What it does |
|---|---|
| `BIOSENSE_RUNTIME_MODE` | `synthetic` (default), `local`, or `remote` |
| `BIOSENSE_ALLOWED_RUNTIMES` | comma-separated, what the picker may offer |
| `BIOSENSE_OMNIGENT_SERVER` | the Omnigent server URL; required for `remote` |
| `BIOSENSE_OMNIGENT_TOKEN` / `_TOKEN_FILE` | bearer credential, read once at startup |
| `BIOSENSE_OMNIGENT_AGENT` | registered agent name (default `biosense_discovery_loop`) |
| `BIOSENSE_OMNIGENT_WORKSPACE` | the runner's working directory |
| `BIOSENSE_OMNIGENT_ALLOW_INSECURE` | permit `http://` to a non-loopback host |
| `BIOSENSE_HOSTED` | this service runs the runtime itself |

Three things to get right for `remote`:

- **The token never reaches the browser.** It is read at startup, sent as an
  `Authorization: Bearer` header, and excluded from `/api/runtime`, `/readyz`,
  every event and every error message. Put it in a platform secret.
- **https, or say so.** A plaintext server that is not loopback is refused unless
  `BIOSENSE_OMNIGENT_ALLOW_INSECURE=1` declares the hop already trusted.
- **Sign-in is Omnigent's.** BioSense forwards a password to the configured
  server once and keeps only the session token, server-side. It stores no
  password and has no user database of its own.

One honest limitation of `remote`: the agents write their artifacts on the
runner's filesystem, which is not BioSense's. Progress, the stage timeline and
the session reference all arrive; structured artifacts only do where they reach
BioSense. That is the reason option A co-locates the processes.

---

## Verified, and not

Verified in this repository:

- `deploy/Dockerfile` **builds**, and the image **runs** under a non-root user
  (2026-10-04). A full prompt-driven run inside the container: 214 events,
  terminal `protocol_succeeded`, both arms meeting the target, reasoning report
  served. Artifacts survive `docker restart` on a mounted volume. Path
  traversal, `/standin_truth.synthetic.json` and an unknown run id all return
  404 from inside the container.
- The **boot sequence in `deploy/start-ai.sh` was exercised end to end** from a
  checkout (2026-10-05): Omnigent server up and answering `/health`, the agent
  registered from source, the container registered as a host, BioSense's own
  probe returning `ok` with `executor: host`, `/readyz` answering 200 with all
  four parts true, the badge reading `REAL AI — ONLINE`, the caps in force (a
  second run inside the cooldown answered 429), and a real session started,
  reaching the agents and failing honestly on a deliberately invalid model key
  with `model_auth_missing` rather than any fabricated result.
- Run recovery: a finished run read back from its own record after the process
  was replaced, with its protocol still exportable.
- `tests/test_app_loop.py`, `tests/test_discovery.py` and
  `tests/test_cloud_runtime.py` cover the refusals, the caps, the readiness
  mapping and the recovery against a live server on a loopback port.

**Not verified here:** `deploy/Dockerfile.ai` has **not been built** — the
environment this was written in has no Docker daemon — and no Railway deploy has
been run from this repository. The boot script it runs was exercised directly, as
above, and the image's own invariants are asserted in the tests, but the build
itself is untested. Build it once locally before pointing a URL at it:

```bash
docker build -f deploy/Dockerfile.ai -t biosense-ai .
docker run --rm -p 8000:8000 -e ANTHROPIC_API_KEY=sk-... \
  -v "$PWD/runs:/app/runs" biosense-ai
curl -s localhost:8000/readyz | python3 -m json.tool
```

---

## Dashboard only

If you want the read-only view with no ability to start work at all, run the
other server against the same volume:

```bash
uv run --frozen python -m biosense.production.serve \
  --runs /app/runs --static webapp --host 0.0.0.0 --port "$PORT"
```

It serves `webapp/index.html` at `/` and cannot approve a protocol, commit a
decision or start anything.

---

## Why Railway, and where Databricks fits

Railway, for this piece, because the thing being hosted is a small HTTP service
with one volume, which is exactly Railway's unit of deployment, and because
anyone can reproduce it: `docker build` and `docker run` locally are the same
path.

Databricks remains the better host for a **lab's own** loop runtime, and for the
same reasons in reverse: it is where governed data, credentials and scheduled
jobs already live, and Omnigent has a documented quickstart there
(<https://docs.databricks.com/aws/en/omnigent/quickstart>). If your evidence and
your people are already in a Databricks workspace, run the agents there, point
BioSense at it with `BIOSENSE_RUNTIME_MODE=remote`, and read the artifact
limitation above before you rely on it.

Four things to settle before running the agents anywhere with real data:

- **Credentials.** `ANTHROPIC_API_KEY` in the environment is read directly, so no
  interactive `omnigent setup` is needed. The `claude-sdk` harness also shells
  out to the Claude CLI where it is configured that way.
- **Authentication.** A runtime that can run commands under your API key must not
  be reachable. In option A it is bound to loopback inside the container and
  nothing routes to it; anywhere else, put it behind authentication.
- **The sandbox.** Every agent config uses `os_env.sandbox.type: auto`, which on
  Linux expects bubblewrap. In a container it may be unavailable or need
  privileges it should not have. Check what the sandbox resolves to before
  trusting the isolation, and do not relax `write_paths` to make a deploy
  succeed.
- **Session lifetime.** A real loop spans days: a protocol goes to a person for
  approval, a bioreactor runs, results come back. Redeploys kill processes, so
  the durable record must be the files on the volume — which it is, and which is
  now also true of a run's own event journal.
