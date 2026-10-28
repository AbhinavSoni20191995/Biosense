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

### Deleting, the trash, and freeing storage

Deleting a run or a project moves it to a trash folder — `<runs>/.trash/` for
runs, `private_data/workspaces/u-<hash>/.trash/projects/` for projects — so a
mistaken click loses nothing. The **Trash** card on the Runs page lists what
*you* deleted, with what each item takes on disk, and offers two things:

| | What it does | Undo? |
|---|---|---|
| **Restore** | puts the run or project back where it was; refused, never overwritten, if one of the same name exists | — |
| **Delete forever** | removes its files from the server's storage (on a hosted deployment, the volume), and reports the space freed | **no** |

**Empty trash** does the second for everything of yours at once. Both ask
first, and the server refuses either without `{"confirm": true}` in the
request (`POST /api/trash/<run|project>/<id>/purge`, `POST /api/trash/empty`),
so nothing is removed by a stray click or a replayed request. The rules are
the deleting rules: only an item's owner may restore or remove it, an operator
alone for a run nobody owns, and a trash id is a name, never a path — it cannot
reach outside the trash or follow a link out of it. Deleting forever is the
only place BioSense unlinks a run, and it acts only on something already in
the trash.

## The three roles

| Role | Who | Real AI | Demo caps |
|---|---|---|---|
| `public` | not signed in | where the deployment offers it | yes |
| `user` | signed in | where the deployment offers it, from the account's daily allowance of runs | yes |
| `admin` | signed in, and listed by this deployment | yes | **no** |

A browser that has not signed in still gets a workspace of its own on a hosted
deployment: an opaque cookie, whose hash is the workspace id. It is a privacy
partition between strangers, not an authentication boundary, and the interface
says "not signed in" because nothing checked who they are.

## How somebody signs in

BioSense keeps its own accounts (`biosense/accounts.py`). A person opens the
page, presses **Sign in / create account**, and makes one with an email address
and a password of at least eight characters. They are signed in at once. The
browser gets an opaque `HttpOnly` session cookie and nothing else; the session
lives in memory on the server.

What is stored, in one JSON file under the private data root
(`private_data/accounts/accounts.json`, mode 0600, never served, never
committed): the email address, a per-account salt and an **scrypt hash** of the
password, when the account was made and last signed in, how many real AI runs
it has started, and any per-account run limit an operator set. A password is
never written anywhere, and a wrong password reads the same as an unknown
account.

### What counts as a run

Every real AI run an account starts: a discovery run, a follow-up round, a
continued run, and **"Compare with the field"** — the short run that adds the
state-of-the-art comparison to an earlier run that has none. The last one
sends one literature task and writes one file, but the literature agent is the
cost, so it is counted like any other. The synthetic demonstration path is
never counted.

### Runs per account, per day

Each account may start **five real AI runs a day** — a rolling 24 hours, like
the demo caps — and `BIOSENSE_RUNS_PER_ACCOUNT` changes that default for
everybody. A run is counted when it starts — after every other check that could
still refuse it, so a refused start costs nothing — and deleting a run later
gives nothing back. The synthetic demonstration path is not counted and has no
limit. The page shows "*n* of 5 real AI runs left today" from the server's own
count, and the sixth start in a day is refused by name, with when the next one
opens, rather than failing. The per-address demo cap and cooldown do not apply
to a signed-in account (it is counted by name instead); the deployment-wide
daily cap still does.

### Asking for more, and granting it

When the day's runs are gone, the account area offers **Ask for more runs**: a
number and a sentence saying what for. The request is kept in the accounts file,
one open request per account, and shown to every signed-in operator as a count
on their ADMIN badge and in a card at the top of the page listing who asked, for
how many, why, and what they have used today. The operator grants (the number
asked for, or another) or declines, from their own account on the page or at
the CLI, and the decision is recorded with their name.

Granted runs go into the account's **bank of extra runs**, which is spent only
once the day's allowance is — so a grant is never lost to the clock, and the
day's allowance still returns on its own. An operator can also give extra runs
or set a different daily limit for one account directly, without a request.
A daily limit of `0` means no limit for that account.

On a hosted deployment a real AI run **needs an account**: a browser that has not
signed in is told to sign in or create one, before the runtime is even probed.
On a laptop (no hosted posture) nothing changes: one local workspace, no sign-in
needed.

