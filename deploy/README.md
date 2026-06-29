# Zoom Dashboard — VPS Deployment

## What's in here
- `deploy/deploy.sh` — one-tap installer for Ubuntu 22.04/24.04 VPS
- `deploy/Dockerfile.backend` — FastAPI + uvicorn image
- `deploy/Dockerfile.frontend` — React build + Nginx (reverse-proxies `/api` → backend)
- `deploy/nginx.conf` — Nginx config
- `deploy/docker-compose.yml` — full stack (mongo + redis + backend + frontend)

## How to deploy on your VPS (146.190.67.195)

### Step 1 — SSH into VPS as root
```bash
ssh root@146.190.67.195
```

### Step 2 — Upload `deploy.sh` (one of three ways)

**Option A — Quick paste (no upload):**
On VPS run:
```bash
nano deploy.sh
# paste the entire deploy/deploy.sh contents, save (Ctrl+X, Y, Enter)
chmod +x deploy.sh && sudo bash deploy.sh
```

**Option B — scp from your local machine:**
```bash
scp deploy/deploy.sh root@146.190.67.195:/root/
ssh root@146.190.67.195 'bash /root/deploy.sh'
```

**Option C — If you host `deploy.sh` somewhere public (gist/github):**
```bash
curl -fsSL https://YOUR_URL/deploy.sh | sudo bash
```

### Step 3 — Wait ~3-5 min
- Installs Docker
- Downloads project zip from Emergent CDN (already public URL embedded)
- Builds 4 containers (mongo, redis, backend, frontend)
- Opens ports 22 + 80
- Seeds admin user

### Step 4 — Open in browser
http://146.190.67.195

Login:
- Email: `admin@finalzoom.com`
- Password: `Admin@FinalZoom2026`  ← change this on first login!

## Manage stack
```bash
cd /opt/zoom-dashboard
docker compose ps              # status of all 4 containers
docker compose logs -f         # follow logs
docker compose logs -f backend # only backend
docker compose restart         # restart all
docker compose down            # stop everything
docker compose up -d --build   # rebuild after code changes
```

## Updating the code
```bash
cd /opt/zoom-dashboard
FORCE_REPULL=1 bash deploy.sh   # re-downloads zip + rebuilds
```

## Customising before first deploy
Set env vars before piping/running deploy.sh:
```bash
ADMIN_EMAIL=you@example.com \
ADMIN_PASSWORD='YourStrongPass!' \
USAGE_LIMIT=50000 \
DISTRIBUTION_MODE=weighted \
bash deploy.sh
```

## What gets installed on the VPS
| Component         | Location                      |
|-------------------|-------------------------------|
| Docker engine     | system-wide                   |
| App code          | `/opt/zoom-dashboard`         |
| Mongo data        | docker volume `mongo_data`    |
| Redis data        | docker volume `redis_data`    |
| Secrets (.env)    | `/opt/zoom-dashboard/.env`    |
| Open ports        | 22 (ssh), 80 (web)            |

## Adding RDP workers (Playwright/Zoom)
Once dashboard is live, open `http://146.190.67.195/workers` → **Add Worker** →
download `.env` → on each RDP follow `RDP_SETUP_LINUX.md` in the project root.
The RDP installer (`worker/install_linux.sh`) is downloadable from the dashboard.

## SSL / domain (optional, later)
If you point a domain at the VPS, drop in Caddy or Traefik in front of the
`frontend` container. Current setup is HTTP-only on port 80.
