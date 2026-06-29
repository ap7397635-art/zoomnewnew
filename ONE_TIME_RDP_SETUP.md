# v9.7 — One-Time Windows RDP Setup (then never open RDP again)

> Bhai, **ek hi baar** yeh do step karne hain RDP pe. Uske baad service apne
> aap reboot, crash, screen-share, network-blip — sab cases mein recover ho
> jaayegi. Naya task aaye ya purana khatam ho — RDP touch karne ki zaroorat
> nahi.

---

## ⚡ ONE COMMAND (PowerShell as Administrator)

```powershell
$env:DASHBOARD_URL="https://YOUR-DASHBOARD-URL"
iwr "$env:DASHBOARD_URL/api/worker/install.ps1" | iex
```

That's it. Script downloads worker, writes `.env`, installs Python +
Playwright Chromium, registers `ZoomWorker` Windows service, and applies
**all** of these host tweaks (idempotent — safe to re-run):

| # | What gets configured                                              | Why                                                 |
|---|-------------------------------------------------------------------|-----------------------------------------------------|
| 1 | NSSM auto-restart on crash (`AppExit Default Restart`)            | If worker ever dies, SCM restarts in 5s             |
| 2 | NSSM log rotation @ 10 MB                                         | Disk doesn't fill over weeks of uptime              |
| 3 | SCM-level recovery (`sc.exe failure …`)                           | Belt-and-suspenders if NSSM itself crashes          |
| 4 | Service `Start SERVICE_AUTO_START`                                | Survives Windows reboots                            |
| 5 | High-performance power plan, no sleep / monitor-off / hibernate   | Box never goes to sleep mid-meeting                 |
| 6 | RDP session: `fDisableAutoReconnect=0`, `NoLockScreen=1`          | Session stays signed-in when you disconnect RDP     |
| 7 | Screensaver / inactivity lock disabled                            | Nothing ever locks the bot tabs                     |
| 8 | Windows Update `NoAutoRebootWithLoggedOnUsers=1`                  | WU never restarts during a live meeting             |
| 9 | TCP ephemeral port range 1024–65000 + `TcpTimedWaitDelay=30`      | Rejoins churn many sockets fast — kernel keeps up   |
|10 | Defender exclusions: `C:\zoom-worker`, chromium/python/nssm       | No scan-on-execute slowdown on every Chrome spawn   |
|11 | Zoom auto-update disabled (`EnableClientAutoUpdate=0`)            | Zoom can't break our selectors mid-task             |

---

## What changes inside the worker (v9.7)

### 1. Member sees ZERO meeting activity (silent UI)
`ZK_HIDE_UI=true` hides:
- Chat panel + chat-popup notifications
- Participants panel + raise-hand popups
- Reactions overlay (floating emojis from OTHER participants)
- Q&A, polls, breakouts, whiteboard, immersive view
- Recording / encryption / network warning indicators
- Top toolbar (meeting topic, view selector, info banners)
- Modals, banners, toasts, tooltips
- Apps / More-menu / Closed Captions buttons

Only the mic-icon + cam-icon footer buttons remain visible — so if you ever
peek at the RDP, every bot tab just looks like "joined with mic + cam".

### 2. Mic + camera show **CONNECTED** for every bot
- `JOIN_WITH_AUDIO_MUTED=false` → mic icon green (not slashed)
- `JOIN_WITH_VIDEO_OFF=false`   → cam icon on (not slashed)
- `MEDIA_KILL_INIT_SCRIPT` (already shipping) overrides `getUserMedia` to
  return a silent audio track + black canvas video track — so Zoom shows
  the icons as connected but **zero real bytes** ever leave the browser.

### 3. Members no longer drop on screen share or "aise hi"
- v9.7 renegotiation guard: if any `RTCPeerConnection.signalingState !=
  'stable'` (screen-share start / stop, codec switch, recovery), the
  worker SKIPS drop-detection that cycle. Bot stays put.
- v9.7 consecutive-error threshold bumped from 10 → 20 cycles (~2 min)
  before falling back to the rejoin path. Real CPU spikes resolve in 10–30s,
  so this is well under the genuine "drop" window.
- The existing 8-layer screen-share survival stack is still active
  (SDP video-strip, transceiver=inactive, getStats spoof, DOM nuke,
  VP8-only codec preference, …).
- `BOT_REJOIN_MAX=9999` + 1–8 s random backoff → if any bot DOES somehow
  drop, it's back in within seconds, for the FULL meeting duration.

### 4. Worker auto-restarts, RDP login session stays
- NSSM `AppExit Default Restart` + `AppRestartDelay 5000`
- SCM `sc.exe failure ZoomWorker restart/5000/restart/5000/restart/5000`
- RDP registry: `fDisableAutoReconnect=0`, `NoLockScreen=1` →
  disconnect ≠ logout; session lives forever
- Between tasks: worker recycles tabs via `RECYCLE_CONTEXT_ON_END=true`
  so the next member-batch picks up pre-warmed contexts in milliseconds.
  No "RDP open karke restart karo" needed.

---

## How to verify (5-minute test)

1. **Trigger task from dashboard** with `members=3`, `timeout=600`.
2. RDP par koi UI nahi dekhna — bas dashboard pe joined=3 turant.
3. Host se Zoom mein **screen share** start karo.
4. Dashboard pe `joined_count` 3 hi rehna chahiye (no drop).
5. Host meeting end kare → ~10s mein worker chunk complete kar deta hai.
6. Dashboard se naya task fire karo → worker turant uthata hai
   (no restart, no manual RDP touch).

If something does drop, `C:\zoom-worker\worker.log` will show:

```
[BotName] PC renegotiating (likely screen share) — skipping drop trigger this cycle
[BotName] dropped, rejoin attempt N
[BotName] meeting ended by host — exiting cleanly
```

---

## Rolling back

If you ever need the old "mic muted on join" behaviour:

```powershell
notepad C:\zoom-worker\.env
# change:
JOIN_WITH_AUDIO_MUTED=true
JOIN_WITH_VIDEO_OFF=true
ZK_HIDE_UI=false
# save, then:
Restart-Service ZoomWorker
```

That's it — no reinstall needed.

— v9.7-silent-ui · 2026-01
