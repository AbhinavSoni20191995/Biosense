# Real AI on your own VM

Railway runs BioSense in a container that may not create Linux user namespaces,
so the agents' sandbox (bubblewrap) cannot start there and real runs are refused
with `agent_sandbox_unavailable`. A small virtual machine has no such limit. This
folder puts the same image on one, behind HTTPS, with automatic redeploys on
every push to `main` — the parts of Railway you were using, without the one that
blocked the agents.

What you get is the design as intended: every agent command runs in its sandbox,
specialists have no network, the agents can write only to the run directory, and
the API key never reaches the agents' tools.

## 1. Create the VM (about 5 minutes)

Any provider works. Pick one:

| Provider | Plan | Price (approx.) |
|---|---|---|
| **Hetzner Cloud** (recommended) | CX22 — 2 vCPU, 4 GB RAM | €4–5 / month |
| DigitalOcean | Basic Droplet — 2 vCPU, 4 GB RAM | $24 / month |
| AWS Lightsail | 4 GB instance | $24 / month |

- **Image:** Ubuntu 24.04 (or Debian 12).
- **Memory:** 4 GB. The app, the Omnigent server and several agent processes run
  at once; 2 GB is too tight.
- **Login:** add your SSH key when the provider asks (safer than a password).
- **Firewall:** if the provider has a cloud firewall, allow inbound **22, 80 and
  443**.

Note the VM's public IP address.

## 2. Run the setup (about 10 minutes, mostly waiting)

Connect to it (`ssh root@YOUR_IP`, or `ssh ubuntu@YOUR_IP` on some providers) and
run:

```bash
curl -fsSL https://raw.githubusercontent.com/AbhinavSoni20191995/Biosense/main/deploy/vm/setup.sh -o setup.sh
sudo bash setup.sh
```

It asks four questions:

1. **Address for the site.** Press Enter to accept the suggested
   `YOUR-IP-WITH-DASHES.sslip.io`. It works immediately with HTTPS and needs no
   DNS. (A domain of your own also works: point its A record at the IP first.)
2. **ANTHROPIC_API_KEY.** Paste your key; nothing is shown as you type. It is
   stored in `/opt/biosense/deploy/vm/.env`, readable by root only.
3. **BIOSENSE_AUTH_SERVER** and 4. **BIOSENSE_ADMIN_USER_IDS.** Copy both from
   your Railway service's *Variables* tab to keep the same sign-in and the same
   admin account. Press Enter to skip if you don't use sign-in.

It then installs Docker, allows the sandbox's user namespaces, builds and starts
BioSense, installs auto-redeploy, waits for the site, and prints the `[sandbox]`
and `[selfcheck]` lines from the boot log. You want to see:

```
[sandbox] agents sandbox OK: ...
[selfcheck] BioSense self-check — PASS
```

## 3. Check it works

1. Open `https://YOUR-ADDRESS`. On the Discovery page, **REAL AI — ONLINE**
   should be selectable, with no warning under it.
2. Sign in as your admin account, open **Runs → System check**, press **Run live
   check**. Every row should be ✓; *Agents talk to each other* proves a real
   session in which the orchestrator asks a specialist to run a tool inside its
   sandbox. (It spends a small amount of model credit.)
3. Run a real prompt.

## Day to day

- **Deploying:** push to `main`. Within five minutes the VM pulls it, rebuilds and
  restarts. A redeploy ends any real run in progress, as on Railway.
- **Deploy now:** `sudo biosense-update --force`
- **Logs:** `sudo docker compose -f /opt/biosense/deploy/vm/docker-compose.yml logs -f biosense`
- **Change a setting** (e.g. a new key): edit `/opt/biosense/deploy/vm/.env`, then
  `sudo biosense-update --force`.
- **Data:** projects and runs live in the Docker volume `biosense_biosense-data`
  and survive rebuilds. Back it up with
  `sudo docker run --rm -v biosense_biosense-data:/d -v "$PWD":/b alpine tar czf /b/biosense-data.tgz -C /d .`
- **Security updates:** Ubuntu installs them automatically
  (`unattended-upgrades`); reboot occasionally (`sudo reboot`; BioSense restarts
  by itself).

## What to do with Railway

Railway keeps deploying from `main`, but its Real AI stays unavailable. Either:

- **keep it as the public demo** — set its variable
  `BIOSENSE_ALLOWED_RUNTIMES=synthetic` so it only offers the synthetic path, or
- **remove the service** once the VM works, so nobody lands on the wrong one.

Projects and runs made on Railway stay there; they are not copied to the VM.

## If something is wrong

| What you see | What it means | Fix |
|---|---|---|
| The site does not load at all | ports 80/443 are closed, or the address does not point at the VM | open 80/443 in the provider's firewall; for your own domain, check its A record |
| `[sandbox] ... UNAVAILABLE (user_namespaces)` | the kernel still blocks user namespaces | `cat /etc/sysctl.d/60-biosense-agent-sandbox.conf`, then `sudo sysctl --system` and `sudo biosense-update --force`; send the `[sandbox]` line if it persists |
| Real AI says the model has no credentials | the key is missing or mistyped | fix `ANTHROPIC_API_KEY` in `.env`, then `sudo biosense-update --force` |
| Sign-in fails | the accounts server address differs from Railway's | copy `BIOSENSE_AUTH_SERVER` exactly from Railway |

## Why the compose file lifts two Docker defaults

Docker's default seccomp profile refuses to create a user namespace, and its
default AppArmor profile refuses the mounts bubblewrap makes inside one — that is
the same refusal Railway gives. `docker-compose.yml` lifts exactly those two for
the BioSense container. The agents themselves are still confined by bubblewrap
and Omnigent's own seccomp filter; what changes is how much of the kernel the one
container may ask for, which is why it belongs on a VM that runs nothing else.
Where a fresh `/proc` mount is refused inside the container, BioSense detects it
at boot and turns on Omnigent's own `/proc`-bind switch automatically.
