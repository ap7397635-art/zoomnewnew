#!/usr/bin/env bash
# =============================================================================
#  Zoom Dashboard — One-Tap VPS Deploy Script
#  Target  : Ubuntu 22.04 / 24.04 VPS  (root or sudo)
#  Stack   : Docker + docker-compose  →  Mongo + Redis + FastAPI + React/Nginx
#  Usage   : curl -fsSL <url-to-this-script> | bash
#            OR  sudo bash deploy.sh
# =============================================================================
set -euo pipefail

# ---------- Config (override via env vars before piping) ----------------------
VPS_PUBLIC_IP="${VPS_PUBLIC_IP:-146.190.67.195}"
APP_DIR="${APP_DIR:-/opt/zoom-dashboard}"
CODE_ZIP_URL="${CODE_ZIP_URL:-https://customer-assets.emergentagent.com/job_rdp-setup-auto/artifacts/q485iyfu_myfinal-jo-ki-pura-complete-hai-main.zip}"
PUBLIC_BACKEND_URL="${PUBLIC_BACKEND_URL:-http://${VPS_PUBLIC_IP}}"
ADMIN_EMAIL="${ADMIN_EMAIL:-admin@finalzoom.com}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-Admin@FinalZoom2026}"
ADMIN_NAME="${ADMIN_NAME:-Admin}"
USAGE_LIMIT="${USAGE_LIMIT:-15000}"
DISTRIBUTION_MODE="${DISTRIBUTION_MODE:-weighted}"
DB_NAME="${DB_NAME:-zoomdb}"

# Pretty logging
GREEN="\033[1;32m"; YEL="\033[1;33m"; RED="\033[1;31m"; CYAN="\033[1;36m"; NC="\033[0m"
log()  { echo -e "${GREEN}[deploy]${NC} $*"; }
warn() { echo -e "${YEL}[deploy]${NC} $*"; }
err()  { echo -e "${RED}[deploy]${NC} $*"; }
step() { echo -e "\n${CYAN}==>${NC} ${CYAN}$*${NC}"; }

[[ $EUID -eq 0 ]] || { err "Run as root:  sudo bash deploy.sh"; exit 1; }

# ---------- 1. Base system ---------------------------------------------------
step "Updating apt + installing base tools"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq curl wget unzip ca-certificates gnupg lsb-release ufw jq >/dev/null

# ---------- 2. Docker + docker compose ---------------------------------------
if ! command -v docker >/dev/null 2>&1; then
  step "Installing Docker Engine"
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  . /etc/os-release
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null
  systemctl enable --now docker
else
  log "Docker already installed: $(docker --version)"
fi

docker compose version >/dev/null 2>&1 || { err "docker compose plugin missing"; exit 1; }

# ---------- 3. Firewall ------------------------------------------------------
step "Opening firewall (22/80)"
ufw --force reset >/dev/null 2>&1 || true
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow 22/tcp   >/dev/null
ufw allow 80/tcp   >/dev/null
ufw --force enable >/dev/null

# ---------- 4. Pull project --------------------------------------------------
step "Preparing app directory at ${APP_DIR}"
mkdir -p "${APP_DIR}"
cd "${APP_DIR}"

if [[ ! -f .deploy_done_code_pull || "${FORCE_REPULL:-0}" == "1" ]]; then
  log "Downloading project zip"
  rm -f /tmp/zoom_project.zip
  curl -fsSL "${CODE_ZIP_URL}" -o /tmp/zoom_project.zip
  rm -rf /tmp/zoom_project_extract
  mkdir -p /tmp/zoom_project_extract
  unzip -q /tmp/zoom_project.zip -d /tmp/zoom_project_extract
  # The zip has a single top-level folder — copy its contents
  TOP_DIR="$(find /tmp/zoom_project_extract -mindepth 1 -maxdepth 1 -type d | head -n1)"
  [[ -n "${TOP_DIR}" ]] || { err "Failed to locate project root inside zip"; exit 1; }
  # Preserve any existing .env and deploy/ overrides
  rsync -a --delete \
        --exclude '.env' \
        --exclude 'deploy/' \
        "${TOP_DIR}/" "${APP_DIR}/"
  touch .deploy_done_code_pull
else
  log "Code already pulled (rerun with FORCE_REPULL=1 to refresh)"
fi

# ---------- 5. Write Docker build context (Dockerfiles + compose) -----------
step "Writing Docker build files"
mkdir -p "${APP_DIR}/deploy"

cat > "${APP_DIR}/deploy/Dockerfile.backend" <<'DOCKERFILE_BACKEND'
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates build-essential && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY backend/requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt
COPY backend/ /app/
EXPOSE 8001
CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port 8001 --workers ${UVICORN_WORKERS:-2} --proxy-headers --forwarded-allow-ips='*'"]
DOCKERFILE_BACKEND

cat > "${APP_DIR}/deploy/Dockerfile.frontend" <<'DOCKERFILE_FRONTEND'
FROM node:20-bullseye AS builder
WORKDIR /app
COPY frontend/package.json frontend/yarn.lock /app/
RUN yarn install --frozen-lockfile --network-timeout 600000
COPY frontend/ /app/
ARG REACT_APP_BACKEND_URL
ENV REACT_APP_BACKEND_URL=${REACT_APP_BACKEND_URL}
RUN yarn build
FROM nginx:1.27-alpine
COPY deploy/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /app/build /usr/share/nginx/html
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
DOCKERFILE_FRONTEND

