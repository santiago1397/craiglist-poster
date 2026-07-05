# Manual setup checklist

Everything I can't automate. Do these in order — Windows-side and VPS-side are
independent, but they need to know each other's tokens.

---

## 1. VPS side (once)

Prereq: your VPS already runs Traefik + Docker per `SERVER_SETUP.md` and the
`traefik-public` network exists.

### 1.1 Native Postgres user + database (Option A)

Native Postgres on the host, reached from the container via `host.docker.internal`:

```bash
sudo -u postgres psql <<'SQL'
CREATE USER craigslist WITH PASSWORD 'STRONG-RANDOM-PASSWORD';
CREATE DATABASE craigslist OWNER craigslist;
GRANT ALL PRIVILEGES ON DATABASE craigslist TO craigslist;
SQL
```

Make sure Postgres accepts connections from the Docker bridge (per
`SERVER_SETUP.md § PostgreSQL strategy → Option A`):

```
# /etc/postgresql/16/main/postgresql.conf
listen_addresses = '*'
```

```
# /etc/postgresql/16/main/pg_hba.conf
host  craigslist  craigslist  172.17.0.0/16  scram-sha-256
```

```bash
sudo systemctl restart postgresql
```

### 1.2 DNS records

Point both hostnames at the VPS IP (A record, ideally AAAA too):

- `craigslist.yourdomain.com` — the SPA
- `api.craigslist.yourdomain.com` — the FastAPI backend

Do this **before** first deploy so Let's Encrypt HTTP-01 can issue certs.

### 1.3 Clone the repo on the VPS

```bash
sudo mkdir -p /opt/santiagoproperties
cd /opt/santiagoproperties
git clone git@github.com:YOUR_USER/craigslist_automation.git
cd craigslist_automation
```

### 1.4 Fill in `.env.prod`

```bash
cp .env.prod.example .env.prod
chmod 600 .env.prod
$EDITOR .env.prod
```

Fields you must set:

| Key                    | How to generate                                                                  |
|------------------------|----------------------------------------------------------------------------------|
| `API_HOST`             | `api.craigslist.yourdomain.com`                                                  |
| `WEB_HOST`             | `craigslist.yourdomain.com`                                                      |
| `POSTGRES_PASSWORD`    | Same one you used in step 1.1                                                    |
| `ADMIN_EMAIL`          | Your email                                                                       |
| `ADMIN_PASSWORD_HASH`  | See step 1.5                                                                     |
| `JWT_SECRET`           | `openssl rand -hex 32`                                                           |
| `COOKIE_DOMAIN`        | `.yourdomain.com` (parent — leading dot)                                         |
| `INGEST_BEARER_TOKEN`  | `openssl rand -hex 32` — copy this, you'll paste into Windows `.env` in step 2.2 |
| `CORS_ORIGINS`         | `https://craigslist.yourdomain.com`                                              |

### 1.5 Hash your admin password

The backend never stores the plaintext. Generate the hash locally once:

```bash
cd backend
uv sync   # if you haven't
uv run hash-password
```

Paste the printed hash into `.env.prod` as `ADMIN_PASSWORD_HASH`.

Alternative: run it inside the built image after `docker compose build`.

### 1.6 Add the SSH alias on your laptop

`~/.ssh/config`:

```ssh-config
Host craigslist
    HostName <your-vps-ip>
    User root
    IdentityFile ~/.ssh/id_ed25519
    IdentitiesOnly yes
```

Test: `ssh craigslist true` should return exit 0 with no password prompt.

### 1.7 First deploy

From your laptop, at the repo root:

```bash
bash scripts/deploy.sh
```

If you use a different path on the VPS, override:

```bash
VPS_REPO_PATH=/opt/whatever bash scripts/deploy.sh
```

Migrations run automatically at container startup (see `backend/Dockerfile`
`CMD`), so the first deploy creates the schema.

Open `https://craigslist.yourdomain.com`, log in, empty dashboard. Ready.

---

## 2. Windows side

### 2.1 Sync the shared events schema (one-time, and after any edit)

