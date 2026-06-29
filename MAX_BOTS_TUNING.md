# FinalZoom — Max-Bots Tuning Guide

Goal: **Maximize bots that successfully join a Zoom meeting** on a beefy RDP
(e.g. 16 vCPU / 128 GB RAM class). v8.7 defaults already aggressive — this
guide explains every knob + Zoom's external limits so you can push further.

---

## A. What actually limits bots per RDP

There are 6 layers; you can only push the bottleneck layer.

| # | Layer | Hard ceiling on 16vCPU/128GB |
|---|-------|------------------------------|
| 1 | **RAM** (Playwright Chromium tab ~120 MB) | ~900 bots |
| 2 | **CPU** (Chromium per-tab ~0.05 vcpu idle, 0.15 at join) | ~400 bots steady, ~150 during burst-join |
| 3 | **Linux ulimit** (open FDs, sockets, threads) | ~1024 default → must raise |
| 4 | **TCP / kernel net** (ephemeral ports, conntrack) | ~28000 default → enough but tune |
| 5 | **Zoom edge rate limit** (same IP, same meeting) | ~40-60 in 60 sec window |
| 6 | **Zoom WebClient SDK** (number of WS sessions to media server) | ~150 per Chromium process |

Default v8.7 settings hit **~400-500 bots** comfortably. Going beyond
500 requires raising ulimits and/or IP rotation.

---

## B. Code-level tunables (v8.7 defaults)

All settable via `.env` on each RDP worker.

```bash
# ─── Capacity formula ─────────────────────────────────────────
AUTO_CAPACITY=true          # auto-compute per-RDP cap
RAM_PER_BOT_MB=120          # Playwright tab footprint
RAM_HEADROOM_PCT=15         # reserve for OS + cache
BOTS_PER_CPU=25.0           # raise to 30 if joins spread over time
MAX_CAPACITY_HARD_CAP=1500  # absolute ceiling per RDP
PRE_SPAWN_FREE_RAM_PCT=10   # pause spawning if free RAM < this

# ─── Spawn ramp ────────────────────────────────────────────────
SPAWN_BURST_SIZE=8          # bots in parallel per batch
SPAWN_DELAY_MS=120          # gap between bursts
                            # 500 bots = 500/8 * 120ms = 7.5 sec ramp

# ─── Browser pooling ───────────────────────────────────────────
TABS_PER_BROWSER=20         # 20 tabs/chromium = RAM-efficient
PREWARM_BROWSERS=4          # hot chromiums at boot (try 6 for 128GB)
PREWARM_HARD_CEILING=400    # max prewarmed contexts standing by
PREWARM_MATCH_ADMIN_CAP=true

# ─── Join reliability ──────────────────────────────────────────
BOT_INITIAL_JOIN_RETRIES=12 # initial attempts before giving up
BOT_REJOIN_MAX=50           # mid-meeting drop recovery attempts
IN_MEETING_CHECK_SEC=8      # how often to verify still in meeting
REJOIN_BACKOFF_MIN=2
REJOIN_BACKOFF_MAX=12
STRICT_ANTI_LEAVE=true      # rejoin for entire meeting hold
MEETING_END_GRACE_SEC=4     # before triggering rejoin on drop

# ─── Per-task ──────────────────────────────────────────────────
MAX_CONCURRENT_TASKS=5      # this RDP can run N tasks in parallel
```

### Profile recommendations

| Box class | BOTS_PER_CPU | MAX_CAP | BURST | PREWARM_BROWSERS |
|---|---|---|---|---|
| 2 vCPU / 4 GB  | 10  | 25    | 2  | 1 |
| 4 vCPU / 8 GB  | 12  | 50    | 4  | 2 |
| 8 vCPU / 16 GB | 18  | 150   | 6  | 3 |
| 16 vCPU / 64 GB | 22 | 400   | 8  | 5 |
| **16 vCPU / 128 GB** | **30** | **700** | **10** | **6** |
| 32 vCPU / 256 GB | 35 | 1500 | 12 | 8 |

For a 16/128 box, drop this in `.env`:
```bash
BOTS_PER_CPU=30
MAX_CAPACITY_HARD_CAP=700
SPAWN_BURST_SIZE=10
PREWARM_BROWSERS=6
PREWARM_HARD_CEILING=600
```

---

## C. OS-level tuning (most impactful for >300 bots)

Run **once** on each RDP, as root:

