# Zoom Dashboard + Worker Fleet

A FastAPI dashboard + React UI + Playwright-based Zoom bot worker
that lets you spin up Zoom WebClient participants on Windows / Linux
RDPs and join meetings silently (mic + camera icons visible, no real
audio/video bytes).

## ⚡ Quick setup

### Windows RDP (Server 2019/2022) — v9.7 silent-UI
**One-time setup, then never open the RDP again.** Follow
[`ONE_TIME_RDP_SETUP.md`](./ONE_TIME_RDP_SETUP.md) — single PowerShell
one-liner that:
- Installs Python + Playwright Chromium + the v9.7 worker
- Registers `ZoomWorker` Windows service with auto-restart on crash + boot
- Applies all host hardening (power plan, RDP session persistence,
  Windows Update auto-restart off, TCP tuning, Defender exclusions,
  Zoom auto-update lock)

### Linux RDP
See [`RDP_SETUP_LINUX.md`](./RDP_SETUP_LINUX.md) + [`worker/install_linux.sh`](./worker/install_linux.sh).

## Architecture
- **Backend**: FastAPI + Mongo + Redis (queue)  — `/app/backend/`
- **Frontend**: React (CRA) — `/app/frontend/`
- **Worker**: Python + Playwright + Chromium — `/app/worker/`
- **Deploy**: docker-compose — `/app/deploy/`

## Key worker docs
| File                              | What it covers                                     |
|-----------------------------------|----------------------------------------------------|
| `ONE_TIME_RDP_SETUP.md`           | Single-command Windows install + verification      |
| `RDP_SETUP.md`                    | Manual Windows setup, multi-RDP fleet config       |
| `RDP_SETUP_LINUX.md`              | Ubuntu 22.04 worker setup (Xvfb + PM2)             |
| `RDP_STABILITY_CARD.md`           | Per-RDP post-deploy hardening checklist            |
| `MAX_BOTS_TUNING.md`              | Capacity formula + OS tunables for 500+ bots/box   |

— v9.7-silent-ui · 2026-01