Whenever you edit `src/craigslist_auto/events.py`, run:

```powershell
python scripts/sync_schemas.py
```

(Docker builds do this automatically via the backend Dockerfile — this is only
for local backend dev.)

### 2.2 Windows `.env` additions

Add these to your existing `.env` at the repo root:

```
REPORTER_URL=https://api.craigslist.yourdomain.com/events
REPORTER_TOKEN=<the same INGEST_BEARER_TOKEN from step 1.4>
MACHINE_ID=desktop-eseva3c
```

`MACHINE_ID` should match `Account.allowed_machine` in `config.py`.

### 2.3 Install the reporter daemon (drains outbox, sends heartbeats)

Right-click → Run with PowerShell (as your normal user, not admin):

```
scripts\install-reporter-daemon.ps1
```

It auto-starts at logon and restarts every minute if it dies.

### 2.4 Install the nightly photo-inventory task

```
scripts\install-photo-inventory-schedule.ps1
```

Runs at 03:00 daily.

### 2.5 Backfill history (one-shot)

```powershell
uv run cl backfill-postgres --dsn "postgresql://craigslist:PASSWORD@YOUR_VPS_IP:5432/craigslist"
```

- Postgres must be reachable from your machine — either open port 5432 in the
  cloud firewall for your IP temporarily, or run this **from the VPS** (SSH in
  and `docker compose exec api sh` then `uv run cl backfill-postgres ...` after
  installing craigslist_auto). Simplest: temporarily open 5432, run once, close.
- Idempotent — safe to re-run if it errors partway.

### 2.6 Confirm end-to-end

```powershell
uv run cl outbox
```

You should see the pending count go up briefly when the daemon flushes, then
settle at 0. The dashboard at `https://craigslist.yourdomain.com` should light
up with account cards within a couple of minutes.

---

## 3. Operations

### Daily

- Nothing. The daemon runs, heartbeats flow, dashboard stays live.

### If a card shows "stats-sync failing"

Whatever the existing `cl status` would tell you. If `login_expired`, run:

```powershell
uv run cl init-account craigsN
```

### If the daemon dies and doesn't restart

```powershell
Get-ScheduledTask -TaskName "CL Reporter Daemon"
Start-ScheduledTask -TaskName "CL Reporter Daemon"
```

### Outbox backing up

```powershell
uv run cl outbox                 # inspect
uv run cl outbox --purge-days 14 # trim sent history
```

If pending is growing unbounded, either the VPS is down (check
`https://api.craigslist.yourdomain.com/health`) or `REPORTER_TOKEN` doesn't
match `INGEST_BEARER_TOKEN`.

### Rotating the ingest bearer token

Regenerate: `openssl rand -hex 32`. Update both `.env.prod` on the VPS
(re-deploy) and Windows `.env` (restart the reporter daemon).

### Rotating the admin password

Re-run `uv run hash-password`, paste into `.env.prod`, redeploy.

### Backup

Nightly `pg_dump` on the VPS host:

```bash
# /etc/cron.daily/craigslist-pg-backup
#!/bin/sh
sudo -u postgres pg_dump -Fc craigslist > /var/backups/craigslist-$(date +\%Y\%m\%d).dump
find /var/backups -name 'craigslist-*.dump' -mtime +30 -delete
```

`chmod +x` and drop it in `/etc/cron.daily/`.

---

## 4. What I did NOT set up

- **Offsite backups** (B2/S3). `pg_dump` writes to the host disk; if the VPS
  dies you lose them. Fine for v1 — Windows still has `stats.sqlite` as an
  authoritative local copy.
- **Alerting** on dead accounts or bulging outbox. You'll notice via the
  dashboard.
- **CI/CD**. `bash scripts/deploy.sh` from your laptop is the deploy.
- **Sentry / structured logging**. Loguru writes JSON in-container; `docker
  compose logs -f` is your friend.
- **2FA on admin login**. Q11e — skipped for v1. Add TOTP with `pyotp` if you
  want it later.
- **IP allowlist on `/events`**. Windows home IP rotates.