```bash
# 1. Raise file descriptors (open sockets/files per process)
cat >> /etc/security/limits.conf <<'EOF'
finalzoom soft nofile 131072
finalzoom hard nofile 131072
finalzoom soft nproc 32768
finalzoom hard nproc 32768
* soft nofile 131072
* hard nofile 131072
EOF

# 2. Raise kernel ephemeral port range + TIME-WAIT reuse (network)
cat >> /etc/sysctl.conf <<'EOF'
net.ipv4.ip_local_port_range = 1024 65000
net.ipv4.tcp_tw_reuse = 1
net.ipv4.tcp_fin_timeout = 15
net.core.somaxconn = 4096
net.core.netdev_max_backlog = 5000
fs.file-max = 2097152
fs.inotify.max_user_watches = 524288
EOF
sysctl -p

# 3. Disable swap entirely (we have 128 GB, never want swap thrashing)
swapoff -a
sed -i '/ swap / s/^/#/' /etc/fstab

# 4. systemd unit: raise per-service nofile
sudo systemctl edit finalzoom-worker
# Add:
# [Service]
# LimitNOFILE=131072
# LimitNPROC=32768
sudo systemctl restart finalzoom-worker
```

After this, single chromium can hold 1000+ WS sockets cleanly.

---

## D. Zoom-side workarounds (the REAL ceiling)

Zoom edge servers throttle **same IP + same meeting**:
- ~40-60 joins in 60s before some get the "Please wait" lobby
- ~150-200 simultaneous WS sessions per IP before "service unavailable"

### 4 ways to bypass:

1. **Wider spawn window** — if you can wait, set `SPAWN_DELAY_MS=300` +
   `SPAWN_BURST_SIZE=3`. 500 bots = 500/3 × 300ms = 50 sec ramp. Slower
   but Zoom rate-limit ne dikkat.

2. **HTTP/SOCKS proxy rotation per bot** — 50 bots per proxy IP. ✅ IMPLEMENTED
   in worker v9.4. Add to `.env`:
   ```bash
   PROXY_LIST_FILE=/etc/finalzoom-proxies.txt  # one socks5://user:pass@ip:port per line
   # or inline:
   PROXY_LIST=socks5://u:p@1.2.3.4:1080,http://5.6.7.8:8080
   PROXIES_PER_BOT=50                          # round-robin: 50 bots per IP, then rotate
   ```
   Worker launches Chromium with a per-context proxy and assigns each bot the
   next IP round-robin. 5 IPs × 50 = 250 bots with no single-IP rate limit.

3. **Multiple public IPs on same box** — buy 4 extra IPs on your VPS
   ($1-2/month each), bind chromium to a specific source IP via
   `--host-resolver-rules` + `--proxy-bypass-list`. Splits 500 bots
   across 5 IPs → no rate limit.

4. **Two smaller RDPs > one giant RDP** — 2× 8 vCPU/64GB boxes = same
   power as 1× 16/128 but from Zoom's perspective looks like 2 separate
   IPs. Cleaner scaling. (You already have heterogeneous fleet support
   in dashboard distribution.)

---

## E. Browser optimization flags (already in code, FYI)

Already in `CHROMIUM_ARGS`:
- `--disable-gpu`, `--disable-software-rasterizer` — no video render
- `--use-fake-device-for-media-stream` — mic/cam icons visible, zero bytes
- `--mute-audio` — kernel-level mute
- Resource blocking via `ctx.route("**/*", _block)` — images/fonts/videos
  from non-Zoom origins blocked

Don't add `--single-process` — that destroys browser pooling, OOMs at 50 bots.

---

## F. Diagnosis — where are MY bots failing?

Run on RDP during a 500-bot test:

```bash
# Watch chromium count
watch 'pgrep -c chromium'

# Watch open sockets
watch 'ss -tan | grep zoom.us | wc -l'

# Watch nofile usage
watch 'lsof -p $(pgrep -f zoom_worker_pool) | wc -l'

# Watch RAM/CPU
htop
```

If chromium count < expected → spawn is failing (raise nofile).
If sockets > 800 but joined < 200 → Zoom rate-limit (slow burst).
If nofile near 1024 → DEFINITELY raise ulimit.

---

## G. Realistic targets after all tuning

| Setup | Joined target | Stable |
|---|---|---|
| Single 16/128 RDP, 1 IP, defaults | 350-450 | 320-400 |
| Single 16/128 RDP, 1 IP, v8.7 tuned + sysctl | 500-650 | 480-600 |
| Single 16/128 RDP, **5 IPs** rotated | 800-900 | 750-850 |
| 2× 8/64 RDP, 2 IPs | 600-700 total | 550-650 |
| 4× 8/64 RDP, 4 IPs | 1000+ total | 950+ |

**Bottom line:** beyond ~500 per IP, scale OUT (more RDPs/IPs) not UP.
The Zoom side wins eventually.

---

Last updated: 2026-05-30
