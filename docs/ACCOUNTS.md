# Accounts, roles and whose work is whose

BioSense keeps accounts for one reason: a scientist's projects, datasets, runs and
reports belong to them, and a shared URL has to be able to say so. This document
is what the account system is, what it is not, and what is deliberately left for
later.

## What an account owns

| | Owned by an account | Where it lives |
|---|---|---|
| Projects you create | yes | `private_data/workspaces/u-<hash>/projects/` |
| Discovery runs you start | yes | the run records its `owner` |
| Reports and protocol exports from your runs | yes | read through the run |
| Benchmarks you build from your runs | yes | built through the run |
| Private datasets | **machine-level, not per-account** — see the limitations |
| The committed project templates | nobody; they are read-only starting points |

A run is reachable only by the account that started it. A run id is sixteen hex
characters and it is **not a capability**: guessing one gets a 404, from the
run endpoint, the event stream, the protocol export, the benchmark builder and
the cancel route alike. The run list holds your own runs and nobody else's.

## The three roles

| Role | Who | Real AI | Demo caps |
|---|---|---|---|
| `public` | not signed in | where the deployment offers it | yes |
| `user` | signed in | where the deployment offers it | yes |
| `admin` | signed in, and listed by this deployment | yes | **no** |

A browser that has not signed in still gets a workspace of its own on a hosted
deployment: an opaque cookie, whose hash is the workspace id. It is a privacy
partition between strangers, not an authentication boundary, and the interface
says "not signed in" because nothing checked who they are.

## How somebody signs in

BioSense has **no password database**. `POST /api/auth/login` forwards the
credentials once to an Omnigent accounts server, which does the authenticating
(argon2 hashes, signed session JWTs, first-user-is-admin, invite-only
registration). The JWT it returns is held server-side; the browser gets an opaque
`HttpOnly` session cookie and nothing else.

Two rules make that safe to build a role on:

* **One issuer.** `BIOSENSE_AUTH_SERVER` names the one Omnigent accounts server
  this deployment trusts to say who somebody is. Without it, sign-in still works
  against a server the caller names — useful on a laptop — but **no such identity
  can ever be an administrator**, because anyone can run an Omnigent server and
  return any id they like.
* **The id comes from the server.** If a server authenticates somebody without
  saying who they are, sign-in is refused. BioSense never falls back to the
  username that was typed: that would let a caller choose their own account id,
  and on a deployment with administrators, choose to be one.

## Designating an administrator

Two environment variables, set where the deployment's secrets are set — never in
source, and never anything a request can carry:

```
BIOSENSE_AUTH_SERVER=https://your-omnigent-accounts-server
BIOSENSE_ADMIN_USER_IDS=alice@lab.example,bob@lab.example
```

Ids are matched as the account system stores them (trimmed, lower-cased) and are
compared **only** for identities issued by `BIOSENSE_AUTH_SERVER`. Naming
administrators without naming the issuer refuses to start, with that reason:
an id alone is not an identity.

`authz.Policy` is the single place any of this is decided. Everything privileged
asks it; nothing re-implements the check.

### What an administrator may skip

Demo policy — the caps that exist to bound what strangers can spend on BioSense's
own provider key:

* the per-caller daily Real AI run cap
* the deployment-wide daily Real AI run cap
* the cooldown between one caller's runs

### What an administrator may not skip

System safety — the limits that exist to keep the service and its data sound
whoever the caller is. None of these consults role anywhere in the code:

* one Real AI run at a time (`MAX_CONCURRENT_REAL`)
* the wall-clock run deadline, and cancellation
* prompt and request-body size limits
* the fixed agent bundle: a request cannot name an agent, an executable or a command
* the private-data boundary, and the refusal to reach a real bioreactor
* **other accounts' projects, runs, datasets and reports**

That last one is deliberate and worth stating twice. A quota exemption is not a
key to anybody's science. An administrator asking for another account's run gets
the same 404 a stranger gets. If an administrative view over other people's data
is ever wanted, it should be built deliberately and visibly — not inherited from
a spending privilege.

**Administrator runs are not free.** They skip the caps that protect the
deployment's budget; they still spend its model credits. They are counted
separately (`admin_real_runs_today` beside `public_real_runs_today`) so a
deployment can see how much of its own spend was its own development.

## Credential modes, and the one that is planned

Every real run records how its model call was paid for:

| Mode | State |
|---|---|
| `platform_demo` | implemented. BioSense's own provider key, under the demo caps. |
| `admin_platform` | implemented. BioSense's own provider key, by a named operator, uncapped by demo policy. |
| `user_byok` | **PLANNED — NOT CURRENTLY IMPLEMENTED.** |

### PLANNED — NOT CURRENTLY IMPLEMENTED: user-provided provider keys

A later version may let a signed-in user connect their own model-provider
credentials, so their usage is bounded by their own account rather than by this
deployment's demo caps. Nothing in this iteration implements it: there is no UI
field asking for a key, no storage for one, and no code path that reads one.

The intended design, recorded now so the shape is not invented under pressure
later:

* the key is **the user's**, never shared between accounts, never a deployment
  default;
* **encrypted at rest or held only for the lifetime of a run** — never written
  in plaintext beside artifacts, and never into a workspace directory;
* **never in a scientific artifact**: not in a run record, an analysis result, a
  hypothesis, a protocol, a benchmark or an export;
* **never in a log line, an event, an error message or an HTTP response**, which
  is the rule the Omnigent token already follows;
* **isolated per user and per run**, so one run can never spend another account's
  credit;
* usage with it is bounded by BioSense's operational safeguards all the same —
  the concurrency gate, the run deadline, the size limits and the data
  boundaries are not demo policy and do not become optional.

The preparation that exists today is deliberate and small: `credential_mode` is
an enum with `user_byok` reserved, it is recorded on every run, and the role and
the credential mode are separate questions answered by one module. Adding the
mode later should be a new branch and a new storage decision — not a migration
of every run already recorded.

## Limitations, stated plainly

* **Private datasets are machine-level, not per-account.** They are never listed
  or served over HTTP, and the web app cannot reach them at all; but on one
  machine they sit in one private root rather than in per-account directories.
  Per-account dataset ownership is not implemented.
* **An anonymous workspace is a cookie.** Clearing it loses the link to those
  runs. It partitions strangers; it does not authenticate them.
* **Sessions are in memory.** A restart signs everybody out, which is the right
  trade for a process that is not a credential store.
* **BioSense admin is not Omnigent admin.** The two are different roles with
  different configuration, and neither grants the other. Omnigent's own
  `is_admin` is kept for display as `provider_is_admin` and authorises nothing
  here.
