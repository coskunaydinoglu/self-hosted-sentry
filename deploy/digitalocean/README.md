# Deploy to a DigitalOcean droplet

One command sets up a fresh Ubuntu droplet with this repository: self-hosted
Sentry, the Symbol Portal (inside Sentry's navigation), the symbol server and the
ingest canary, behind Caddy with automatic HTTPS.

## Droplet

| | Demo | Notes |
|---|---|---|
| Image | Ubuntu 24.04 LTS x64 | 22.04 works too |
| Size | Basic, 8 vCPU / 16 GB / 320 GB (`s-8vcpu-16gb`) | `--errors-only` fits 4 vCPU / 8 GB, without crash-free rates |
| Auth | your SSH key | e.g. `~/.ssh/id_ed25519.pub` |
| Region | closest to the team, e.g. `fra1` or `ams3` | |

No DNS is required: without `--domain` the script uses
`sentry-<ip-with-dashes>.sslip.io`, which resolves to the droplet, and Caddy gets
a Let's Encrypt certificate for it. Pass `--domain` once you have a real name
pointing at the droplet.

## Create the droplet with doctl

```bash
doctl auth init                                   # paste the API token in your own terminal
deploy/digitalocean/create-droplet.sh             # cheapest size meeting 2 vCPU / 8 GB, fra1
deploy/digitalocean/create-droplet.sh --full      # 4 vCPU / 16 GB for feature-complete
```

It imports `~/.ssh/id_ed25519.pub` if DigitalOcean does not know it yet, shows
the chosen size and monthly price, asks for confirmation, creates an Ubuntu
24.04 droplet tagged `sentry-demo`, waits for SSH and prints the IP.

## Run

```bash
deploy/digitalocean/deploy.sh 203.0.113.10 --email you@example.com
```

What it does, idempotently:

1. Checks SSH, CPU/RAM/disk/arch.
2. Installs Docker and Caddy, adds 4 GB swap, opens 22/80/443 in ufw.
3. Rsyncs the repository to `/opt/sentry-self-hosted`, excluding local secrets
   (`.env`, generated configs, `sentry.properties`, build outputs). The remote
   `.env` starts from the committed defaults.
4. Binds Sentry to `127.0.0.1:9000`, sets `system.url-prefix`, the proxy/CSRF
   settings and the Caddyfile.
5. Runs `install.sh` non-interactively, starts the stack, creates the admin user
   with a generated password stored in `/opt/sentry-self-hosted/.admin-password`.
6. Protects the Symbol Portal with basic auth; the generated `user:password` is in
   `/opt/sentry-self-hosted/.portal-basic-auth`. The portal carries a
   write-capable token, so do not remove this on an internet-facing host.

Afterwards create a Personal Token in Sentry, set `SYMBOL_PORTAL_SENTRY_TOKEN` in
the droplet's `.env`, `docker compose up -d --force-recreate symbol-portal`, and run
`scripts/bootstrap-mobile-projects.py` against the HTTPS URL.

Re-running the script later deploys new commits and upgrades Sentry through
`install.sh`, keeping data and generated secrets.
