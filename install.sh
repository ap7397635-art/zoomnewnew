#!/usr/bin/env bash
# =============================================================================
#  Zoom Dashboard — One-Tap VPS Deploy (Ubuntu 22.04/24.04)
#  Usage: curl -fsSL <dashboard>/api/install/vps.sh | sudo VPS_PUBLIC_IP=<ip> bash
# =============================================================================
set -euo pipefail

VPS_PUBLIC_IP="${VPS_PUBLIC_IP:-}"
APP_DIR="${APP_DIR:-/opt/zoom-dashboard}"
# v9.7: DASHBOARD_URL = the live Emergent preview that serves the LATEST source.
# Override via env var if you fork or self-host the source tree.
DASHBOARD_URL="${DASHBOARD_URL:-https://7379a92a-9e8f-44af-bc6f-56b7b071c5e8.preview.emergentagent.com}"
CODE_ZIP_URL="${CODE_ZIP_URL:-${DASHBOARD_URL}/api/install/snapshot.zip}"
PUBLIC_BACKEND_URL="${PUBLIC_BACKEND_URL:-http://${VPS_PUBLIC_IP}}"
ADMIN_EMAIL="${ADMIN_EMAIL:-admin@finalzoom.com}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-Admin@FinalZoom2026}"
ADMIN_NAME="${ADMIN_NAME:-Admin}"
USAGE_LIMIT="${USAGE_LIMIT:-15000}"
DISTRIBUTION_MODE="${DISTRIBUTION_MODE:-weighted}"
DB_NAME="${DB_NAME:-zoomdb}"

GREEN="\033[1;32m"; YEL="\033[1;33m"; RED="\033[1;31m"; CYAN="\033[1;36m"; NC="\033[0m"
log()  { echo -e "${GREEN}[deploy]${NC} $*"; }
warn() { echo -e "${YEL}[deploy]${NC} $*"; }
err()  { echo -e "${RED}[deploy]${NC} $*"; }
step() { echo -e "\n${CYAN}==>${NC} ${CYAN}$*${NC}"; }

[[ $EUID -eq 0 ]] || { err "Run as root:  sudo bash $0"; exit 1; }
if [[ -z "${VPS_PUBLIC_IP}" ]]; then
  err "VPS_PUBLIC_IP env var is required. Example:"
  err "  curl -fsSL ${DASHBOARD_URL}/api/install/vps.sh | sudo VPS_PUBLIC_IP=1.2.3.4 bash"
  exit 1
fi
log "VPS_PUBLIC_IP=${VPS_PUBLIC_IP}  DASHBOARD_URL=${DASHBOARD_URL}"

step "Updating apt + installing base tools"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq curl wget unzip ca-certificates gnupg lsb-release ufw jq rsync >/dev/null

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

step "Opening firewall (22/80)"
ufw --force reset >/dev/null 2>&1 || true
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow 22/tcp   >/dev/null
ufw allow 80/tcp   >/dev/null
ufw --force enable >/dev/null

step "Preparing app directory at ${APP_DIR}"
mkdir -p "${APP_DIR}"
cd "${APP_DIR}"

step "Pulling fresh code snapshot from ${CODE_ZIP_URL}"
rm -f /tmp/zoom_project.zip
curl -fsSL "${CODE_ZIP_URL}" -o /tmp/zoom_project.zip
rm -rf /tmp/zoom_project_extract
mkdir -p /tmp/zoom_project_extract
unzip -q /tmp/zoom_project.zip -d /tmp/zoom_project_extract
if [[ -d /tmp/zoom_project_extract/project ]]; then
  TOP_DIR=/tmp/zoom_project_extract/project
else
  TOP_DIR="$(find /tmp/zoom_project_extract -mindepth 1 -maxdepth 1 -type d | head -n1)"
fi
[[ -n "${TOP_DIR}" ]] || { err "Failed to locate project root inside zip"; exit 1; }
rsync -a --delete --exclude '.env' --exclude 'deploy/' "${TOP_DIR}/" "${APP_DIR}/"

step "Writing Dockerfiles + docker-compose"
mkdir -p "${APP_DIR}/deploy"

cat > "${APP_DIR}/deploy/Dockerfile.backend" <<'DOCKERFILE_BACKEND'
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates build-essential && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
COPY backend/requirements.txt /srv/backend/requirements.txt
RUN pip install --extra-index-url https://d33sy5i8bnduwe.cloudfront.net/simple/ -r /srv/backend/requirements.txt
COPY backend/ /srv/backend/
COPY worker/ /srv/worker/
COPY install_vps.sh /srv/install_vps.sh
COPY README.m[d] RDP_SETUP.m[d] RDP_SETUP_LINUX.m[d] RDP_STABILITY_CARD.m[d] MAX_BOTS_TUNING.m[d] /srv/
WORKDIR /srv/backend
EXPOSE 8001
CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port 8001 --workers ${UVICORN_WORKERS:-2} --proxy-headers --forwarded-allow-ips='*'"]
DOCKERFILE_BACKEND

cat > "${APP_DIR}/deploy/Dockerfile.frontend" <<'DOCKERFILE_FRONTEND'
FROM node:20-bullseye AS builder
WORKDIR /app
COPY frontend/package.json /app/
COPY frontend/yarn.loc[k] /app/
RUN yarn install --network-timeout 600000
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

if [[ ! -f "${APP_DIR}/.env" ]]; then
  step "Generating .env (one-time, JWT auto-random)"
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

export PUBLIC_BACKEND_URL

step "Building containers"
cd "${APP_DIR}"
docker compose pull --ignore-pull-failures 2>/dev/null || true
docker compose build

step "(Re)Starting stack"
docker compose down --remove-orphans 2>/dev/null || true
docker compose up -d

step "Waiting for backend to be healthy"
for i in {1..60}; do
  http_code="$(curl -fsS -o /dev/null -w "%{http_code}" "http://localhost/api/auth/me" 2>/dev/null || echo 000)"
  if [[ "${http_code}" == "401" || "${http_code}" == "200" ]]; then
    log "Backend responding (HTTP ${http_code})"
    break
  fi
  printf '.'
  sleep 2
done
echo

step "Verifying critical endpoints"
for ep in "/" "/api/auth/me" "/api/worker/install.ps1" "/api/install/vps.sh"; do
  code="$(curl -fsS -o /dev/null -w "%{http_code}" "http://localhost${ep}" 2>/dev/null || echo 000)"
  printf "  %-32s -> HTTP %s\n" "${ep}" "${code}"
done

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
echo
echo -e "  ${YEL}Tip:${NC} Re-run this same one-liner to pull fresh code + rebuild."
echo