cat > "${APP_DIR}/deploy/nginx.conf" <<'NGINX_CONF'
server {
    listen 80 default_server;
    server_name _;
    root /usr/share/nginx/html;
    index index.html;
    client_max_body_size 50m;
    location /api/ {
        proxy_pass http://backend:8001;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
        proxy_buffering off;
    }
    location ~* \.(?:css|js|woff2?|ttf|eot|svg|jpg|jpeg|png|gif|ico|webp)$ {
        expires 30d; access_log off;
        add_header Cache-Control "public, max-age=2592000, immutable";
        try_files $uri =404;
    }
    location / { try_files $uri $uri/ /index.html; }
}
NGINX_CONF

cat > "${APP_DIR}/docker-compose.yml" <<'COMPOSE_YAML'
services:
  mongo:
    image: mongo:7
    container_name: zoom-mongo
    restart: unless-stopped
    volumes: [mongo_data:/data/db]
    networks: [zoomnet]
    healthcheck:
      test: ["CMD", "mongosh", "--quiet", "--eval", "db.adminCommand('ping').ok"]
      interval: 10s
      timeout: 5s
      retries: 10
  redis:
    image: redis:7-alpine
    container_name: zoom-redis
    restart: unless-stopped
    volumes: [redis_data:/data]
    networks: [zoomnet]
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 3s
      retries: 5
  backend:
    build:
      context: .
      dockerfile: deploy/Dockerfile.backend
    container_name: zoom-backend
    restart: unless-stopped
    depends_on:
      mongo: { condition: service_healthy }
      redis: { condition: service_healthy }
    env_file: [.env]
    environment:
      MONGO_URL: mongodb://mongo:27017
      REDIS_URL: redis://redis:6379/0
    networks: [zoomnet]
    expose: ["8001"]
  frontend:
    build:
      context: .
      dockerfile: deploy/Dockerfile.frontend
      args:
        REACT_APP_BACKEND_URL: ${PUBLIC_BACKEND_URL}
    container_name: zoom-frontend
    restart: unless-stopped
    depends_on: [backend]
    networks: [zoomnet]
    ports: ["80:80"]
volumes: { mongo_data: {}, redis_data: {} }
networks:
  zoomnet: { driver: bridge }
COMPOSE_YAML

# ---------- 6. Generate .env (one-time) --------------------------------------
if [[ ! -f "${APP_DIR}/.env" ]]; then
  step "Generating .env (one-time)"
  JWT_SECRET="$(openssl rand -hex 48)"
  cat > "${APP_DIR}/.env" <<ENV
MONGO_URL=mongodb://mongo:27017
DB_NAME=${DB_NAME}
REDIS_URL=redis://redis:6379/0
JWT_SECRET=${JWT_SECRET}
ADMIN_EMAIL=${ADMIN_EMAIL}
ADMIN_PASSWORD=${ADMIN_PASSWORD}
ADMIN_NAME=${ADMIN_NAME}
USAGE_LIMIT=${USAGE_LIMIT}
DISTRIBUTION_MODE=${DISTRIBUTION_MODE}
UVICORN_WORKERS=2
PUBLIC_BACKEND_URL=${PUBLIC_BACKEND_URL}
ENV
  chmod 600 "${APP_DIR}/.env"
else
  log ".env already exists — keeping existing secrets"
fi

# Export PUBLIC_BACKEND_URL so docker compose interpolation works on this run
export PUBLIC_BACKEND_URL

# ---------- 7. Build + Up ----------------------------------------------------
step "Building containers (first run ~3-5 min)"
cd "${APP_DIR}"
docker compose pull --ignore-pull-failures || true
docker compose build

step "Starting stack"
docker compose up -d

step "Waiting for backend to be healthy"
for i in {1..60}; do
  if curl -fsS "http://localhost/api/auth/me" -o /dev/null -w "%{http_code}" 2>/dev/null | grep -qE '401|200'; then
    log "Backend responding on http://localhost"
    break
  fi
  printf '.'
  sleep 2
done
echo

# ---------- 8. Done ----------------------------------------------------------
echo
echo -e "${GREEN}==================================================================${NC}"
echo -e "${GREEN}  ✅ Zoom Dashboard is LIVE${NC}"
echo -e "${GREEN}==================================================================${NC}"
echo -e "  ${CYAN}URL${NC}        : ${PUBLIC_BACKEND_URL}"
echo -e "  ${CYAN}Admin email${NC} : ${ADMIN_EMAIL}"
echo -e "  ${CYAN}Admin pass${NC}  : ${ADMIN_PASSWORD}"
echo
echo -e "  ${CYAN}Manage stack:${NC}"
echo -e "    cd ${APP_DIR}"
echo -e "    docker compose ps            # status"
echo -e "    docker compose logs -f       # live logs"
echo -e "    docker compose restart       # restart all"
echo -e "    docker compose down          # stop"
echo -e "    docker compose up -d --build # rebuild after code changes"
echo
echo -e "  ${YEL}Tip:${NC} Change ADMIN_PASSWORD after first login!"
echo
