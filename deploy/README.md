# Hosting BioSense-AI

Three different things can be hosted, and keeping them apart is the whole design.

| | What it is | Holds model credentials | Can start work | Safe behind a public URL |
|---|---|---|---|---|
| **Console** | `biosense.production.app` + `webapp/console.html` — type a question, watch a loop, read its reasoning report | no | yes, **synthetic stand-in only** | yes, with the caveats below |
| **Dashboard** | `biosense.production.serve` + `webapp/index.html` — a strictly read-only view of what a loop has written | no | no | yes |
| **Loop runtime** | `omnigent run discovery_loop` — the orchestrator and its specialists | **yes** | yes, for real | **only behind authentication** |

The console starts work, so it has a different threat model from the dashboard.
That is why it is a separate server rather than a few extra routes on
`serve.py`: the read-only guarantees of the dashboard stay intact and provable.

## Why hosting the console is reasonable

Not by policy, but because the code refuses:

- **It cannot reach a bioreactor.** `engine.preflight` refuses any request whose
  `bioreactor_source` is not `synthetic_standin`, and `prompt.parse` sets that
  field itself, so there is no text a visitor can type that reaches a wet-lab
  path. A wet-lab run always needs a named human approver regardless of mode,
  and the engine refuses to self-approve when the gates require one.
- **It cannot pretend a person approved anything.** The approver string every
  app run records says in words that no human approved it and that no wet-lab
  run is authorised.
- **It cannot read the stand-in's hidden answers.** No handler serves a file
  whose name contains `truth`, and the report generator will not read one
  either. The file ships inside the image and is still unreachable over HTTP.
- **It cannot spend money.** Nothing on this path calls a model. There are no
  model credentials in the image.
- **It cannot be made to run forever.** Concurrency, queue depth, prompt length,
  events per run and the time a finished run's events are kept are all capped in
  `biosense/production/app.py`.

What it still is: a public URL that runs CPU work on demand. Put it behind
whatever rate limiting your platform offers, and treat `MAX_CONCURRENT` as the
real cost control.

## Verified, and not

Verified on 2026-10-04, in this repository:

- `deploy/Dockerfile` **builds**, and the image **runs** under a non-root user.
  (The previous note here said the Dockerfile had never been built. It had not,
  and building it found two real faults: `agent_tools.py` and
  `output.schema.json` live at the repository root and are reached from inside
  the package, so the image started and then failed on the first request. Both
  are now copied.)
- A full prompt-driven run inside the container: 214 events, terminal
  `protocol_succeeded`, both arms meeting the target, reasoning report served.
- Artifacts survive `docker restart` on a mounted volume, and the report for a
  loop written before the restart is still served afterwards.
- Path traversal, `/standin_truth.synthetic.json` and an unknown run id all
  return 404 from inside the container.
- `tests/test_app_loop.py` covers the same refusals against a live server on a
  loopback port, so they are regression-tested rather than checked once.

Not verified here: no Railway deploy has been run from this repository. The
commands below follow Railway's documented flow and the image they build is the
one that was tested, but the deploy itself is untested.

## Which runtimes a deployment offers

BioSense reads its runtime from the environment at startup, and offers the
browser only what the deployment can actually serve. A button that cannot work
is worse than one that is not there.

| Variable | What it does |
|---|---|
| `BIOSENSE_RUNTIME_MODE` | `synthetic` (default), `local`, or `remote` |
| `BIOSENSE_ALLOWED_RUNTIMES` | comma-separated, what the picker may offer |
| `BIOSENSE_OMNIGENT_SERVER` | the Omnigent server URL; required for `remote` |
| `BIOSENSE_OMNIGENT_TOKEN` / `_TOKEN_FILE` | bearer credential, read once at startup |
| `BIOSENSE_OMNIGENT_AGENT` | registered agent name (default `biosense_discovery_loop`) |
| `BIOSENSE_OMNIGENT_WORKSPACE` | the runner's working directory |
| `BIOSENSE_OMNIGENT_ALLOW_INSECURE` | permit `http://` to a non-loopback host |

**The default is synthetic-only, and that is what makes the hosted console
defensible.** The image installs no extras, holds no Omnigent and no model
credentials, and the runtime picker shows one option with the reason beside the
others. Turning on real AI is a configuration change rather than different code:
set the variables above, add the `omnigent` extra to the image, and point it at a
server that has a registered host.

Three things to get right before you do:

- **The token never reaches the browser.** It is read at startup, sent as an
  `Authorization: Bearer` header, and excluded from `/api/runtime`, every event
  and every error message. Put it in a platform secret, not in the image.
