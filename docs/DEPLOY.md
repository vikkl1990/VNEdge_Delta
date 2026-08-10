# Deploying VNEDGE (multi-lane shadow) to a Linux VPS

Target per the project charter: Ubuntu VPS + Docker. Everything below is for
the live-data shadow workspace — no API keys, no live orders. Each lane runs
through sizing, the pre-trade risk gateway, journaling, reconciliation, and
dashboard telemetry, but shadow mode never submits to a broker.

## 1. VPS prep (once)

```bash
# Ubuntu 22.04+
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER   # re-login after this
```

Recommended: AWS Mumbai / any region; latency barely matters for 1h-bar paper
trading. 1 vCPU / 1–2 GB RAM is plenty.

## 2. Deploy code with git provenance

Deploy from a real git checkout on the VPS. Do not run production/shadow
services from an rsynced tree without `.git`; that removes commit attribution,
rollback, and a reliable `git rev-parse HEAD`.

First install:

```bash
git clone https://github.com/vikkl1990/vnedge.git ~/vnedge
cd ~/vnedge
git checkout main
git pull --ff-only
git rev-parse HEAD
```

Update an existing deploy — ALWAYS via the lock-serialized script (two
concurrent composes SIGKILLed the stack on 2026-07-07):

```bash
cd ~/vnedge && ./scripts/deploy.sh
```

Manual fallback (only if the script itself is broken):

```bash
cd ~/vnedge
git fetch --prune origin
git checkout main
git reset --hard origin/main
git clean -fd --exclude=.env --exclude=data --exclude=logs \
    --exclude=research/paper_trials --exclude=research/live_research \
    --exclude=deploy/certs
git rev-parse HEAD
```

Before restarting services, record the printed commit in the deploy log or
operator notes. Rollback is the inverse: `git fetch`, then
`git reset --hard <known-good-commit>`, followed by `docker compose up -d --build`.

## 3. Ship trial/runtime state

```bash
# trial state (REQUIRED to continue the current trial, not restart it):
# stop the local process FIRST so the account snapshot is final
kill <local-trial-pid>
rsync -av ~/Desktop/VNEDGE/logs/paper_trials/ vps:~/vnedge/logs/paper_trials/
rsync -av ~/Desktop/VNEDGE/research/paper_trials/ vps:~/vnedge/research/paper_trials/
```

The account store (`logs/paper_trials/<lane>.account.json`) is what makes
this a CONTINUATION for paper lanes and a stable audit trail for shadow
lanes. Never run the same lane id on two machines at once.

Live research files under `research/live_research/` are generated runtime
state, not source code. Preserve or archive them when they are evidence, but do
not commit them.

## 4. Configure and start

```bash
cd ~/vnedge
cat > .env <<EOF
DASHBOARD_TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")
TELEGRAM_BOT_TOKEN=            # optional: @BotFather token
TELEGRAM_CHAT_ID=              # optional: your chat id
# optional grid expansion; defaults shown
MULTI_LANE_EXCHANGES=binanceusdm,bybit
MULTI_LANE_SYMBOLS=BTC/USDT:USDT
EOF
docker compose up -d --build
docker compose logs -f multi-lane-shadow   # watch lane build/resume lines
```

`restart: unless-stopped` + per-lane account stores = reboots continue the
same governed workspace.

## 5. Dashboard access

Two paths. The app container port itself is mapped to the VPS loopback only;
the private SSH tunnel remains the most locked-down option:

```bash
ssh -N -L 8080:127.0.0.1:8080 vps
open "http://127.0.0.1:8080/?token=<DASHBOARD_TOKEN from vps .env>"
```

The `dashboard-tls` service additionally serves the dashboard publicly over
HTTPS on port 8765 (self-signed). Harden that edge per §7 — IP allowlist,
certificate upgrade, token rotation — before relying on it.

## 6. Daily checks (mostly automated now)

Telegram (if configured) pushes: fills, stale feed, kill switch, journal
failures, daily-loss stop, loss streaks, drawdown-envelope breaches.
Manual fallback:

```bash
docker compose ps
tail vnedge/logs/paper_trials/funding_mr_btc_v1_20260703.alerts.jsonl
cat  vnedge/logs/paper_trials/funding_mr_btc_v1_20260703.account.json
```

Emergency halt (entries stop, exits still allowed):

```bash
touch vnedge/logs/paper_trials/funding_mr_btc_v1_20260703.KILL
```

## 7. Edge hardening (the public TLS front)