### What the person running the deployment can set

| | Default | |
|---|---|---|
| `BIOSENSE_RUNS_PER_ACCOUNT` | `5` | real AI runs per account per day; `0` means no limit of that kind |
| `BIOSENSE_SIGNUP` | `open` | `closed` refuses new accounts; existing ones still sign in |
| `BIOSENSE_SIGNUP_CODE` | unset | when set, sign-up asks for this code once, so a public address is open to your lab only. Never shown to the page. |
| `BIOSENSE_ACCOUNTS` | `on` | `off` turns local accounts off (see below) |

And from the command line, inside the deployment:

```bash
python -m biosense.accounts list                                   # who, runs today, daily limit, extra runs
python -m biosense.accounts requests                               # open requests for extra runs
python -m biosense.accounts approve <request_id> [--runs N]        # grant one (and decline)
python -m biosense.accounts grant alice@lab.example 20             # extra runs, no request needed
python -m biosense.accounts limit alice@lab.example 10             # a different daily limit (0 = none)
python -m biosense.accounts reset-password alice@lab.example '…'   # there is no reset e-mail
python -m biosense.accounts disable alice@lab.example              # and enable
python -m biosense.accounts create alice@lab.example '…'           # bypasses a closed sign-up
```

### Sign-in through an Omnigent accounts server (only with local accounts off)

The earlier design, kept for a deployment that already runs an Omnigent server
in accounts mode and would rather not keep accounts here: set
`BIOSENSE_ACCOUNTS=off` and `BIOSENSE_AUTH_SERVER=<that server>`. Then
`POST /api/auth/login` forwards the credentials once, holds the JWT it gets back
server-side, and the id comes from the server, never from the form. With local
accounts on (the default) `BIOSENSE_AUTH_SERVER` is ignored and the page never
asks for a server address: the Omnigent runtime inside a hosted service is a
loopback single user that nobody signs in to.

## Designating an administrator

One environment variable, set where the deployment's secrets are set — never in
source, and never anything a request can carry:

```
BIOSENSE_ADMIN_USER_IDS=alice@lab.example,bob@lab.example
```

Ids are matched as the account system stores them (trimmed, lower-cased) and
only for identities **this instance authenticated** — the issuer written into
an identity by `biosense.accounts` is the instance itself. Create the account
with a listed email and it is an operator; an identity any other server vouched
for is never one, however it is named. (With local accounts off, the issuer is
`BIOSENSE_AUTH_SERVER`, and naming administrators without naming it refuses to
start: an id alone is not an identity.)

`authz.Policy` is the single place any of this is decided. Everything privileged
asks it; nothing re-implements the check.

### What an administrator may skip

Demo policy — the caps that exist to bound what strangers can spend on BioSense's
own provider key:

* the per-account daily allowance of real AI runs
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
the same 404 a stranger gets — unless the deployment turns on the one
administrative view that exists, below, which is built deliberately and visibly
rather than inherited from a spending privilege.

### Oversight: an operator may view and download every account's runs

```
BIOSENSE_ADMIN_OVERSIGHT=on
```

With this set, an operator (and nobody else) may open any account's run page,
follow its event stream, and download its protocol, its round page and its
specialist sections. The Runs page gains an "every account's runs" box, and
each run then shows the account that started it. The policy description
reports `admin_oversight: true` in public, so the people it reaches can see it
is on.

What it does not do: it grants **no write**. Deleting, stopping, extending,
pausing, continuing or following up another account's run is refused exactly
as before; those routes scope to the caller and never consult this switch.
Projects and private datasets are untouched by it. It is off by default and
switches off again by removing the variable, with nothing else to change.

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
* **Sessions are in memory.** A restart signs everybody out; the accounts
  themselves are on disk and survive it.
* **No e-mail is sent.** Addresses are not verified and there is no reset link;
  an operator resets a password from the command line.
* **The accounts file is one machine's.** An account made on one deployment does
  not exist on another.
* **BioSense admin is not Omnigent admin.** The two are different roles with
  different configuration, and neither grants the other. Omnigent's own
  `is_admin` is kept for display as `provider_is_admin` and authorises nothing
  here.