- **https, or say so.** A plaintext server that is not loopback is refused unless
  `BIOSENSE_OMNIGENT_ALLOW_INSECURE=1` declares the hop already trusted.
- **Sign-in is Omnigent's.** BioSense forwards a password to the configured
  server once and keeps only the session token, server-side. It stores no
  password and has no user database of its own. Without a configured server
  there is one local workspace, and the interface says that it is private
  because the machine is, not because anything checked.

## Console on Railway

```bash
railway init
railway up                      # uses deploy/railway.json -> deploy/Dockerfile
railway volume add --mount-path /data
```

Railway sets `$PORT` and the image binds `0.0.0.0`. Two things matter:

1. **The volume.** Railway's container filesystem is ephemeral. Without a volume
   at `/data`, every redeploy discards the loop artifacts and the reports
   written beside them. `RUNS_DIR` defaults to `/data/runs`.
2. **What the console writes is what the dashboard reads.** They share the same
   `runs/` layout, so pointing both at one volume gives you a console that
   starts runs and a dashboard that only ever reads them.

To bring in loops produced elsewhere — a real session on a lab machine, say —
sync the directory in:

```bash
rsync -a --delete runs/loop-20261004-0930/ <host>:/data/runs/loop-20261004-0930/
```

A loop directory is append-mostly JSON, so a plain sync is enough.

### Why Railway and not Databricks

Both were on the table. Railway, for this piece, because:

- The thing being hosted is a small, stateless HTTP service with one volume.
  Railway's unit of deployment is exactly that; the image here is 
  self-contained and needs no workspace, no cluster and no catalog.
- It needs no credentials at all, which is the property that makes a public URL
  defensible. A managed Databricks workspace is the wrong shape for a service
  whose main security claim is that it holds nothing.
- Anyone can reproduce it: `docker build` and `docker run` locally are the same
  path, and both were exercised here.

Databricks is the better host for the **loop runtime** rather than the console,
and for the same reasons in reverse: it is where governed data, credentials and
scheduled jobs already live, and Omnigent has a documented quickstart there
(<https://docs.databricks.com/aws/en/omnigent/quickstart>). If your evidence and
your people are already in a Databricks workspace, run the agents there and host
the console separately — the loop's own artifacts are the integration surface
between them.

## Dashboard only

If you want the read-only view with no ability to start work at all, run the
other server against the same volume:

```bash
uv run --frozen python -m biosense.production.serve \
  --runs /data/runs --static webapp --host 0.0.0.0 --port "$PORT"
```

It serves `webapp/index.html` at `/`. The console image serves
`webapp/console.html` at `/` and keeps the tracker at `/index.html`.

## Loop runtime in a container

`omnigent server` is built for this: it runs in the foreground, takes
`--host 0.0.0.0 --port $PORT`, pre-registers a bundle with
`--agent discovery_loop`, serves its own bundled web UI (no Node build — the
wheel ships it), and takes `--no-open` for headless boots. State goes in
`--database-uri` and artifacts in `--artifact-location`, both of which belong on
a volume.

Omnigent splits state from execution: the server holds conversations, a
registered **host** runs the harnesses. One container can be both:

```bash
omnigent server --host 0.0.0.0 --port "$PORT" --agent discovery_loop \
  --no-open --database-uri "sqlite:////data/chat.db" \
  --artifact-location /data/artifacts --admin-password "$ADMIN_PASSWORD" &
omnigent host --server "http://127.0.0.1:$PORT" --non-interactive
```

Four things to settle before this is worth deploying:

- **Credentials.** `ANTHROPIC_API_KEY` (or `OMNIGENT_ANTHROPIC_API_KEY`) in the
  environment is read directly, so no interactive `omnigent setup` is needed.
  The `claude-sdk` harness also shells out to the Claude CLI, so the image needs
  it: `curl -fsSL https://claude.ai/install.sh | bash`.
- **Authentication.** A public URL that can run shell commands under your API
  key must not be open. Set `--admin-password` on first boot and keep the
  service private until you have confirmed the login works.
- **The sandbox.** Every agent config uses `os_env.sandbox.type: auto`, which on
  Linux expects bubblewrap. In a container it may be unavailable or need
  privileges it should not have. Check what the sandbox actually resolves to
  before trusting the isolation, and do not relax `write_paths` to make a deploy
  succeed.
- **Session lifetime.** A real loop spans days: a protocol goes to a person for
  approval, a bioreactor runs, results come back. Railway redeploys kill
  processes, so the durable record must be the files on the volume — which it is.
  Do not rely on a live session surviving.

For a lab, the honest split is: run the loop where the data and the people are,
host the console and the dashboard. The loop's own artifacts are the integration
surface.