`dashboard-tls` (Caddy, `deploy/Caddyfile`) exposes the read-only dashboard
over HTTPS on port 8765 so no SSH tunnel is needed. Three controls apply, in
order:

1. **IP allowlist** (`DASHBOARD_ALLOWLIST`) — who may connect at all.
2. **TLS** — self-signed by default; real certificate once you own a domain.
3. **Bearer token** (`DASHBOARD_TOKEN`) — mandatory app-level auth on every
   route; the dashboard has zero control endpoints regardless.

### IP allowlist

Add a comma-separated CIDR/IP list to `.env` and restart the front:

```bash
# home/office ranges; single IPs work with or without /32
echo 'DASHBOARD_ALLOWLIST=203.0.113.7/32,198.51.100.0/24' >> .env
docker compose up -d dashboard-tls
```

Requests from any other source IP get a plain 403 before ever reaching the
dashboard container. Notes:

- **TRADE-OFF, read this:** leaving `DASHBOARD_ALLOWLIST` empty keeps the
  historical behaviour — the dashboard is reachable from the ENTIRE internet
  and only the bearer token protects it. That may be a deliberate choice
  (mobile access from changing IPs), but it makes the token the single wall:
  treat it like a password and rotate it (below).
- If your ISP rotates your address, allow the ISP's CIDR block rather than a
  single IP, or fall back to the SSH tunnel in §5.
- IPv6 clients need their own entries; the empty default allows both
  families (`0.0.0.0/0 ::/0`).
- If `docker compose logs dashboard-tls` shows every request coming from a
  Docker bridge IP (172.16.0.0/12), the daemon is using its userland proxy
  and the allowlist cannot see real client IPs — set
  `"userland-proxy": false` in `/etc/docker/daemon.json` (then restart
  Docker) or use the tunnel instead.
- A malformed CIDR makes Caddy exit at startup (fail closed — nothing is
  served). Check `docker compose logs dashboard-tls` after any change.

### Certificate upgrade path (self-signed → real cert)

Self-signed TLS encrypts the token in transit but trains you to click
through browser warnings. Once you own a domain:

1. DNS A (and AAAA) record for the domain → VPS IP.
2. Uncomment the `"80:80"` / `"443:443"` port mappings on `dashboard-tls` in
   docker-compose.yml and open both ports in the cloud security list (80 is
   needed for the ACME HTTP-01 challenge).
3. `echo 'DASHBOARD_DOMAIN=dash.example.com' >> .env`
4. Uncomment the `{$DASHBOARD_DOMAIN}` site block at the bottom of
   `deploy/Caddyfile` — Let's Encrypt issuance and renewal are automatic
   (certificates persist in the `caddy_data` volume).
5. `docker compose up -d dashboard-tls`, then browse to
   `https://dash.example.com/?token=...` — no warning; the same IP allowlist
   applies on that block too. Optionally retire the self-signed `:8765`
   block and its port mapping afterwards.

### Token rotation

The token is minted in §4. To rotate: write a new value into `.env`
(`python3 -c "import secrets; print(secrets.token_urlsafe(24))"`), then
`docker compose up -d multi-lane-shadow` so the dashboard process picks it
up — the TLS front never sees the token, so it needs no restart. Old links
stop working immediately. Rotate on any suspicion of leakage, after
removing someone's allowlist entry, and whenever the dashboard ran
allowlist-open for an extended period.

### Rate limiting (documented, not enabled)

Stock `caddy:2-alpine` ships NO native request-rate limiter: `rate_limit`
comes from the mholt/caddy-ratelimit plugin, which requires a custom
xcaddy-built image. This repo deliberately does not swap the pinned stock
image for a custom build. The current brute-force posture is the IP
allowlist plus a 24-byte-entropy bearer token whose rejection is a constant
401 — no oracle to iterate against. If defence in depth is wanted later,
build an image `FROM caddy:2-builder` with
`xcaddy build --with github.com/mholt/caddy-ratelimit`, pin it by digest,
and add a `rate_limit` block to the site in `deploy/Caddyfile`.

## Notes

- The image has NOT been built on the dev Mac (no Docker there) — first
  `docker compose build` on the VPS is the validation step; all Python deps
  ship manylinux wheels for 3.12-slim.
- systemd alternative (no Docker): copy the repo, create a venv, and wrap
  `python -m vnedge.runtime.paper_trial ... --hours 720 --dashboard` in a
  unit with `Restart=always` — the account store makes restarts safe either way.
