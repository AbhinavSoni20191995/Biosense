# Hosting BioSense-AI

Two different things get hosted, and keeping them apart is the whole design:

| | What it is | Needs model credentials | Safe behind a public URL |
|---|---|---|---|
| **Dashboard** | `biosense.production.serve` + `webapp/` — a read-only view of the JSON a loop has written | no | yes, with the caveats below |
| **Loop runtime** | `omnigent run discovery_loop` — the orchestrator and its specialists | yes | only behind authentication |

The dashboard cannot start work, approve a protocol, commit a decision or answer
a consult. Those stay CLI acts with a named person behind them, which is what
makes a public dashboard reasonable in the first place.

## Verified here, and not

Verified on 2026-10-04: `biosense.production.serve` serves `/api/loops`,
`/api/loops/<id>`, `/healthz` and the tracker page; it refuses path traversal,
refuses any file whose name contains `truth`, and skips malformed or oversized
files instead of failing. `tests/test_serve.py` covers all of that.

Not verified here: the Dockerfile has never been built — this machine has no
container runtime — and no Railway deploy has been run. Treat both as a starting
recipe, not a tested artifact.

## Dashboard on Railway

```bash
railway init
railway up                      # uses deploy/railway.json -> deploy/Dockerfile
railway volume add --mount-path /data
```

Railway sets `$PORT` and the image binds `0.0.0.0`. Two things matter:

1. **The volume.** Railway's container filesystem is ephemeral; without a volume
   mounted at `/data`, every redeploy discards the loop artifacts. `RUNS_DIR`
   defaults to `/data/runs`.
2. **Getting artifacts in.** The image contains no loops. Either run the loop in
   a second service that writes to the same volume, or push from wherever the
   loop runs:
   ```bash
   rsync -a --delete runs/loop-20261004-0930/ <host>:/data/runs/loop-20261004-0930/
   ```
   A loop directory is append-mostly JSON, so a plain sync is enough; the
   dashboard polls every 4 seconds and picks up new decisions as they land.

The published artifact version of the tracker cannot fetch a `localhost` API —
a browser blocks an HTTPS page calling plain HTTP. That is why the page is served
from the same origin as the API. Opened as a file or as a published artifact, the
fetch fails quietly and the recorded CAR-T replay stands.

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
host the dashboard. The loop's own artifacts are the integration surface.
