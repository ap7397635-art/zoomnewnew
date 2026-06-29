"""
Zoom Worker v8 — Ultra-Optimized Playwright Browser Pool Worker
================================================================

Key optimizations vs v7-lean (Selenium multiprocess):
  1. BROWSER POOLING  — 1 chromium = many contexts (15-25 tabs per browser).
                         RAM drops ~70% vs 1-process-per-bot.
  2. PLAYWRIGHT       — faster, leaner, native async, better selectors.
  3. XVFB (Linux)     — virtual display so chromium runs without GUI overhead.
  4. AUTO CLEANUP     — page.close + context.close on meeting end + periodic
                         `pkill -f chromium` sweep for orphans.
  5. SEQUENTIAL FILL  — relies on backend's DISTRIBUTION_MODE=greedy.
  6. HEALTH MONITOR   — separate thread monitors cpu/ram and DYNAMICALLY
                         lowers reported_capacity to backend (`if cpu>75: max-=5`).
  7. AUTO RESTART     — if any browser dies, the pool spawns a replacement.
  8. ZERO IDLE        — pool grows on demand, shrinks when tasks complete.
"""
from __future__ import annotations

import os
import sys
import gc
import time
import json
import socket
import signal
import asyncio
import logging
import platform
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Set
from dataclasses import dataclass, field

try:
    from dotenv import load_dotenv
except ImportError:
    print("Run: pip install -r requirements.txt"); sys.exit(1)

import requests

try:
    import psutil
except ImportError:
    psutil = None

try:
    from playwright.async_api import async_playwright, Browser, BrowserContext, Page
except ImportError:
    print("Playwright missing. Run: pip install playwright && playwright install chromium")
    sys.exit(1)

# ---------------------------------------------------------------- env
ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "").rstrip("/")
WORKER_TOKEN = os.environ.get("WORKER_TOKEN", "")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "15"))
# v8.9: STABILITY > SPEED.
#   SPAWN_DELAY_MS 120 → 200 (slower ramp = less CPU spike during join wave)
#   SPAWN_BURST_SIZE 8 → 4 (smaller bursts = more even CPU curve)
# Result on 90-bot test: CPU peak drops from 100% to ~65%, no no_meeting_shell
# casualties. Total ramp time only slightly higher (~18s vs ~9s for 500 bots).
# v9.3 — JOIN SPEED BOOST
# Since v9.1 fixed the screen-share CPU spike (the original reason these were
# slow), we can ramp spawn rate way back up:
#   SPAWN_DELAY_MS  200 → 80   (5x faster between bursts)
#   SPAWN_BURST_SIZE  4 → 8    (2x more bots per burst)
#   100 members spawn-gating now ≈ 1.3s (was ~5s).
# v10: 400ms between bursts of 5 — balances speed vs Zoom rate-limit.
# With proxy rotation each burst comes from different IP → safe to be aggressive.
# v9.8 USER REQUEST: "gap nhi chhaiye, ek sath hona chhaiye bhai" —
# default spawn delay forced to 0 so all bots fire simultaneously.
# Operator can still override via SPAWN_DELAY_MS env if needed.
SPAWN_DELAY_MS = int(os.environ.get("SPAWN_DELAY_MS", "0"))
# v9.8: bigger default burst so 100 bots fire in essentially one wave.
# Combined with SPAWN_DELAY_MS=0 → all bots spawn simultaneously, no gap.
SPAWN_BURST_SIZE = int(os.environ.get("SPAWN_BURST_SIZE", "100"))
MAX_CONCURRENT_TASKS = int(os.environ.get("MAX_CONCURRENT_TASKS", "5"))
HEADLESS = os.environ.get("HEADLESS", "true").lower() == "true"   # v8.3.4: default headless
LOCAL_NAMES_FILE = os.environ.get("LOCAL_NAMES_FILE", "").strip()

# v8.3.4: ===== JOIN with AUDIO/VIDEO OFF =====
# v9.7 USER REQUEST: "bss memeber mic video ke sath connect ho aur sabke mic
# connect ho" → defaults flipped to FALSE so each bot enters the meeting with
# its mic + camera icons visible as CONNECTED (not the slashed-mic / off-cam
# look). The MEDIA_KILL_INIT_SCRIPT still overrides getUserMedia to return a
# silent audio track + black canvas video track, so even though Zoom shows the
# icons green, no real audio/video bytes ever leave the browser. Best of both
# worlds: bots LOOK live, but transmit nothing harmful.
# Override with JOIN_WITH_AUDIO_MUTED=true / JOIN_WITH_VIDEO_OFF=true if you
# want the old muted-on-join behaviour.
JOIN_WITH_AUDIO_MUTED = os.environ.get("JOIN_WITH_AUDIO_MUTED", "false").lower() == "true"
JOIN_WITH_VIDEO_OFF   = os.environ.get("JOIN_WITH_VIDEO_OFF", "false").lower() == "true"
# Push the (possibly visible) chromium window off-screen so even in non-headless
# mode the RDP user never sees floating browsers. Works on both Win + Linux.
OFFSCREEN_WINDOW = os.environ.get("OFFSCREEN_WINDOW", "true").lower() == "true"

# Browser pooling: 1 chromium = TABS_PER_BROWSER contexts.
# 20 is the sweet spot (RAM efficient + no shared-process crash).
TABS_PER_BROWSER = int(os.environ.get(
    "TABS_PER_BROWSER",
    "30" if os.environ.get("ULTRA_LITE", "false").lower() == "true" else "20"
))
# v8.9: NEVER-LEAVE DEFAULTS. The user wants bots to drop ONLY when:
#   (a) host ends the meeting (detected via _meeting_ended)
#   (b) admin cancels the task from dashboard
# All defaults below tuned for maximum stability over speed.
BOT_INITIAL_JOIN_RETRIES = int(os.environ.get("BOT_INITIAL_JOIN_RETRIES", "20"))
BOT_REJOIN_MAX = int(os.environ.get("BOT_REJOIN_MAX", "99999"))   # ≈ infinite — only meeting-end stops it
IN_MEETING_CHECK_SEC = int(os.environ.get(
    "IN_MEETING_CHECK_SEC",
    "10" if os.environ.get("ULTRA_LITE", "false").lower() == "true" else "8"
))
KICK_DETECT_WINDOW = int(os.environ.get("KICK_DETECT_WINDOW", "300"))
STRICT_ANTI_LEAVE = os.environ.get("STRICT_ANTI_LEAVE", "true").lower() == "true"
# Rejoin backoff: min 1s, max 5s (was 8s — faster reconnect)
REJOIN_BACKOFF_MIN = int(os.environ.get("REJOIN_BACKOFF_MIN", "1"))
REJOIN_BACKOFF_MAX = int(os.environ.get("REJOIN_BACKOFF_MAX", "5"))
MEETING_END_GRACE_SEC = int(os.environ.get("MEETING_END_GRACE_SEC", "8"))

# v8.4: ===== REACTIONS =====
# When a task has participant_reactions=True OR floating_emoji=True, each bot
# spawns a side-coroutine that periodically clicks the Zoom reaction button
# and picks a random emoji. The interval (seconds) is read from the TASK
# payload (`reaction_interval_min`/`reaction_interval_max`) — falls back here.
REACTION_INTERVAL_MIN_DEFAULT = int(os.environ.get("REACTION_INTERVAL_MIN", "30"))
REACTION_INTERVAL_MAX_DEFAULT = int(os.environ.get("REACTION_INTERVAL_MAX", "90"))
REACTIONS_ENABLED = os.environ.get("REACTIONS_ENABLED", "true").lower() == "true"

# v8.4: ===== KEEP TABS WARM ON CLEANUP =====
# When ON (default), instead of closing the BrowserContext at the end of a
# meeting we RECYCLE it — blank the page, return it to the prewarm ready pool.
# Next task uses it instantly. Massive CPU saving since spinning up a new
# context costs ~800ms-1.5s. User explicitly requested:
#   "rdp clean mtlb procese bnd but jo tabs open hai vo nhi bnd honge"
RECYCLE_CONTEXT_ON_END = os.environ.get("RECYCLE_CONTEXT_ON_END", "true").lower() == "true"

# Auto health-thresholds. When CPU/RAM exceed these we throttle.
CPU_THROTTLE_PCT = float(os.environ.get("CPU_THROTTLE_PCT", "95"))
RAM_THROTTLE_PCT = float(os.environ.get("RAM_THROTTLE_PCT", "92"))
DYNAMIC_LIMIT_STEP = int(os.environ.get("DYNAMIC_LIMIT_STEP", "5"))
# v9.5: how many CONSECUTIVE high-pressure checks (10s each) before we
# actually start throttling. A single CPU spike during spawn-burst should
# NEVER reduce capacity — user logs showed dynamic_cap going to 1 on a
# 100% momentary spike even though the meeting completed 50/50.
DYNAMIC_THROTTLE_CONSECUTIVE = int(os.environ.get("DYNAMIC_THROTTLE_CONSECUTIVE", "4"))

# Capacity computation (lean Playwright = ~120 MB per tab w/ shared browser).
# v8.7 defaults tuned for BEEFY boxes (16 vCPU / 128 GB RAM class):
#   • BOTS_PER_CPU 8 → 25: Chromium tabs are I/O-bound, not CPU-bound. On
#     a 16-core box this raises CPU-based ceiling from 128 → 400 bots.
#   • MAX_CAPACITY_HARD_CAP 500 → 1500: hard ceiling for monster machines.
#     Final capacity is still capped by free RAM × headroom.
#   • RAM_HEADROOM_PCT 20 → 15: claim slightly more RAM safely; OS + cache
#     only need ~10 GB on 128 GB boxes.
AUTO_CAPACITY = os.environ.get("AUTO_CAPACITY", "true").lower() == "true"

# ============================================================
# v9.7 ULTRA_LITE MODE — squeeze max bots per RDP with min RAM/CPU
# ------------------------------------------------------------
# Toggle via .env:  ULTRA_LITE=true
# When ON, the worker uses the aggressive footprint defaults below
# UNLESS the user has explicitly overridden each value.
#
#                 default     ULTRA_LITE
# RAM_PER_BOT_MB  120 MB      80 MB     (~1.5× density)
# BOTS_PER_CPU    25          40        (~1.6× CPU density)
# TABS_PER_BROWSER 20         30        (fewer chromiums = less RAM)
# V8 heap          256 MB      96 MB     (smaller per-tab heap)
# IN_MEETING_CHECK_SEC 6      10        (less polling = less CPU)
# WARMUP_INTERVAL_SEC 15      30
# Renderer fps     1 Hz       0.25 Hz   (already throttled in post_join)
# ============================================================
ULTRA_LITE = os.environ.get("ULTRA_LITE", "false").lower() == "true"
def _ul_default(key: str, lite_val, normal_val):
    """Return the env-var value if set, else ULTRA_LITE value, else normal."""
    return os.environ.get(key, lite_val if ULTRA_LITE else normal_val)

RAM_PER_BOT_MB = int(_ul_default("RAM_PER_BOT_MB", "80", "120"))
RAM_HEADROOM_PCT = float(os.environ.get("RAM_HEADROOM_PCT", "15"))
BOTS_PER_CPU = float(_ul_default("BOTS_PER_CPU", "40.0", "25.0"))
MAX_CAPACITY_HARD_CAP = int(os.environ.get("MAX_CAPACITY_HARD_CAP", "1500"))
PRE_SPAWN_FREE_RAM_PCT = float(os.environ.get("PRE_SPAWN_FREE_RAM_PCT", "10"))
# v8.8: CPU gate — pause spawning when CPU is above this percent. Stops the
# 100% CPU death-spiral seen in user logs where bots kept spawning while
# already-running ones were starving for CPU and dropping to no_meeting_shell.
# Default 88 leaves room for join-time spikes; set to 0 to disable.
# v9.3: raise CPU gate 80 → 92 because v9.1 killed the screen-share spike — we
# now have plenty of headroom and the old 80% gate was the main thing pausing
# the join wave for 30s every time CPU briefly poked above it.
PRE_SPAWN_MAX_CPU_PCT = float(os.environ.get("PRE_SPAWN_MAX_CPU_PCT", "99"))

# Periodic cleanup sweep (kills orphan chromium not tracked by pool).
CLEANUP_INTERVAL_SEC = int(os.environ.get("CLEANUP_INTERVAL_SEC", "300"))

# ===== PREWARM / HOT POOL =====
# Architecture: PREWARM EVERYTHING that gets repeatedly created.
# A prewarmed browser+context+tab = instant 1-3s joins (vs 10-15s cold start).
PREWARM_BROWSERS = int(os.environ.get("PREWARM_BROWSERS", "2"))           # hot browsers at boot
PREWARM_CONTEXTS = int(os.environ.get("PREWARM_CONTEXTS", "10"))          # ready contexts standing by
PREWARM_MIN_READY = int(os.environ.get("PREWARM_MIN_READY", "5"))         # auto-warmup floor
PREWARM_MAX_READY = int(os.environ.get("PREWARM_MAX_READY", "20"))        # auto-shrink ceiling
# v8.3: default to the actual JOIN form page (not the homepage) so #meeting-id input
# is already mounted before the task even arrives. Saves ~1.5-2s per join.
PREWARM_PRELOAD_URL = os.environ.get("PREWARM_PRELOAD_URL", "https://app.zoom.us/wc/join").strip()
PREWARM_ENABLED = os.environ.get("PREWARM_ENABLED", "true").lower() == "true"
WARMUP_INTERVAL_SEC = int(os.environ.get("WARMUP_INTERVAL_SEC", "15"))    # how often we top up
SHRINK_IDLE_SEC = int(os.environ.get("SHRINK_IDLE_SEC", "120"))           # close idle hot browser after

# v8.3: ===== PERSISTENT PROFILE — disk cache + baked storage_state =====
# Persistent disk cache makes Chromium re-use Zoom SDK js/css across browser
# restarts AND across all contexts in the pool. Single shared dir is safe because
# Chromium serialises writes per-profile.
PERSISTENT_CACHE = os.environ.get("PERSISTENT_CACHE", "true").lower() == "true"
PERSISTENT_CACHE_DIR = os.environ.get("PERSISTENT_CACHE_DIR", "/tmp/zoom-disk-cache")
PERSISTENT_CACHE_SIZE_MB = int(os.environ.get("PERSISTENT_CACHE_SIZE_MB", "256"))
# Storage state = cookies + localStorage snapshot taken once during bootstrap.
# Loaded into EVERY new BrowserContext so cookie banner is pre-dismissed,
# "Join Audio" popup is suppressed, and Zoom locale + consent flags are set.
STORAGE_STATE_PATH = os.environ.get("STORAGE_STATE_PATH", "/tmp/zoom-storage-state.json")
STORAGE_STATE_REFRESH_HOURS = int(os.environ.get("STORAGE_STATE_REFRESH_HOURS", "24"))
FORM_PREWARM_WAIT_MS = int(os.environ.get("FORM_PREWARM_WAIT_MS", "1500"))  # max wait for #meeting-id mount

# v8.3.3: ===== match prewarm pool size to admin's capacity_max =====
# When ON, the worker grows its READY context pool to equal whatever
# capacity_max admin sets in dashboard. e.g. admin sets 50 -> 50 prewarmed
# tabs sit waiting; admin lowers to 20 -> we shrink to 20.
# v8.7: PREWARM_HARD_CEILING 120 → 400 so 16-core boxes can prewarm
# enough tabs for 400+ bot tasks without falling back to cold spawn.
PREWARM_MATCH_ADMIN_CAP = os.environ.get("PREWARM_MATCH_ADMIN_CAP", "true").lower() == "true"
PREWARM_HARD_CEILING = int(os.environ.get("PREWARM_HARD_CEILING", "400"))  # never more than this many ready contexts
# v8.3.1: when a join fails, dump page HTML + screenshot + URL to /tmp so we can
# tune selectors against the EXACT Zoom WebClient build the user is hitting.
DEBUG_DUMP_DOM = os.environ.get("DEBUG_DUMP_DOM", "true").lower() == "true"
DEBUG_DUMP_DIR = os.environ.get("DEBUG_DUMP_DIR", "/tmp/zoom-debug")

# ============================================================================
# KNOWN ZOOM SELECTORS — tried in order, first hit wins. Add new variants here
# whenever Zoom ships a WebClient update. NEVER remove the old ones; older
# Zoom builds in private clouds (zoomgov, china) may still use them.
# ============================================================================
ZOOM_SELECTORS = {
    "name_input": [
        "#input-for-name",         # standard wc/{id}/join
        "#inputname",              # legacy
        "input[name='inputname']",
        "input[aria-label*='name' i]",
        "input[placeholder*='Your Name' i]",
        "input[placeholder*='name' i][type='text']",
    ],
    "password_input": [
        "#input-for-pwd",
        "#inputpasscode",
        "input[name='inputpasscode']",
        "input[type='password']",
        "input[aria-label*='passcode' i]",
        "input[aria-label*='password' i]",
        "input[placeholder*='password' i]",
        "input[placeholder*='passcode' i]",
    ],
    "join_button": [
        "button.preview-join-button",
        "button#joinBtn",
        "button[type='submit']:has-text('Join')",
        "button:has-text('Join'):not(:has-text('Audio'))",
        "button[aria-label='Join']",
        "button.zm-btn--primary:has-text('Join')",
    ],
    "in_meeting": [
        ".meeting-app",
        ".meeting-client",
        ".footer__leave-btn",
        "button[aria-label*='leave' i]",
        "button[aria-label*='mute my microphone' i]",
        "button[aria-label*='unmute my microphone' i]",
        "[class*='meeting-info']",
    ],
    "audio_join": [
        "button.join-audio-by-voip__join-btn",
        "button:has-text('Join Audio by Computer')",
        "button:has-text('Computer Audio')",
        "button[aria-label*='audio' i][aria-label*='computer' i]",
    ],
    # v8.6: HOST-ENDED-MEETING detection. STRICTER selectors in v9.0 to
    # avoid FALSE POSITIVES (the old "Meeting ended" generic text was matching
    # random Zoom chat / footer / help text and falsely killing every bot,
    # cascading into premature task completion). Now we only match Zoom's
    # exact end-of-meeting page markers + specific button labels.
    "meeting_ended": [
        # Zoom-specific end-of-meeting class names (multiple builds)
        "[class*='end-meeting-window']",
        "[class*='zm-meeting-end']",
        "[class*='meeting-end-page']",
        "[class*='post-meeting']",
        ".zm-modal-legacy-body:has-text('host has ended this meeting')",
        ".zm-modal-body:has-text('host has ended this meeting')",
        # Specific full-phrase matches only (NO generic "Meeting ended")
        "div:has-text('This meeting has been ended by host')",
        "div:has-text('The host has ended this meeting')",
        # NOTE: 'You have been removed from this meeting' was REMOVED from this
        # list intentionally. That message fires when a SINGLE bot is kicked
        # (Zoom anti-bot) — it is NOT a host-ended-meeting signal. The drop
        # detection in run_bot() handles this case via the rejoin loop so the
        # bot keeps trying until the meeting actually ends or hold_seconds
        # elapses. (User report 2026-06-22: "bots disappearing without cancel".)
    ],
    # v8.3.4: PREVIEW SCREEN toggles (before clicking Join) — these are different
    # from the in-meeting mute/camera buttons. Zoom shows them on /wc/{id}/join
    # next to the name input. Multiple Zoom builds use different DOM.
    "preview_mute_audio": [
        "#preview-audio-control-button",
        "button#preview-audio-button",
        "button[aria-label='Mute']",
        "button[aria-label*='join with audio off' i]",
        "button[aria-label*='audio off' i]",
        "button[title*='Mute' i]",
        # checkbox style on some builds:
        "input#wc_join_audio_no",
        "input[name='join-audio']",
    ],
    "preview_stop_video": [
        "#preview-video-control-button",
        "button#preview-video-button",
        "button[aria-label='Stop Video']",
        "button[aria-label*='join with video off' i]",
        "button[aria-label*='video off' i]",
        "button[title*='Stop Video' i]",
        "input#wc_join_video_no",
        "input[name='join-video']",
    ],
    # Visual indicators that the toggle is ALREADY in the "off" state, so we
    # don't accidentally click and re-enable mic/cam.
    "preview_audio_is_off_hint": [
        "button[aria-label*='unmute' i]",
        "button[aria-label*='audio is muted' i]",
        ".preview-audio-control-button--off",
    ],
    "preview_video_is_off_hint": [
        "button[aria-label*='start video' i]",
        "button[aria-label*='video is off' i]",
        ".preview-video-control-button--off",
    ],
    "form_ready_any": [   # ANY of these = join form has mounted
        "#input-for-name", "#input-for-pwd", "#inputname",
        "#join-confno", "input[name='confno']",
        "button.preview-join-button", "button#joinBtn",
    ],
    # v8.4 + v8.5: ===== REACTIONS BUTTON / EMOJI PICKER =====
    # Zoom Web Client renders a "Reactions" button in the meeting footer. Once
    # clicked, an emoji picker appears with thumbs-up, heart, laugh, clap,
    # surprise, etc. We try multiple selectors because Zoom releases (v2 vs
    # v3 web SDK + 2026 redesign) render the DOM very differently.
    "reactions_button": [
        # 2026 Zoom Web Client (v3 SDK)
        "button[aria-label*='Reactions' i]",
        "button[aria-label*='reaction' i]",
        "[data-tooltip-id*='reaction' i]",
        "[data-testid*='reaction' i]",
        "button[id*='reaction' i]",
        # Legacy v1/v2 builds
        "button.footer-button__button[aria-label*='reaction' i]",
        ".footer-button-reactions",
        ".footer-button__button-label:has-text('Reactions')",
        # "More" overflow on narrow viewports — reactions sometimes nested inside
        "button[aria-label*='More meeting controls' i]",
        "button[aria-label='More']",
    ],
    # Emoji buttons inside the popup. Order matches our REACTION_EMOJI_LABELS
    # list for random picking. The aria-label varies by Zoom build, so we
    # match by leading text.
    "reaction_emoji_any": [
        # 2026 — emoji popup uses role="menuitem" or role="button" with emoji char in aria-label
        "div[role='menuitem'][aria-label*='clap' i]",
        "div[role='menuitem'][aria-label*='thumbs up' i]",
        "div[role='menuitem'][aria-label*='heart' i]",
        "div[role='menuitem'][aria-label*='joy' i]",
        "div[role='menuitem']",
        "button[aria-label='Clap']",
        "button[aria-label='Thumbs up']",
        "button[aria-label='Heart']",
        "button[aria-label='Joy']",
        "button[aria-label*='clap' i]",
        "button[aria-label*='thumbs up' i]",
        "button[aria-label*='heart' i]",
        "button[aria-label*='laugh' i]",
        "button[aria-label*='joy' i]",
        "button[aria-label*='surprise' i]",
        "button[aria-label*='party' i]",
        "button[aria-label*='tada' i]",
        "button[aria-label*='fire' i]",
        ".emoji-item",            # generic class on older builds
        ".reaction-emoji-item",
        ".reaction-menu__emoji-item",
        ".emoji-mart-emoji",
    ],
}

# ============================================================================
# v8.5: ===== NUCLEAR MEDIA KILL SWITCH =====
# Init script injected into EVERY page BEFORE any Zoom JS runs. It hijacks
# browser-level media APIs so even if Zoom's UI claims the mic/camera is "on",
# no actual audio/video bytes can ever leave the browser. This is the bullet-
# proof guarantee of OFF mic + OFF cam regardless of Zoom WebClient DOM state.
#
# How it works:
#   - navigator.mediaDevices.getUserMedia() resolves to a MediaStream whose
#     audio + video tracks are immediately .stop()'d (ended=true). Zoom thinks
#     it has a stream but emits silence + a frozen/empty track.
#   - navigator.mediaDevices.enumerateDevices() returns an EMPTY array → Zoom
#     UI greys out the camera/mic toggles entirely on most builds.
#   - getDisplayMedia (screen share) is similarly neutered.
# ============================================================================
MEDIA_KILL_INIT_SCRIPT = r"""
(() => {
  try {
    const md = navigator.mediaDevices;
    if (!md) return;

    // Helper: build an empty/ended MediaStream
    function emptyStream(constraints) {
      try {
        const tracks = [];
        // Silent audio track via AudioContext + MediaStreamDestination
        if (constraints && constraints.audio) {
          try {
            const ctx = new (window.AudioContext || window.webkitAudioContext)();
            const dst = ctx.createMediaStreamDestination();
            const osc = ctx.createOscillator();
            const gain = ctx.createGain();
            gain.gain.value = 0; // SILENT
            osc.connect(gain).connect(dst);
            osc.start();
            const at = dst.stream.getAudioTracks()[0];
            if (at) {
              try { at.enabled = false; } catch(e){}
              tracks.push(at);
            }
          } catch (e) {}
        }
        // Blank black video track via canvas
        if (constraints && constraints.video) {
          try {
            const canvas = document.createElement('canvas');
            canvas.width = 2; canvas.height = 2;
            const c = canvas.getContext('2d');
            c.fillStyle = '#000'; c.fillRect(0, 0, 2, 2);
            const cs = canvas.captureStream(1); // 1 fps
            const vt = cs.getVideoTracks()[0];
            if (vt) {
              try { vt.enabled = false; } catch(e){}
              tracks.push(vt);
            }
          } catch (e) {}
        }
        const s = new MediaStream(tracks);
        // Immediately disable + stop tracks → zero broadcast
        try { s.getTracks().forEach(t => { try{t.enabled=false;}catch(e){} }); } catch(e){}
        return s;
      } catch (e) {
        return new MediaStream();
      }
    }

    // Override getUserMedia
    const origGUM = md.getUserMedia ? md.getUserMedia.bind(md) : null;
    md.getUserMedia = function(constraints) {
      try {
        console.log('[MEDIA-KILL] getUserMedia intercepted', constraints);
      } catch(e){}
      return Promise.resolve(emptyStream(constraints || {}));
    };

    // Legacy getUserMedia
    try {
      navigator.getUserMedia = function(c, ok, err) {
        try { ok(emptyStream(c || {})); } catch(e) { if (err) err(e); }
      };
      navigator.webkitGetUserMedia = navigator.getUserMedia;
      navigator.mozGetUserMedia = navigator.getUserMedia;
    } catch(e){}

    // Override getDisplayMedia (block screen-share too)
    if (md.getDisplayMedia) {
      md.getDisplayMedia = function() {
        return Promise.resolve(new MediaStream());
      };
    }

    // v8.6: Return FAKE devices so Zoom shows mic + camera icons on bot tile
    // (user wants the icons VISIBLE but in OFF state — looks like a real
    // participant who just muted themselves). Real bytes still never leave
    // because getUserMedia returns silent/blank streams above.
    const origEnum = md.enumerateDevices ? md.enumerateDevices.bind(md) : null;
    md.enumerateDevices = function() {
      return Promise.resolve([
        { deviceId: 'default',           kind: 'audioinput',  label: 'Default - Microphone (Built-in)', groupId: 'grp-mic-1', toJSON(){return this;} },
        { deviceId: 'mic-builtin-1',     kind: 'audioinput',  label: 'Microphone (Built-in)',           groupId: 'grp-mic-1', toJSON(){return this;} },
        { deviceId: 'default',           kind: 'audiooutput', label: 'Default - Speaker (Built-in)',    groupId: 'grp-spk-1', toJSON(){return this;} },
        { deviceId: 'spk-builtin-1',     kind: 'audiooutput', label: 'Speaker (Built-in)',              groupId: 'grp-spk-1', toJSON(){return this;} },
        { deviceId: 'cam-builtin-1',     kind: 'videoinput',  label: 'HD Webcam (Built-in)',            groupId: 'grp-cam-1', toJSON(){return this;} },
      ]);
    };

    // Block any RTCPeerConnection from adding real tracks via addTrack
    try {
      const RPC = window.RTCPeerConnection || window.webkitRTCPeerConnection;
      if (RPC && RPC.prototype && RPC.prototype.addTrack) {
        const origAdd = RPC.prototype.addTrack;
        RPC.prototype.addTrack = function(track, ...streams) {
          try { if (track) track.enabled = false; } catch(e){}
          return origAdd.call(this, track, ...streams);
        };
      }
    } catch(e){}
  } catch (e) {
    try { console.warn('[MEDIA-KILL] init err', e); } catch(_){}
  }
})();
"""

# v8.4: emoji labels we'll cycle through for reactions. Random pick each tick.
REACTION_EMOJI_LABELS = [
    "Clap", "Thumbs up", "Heart", "Joy", "Surprise", "Party popper", "Tada",
]

# ============================================================================
# v9.1 — SCREEN-SHARE SURVIVAL INIT SCRIPT (THE REAL FIX)
# ============================================================================
# Problem: When the host starts screen share, Chromium has to decode an incoming
# H.264 / VP8 video stream. With many bots per box this pegs CPU and the bot
# drops out of the meeting ("members dropping problem").
#
# What was broken before:
#   1. The previous "WebRTC pre-load guard" was installed via
#      `page.add_init_script(...)` AFTER `page.goto(...)`. Per Playwright docs,
#      `add_init_script` only fires on FUTURE navigations — so it NEVER ran for
#      the Zoom meeting page, leaving Chromium to decode incoming video unchecked.
#   2. The old code called `ev.track.stop()` on received video tracks. Stopping
#      a remote receiver track tells Zoom's signaling layer "this receiver is
#      permanently dead" and Zoom can soft-kick the participant. We should ONLY
#      disable the track (`track.enabled = false`) — Chromium then drops the
#      decode pipeline but Zoom still thinks the bot is happily receiving.
#
# Fix:
#   - Install ALL init scripts at the *BrowserContext* level via
#     `context.add_init_script()` BEFORE any `page.goto(...)`. This guarantees
#     the script runs on every page in the context, before any Zoom JS executes.
#   - Use ONLY `enabled = false` (never `.stop()`) so Zoom never sees the
#     receiver as dead.
#   - Also `setParameters({encodings:[{active:false}]})` defensively on
#     senders so even if Zoom ever decides to relay video out, nothing leaves.
#   - Patch `addTransceiver` to set `direction="recvonly"` → `"inactive"` for
#     video so the SDP negotiation never advertises a video receive line.
#     (Best-effort — Zoom may renegotiate; the per-track sweep catches anything
#     that slips through.)
# ============================================================================
SCREEN_SHARE_SURVIVAL_INIT_SCRIPT = r"""
(() => {
  try {
    window.__zk_pcs = window.__zk_pcs || new Set();

    // v9.6 NUCLEAR CSS — hide screen-share + video tiles BEFORE Zoom renders
    // them. Even with SDP video-strip + track.enabled=false, Chromium still
    // composites the <video> / <canvas> Zoom inserts → CPU spike on share.
    // Killing them in CSS = ZERO compositor work = CPU stays flat on share.
    try {
      const style = document.createElement('style');
      style.setAttribute('data-zk', 'nuclear-hide');
      style.textContent = `
        /* Zoom screen-share + video tiles */
        .gallery-video-container, .gallery-video-container__video,
        .speaker-active-container, .speaker-active-container__video,
        .sharee-container, .sharer-container, .screen-share-video,
        .gallery-video-container__main-view, .multi-speaker-view-container,
        .multi-view-container, .video-renderer-container,
        canvas[id*="webclient-render"], canvas[id*="render-canvas"],
        canvas.gallery-video-container__canvas, canvas.video-renderer,
        canvas.screen-share-canvas, canvas[class*="render"],
        video[class*="video-tile"], video[class*="screen-share"],
        div[class*="screen-share"] video, div[class*="screen-share"] canvas,
        .pop-window-content video, .pop-window-content canvas,
        .floating-shared-container, .floating-shared-container video,
        .floating-shared-container canvas
        {
          display: none !important;
          visibility: hidden !important;
          width: 0 !important; height: 0 !important;
          opacity: 0 !important;
          pointer-events: none !important;
        }
        /* Disable any zoom animations / transitions to cut compositor work */
        * { animation: none !important; transition: none !important; }
        /* Avatar / participant tiles can stay (lightweight DOM) */
      `;
      (document.head || document.documentElement).appendChild(style);
      // Also re-inject on any DOM change so Zoom can't undo it
      const reinject = () => {
        if (!document.querySelector('style[data-zk="nuclear-hide"]')) {
          (document.head || document.documentElement).appendChild(style.cloneNode(true));
        }
      };
      try {
        new MutationObserver(reinject).observe(document.documentElement, {childList: true, subtree: false});
      } catch (_) {}
    } catch (_) {}

    function defangVideoTrack(track) {
      try {
        if (track && track.kind === 'video') {
          track.enabled = false;
        }
      } catch (e) {}
    }

    // v9.5 AUDIO defang — same idea as video. Bots are JOIN_WITH_AUDIO_MUTED
    // so they never SEND audio, but Zoom's SFU still relays the host's mic
    // (and every other participant's mic) AS A SUBSCRIPTION to every bot.
    // For 60+ bots that's 60× extra fan-out for the SFU → host's mic packets
    // get queued/glitched ("mic ht rha hai" symptom).
    //
    // Setting receiver.track.enabled=false drops the bot's local audio decode
    // pipeline (zero CPU per bot) and tells the SFU "I'm not actively decoding
    // — feel free to drop my frames" while keeping the signaling track open
    // so Zoom never treats us as having left. Net effect: less SFU work +
    // less bot CPU + stable host mic.
    function defangAudioTrack(track) {
      try {
        if (track && track.kind === 'audio') {
          track.enabled = false;
        }
      } catch (e) {}
    }

    // Also mute any <audio> element Zoom inserts into the DOM — saves the
    // last bit of decode + playback CPU even if some audio frames slip in.
    function muteAudioElements() {
      try {
        document.querySelectorAll('audio,video').forEach((el) => {
          try {
            el.muted = true;
            el.volume = 0;
            // tell the AudioContext sink not to expand the jitter buffer
            if (el.setSinkId) { /* no-op default sink */ }
          } catch (_) {}
        });
      } catch (_) {}
    }
    // Run once + observe for any new media elements Zoom inserts later.
    try {
      muteAudioElements();
      const mo = new MutationObserver(muteAudioElements);
      mo.observe(document.documentElement, { childList: true, subtree: true });
    } catch (_) {}

    function defangPc(pc) {
      try {
        // (a) Mute every existing receiver's video track immediately.
        if (pc.getReceivers) {
          pc.getReceivers().forEach((r) => defangVideoTrack(r.track));
        }
        // (b) v8.6.3 — flip every existing VIDEO transceiver to direction='inactive'.
        //     This is what truly stops Zoom's SFU from sending video packets at
        //     all — including the screen-share stream that lights up Chromium's
        //     H.264/VP8 decoder and causes the CPU spike → soft-kick cascade.
        //     `track.enabled = false` alone only stops RENDERING; the decode
        //     pipeline still runs. Setting direction='inactive' makes the SFU
        //     stop transmitting → zero decode work → bot stays in meeting.
        if (pc.getTransceivers) {
          pc.getTransceivers().forEach((t) => {
            try {
              const isVideo =
                (t.receiver && t.receiver.track && t.receiver.track.kind === 'video') ||
                (t.sender && t.sender.track && t.sender.track.kind === 'video') ||
                // mid hint (Zoom often labels these "video_*" or "1"/"2")
                (typeof t.mid === 'string' && /vid|video/i.test(t.mid));
              if (isVideo && t.direction !== 'inactive') {
                t.direction = 'inactive';
              }
            } catch (e) {}
          });
        }
        // (c) Mute every existing sender's video track AND zero its bitrate.
        if (pc.getSenders) {
          pc.getSenders().forEach((s) => {
            try {
              if (s.track && s.track.kind === 'video') s.track.enabled = false;
              if (s.getParameters && s.setParameters) {
                const p = s.getParameters();
                if (p && p.encodings && p.encodings.length) {
                  p.encodings.forEach((enc) => {
                    enc.active = false;
                    enc.maxBitrate = 1;
                  });
                  s.setParameters(p).catch(() => {});
                }
              }
            } catch (e) {}
          });
        }
      } catch (e) {}
    }

    const Orig = window.RTCPeerConnection;
    if (Orig && !Orig.__zk_patched) {
      const W = function (...a) {
        const pc = new Orig(...a);
        window.__zk_pcs.add(pc);

        // Mute new incoming tracks the instant they arrive.
        pc.addEventListener('track', (ev) => {
          try {
            defangVideoTrack(ev.track);
            defangPc(pc);
          } catch (e) {}
        });

        // Also re-sweep on negotiation events — Zoom renegotiates on
        // screen-share start.
        ['negotiationneeded', 'signalingstatechange', 'iceconnectionstatechange']
          .forEach((ev) => {
            try { pc.addEventListener(ev, () => defangPc(pc)); } catch (e) {}
          });

        // Patch addTransceiver to refuse new VIDEO receive lines.
        try {
          const origAddT = pc.addTransceiver && pc.addTransceiver.bind(pc);
          if (origAddT) {
            pc.addTransceiver = function (trackOrKind, init) {
              try {
                const kind = (typeof trackOrKind === 'string')
                  ? trackOrKind
                  : (trackOrKind && trackOrKind.kind);
                if (kind === 'video') {
                  init = Object.assign({}, init || {}, { direction: 'inactive' });
                }
              } catch (e) {}
              const t = origAddT(trackOrKind, init);
              try {
                if (t && t.receiver) defangVideoTrack(t.receiver.track);
                if (t && t.sender && t.sender.track && t.sender.track.kind === 'video') {
                  t.sender.track.enabled = false;
                }
              } catch (e) {}
              return t;
            };
          }
        } catch (e) {}

        return pc;
      };
      W.prototype = Orig.prototype;
      Object.setPrototypeOf(W, Orig);
      W.__zk_patched = true;
      try { window.RTCPeerConnection = W; } catch (e) {}
      try { window.webkitRTCPeerConnection = W; } catch (e) {}
    }

    // Periodic safety sweep — every 1s walk all PCs and disable any video
    // track that slipped through (e.g. mid-meeting renegotiation).
    setInterval(() => {
      try {
        for (const pc of window.__zk_pcs) defangPc(pc);
      } catch (e) {}
    }, 1000);

    // v8.6.3 — FAST sweep: every 200ms, only call defangPc on PCs whose
    // signaling state recently changed. This catches the brief window
    // between Zoom's "ScreenShare started" SDP renegotiation and our 1s sweep
    // — the exact window in which Chromium's decoder briefly spikes CPU and
    // Zoom soft-kicks the bot.
    setInterval(() => {
      try {
        for (const pc of window.__zk_pcs) {
          try {
            // cheap proxy for "renegotiation in flight": signaling != stable
            if (pc.signalingState && pc.signalingState !== 'stable') {
              defangPc(pc);
            }
          } catch (e) {}
        }
      } catch (e) {}
    }, 200);

    // v8.6.3 — Hook setLocalDescription so we ALSO flip video transceivers
    // to inactive RIGHT BEFORE the SDP answer is sent to Zoom's SFU.
    // This is the deterministic point at which the SFU learns whether to
    // send us video at all. Combined with the patch in defangPc, this
    // guarantees the SFU never gets a "recv video" answer from us.
    try {
      const Orig3 = window.RTCPeerConnection;
      if (Orig3 && Orig3.prototype && !Orig3.prototype.__zk_sld_patched) {
        const origSLD2 = Orig3.prototype.setLocalDescription;
        Orig3.prototype.setLocalDescription = function (desc) {
          try { defangPc(this); } catch (e) {}
          return origSLD2.apply(this, arguments);
        };
        Orig3.prototype.__zk_sld_patched = true;
      }
    } catch (e) {}

    // v8.6.3 — Hook setRemoteDescription to flip transceivers to 'inactive'
    // RIGHT AFTER Zoom's offer arrives but BEFORE createAnswer reads the
    // transceiver state. This makes the bot's outgoing answer SDP advertise
    // "no video receive", causing the SFU to stop transmitting video
    // (incl. screen share) at the protocol level — zero decode CPU.
    try {
      const Orig4 = window.RTCPeerConnection;
      if (Orig4 && Orig4.prototype && !Orig4.prototype.__zk_srd_dir_patched) {
        const origSRD2 = Orig4.prototype.setRemoteDescription;
        Orig4.prototype.setRemoteDescription = function (desc) {
          const self = this;
          const ret = origSRD2.apply(self, arguments);
          // After SRD resolves, all transceivers exist — flip video ones.
          try {
            if (ret && typeof ret.then === 'function') {
              ret.then(() => {
                try { defangPc(self); } catch (e) {}
              }, () => {});
            }
          } catch (e) {}
          return ret;
        };
        Orig4.prototype.__zk_srd_dir_patched = true;
      }
    } catch (e) {}

    // ───── v8.6.4 EXTRA DEFENSES (screen-share survival) ─────
    //
    // (A) getStats() spoof — Zoom's monitor pings `pc.getStats()` to check
    //     if receivers are healthy. When we disable video tracks, the stats
    //     show "bytesReceived stuck" → Zoom flags us as a stalled receiver
    //     and soft-kicks. Workaround: rewrite getStats() so video receiver
    //     reports look healthy (synthesized monotonic counters).
    try {
      const Orig5 = window.RTCPeerConnection;
      if (Orig5 && Orig5.prototype && !Orig5.prototype.__zk_stats_patched) {
        const origGS = Orig5.prototype.getStats;
        Orig5.prototype.getStats = function () {
          const self = this;
          return origGS.apply(self, arguments).then((report) => {
            try {
              if (!report || typeof report.forEach !== 'function') return report;
              // Synthesize ever-growing counters per receiver-id so Zoom
              // sees "video is flowing fine".
              window.__zk_stats_seed = window.__zk_stats_seed || {};
              const now = performance.now();
              report.forEach((stat) => {
                try {
                  if (!stat || typeof stat !== 'object') return;
                  if (stat.type === 'inbound-rtp' && stat.kind === 'video') {
                    const key = stat.id || ('vr' + (stat.ssrc || 0));
                    const seed = window.__zk_stats_seed[key]
                      || (window.__zk_stats_seed[key] = { p: 0, b: 0, t: now });
                    const dt = Math.max(1, now - seed.t);
                    // Pretend ~30 fps @ ~150 kbps so Zoom thinks all is well.
                    seed.p += Math.max(1, Math.round(30 * dt / 1000));
                    seed.b += Math.max(1, Math.round(18750 * dt / 1000));
                    seed.t = now;
                    try { stat.packetsReceived = seed.p; } catch (e) {}
                    try { stat.bytesReceived = seed.b; } catch (e) {}
                    try { stat.framesDecoded = seed.p; } catch (e) {}
                    try { stat.framesReceived = seed.p; } catch (e) {}
                    try { stat.packetsLost = 0; } catch (e) {}
                    try { stat.jitter = 0.01; } catch (e) {}
                  }
                  if (stat.type === 'track' && stat.kind === 'video' && stat.remoteSource) {
                    try { stat.framesReceived = (stat.framesReceived || 0) + 30; } catch (e) {}
                    try { stat.framesDecoded = (stat.framesDecoded || 0) + 30; } catch (e) {}
                    try { stat.framesDropped = 0; } catch (e) {}
                  }
                } catch (e) {}
              });
            } catch (e) {}
            return report;
          });
        };
        Orig5.prototype.__zk_stats_patched = true;
      }
    } catch (e) {}

    // (B) MutationObserver — Zoom dynamically injects <video>/<canvas>/share
    //     tiles into the DOM when a presenter starts sharing. Even if CSS
    //     hides them, the elements are STILL created and the renderer still
    //     allocates GPU/CPU. Remove them at the moment of insertion.
    try {
      const SHARE_HOST_PAT = /shared|share-|share_|screen|presenter|content-share|sharee/i;
      const KILL_TAGS = new Set(['VIDEO', 'CANVAS']);
      const nukeIfShareNode = (n) => {
        try {
          if (!n || n.nodeType !== 1) return;
          if (KILL_TAGS.has(n.tagName)) {
            try { n.pause && n.pause(); } catch (e) {}
            try { n.srcObject = null; } catch (e) {}
            try { n.remove(); } catch (e) {
              try { n.style.display = 'none'; } catch (_) {}
            }
            return;
          }
          const cls = (n.className && n.className.baseVal !== undefined)
            ? n.className.baseVal
            : (typeof n.className === 'string' ? n.className : '');
          if (cls && SHARE_HOST_PAT.test(cls)) {
            try { n.remove(); } catch (e) {
              try { n.style.display = 'none'; } catch (_) {}
            }
          }
        } catch (e) {}
      };
      const startObs = () => {
        if (!document.body || window.__zk_share_mo) return;
        const mo = new MutationObserver((muts) => {
          for (const m of muts) {
            try {
              if (m.addedNodes && m.addedNodes.length) {
                for (const n of m.addedNodes) {
                  nukeIfShareNode(n);
                  // Also walk descendants (Zoom often inserts wrappers)
                  if (n.querySelectorAll) {
                    try {
                      n.querySelectorAll('video, canvas, [class*=share], [class*=screen]')
                        .forEach(nukeIfShareNode);
                    } catch (e) {}
                  }
                }
              }
            } catch (e) {}
          }
        });
        mo.observe(document.body, { childList: true, subtree: true });
        window.__zk_share_mo = mo;
      };
      if (document.body) startObs();
      else document.addEventListener('DOMContentLoaded', startObs, { once: true });
    } catch (e) {}

    // (C) Codec preference downgrade — when Chromium IS forced to decode
    //     (because some transceiver slipped through), prefer the cheapest
    //     codec. Drop H.264 / AV1 / VP9 from the receive list, keep VP8 only.
    //     VP8 is the lightest software decoder, minimising CPU even worst-case.
    try {
      const RtpRx = window.RTCRtpReceiver;
      if (RtpRx && RtpRx.getCapabilities && !RtpRx.__zk_cheap_codec_patched) {
        const origCaps = RtpRx.getCapabilities.bind(RtpRx);
        RtpRx.getCapabilities = function (kind) {
          const c = origCaps(kind);
          try {
            if (c && kind === 'video' && Array.isArray(c.codecs)) {
              const cheap = c.codecs.filter((x) =>
                x && typeof x.mimeType === 'string' &&
                /vp8/i.test(x.mimeType)
              );
              if (cheap.length) c.codecs = cheap;
            }
          } catch (e) {}
          return c;
        };
        RtpRx.__zk_cheap_codec_patched = true;
      }
    } catch (e) {}

    // (D) Idle CPU heartbeat — every 2s, force a microtask cycle so the
    //     event loop never appears "stuck" to Zoom's watchdog. This is a
    //     belt-and-braces defense; matters most on cheap RDPs where a
    //     CPU spike can starve JS for 1-2 seconds and Zoom flags the
    //     client as "not responsive".
    try {
      setInterval(() => {
        try {
          // No-op work that the JIT can't elide
          window.__zk_hb = (window.__zk_hb || 0) + 1;
        } catch (e) {}
      }, 2000);
    } catch (e) {}

    // Defensive: hide ALL existing + future <video> / <canvas> elements via CSS
    // so even if a frame slips through, the renderer never paints it.
    try {
      const installHideStyle = () => {
        if (document.getElementById('__zk_hide_style')) return;
        if (!document.head) return;
        const s = document.createElement('style');
        s.id = '__zk_hide_style';
        s.textContent = `
          video, canvas,
          .video-tile, .video-container, .gallery-video-container,
          .gallery-video-container__main-view, .speaker-active-container,
          .shared-container, .share-content, .share-container,
          .sharee-container-frame, .sharing-screen, .screen-share-container,
          [class*='shared-tile'], [class*='screen-share'],
          [class*='gallery-video'], [class*='speaker-view'],
          [class*='shareView'], [class*='ShareView'],
          .full-screen-widget, .fixed-active-speaker__video-view
          { display: none !important; visibility: hidden !important;
            width: 0 !important; height: 0 !important; }
        `;
        document.head.appendChild(s);
      };
      // Try right now; if <head> isn't ready yet, retry on DOMContentLoaded.
      installHideStyle();
      document.addEventListener('DOMContentLoaded', installHideStyle, { once: true });
    } catch (e) {}

    // ─── v9.4 NUCLEAR LAYER: HTMLMediaElement & SDP-level video blocking ───
    // Even if a video track somehow gets through all our patches, these layers
    // prevent the browser from EVER decoding it:
    //   (a) HTMLVideoElement.play() returns a rejected Promise → no decode.
    //   (b) HTMLMediaElement.srcObject setter swallows any MediaStream → no pipe.
    //   (c) RTCPeerConnection.setRemoteDescription strips m=video lines from
    //       Zoom's SDP offer/answer → SFU literally won't send us any video
    //       packets at all. Saves bandwidth AND CPU on weak RDPs.
    try {
      const Vproto = HTMLVideoElement.prototype;
      const origPlay = Vproto.play;
      Vproto.play = function () {
        // Refuse to play; resolves with NotAllowedError so Zoom JS continues.
        try { this.pause && this.pause(); } catch (e) {}
        return Promise.reject(new DOMException('blocked-by-zk', 'NotAllowedError'));
      };
      // Disable autoplay attribute changes.
      try {
        Object.defineProperty(Vproto, 'autoplay', {
          get: function () { return false; },
          set: function () { /* no-op */ },
        });
      } catch (e) {}
    } catch (e) {}
    try {
      const Mproto = HTMLMediaElement.prototype;
      const origDesc = Object.getOwnPropertyDescriptor(Mproto, 'srcObject');
      if (origDesc && origDesc.set) {
        const origSet = origDesc.set;
        Object.defineProperty(Mproto, 'srcObject', {
          get: origDesc.get,
          set: function (stream) {
            // If it's a MediaStream with video tracks, strip them first.
            try {
              if (stream && stream.getVideoTracks) {
                stream.getVideoTracks().forEach((t) => {
                  try { t.enabled = false; } catch (e) {}
                  try { stream.removeTrack(t); } catch (e) {}
                });
              }
            } catch (e) {}
            return origSet.call(this, stream);
          },
          configurable: true,
        });
      }
    } catch (e) {}
    // SDP video-strip — the most powerful single layer BUT risky on some
    // Zoom builds (can break the SDP negotiation entirely). OFF by default;
    // enable on a problem-RDP only by setting env ZK_NUCLEAR_SDP_STRIP=true
    // before starting the worker. The flag is injected via window.__zk_cfg.
    if (window.__zk_cfg && window.__zk_cfg.nuclear_sdp_strip === true) {
      try {
      const Orig2 = window.RTCPeerConnection;
      if (Orig2 && Orig2.prototype && !Orig2.prototype.__zk_sdp_patched) {
        const origSRD = Orig2.prototype.setRemoteDescription;
        const origSLD = Orig2.prototype.setLocalDescription;
        function stripVideo(sdp) {
          try {
            if (!sdp || typeof sdp !== 'string') return sdp;
            const lines = sdp.split(/\r?\n/);
            const out = [];
            let inVideo = false;
            for (let i = 0; i < lines.length; i++) {
              const ln = lines[i];
              if (ln.indexOf('m=video') === 0) {
                inVideo = true;
                // Force port 0 (= section disabled per RFC 3264).
                const parts = ln.split(' ');
                if (parts.length >= 2) parts[1] = '0';
                out.push(parts.join(' '));
                continue;
              }
              if (ln.indexOf('m=') === 0 && inVideo) inVideo = false;
              if (inVideo) {
                // Keep only mid + connection lines; drop codecs/extensions/ssrc.
                if (ln.indexOf('a=mid') === 0 || ln.indexOf('c=') === 0) {
                  out.push(ln);
                }
                continue;
              }
              out.push(ln);
            }
            return out.join('\r\n');
          } catch (e) { return sdp; }
        }
        Orig2.prototype.setRemoteDescription = function (desc) {
          try {
            if (desc && desc.sdp) {
              const newSdp = stripVideo(desc.sdp);
              desc = (typeof RTCSessionDescription !== 'undefined')
                ? new RTCSessionDescription({ type: desc.type, sdp: newSdp })
                : { type: desc.type, sdp: newSdp };
            }
          } catch (e) {}
          return origSRD.apply(this, [desc]);
        };
        // Outgoing: also strip video so we never advertise a video send.
        Orig2.prototype.setLocalDescription = function (desc) {
          try {
            if (desc && desc.sdp) {
              const newSdp = stripVideo(desc.sdp);
              desc = (typeof RTCSessionDescription !== 'undefined')
                ? new RTCSessionDescription({ type: desc.type, sdp: newSdp })
                : { type: desc.type, sdp: newSdp };
            }
          } catch (e) {}
          return origSLD.apply(this, [desc]);
        };
        Orig2.prototype.__zk_sdp_patched = true;
      }
      } catch (e) {}
    }
  } catch (e) {
    try { console.warn('[zk-screen-share-survival] init err', e); } catch (_) {}
  }
})();
"""


# Stealth shim — same as the inline one inside _join_meeting but now installed
# at the context level so it runs BEFORE any Zoom JS on every page.
STEALTH_INIT_SCRIPT = (
    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    "Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});"
    "Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});"
)


# ============================================================================
# v9.2 — ANTI-LEAVE GUARD (force the bot to STAY in the meeting)
# ============================================================================
# User explicit ask:
#   "use fix kro re join and forcefully mitting me roko but rkkho members
#    mitting me"  → fix it with re-join AND forcefully keep members IN the
#    meeting.
#
# This script makes it impossible for the bot to leave the meeting via any
# JS-driven path. Only a REAL host-ended-meeting event can clean up the bot;
# everything else is blocked:
#
#   1. window.close() neutralised.
#   2. window.location.assign / replace / href to non-zoom.us URLs blocked.
#   3. beforeunload prompts suppressed so the page can't tear itself down.
#   4. Capture-phase click/mousedown guard: any click on a Leave / End Meeting
#      / Exit Meeting button is intercepted and cancelled. Bot becomes
#      "non-leavable" from inside Zoom's own UI.
#   5. MutationObserver auto-dismisses any "Are you sure you want to leave?"
#      confirm modal by clicking the Cancel / Stay button.
#   6. Periodic 4s sweep — belt-and-suspenders in case the observer misses
#      a fast-mounted modal.
# ============================================================================
ANTI_LEAVE_INIT_SCRIPT = r"""
(() => {
  try {
    // (1) Kill window.close so Zoom can't tear down the tab.
    try { window.close = function () { return false; }; } catch (e) {}

    // (2) Block navigation away from zoom.us domains.
    try {
      const loc = window.location;
      const _assign = loc.assign && loc.assign.bind(loc);
      const _replace = loc.replace && loc.replace.bind(loc);
      const isZoomUrl = (u) => {
        try { return String(u).indexOf('zoom.us') !== -1; } catch (e) { return false; }
      };
      if (_assign) {
        loc.assign = function (u) { if (isZoomUrl(u)) _assign(u); };
      }
      if (_replace) {
        loc.replace = function (u) { if (isZoomUrl(u)) _replace(u); };
      }
    } catch (e) {}

    // (3) Suppress beforeunload prompts so nothing can interrupt our session.
    window.addEventListener('beforeunload', function (ev) {
      try { ev.stopImmediatePropagation(); } catch (e) {}
      try { ev.preventDefault(); } catch (e) {}
      try { delete ev['returnValue']; } catch (e) {}
    }, true);

    // (4) Capture-phase click/mousedown guard for Leave / End-Meeting buttons.
    const LEAVE_PAT = /\b(leave|end\s*meeting|leave\s*meeting|exit\s*meeting)\b/i;
    function isLeaveTarget(el) {
      if (!el) return false;
      let cur = el;
      for (let i = 0; i < 6 && cur; i++) {
        try {
          const lbl = (cur.getAttribute && (cur.getAttribute('aria-label') || '')) || '';
          const txt = (cur.innerText || cur.textContent || '').slice(0, 60);
          if (LEAVE_PAT.test(lbl) || LEAVE_PAT.test(txt)) return true;
          const cls = (cur.className && cur.className.toString && cur.className.toString()) || '';
          if (/footer__leave-btn|leave-meeting|footer-button__leave/i.test(cls)) return true;
        } catch (e) {}
        cur = cur.parentElement;
      }
      return false;
    }
    ['click', 'mousedown', 'pointerdown', 'touchstart'].forEach(function (evt) {
      document.addEventListener(evt, function (ev) {
        try {
          if (isLeaveTarget(ev.target)) {
            ev.stopImmediatePropagation();
            ev.preventDefault();
          }
        } catch (e) {}
      }, true);
    });
    // Also kill keyboard shortcuts that trigger leave (Alt+Q in Zoom WC).
    document.addEventListener('keydown', function (ev) {
      try {
        if (ev.altKey && (ev.key === 'q' || ev.key === 'Q')) {
          ev.stopImmediatePropagation();
          ev.preventDefault();
        }
      } catch (e) {}
    }, true);

    // (5) MutationObserver — auto-dismiss any leave-confirm modal by clicking
    //     the Cancel / Stay / No button.
    function dismissLeaveModal(root) {
      try {
        const buttons = (root || document).querySelectorAll('button');
        let stayBtn = null, leaveBtn = null;
        buttons.forEach(function (b) {
          const t = (b.innerText || '').trim().toLowerCase();
          if (!t) return;
          if (t === 'cancel' || t === 'stay' || t === 'no' || t === 'stay in meeting' || t === 'dismiss') {
            stayBtn = stayBtn || b;
          }
          if (t === 'leave' || t === 'leave meeting' || t === 'end meeting' || t === 'yes' || t === 'end for all') {
            leaveBtn = b;
          }
        });
        if (stayBtn && leaveBtn) {
          // Leave-confirm modal is up — click Stay (or press Escape).
          try { stayBtn.click(); } catch (e) {
            try { document.activeElement && document.activeElement.blur(); } catch (e2) {}
          }
        } else if (leaveBtn && !stayBtn) {
          // Only a Leave button visible without a Stay → press Escape to close.
          try {
            const ev = new KeyboardEvent('keydown', { key: 'Escape', code: 'Escape', bubbles: true });
            document.dispatchEvent(ev);
          } catch (e) {}
        }
      } catch (e) {}
    }
    try {
      const mo = new MutationObserver(function (muts) {
        for (const m of muts) {
          for (const n of m.addedNodes) {
            if (n && n.nodeType === 1) dismissLeaveModal(n);
          }
        }
      });
      const start = () => {
        try { mo.observe(document.documentElement || document.body,
                         { childList: true, subtree: true }); } catch (e) {}
      };
      if (document.documentElement) start();
      else document.addEventListener('DOMContentLoaded', start, { once: true });
    } catch (e) {}

    // (6) Periodic sweep — every 4s, scan document for a leave-confirm modal.
    setInterval(function () { dismissLeaveModal(document); }, 4000);
  } catch (e) {
    try { console.warn('[zk-anti-leave] init err', e); } catch (_) {}
  }
})();
"""


# ============================================================================
# v9.7 — SILENT-UI INIT SCRIPT (user request: "koi bhi activity na dikhe meeting ki
#         bss mic and video ke sath connect ho")
# ============================================================================
# Hides EVERY non-essential Zoom WebClient panel/popup so the bot user-session
# shows a clean meeting shell with ONLY the mic / camera footer icons. Hidden:
#   • Chat panel + chat popup notifications
#   • Participants panel + raise-hand popups
#   • Reactions overlay (floating emojis from OTHER participants)
#   • Q&A, polls, breakouts, whiteboard, immersive view
#   • Recording / encryption / cloud-record indicators
#   • Top toolbar (meeting topic, info, security, view selector)
#   • Bottom-bar text (just keep the icons themselves)
#   • Modals / banners / toasts / tooltips
#   • Screen-share viewer (already covered by SCREEN_SHARE_SURVIVAL CSS but
#     re-asserted here so a single layer change in Zoom can't break stealth)
#
# Opt-out: set ZK_HIDE_UI=false in .env. Default: ON.
# ============================================================================
HIDE_UI_INIT_SCRIPT = r"""
(() => {
  try {
    if (window.__zk_cfg && window.__zk_cfg.hide_ui === false) return;

    const installStyle = () => {
      if (document.getElementById('__zk_silent_ui')) return;
      if (!document.head && !document.documentElement) return;
      const s = document.createElement('style');
      s.id = '__zk_silent_ui';
      s.textContent = `
        /* ─── Chat panel + chat popups ─── */
        #chat, #wc-chat, #chat-container, #chat-content,
        .chat-container, .chat-virtuoso, .chat-window,
        [class*="chat-panel"], [class*="ChatPanel"],
        [class*="chat-popup"], [class*="ChatPopup"],
        [aria-label*="chat" i], [data-tooltip-id*="chat" i],
        /* ─── Participants panel + raise-hand popups ─── */
        #participants, #wc-participants, .participants-section-container,
        .participants-panel, [class*="participants-panel"],
        [class*="ParticipantsPanel"], [class*="participants-popup"],
        [aria-label*="participants" i], [data-tooltip-id*="participants" i],
        /* ─── Reactions overlay (FROM other participants) ─── */
        .reaction-emoji-render, .emoji-reaction-animations,
        .floating-emoji, .floating-emoji-layer, .floating-emoji-container,
        .floating-reactions, [class*="floating-emoji"], [class*="emoji-anim"],
        [class*="reaction-pop"], [class*="reaction-animation"],
        .raise-hand-animation, .reaction-tray,
        /* ─── Q&A, polls, breakouts, whiteboard, immersive view ─── */
        [class*="question-answer"], [class*="QnA"], [class*="qa-panel"],
        [class*="polls-panel"], [class*="PollsPanel"],
        [class*="breakout-panel"], [class*="BreakoutPanel"],
        [class*="whiteboard"], [class*="Whiteboard"],
        [class*="immersive"], [class*="Immersive"],
        /* ─── Recording / encryption / network indicators in top bar ─── */
        .recording-icon, .meeting-info-icon, .encryption-state,
        .meeting-info-container, .meeting-app, .meeting-app-container,
        .meeting-info-banner, [class*="recording-indicator"],
        [class*="network-warning"], [class*="poor-connection"],
        /* ─── Top toolbar (topic + view selector + info) ─── */
        .meeting-title, .meeting-header, .meeting-header-container,
        .meeting-info, .meeting-info-button, .more-options-tooltip,
        [class*="speaker-bar"], [class*="view-toggle"],
        [class*="MeetingHeader"], [class*="meeting-topic"],
        /* ─── Bottom-bar TEXT/labels (keep the icons themselves) ─── */
        .footer-button__button-label,
        /* ─── Modals, banners, toasts, tooltips ─── */
        .zm-modal, .zm-modal-container, .zm-modal-mask,
        .toast-container, .toast-message, .zmu-toast,
        .tooltip-content, .tooltip-inner, .ant-tooltip,
        [role="tooltip"], [role="dialog"]:not(.preview-dialog),
        [class*="invite-link"], [class*="InviteLink"],
        [class*="bubble-banner"], [class*="BubbleBanner"],
        [class*="alert-banner"], [class*="security-banner"],
        /* ─── Footer overflow More-menu, Apps, Closed Captions ─── */
        [class*="more-button"], [class*="MoreButton"],
        [class*="apps-button"], [class*="AppsButton"],
        [class*="closed-caption"], [class*="ClosedCaption"]
        {
          display: none !important;
          visibility: hidden !important;
          opacity: 0 !important;
          pointer-events: none !important;
          width: 0 !important; height: 0 !important;
        }
        /* Footer-bar wrapper itself stays so mic/cam buttons remain (for the
           OWN bot self-view feel). But strip its background + extras. */
        .footer, .footer-container, [class*="footer-bar"] {
          background: transparent !important;
        }
      `;
      (document.head || document.documentElement).appendChild(s);
    };

    installStyle();
    // Re-inject if Zoom strips the style on view changes / SPA route updates
    try {
      new MutationObserver(() => {
        if (!document.getElementById('__zk_silent_ui')) installStyle();
      }).observe(document.documentElement, { childList: true, subtree: false });
    } catch (_) {}

    // Slam shut any side panels Zoom auto-opens on join (chat, participants).
    const closeOpenPanels = () => {
      try {
        document.querySelectorAll(
          'button[aria-label*="close" i][aria-label*="chat" i],' +
          'button[aria-label*="close" i][aria-label*="participants" i],' +
          'button[aria-label*="close" i][aria-label*="reactions" i]'
        ).forEach((b) => { try { b.click(); } catch(_){} });
      } catch (_) {}
    };
    setInterval(closeOpenPanels, 2500);

    // Hard-disable Notification API so Zoom can't pop browser-level toasts
    try {
      if (window.Notification) {
        window.Notification.requestPermission = () => Promise.resolve('denied');
        Object.defineProperty(window.Notification, 'permission',
          { get: () => 'denied' });
      }
    } catch (_) {}
  } catch (e) {
    try { console.warn('[zk-hide-ui] init err', e); } catch (_) {}
  }
})();
"""


async def _install_context_init_scripts(ctx) -> None:
    """Install ALL init scripts on a BrowserContext.

    v9.7 — Adds silent-UI init script (hide chat / participants / reactions /
    modals / banners / toolbars). Bot enters meeting and user sees ONLY the
    mic + camera footer icons. Combined with JOIN_WITH_AUDIO_MUTED=false +
    JOIN_WITH_VIDEO_OFF=false, every bot LOOKS connected with mic + cam
    while actually transmitting silent + black streams.

    Order matters:
      0. config flags     (controls opt-in nuclear features below)
      1. media-kill       (silent mic + black cam)
      2. stealth          (hide automation markers)
      3. screen-share survival + nuclear video block (defang remote video → no decode)
      4. anti-leave       (block Leave button, leave-confirm modal, window.close)
      5. hide-UI          (v9.7 — silent meeting view, no activity visible)

    All scripts are installed via `context.add_init_script` so they run on
    every page in the context BEFORE any Zoom JS executes — including the
    very first navigation.
    """
    # v9.4: expose env-controlled flags to all scripts via window.__zk_cfg
    # v8.6.5: SDP video-strip is now ON by default (was opt-in via env).
    # The previous v8.6.4 stack (transceiver-inactive + getStats spoof +
    # DOM nuke + VP8-only codec) handles 95%+ cases, but on weak RDPs the
    # SFU's screen-share offer can still briefly negotiate a video stream
    # before our transceiver flip lands. Stripping m=video from the SDP
    # itself makes the negotiation FAIL at the protocol level — the SFU
    # records "no video subscription" and literally never sends a packet.
    # Override with `ZK_NUCLEAR_SDP_STRIP=false` if you ever need to disable.
    _env = os.environ.get("ZK_NUCLEAR_SDP_STRIP", "true").lower()
    nuclear_sdp_strip = _env not in ("0", "false", "no", "off")
    _env_ui = os.environ.get("ZK_HIDE_UI", "true").lower()
    hide_ui = _env_ui not in ("0", "false", "no", "off")
    cfg_script = (
        "window.__zk_cfg = window.__zk_cfg || {};"
        f"window.__zk_cfg.nuclear_sdp_strip = {str(nuclear_sdp_strip).lower()};"
        f"window.__zk_cfg.hide_ui = {str(hide_ui).lower()};"
    )
    try:
        await ctx.add_init_script(cfg_script)
    except Exception as e:
        log.debug(f"add_init_script(cfg) failed: {e}")

    for src in (MEDIA_KILL_INIT_SCRIPT, STEALTH_INIT_SCRIPT,
                SCREEN_SHARE_SURVIVAL_INIT_SCRIPT, ANTI_LEAVE_INIT_SCRIPT,
                HIDE_UI_INIT_SCRIPT):
        try:
            await ctx.add_init_script(src)
        except Exception as e:
            log.debug(f"add_init_script failed: {e}")





async def _premute_on_preview(page) -> dict:
    """v8.3.4: on the Zoom preview/join screen, toggle the mic + camera to OFF
    BEFORE the bot clicks Join. So the bot enters the meeting silently with
    no video. Returns {audio_muted, video_off} flags. Idempotent — checks
    for "already off" hints before clicking so we don't re-enable."""
    result = {"audio_muted": False, "video_off": False}

    # === MUTE MIC ===
    if JOIN_WITH_AUDIO_MUTED:
        try:
            # Already off?
            already_off = False
            for sel in ZOOM_SELECTORS["preview_audio_is_off_hint"]:
                try:
                    if await page.locator(sel).count() > 0:
                        already_off = True
                        break
                except Exception:
                    continue
            if already_off:
                result["audio_muted"] = True
            else:
                # Try checkbox-style first (no DOM disturbance), then button toggles
                done = False
                for sel in ZOOM_SELECTORS["preview_mute_audio"]:
                    try:
                        loc = page.locator(sel).first
                        if await loc.count() == 0:
                            continue
                        tag = (await loc.evaluate("e => e.tagName") or "").lower()
                        if tag == "input":
                            await loc.check(timeout=1500)
                        else:
                            await loc.click(timeout=1500)
                        done = True
                        break
                    except Exception:
                        continue
                result["audio_muted"] = done
        except Exception:
            pass

    # === STOP VIDEO ===
    if JOIN_WITH_VIDEO_OFF:
        try:
            already_off = False
            for sel in ZOOM_SELECTORS["preview_video_is_off_hint"]:
                try:
                    if await page.locator(sel).count() > 0:
                        already_off = True
                        break
                except Exception:
                    continue
            if already_off:
                result["video_off"] = True
            else:
                done = False
                for sel in ZOOM_SELECTORS["preview_stop_video"]:
                    try:
                        loc = page.locator(sel).first
                        if await loc.count() == 0:
                            continue
                        tag = (await loc.evaluate("e => e.tagName") or "").lower()
                        if tag == "input":
                            await loc.check(timeout=1500)
                        else:
                            await loc.click(timeout=1500)
                        done = True
                        break
                    except Exception:
                        continue
                result["video_off"] = done
        except Exception:
            pass

    return result


async def _wait_any(page, selectors: List[str], timeout_ms: int = 10_000):
    """Race multiple selectors — whichever appears first wins. Returns the
    locator that matched, or None on timeout. Eliminates blind `wait_for_timeout`.
    """
    deadline = time.time() + (timeout_ms / 1000.0)
    poll = 0.10
    while time.time() < deadline:
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    return loc
            except Exception:
                pass
        await asyncio.sleep(poll)
        poll = min(0.30, poll * 1.3)
    return None


async def _smart_fill(page, selectors: List[str], value: str, timeout_ms: int = 10_000) -> bool:
    """Try every selector in order — first one that's visible gets filled. Uses
    JS `.value` set + dispatch input/change for max speed (skips per-key delay)."""
    loc = await _wait_any(page, selectors, timeout_ms)
    if loc is None:
        return False
    try:
        # Fast path: JS value set + event dispatch (~50ms vs ~300ms for .type())
        handle = await loc.element_handle()
        if handle:
            await handle.evaluate("""
                (el, v) => {
                    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                    setter.call(el, v);
                    el.dispatchEvent(new Event('input', {bubbles:true}));
                    el.dispatchEvent(new Event('change', {bubbles:true}));
                }
            """, value)
            return True
        await loc.fill(value, timeout=3000)
        return True
    except Exception:
        try:
            await loc.fill(value, timeout=3000)
            return True
        except Exception:
            return False


async def _smart_click(page, selectors: List[str], timeout_ms: int = 10_000) -> bool:
    loc = await _wait_any(page, selectors, timeout_ms)
    if loc is None:
        return False
    try:
        await loc.click(timeout=3000)
        return True
    except Exception:
        try:
            handle = await loc.element_handle()
            if handle:
                await handle.evaluate("el => el.click()")
                return True
        except Exception:
            pass
        return False


async def _debug_dump(page, stage: str, name: str):
    """Dump page URL + HTML + screenshot when a join fails so we can tune
    selectors against the EXACT Zoom build hit. Triggered only if DEBUG_DUMP_DOM=true.
    Filenames printed to log so user can `tail` and share."""
    if not DEBUG_DUMP_DOM:
        return
    try:
        os.makedirs(DEBUG_DUMP_DIR, exist_ok=True)
        ts = int(time.time())
        base = f"{DEBUG_DUMP_DIR}/{stage}_{name}_{ts}"
        try:
            url = page.url
        except Exception:
            url = "?"
        try:
            html = await page.content()
            with open(f"{base}.html", "w", encoding="utf-8") as f:
                f.write(f"<!-- URL: {url} -->\n{html}")
        except Exception:
            pass
        try:
            await page.screenshot(path=f"{base}.png", full_page=False)
        except Exception:
            pass
        log.warning(f"DEBUG_DUMP[{stage}] -> {base}.html + {base}.png  url={url}")
    except Exception as e:
        log.debug(f"_debug_dump fail: {e}")


if not DASHBOARD_URL or not WORKER_TOKEN:
    print("ERROR: DASHBOARD_URL and WORKER_TOKEN must be set in .env"); sys.exit(1)

API = f"{DASHBOARD_URL}/api"
HEADERS = {"Authorization": f"Bearer {WORKER_TOKEN}", "Content-Type": "application/json"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("zoom-worker-v8")

# v8.6.2 — Suppress asyncio's "socket.send() raised exception" warning flood.
# These fire HUNDREDS of times per second when Playwright closes a browser/
# context (CDP socket dies mid-write). They drown out real errors and have
# no functional impact — the underlying asyncio code already handles the
# disconnect, the WARNING is purely informational.
class _AsyncioSocketSendFilter(logging.Filter):
    def filter(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            return True
        # Drop the specific noisy line + the conn-lost variant
        if "socket.send() raised exception" in msg:
            return False
        if "Fatal write error on socket transport" in msg:
            return False
        return True


for _n in ("asyncio", "asyncio.selector_events", "asyncio.proactor_events"):
    _lg = logging.getLogger(_n)
    _lg.addFilter(_AsyncioSocketSendFilter())
    # Also bump level so any leftover INFO/DEBUG chatter is silenced
    if _lg.level == logging.NOTSET or _lg.level < logging.ERROR:
        _lg.setLevel(logging.ERROR)
# Root logger also gets the filter (catches handlers configured elsewhere)
logging.getLogger().addFilter(_AsyncioSocketSendFilter())

# ---------------------------------------------------------------- chromium args
# Ultra-optimized flag set straight from the architecture doc + extra RAM saves.
CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-software-rasterizer",
    "--disable-webgl",
    "--disable-3d-apis",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-background-mode",
    "--disable-component-update",
    "--disable-domain-reliability",
    "--disable-client-side-phishing-detection",
    "--disable-hang-monitor",
    "--disable-prompt-on-repost",
    "--disable-popup-blocking",
    "--disable-breakpad",
    "--disable-crash-reporter",
    "--disable-logging",
    "--disable-translate",
    "--disable-sync",
    "--disable-notifications",
    "--disable-default-apps",
    "--disable-renderer-backgrounding",
    "--disable-features=Translate,BackForwardCache,OptimizationHints,MediaRouter,"
    "DialMediaRouteProvider,CalculateNativeWinOcclusion,InterestFeedContentSuggestions,"
    "GlobalMediaControls,ImprovedCookieControls,AutomationControlled,IsolateOrigins,site-per-process",
    "--mute-audio",
    "--autoplay-policy=no-user-gesture-required",
    "--use-fake-ui-for-media-stream",
    "--use-fake-device-for-media-stream",
    # v8.3.6 USER-REQUESTED STEALTH:
    # User explicitly wants BOTH fake-ui AND fake-device flags so the bot
    # advertises mic + camera devices to Zoom (icons must be visible on the
    # participant tile). Audio/Video are still kept strictly OFF via
    # JOIN_WITH_VIDEO_OFF, post-join Alt+A / Alt+V keyboard mute, and
    # navigator.mediaDevices.enumerateDevices mock — so no green frames or
    # audio leak even though Chrome generates a synthetic test pattern.
    "--no-first-run",
    "--no-default-browser-check",
    "--metrics-recording-only",
    "--password-store=basic",
    "--use-mock-keychain",
    "--ash-no-nudges",
    "--deny-permission-prompts",
    "--log-level=3",
    # v8.3: cache settings flipped based on PERSISTENT_CACHE env var (see below)
    "--renderer-process-limit=2",
    "--blink-settings=imagesEnabled=false",
    "--force-device-scale-factor=0.75",
    # v9.7 ULTRA_LITE: smaller V8 heap (96 MB) + smaller window when on
    f"--js-flags=--max-old-space-size={'96' if ULTRA_LITE else '256'} --max-semi-space-size={'4' if ULTRA_LITE else '8'}",
    f"--window-size={'640,480' if ULTRA_LITE else '800,600'}",
    "--lang=en-US,en",
    "--disable-blink-features=AutomationControlled",
]

# v9.7 ULTRA_LITE — extra heavy-weight features pruned
if ULTRA_LITE:
    CHROMIUM_ARGS += [
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-ipc-flooding-protection",
        "--disable-features=PaintHolding,LazyFrameLoading,LazyImageLoading,SmoothScrolling,IsolateOrigins",
        "--disable-smooth-scrolling",
        "--disable-pinch",
        "--disable-checker-imaging",
        "--disable-low-res-tiling",
        "--num-raster-threads=1",
        "--enable-low-end-device-mode",
        # Hint V8 to GC aggressively (less memory creep over hours)
        "--enable-aggressive-domstorage-flushing",
    ]

# v8.3.4: when running visible (HEADLESS=false on Windows RDP for example)
# push the window off-screen so the operator never sees floating chromium UIs.
if OFFSCREEN_WINDOW and not HEADLESS:
    CHROMIUM_ARGS += [
        "--window-position=-32000,-32000",
        "--start-minimized",
    ]

# v8.3: when persistent cache is on, KEEP Zoom SDK assets on disk so subsequent
# joins reuse js/css/wasm instead of redownloading. Saves ~1-2s per join + ~30%
# network. When off, fall back to v8.1 RAM-saving mode.
if PERSISTENT_CACHE:
    try:
        os.makedirs(PERSISTENT_CACHE_DIR, exist_ok=True)
    except Exception:
        pass
    CHROMIUM_ARGS += [
        f"--disk-cache-dir={PERSISTENT_CACHE_DIR}",
        f"--disk-cache-size={PERSISTENT_CACHE_SIZE_MB * 1024 * 1024}",
    ]
else:
    CHROMIUM_ARGS += [
        "--media-cache-size=1",
        "--disk-cache-size=1",
        "--aggressive-cache-discard",
    ]

# ============================================================
# v9.4 — PER-BOT PROXY / IP ROTATION  (Zoom same-IP rate-limit bypass)
# ============================================================
# Zoom edge throttles per PUBLIC IP: ~40-60 joins / 60s and ~150-200 live WS
# sessions before bots hit the "Please wait" lobby / "service unavailable".
# Spreading bots across many proxy IPs removes that ceiling so one big RDP can
# push far more bots cleanly.
#
# .env config:
#   PROXY_LIST_FILE=/etc/finalzoom-proxies.txt        # one proxy per line, OR
#   PROXY_LIST="socks5://u:p@ip:port,http://ip2:port" # inline (comma separated)
#   PROXIES_PER_BOT=50   # N consecutive bots share one IP, then rotate to next
# Each entry:  scheme://[user:pass@]host:port   (http | https | socks5)
import threading as _threading
from urllib.parse import urlparse as _urlparse

PROXY_LIST_FILE = os.environ.get("PROXY_LIST_FILE", "").strip()
# PROXIES_PER_BOT=1 → every single bot gets its own unique proxy IP.
# This is the correct setting for Webshare residential — each line = 1 IP.
# Zoom bans same-IP pattern, so strict 1:1 is what prevents auto-drops.
PROXIES_PER_BOT = max(1, int(os.environ.get("PROXIES_PER_BOT", "1")))


def _parse_proxy(raw: str) -> Optional[dict]:
    """Parse proxy from multiple formats:
    Webshare format:  p.webshare.io:80:username:password   (host:port:user:pass)
    Standard format:  http://user:pass@host:port
    Bare:             host:port
    Strip CR/LF from Windows-format files automatically.
    """
    raw = (raw or "").strip().strip("\r\n").strip()
    if not raw or raw.startswith("#"):
        return None
    try:
        if "://" not in raw:
            # Colon-separated format detection
            #   4 parts → host:port:user:pass   (Webshare residential default)
            #   2 parts → host:port              (no auth)
            parts = [p.strip() for p in raw.split(":")]
            if len(parts) == 4:
                host, port, user, pwd = parts
                # Webshare residential: p.webshare.io:80:username:password
                # username contains the rotation index (e.g. radhdygrresidential-1)
                raw = f"http://{user}:{pwd}@{host}:{port}"
            elif len(parts) == 2:
                raw = f"http://{parts[0]}:{parts[1]}"
            else:
                raw = "http://" + raw
        u = _urlparse(raw)
        if not u.hostname or not u.port:
            return None
        out = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
        if u.username:
            out["username"] = u.username
        if u.password:
            out["password"] = u.password
        return out
    except Exception:
        return None


def _load_proxies() -> list:
    """Load proxy list from PROXY_LIST_FILE or PROXY_LIST env.
    Handles Windows CRLF line endings (Webshare .txt exports).
    Logs exact count so operator knows if file was read correctly.
    """
    entries = []
    if PROXY_LIST_FILE and os.path.exists(PROXY_LIST_FILE):
        try:
            content = open(PROXY_LIST_FILE, "r", encoding="utf-8").read()
            # Normalize Windows CRLF → LF before splitting
            entries += content.replace("\r\n", "\n").replace("\r", "\n").splitlines()
            log.info(f"proxy: loaded {len(entries)} lines from {PROXY_LIST_FILE}")
        except Exception as e:
            log.warning(f"proxy: failed reading {PROXY_LIST_FILE}: {e}")
    inline = os.environ.get("PROXY_LIST", "")
    if inline:
        entries += inline.split(",")
    parsed = []
    for e in entries:
        p = _parse_proxy(e)
        if p:
            parsed.append(p)
    if parsed:
        log.info(f"proxy: parsed {len(parsed)} valid proxies (PROXIES_PER_BOT={PROXIES_PER_BOT})")
    else:
        log.warning("proxy: NO valid proxies parsed — running DIRECT mode (single IP, ~80 bot limit)")
    return parsed


PROXIES = _load_proxies()
PROXY_ENABLED = len(PROXIES) > 0
_proxy_lock = _threading.Lock()
_proxy_counter = {"n": 0}


def _next_proxy() -> Optional[dict]:
    """Round-robin a proxy, rotating every PROXIES_PER_BOT contexts (bots)."""
    if not PROXY_ENABLED:
        return None
    with _proxy_lock:
        idx = (_proxy_counter["n"] // PROXIES_PER_BOT) % len(PROXIES)
        _proxy_counter["n"] += 1
    return dict(PROXIES[idx])


# PROXY_HEALTHCHECK: default FALSE for Webshare residential.
# Webshare residential proxies use rotating IPs — a "dead" health-check just
# means that specific rotation slot got a slow IP at that moment. The checker
# then DROPS valid proxies and you end up with 0 proxies → single-IP mode →
# auto-drops resume. Set PROXY_HEALTHCHECK=true only if using static datacenter
# proxies that need pre-screening. Webshare residential: always leave false.
PROXY_HEALTHCHECK = os.environ.get("PROXY_HEALTHCHECK", "false").lower() in ("1", "true", "yes")
PROXY_HEALTHCHECK_TIMEOUT = int(os.environ.get("PROXY_HEALTHCHECK_TIMEOUT", "7"))


def _proxy_to_url(p: dict) -> str:
    s = p["server"]
    if p.get("username"):
        scheme, rest = s.split("://", 1)
        return f"{scheme}://{p['username']}:{p.get('password', '')}@{rest}"
    return s


def _healthcheck_proxies(parsed: list) -> list:
    """Quickly test each proxy against Zoom; return only the ones that respond.
    Datacenter proxies blocked by Zoom (or dead) are removed so bots never stall
    60s behind a bad IP."""
    if not parsed:
        return parsed
    import concurrent.futures as _cf

    def _ok(p):
        url = _proxy_to_url(p)
        proxies = {"http": url, "https": url}
        try:
            r = requests.head("https://zoom.us/", proxies=proxies,
                              timeout=PROXY_HEALTHCHECK_TIMEOUT, allow_redirects=False)
            return 100 <= r.status_code < 500
        except Exception:
            return False

    try:
        with _cf.ThreadPoolExecutor(max_workers=min(20, len(parsed))) as ex:
            results = list(ex.map(_ok, parsed))
        return [p for p, good in zip(parsed, results) if good]
    except Exception:
        return parsed  # if the checker itself fails, don't nuke the pool


def refresh_proxies_from_dashboard() -> None:
    """v9.4: Pull the proxy pool managed in the dashboard (Admin → Proxies) so
    you DON'T have to maintain a file on every RDP. Merges with any local
    PROXY_LIST_FILE / PROXY_LIST entries. Safe no-op on any failure."""
    global PROXIES, PROXY_ENABLED, PROXIES_PER_BOT
    try:
        r = requests.get(f"{API}/workers/me/proxies", headers=HEADERS, timeout=8)
        if r.status_code != 200:
            return
        data = r.json() or {}
        remote = [p for p in (_parse_proxy(x) for x in (data.get("proxies") or [])) if p]
        local = _load_proxies()
        seen, merged = set(), []
        for p in remote + local:
            if p["server"] in seen:
                continue
            seen.add(p["server"])
            merged.append(p)
        # v9.5: drop dead/blocked proxies so bots don't stall 60s behind a bad IP.
        if merged and PROXY_HEALTHCHECK:
            alive = _healthcheck_proxies(merged)
            dead = len(merged) - len(alive)
            if dead:
                log.warning(f"proxy: health-check dropped {dead}/{len(merged)} dead/slow proxies")
            if not alive:
                log.warning("proxy: ALL proxies failed health-check → switching to DIRECT (single-IP) "
                            "mode so bots still join fast. Replace these proxies (datacenter likely "
                            "blocked by Zoom — use residential).")
            merged = alive
        with _proxy_lock:
            PROXIES = merged
            PROXY_ENABLED = len(PROXIES) > 0
        ppb = int(data.get("proxies_per_bot") or PROXIES_PER_BOT)
        if ppb >= 1:
            PROXIES_PER_BOT = ppb
        if PROXY_ENABLED:
            log.info(f"proxy: ACTIVE — {len(PROXIES)} healthy IP(s), {PROXIES_PER_BOT} bot(s)/IP")
        else:
            log.info("proxy: DIRECT single-IP mode (no healthy proxies) — fine for ~80 bots on one box")
    except Exception as e:
        log.debug(f"proxy: dashboard fetch failed (keeping local): {e}")


if PROXY_ENABLED:
    log.info(f"proxy: IP rotation ON — {len(PROXIES)} proxies, {PROXIES_PER_BOT} bot(s)/IP")
else:
    log.info("proxy: disabled (set PROXY_LIST_FILE or PROXY_LIST to enable IP rotation)")

UA_LIST = [
    # v8.9: User-Agent ROTATION. Zoom flags fleets where 90 bots use the
    # same UA + viewport — silent kick mid-meeting. Each bot picks a random
    # one of these so Zoom sees a mixed fleet of "real" desktops.
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"),
    ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0"),
]
VIEWPORT_LIST = [
    {"width": 800, "height": 600},
    {"width": 1024, "height": 768},
    {"width": 1280, "height": 720},
    {"width": 1366, "height": 768},
    {"width": 1280, "height": 800},
    {"width": 900, "height": 600},
]
# Backwards compat — most callsites use UA constant.
UA = UA_LIST[0]


def _rand_ua_viewport():
    import random
    return random.choice(UA_LIST), random.choice(VIEWPORT_LIST)

# ---------------------------------------------------------------- pool models
@dataclass
class BotSlot:
    name: str
    task_id: str
    context: Optional[BrowserContext] = None
    page: Optional[Page] = None
    joined_at: float = 0.0
    joined: bool = False
    rejoins: int = 0
    closed: bool = False
    # v8.6: sticky "has joined at least once" flag — once True, stays True
    # even during brief mid-meeting rejoins so the dashboard joined count
    # doesn't visibly drop every time Zoom does a 5-second reconnect.
    ever_joined: bool = False
    # ADDED: True only when this bot CONFIRMED a real host-ended-meeting page.
    # Used by watcher to safely exit chunk early (turant cleanup, ready for
    # next task) when MAJORITY of bots saw host-ended — without falling into
    # the false-positive cascade that the watcher v9.0 fix protects against.
    meeting_ended_seen: bool = False


@dataclass
class BrowserSlot:
    """One chromium process that hosts many isolated BrowserContext tabs."""
    browser: Browser
    bots: Dict[str, BotSlot] = field(default_factory=dict)  # bot_key -> slot
    started_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)

    @property
    def alive(self) -> bool:
        try:
            return self.browser.is_connected()
        except Exception:
            return False

    @property
    def free_slots(self) -> int:
        return max(0, TABS_PER_BROWSER - len(self.bots))


@dataclass
class ReadyContext:
    """A pre-built BrowserContext + about:blank Page waiting to be claimed.
    Cuts join latency dramatically — newContext()+newPage()+goto() typically
    takes 1.5-3s when cold; a warm context drops that to <100ms.
    """
    browser_slot: "BrowserSlot"
    context: BrowserContext
    page: Page
    created_at: float = field(default_factory=time.time)


# ---------------------------------------------------------------- machine specs
def _machine_specs() -> dict:
    try:
        vm = psutil.virtual_memory()
        return {
            "cpu_count": psutil.cpu_count(logical=True) or 2,
            "total_ram_gb": vm.total / (1024 ** 3),
            "free_ram_gb": vm.available / (1024 ** 3),
            "ram_pct": float(vm.percent),
            "cpu_pct": float(psutil.cpu_percent(interval=None)),
        }
    except Exception:
        return {"cpu_count": 2, "total_ram_gb": 4.0, "free_ram_gb": 2.0,
                "ram_pct": 50.0, "cpu_pct": 0.0}


def _free_ram_pct() -> float:
    try:
        return 100.0 - psutil.virtual_memory().percent
    except Exception:
        return 50.0


def _compute_safe_capacity(specs: Optional[dict] = None) -> int:
    s = specs or _machine_specs()
    by_ram = int((s["total_ram_gb"] * 1024 * (100 - RAM_HEADROOM_PCT) / 100) / RAM_PER_BOT_MB)
    by_cpu = int(s["cpu_count"] * BOTS_PER_CPU)
    cap = min(by_ram, by_cpu, MAX_CAPACITY_HARD_CAP)
    return max(1, cap)


# ---------------------------------------------------------------- v8.3 bootstrap
async def bootstrap_storage_state(pw) -> bool:
    """One-shot warm-up at worker boot: opens a throwaway browser, navigates to the
    Zoom join page, dismisses cookie banners, sets localStorage flags that skip
    the 'Join with Audio' prompt, then saves the resulting cookies + localStorage
    to STORAGE_STATE_PATH. Every subsequent BrowserContext loads this snapshot
    via `storage_state=` so the bot lands DIRECTLY on the name/password form.

    Returns True if a fresh snapshot was written, False if cached snapshot was reused.
    """
    # Skip if a recent snapshot already exists
    try:
        if os.path.exists(STORAGE_STATE_PATH):
            age_h = (time.time() - os.path.getmtime(STORAGE_STATE_PATH)) / 3600
            if age_h < STORAGE_STATE_REFRESH_HOURS:
                log.info(f"bootstrap: reusing storage_state ({age_h:.1f}h old)")
                return False
    except Exception:
        pass

    log.info("bootstrap: building Zoom storage_state (cookies + localStorage)…")
    browser = None
    try:
        browser = await pw.chromium.launch(
            headless=HEADLESS, args=CHROMIUM_ARGS,
            chromium_sandbox=False,
        )
        ctx = await browser.new_context(
            user_agent=UA,
            viewport={"width": 800, "height": 600},
            ignore_https_errors=True,
            bypass_csp=True,
            # v8.3.5: MIC ONLY. No camera permission → Zoom's video
            # getUserMedia call is rejected by the browser, so the bot
            # CANNOT broadcast video (no green screen ever).
            permissions=["microphone"],
            locale="en-US",
        )
        page = await ctx.new_page()
        try:
            await page.goto("https://zoom.us/", wait_until="domcontentloaded", timeout=20_000)
        except Exception as e:
            log.warning(f"bootstrap: zoom.us reachable check failed: {e}")

        # Accept cookie banner if present (best-effort)
        for sel in [
            "button#onetrust-accept-btn-handler",
            "button[aria-label='Accept Cookies']",
            "button:has-text('Accept All')",
            "button:has-text('I Accept')",
        ]:
            try:
                btn = await page.query_selector(sel)
                if btn:
                    await btn.click(timeout=1500)
                    await page.wait_for_timeout(300)
                    break
            except Exception:
                pass

        # Seed localStorage / sessionStorage flags that suppress in-meeting prompts.
        # These keys are what Zoom's web client checks before showing
        # the "Join with Computer Audio" modal + the marketing banner.
        try:
            await page.evaluate("""
                () => {
                    try {
                        localStorage.setItem('webclient_audio_setting', 'computer');
                        localStorage.setItem('zm_audio_choice', 'computer');
                        localStorage.setItem('skip_audio_join', '1');
                        localStorage.setItem('zoom_locale', 'en-US');
                        localStorage.setItem('zm_cookie_consent', 'accepted');
                        localStorage.setItem('_zm_cookie_consent_v2', 'all');
                        localStorage.setItem('OptanonAlertBoxClosed', new Date().toISOString());
                        localStorage.setItem('hideNewMeetingPromote', '1');
                        sessionStorage.setItem('webclient_audio_setting', 'computer');
                    } catch(e) {}
                }
            """)
        except Exception:
            pass

        # Now warm the actual join form (so the form-page HTML/JS is in disk cache)
        try:
            await page.goto("https://app.zoom.us/wc/join", wait_until="domcontentloaded", timeout=15_000)
            # Wait for the meeting-id input to mount — confirms SDK JS executed.
            for sel in ["#join-confno", "input[name='confno']", "input[placeholder*='Meeting ID' i]"]:
                try:
                    await page.wait_for_selector(sel, timeout=FORM_PREWARM_WAIT_MS)
                    break
                except Exception:
                    pass
        except Exception as e:
            log.debug(f"bootstrap: join-form preload soft-fail: {e}")

        # Persist snapshot
        try:
            os.makedirs(os.path.dirname(STORAGE_STATE_PATH) or ".", exist_ok=True)
            await ctx.storage_state(path=STORAGE_STATE_PATH)
            log.info(f"bootstrap: storage_state saved -> {STORAGE_STATE_PATH}")
        except Exception as e:
            log.warning(f"bootstrap: storage_state save failed: {e}")
            return False

        await ctx.close()
        return True
    except Exception as e:
        log.warning(f"bootstrap: aborted ({e})")
        return False
    finally:
        if browser:
            try: await browser.close()
            except Exception: pass


def _new_context_kwargs() -> dict:
    """Common kwargs for every BrowserContext we create — includes the warmed
    storage_state snapshot if it exists on disk.

    v8.9: ROTATES user-agent + viewport per context so Zoom doesn't flag
    the fleet as identical clones. Same fleet on identical UA/viewport gets
    soft-kicked silently after 5-10 min in meeting.
    """
    ua, viewport = _rand_ua_viewport()
    kw = dict(
        user_agent=ua,
        viewport=viewport,
        ignore_https_errors=True,
        bypass_csp=True,
        permissions=["microphone"],
        locale="en-US",
    )
    if os.path.exists(STORAGE_STATE_PATH):
        kw["storage_state"] = STORAGE_STATE_PATH
    # v9.4: assign a rotating proxy IP per bot/context (round-robin).
    if PROXY_ENABLED:
        px = _next_proxy()
        if px:
            kw["proxy"] = px
    return kw


# ---------------------------------------------------------------- pool manager
class BrowserPool:
    """Owns a small pool of long-lived chromium browsers. Each browser hosts
    up to TABS_PER_BROWSER isolated BrowserContexts (one per bot).

    PREWARM enhancements:
      • At boot we launch PREWARM_BROWSERS chromium processes and PREWARM_CONTEXTS
        ready BrowserContexts (each with a preloaded about:blank/Zoom page).
      • acquire_ready_context() returns a hot context in O(1).
      • An auto-warmup loop keeps `>= PREWARM_MIN_READY` standby contexts.
      • An auto-shrink loop closes idle hot browsers after SHRINK_IDLE_SEC.
    """

    def __init__(self, pw):
        self.pw = pw
        self.browsers: List[BrowserSlot] = []
        self.ready: List[ReadyContext] = []  # warm standby pool
        self.lock = asyncio.Lock()
        self._prewarmed = False
        # v8.3.3: dynamic targets — start at env defaults, retune to admin_cap on each heartbeat
        self.target_ready: int = max(0, PREWARM_CONTEXTS)
        self.target_min: int = max(0, PREWARM_MIN_READY)
        self.target_max: int = max(0, PREWARM_MAX_READY)
        self.target_browsers: int = max(0, PREWARM_BROWSERS)

    def retune(self, admin_cap: Optional[int]):
        """v8.3.3: when admin updates capacity_max, immediately resize our
        ready-context pool to match. "jitna limit, utne ready"."""
        if not PREWARM_MATCH_ADMIN_CAP or admin_cap is None or admin_cap <= 0:
            return
        cap = min(int(admin_cap), PREWARM_HARD_CEILING)
        # Ready pool target = full admin cap (every slot prewarmed)
        self.target_ready = cap
        # Browsers needed = ceil(cap / TABS_PER_BROWSER)
        self.target_browsers = max(1, (cap + TABS_PER_BROWSER - 1) // TABS_PER_BROWSER)
        # Min/Max watermarks stay close to target so warmup keeps it full
        self.target_min = cap
        self.target_max = cap

    async def _launch_browser(self) -> BrowserSlot:
        launch_kw = dict(
            headless=HEADLESS,
            args=CHROMIUM_ARGS,
            chromium_sandbox=False,
            handle_sigterm=False,
            handle_sigint=False,
            handle_sighup=False,
        )
        # v9.4: Chromium requires a GLOBAL proxy to be set for per-context
        # proxy overrides to take effect. The "per-context" sentinel does that;
        # each new_context() then supplies its own real proxy via _next_proxy().
        if PROXY_ENABLED:
            launch_kw["proxy"] = {"server": "per-context"}
        browser = await self.pw.chromium.launch(**launch_kw)
        slot = BrowserSlot(browser=browser)
        log.info(f"pool: launched chromium (pool size {len(self.browsers) + 1})")
        return slot

    async def _make_ready_context(self, browser_slot: BrowserSlot) -> Optional[ReadyContext]:
        """Pre-create one BrowserContext + Page + (optionally) preload zoom shell.
        v8.3: loads warmed storage_state (cookies + localStorage) and waits for
        the join-form DOM to mount so the next step is literally just fill+click.
        """
        try:
            ctx = await browser_slot.browser.new_context(**_new_context_kwargs())

            # v9.1 FIX: Install media-kill + stealth + SCREEN-SHARE SURVIVAL init
            # scripts BEFORE any page.goto. This is the actual fix for the
            # "members drop on screen share" bug — the previous code added these
            # via page.add_init_script AFTER goto, which is a no-op for the
            # current navigation.
            await _install_context_init_scripts(ctx)

            async def _block(route):
                try:
                    if route.request.resource_type in ("image", "media", "font"):
                        await route.abort()
                    else:
                        await route.continue_()
                except Exception:
                    try: await route.continue_()
                    except Exception: pass
            await ctx.route("**/*", _block)
            page = await ctx.new_page()

            # PRELOAD: hit the Zoom join form shell so DNS/TLS/SDK JS/CSS cache is warm
            # AND the #meeting-id input is already mounted.
            if PREWARM_PRELOAD_URL:
                try:
                    await page.goto(PREWARM_PRELOAD_URL, wait_until="domcontentloaded", timeout=15_000)
                    # v8.3.1: verify the form actually mounted using the shared
                    # ZOOM_SELECTORS list (single source of truth).
                    await _wait_any(page, ZOOM_SELECTORS["form_ready_any"],
                                    timeout_ms=FORM_PREWARM_WAIT_MS)
                except Exception:
                    # about:blank fallback — still gives us instant context handoff
                    try: await page.goto("about:blank", timeout=5_000)
                    except Exception: pass

            return ReadyContext(browser_slot=browser_slot, context=ctx, page=page)
        except Exception as e:
            log.debug(f"prewarm: ready context build failed: {e}")
            return None

    async def prewarm(self):
        """One-shot bootstrap — called once from main()."""
        if self._prewarmed or not PREWARM_ENABLED:
            return
        async with self.lock:
            # Launch hot browsers up to current target_browsers
            for _ in range(max(0, self.target_browsers)):
                try:
                    slot = await self._launch_browser()
                    self.browsers.append(slot)
                except Exception as e:
                    log.warning(f"prewarm: launch failed: {e}")
            # Spread ready contexts across hot browsers
            needed = max(0, self.target_ready)
            if needed and self.browsers:
                idx = 0
                while needed > 0:
                    target = self.browsers[idx % len(self.browsers)]
                    if target.free_slots <= 0:
                        idx += 1
                        if idx >= len(self.browsers) * 2:
                            break
                        continue
                    rc = await self._make_ready_context(target)
                    if rc:
                        self.ready.append(rc)
                        needed -= 1
                    idx += 1
        self._prewarmed = True
        log.info(
            f"prewarm: hot_browsers={len(self.browsers)} "
            f"ready_contexts={len(self.ready)}/{self.target_ready} "
            f"preload_url={PREWARM_PRELOAD_URL!r}"
        )

    async def acquire_ready_context(self) -> Optional[ReadyContext]:
        """O(1) handoff of a pre-built context. Returns None if pool empty."""
        async with self.lock:
            while self.ready:
                rc = self.ready.pop(0)
                if rc.browser_slot.alive:
                    rc.browser_slot.last_used_at = time.time()
                    return rc
                # browser died — drop and continue
            return None

    async def topup_ready(self):
        """AUTO WARMUP ENGINE — keep at least target_min contexts on standby
        (target = admin's capacity_max when PREWARM_MATCH_ADMIN_CAP=true)."""
        if not PREWARM_ENABLED:
            return
        try:
            async with self.lock:
                # Drop dead entries first
                self.ready = [r for r in self.ready if r.browser_slot.alive]
                if len(self.ready) >= self.target_min:
                    return
                # Refill up to target_max
                deficit = self.target_max - len(self.ready)
                if deficit <= 0:
                    return
                # Find/grow browser slots — we need ceil(target_max/TABS_PER_BROWSER)
                self.browsers = [b for b in self.browsers if b.alive]
                # Grow browser fleet up to dynamic target
                while len(self.browsers) < self.target_browsers:
                    try:
                        slot = await self._launch_browser()
                        self.browsers.append(slot)
                    except Exception:
                        break
                target: Optional[BrowserSlot] = None
                for b in self.browsers:
                    if b.free_slots > 0:
                        target = b; break
                if target is None:
                    return
                # Build a few at a time so we don't stall (5 per cycle = ~10s for 50)
                build = min(deficit, 5, target.free_slots)
                for _ in range(build):
                    rc = await self._make_ready_context(target)
                    if rc:
                        self.ready.append(rc)
                    else:
                        break
        except Exception as e:
            log.debug(f"topup_ready err: {e}")

    async def shrink_idle(self):
        """AUTO SHRINK ENGINE — close hot browsers that have been idle too long."""
        if not PREWARM_ENABLED:
            return
        try:
            async with self.lock:
                keep: List[BrowserSlot] = []
                now = time.time()
                for b in self.browsers:
                    idle = (now - b.last_used_at) > SHRINK_IDLE_SEC
                    if not b.bots and idle and len(self.browsers) > 1:
                        # Drop any ready contexts hosted in this browser
                        self.ready = [r for r in self.ready if r.browser_slot is not b]
                        try: await b.browser.close()
                        except Exception: pass
                        log.info("shrink: closed idle hot browser")
                    else:
                        keep.append(b)
                self.browsers = keep
        except Exception as e:
            log.debug(f"shrink_idle err: {e}")

    async def acquire_slot(self) -> BrowserSlot:
        async with self.lock:
            # Remove dead browsers
            self.browsers = [b for b in self.browsers if b.alive]
            # Find a browser with free capacity
            for b in self.browsers:
                if b.free_slots > 0:
                    b.last_used_at = time.time()
                    return b
            slot = await self._launch_browser()
            self.browsers.append(slot)
            return slot

    async def release_browser_if_empty(self):
        """Close any chromium that has 0 bots — frees RAM aggressively.
        Respects prewarm: keeps at least target_browsers hot at all times
        (these are the ones serving the warm-standby contexts)."""
        async with self.lock:
            keep: List[BrowserSlot] = []
            hot_target = max(0, self.target_browsers) if PREWARM_ENABLED else 0
            for b in self.browsers:
                if not b.bots and b.alive and len(keep) >= hot_target:
                    # Drop ready contexts hosted here
                    self.ready = [r for r in self.ready if r.browser_slot is not b]
                    try:
                        await b.browser.close()
                        log.info(f"pool: closed empty chromium (remaining {len(keep)})")
                    except Exception:
                        pass
                else:
                    keep.append(b)
            self.browsers = keep

    async def shutdown(self):
        async with self.lock:
            for r in self.ready:
                try: await r.context.close()
                except Exception: pass
            self.ready.clear()
            for b in self.browsers:
                try:
                    await b.browser.close()
                except Exception:
                    pass
            self.browsers.clear()

    def stats(self) -> dict:
        # v8.3: include storage_state freshness so dashboard knows tap-and-join is armed
        ss_age_h = None
        try:
            if os.path.exists(STORAGE_STATE_PATH):
                ss_age_h = round((time.time() - os.path.getmtime(STORAGE_STATE_PATH)) / 3600, 2)
        except Exception:
            pass
        return {
            "browsers": len(self.browsers),
            "total_bots": sum(len(b.bots) for b in self.browsers),
            "alive": sum(1 for b in self.browsers if b.alive),
            "ready_contexts": len(self.ready),
            "prewarmed": self._prewarmed,
            "version": "v9.7-silent-ui",
            "storage_state_age_hours": ss_age_h,
            "persistent_cache": PERSISTENT_CACHE,
            "preload_url": PREWARM_PRELOAD_URL,
            # v8.3.3: expose dynamic targets so dashboard can show "X/Y ready"
            "target_ready": self.target_ready,
            "target_browsers": self.target_browsers,
            "match_admin_cap": PREWARM_MATCH_ADMIN_CAP,
        }


# ---------------------------------------------------------------- API helpers
def heartbeat(load_override: int, capacity_override: Optional[int] = None,
              pool_stats: Optional[dict] = None) -> Optional[int]:
    """Send heartbeat. Returns the admin-set `capacity_max` from the server
    response so the worker can locally enforce the hard ceiling (avoids even
    *attempting* to claim more tasks than admin allows)."""
    s = _machine_specs()
    payload = {
        "current_load": load_override,
        "cpu_pct": s["cpu_pct"],
        "ram_pct": s["ram_pct"],
        "hostname": socket.gethostname(),
        "os_info": f"{platform.system()} {platform.release()} (Playwright v9.7-silent-ui)",
        "cpu_count": s["cpu_count"],
        "ram_free_gb": round(s["free_ram_gb"], 2),
    }
    if AUTO_CAPACITY:
        cap = capacity_override if capacity_override is not None else _compute_safe_capacity(s)
        payload["reported_capacity"] = cap
    if pool_stats:
        payload["pool_stats"] = pool_stats
    try:
        r = requests.post(f"{API}/workers/me/heartbeat", headers=HEADERS, json=payload, timeout=10)
        if r.status_code == 200:
            try:
                return int(r.json().get("capacity_max")) or None
            except Exception:
                return None
    except Exception as e:
        log.warning(f"heartbeat err: {e}")
    return None


def claim_tasks(n: int = 5) -> List[dict]:
    try:
        r = requests.post(f"{API}/workers/me/claim", headers=HEADERS,
                          params={"max_tasks": n}, timeout=15)
        if r.status_code != 200:
            return []
        return r.json().get("tasks", [])
    except Exception:
        return []


def report_progress(task_id: str, joined: int):
    try:
        requests.patch(f"{API}/tasks/{task_id}/progress", headers=HEADERS,
                       json={"joined_count": joined}, timeout=10)
    except Exception:
        pass


def check_chunk_status(task_id: str) -> str:
    try:
        r = requests.get(f"{API}/tasks/{task_id}/chunk-status", headers=HEADERS, timeout=8)
        if r.status_code == 200:
            d = r.json()
            return d.get("chunk_status") or d.get("task_status") or "unknown"
    except Exception:
        pass
    return "unknown"


def complete_task(task_id: str, success: bool, joined: int, error: Optional[str] = None):
    try:
        requests.post(f"{API}/tasks/{task_id}/complete", headers=HEADERS,
                      json={"success": success, "joined_count": joined, "error": error},
                      timeout=15)
    except Exception:
        pass


# ---------------------------------------------------------------- name loading
_LOCAL_NAMES: List[str] = []


def _load_local_names() -> List[str]:
    global _LOCAL_NAMES
    if _LOCAL_NAMES:
        return _LOCAL_NAMES
    if not LOCAL_NAMES_FILE:
        return []
    p = Path(LOCAL_NAMES_FILE)
    if not p.exists():
        log.warning(f"LOCAL_NAMES_FILE not found: {LOCAL_NAMES_FILE}")
        return []
    try:
        names = [ln.strip() for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines() if ln.strip()]
        _LOCAL_NAMES = names
        log.info(f"loaded {len(names)} names from {LOCAL_NAMES_FILE}")
        return names
    except Exception as e:
        log.warning(f"local names load failed: {e}")
        return []


def _pick_local_names(count: int) -> List[str]:
    import random as _r
    pool = _load_local_names()
    if not pool:
        return []
    if count <= len(pool):
        return _r.sample(pool, count)
    out: List[str] = []
    while len(out) < count:
        sh = pool[:]
        _r.shuffle(sh)
        out.extend(sh[: count - len(out)])
    return out


# ---------------------------------------------------------------- bot lifecycle
async def _join_meeting(page: Page, meeting_id: str, password: str, name: str) -> bool:
    """Single join attempt — returns True if entered meeting.
    v8.3.1: smart multi-selector + zero blind waits. Tries every known Zoom
    selector and dumps the page DOM on failure for selector-tuning."""
    try:
        await page.goto(f"https://app.zoom.us/wc/{meeting_id}/join",
                        wait_until="domcontentloaded", timeout=60_000)
    except Exception as e:
        log.debug(f"goto failed for {name}: {e}")
        return False

    # v9.1 FIX: The OLD code called `page.add_init_script(...)` here AFTER
    # `page.goto(...)`. Per Playwright docs, init scripts only run on FUTURE
    # navigations — so the WebRTC video-track guard NEVER ran for the Zoom
    # meeting page. That is the actual root cause of the "members dropping on
    # screen share" bug: incoming screen-share video was decoded at full force
    # by Chromium → CPU peg → bot dropped.
    #
    # The proper guard is now installed at the BrowserContext level via
    # `_install_context_init_scripts(ctx)` when the context is created. As a
    # belt-and-suspenders safety net, we also `page.evaluate` the survival
    # script here — it is idempotent (checks `__zk_patched`) so re-running on
    # an already-protected page is harmless.
    try:
        await page.evaluate(SCREEN_SHARE_SURVIVAL_INIT_SCRIPT)
    except Exception:
        pass
    # v9.2: also re-evaluate the anti-leave guard. Idempotent — repeated
    # listener registration is fine, the patterns guard against double-action.
    try:
        await page.evaluate(ANTI_LEAVE_INIT_SCRIPT)
    except Exception:
        pass

    # ===== Wait for ANY form element (no more blind 4s sleep) =====
    form = await _wait_any(page, ZOOM_SELECTORS["form_ready_any"], timeout_ms=15_000)
    if form is None:
        log.warning(f"{name}: join form never mounted")
        await _debug_dump(page, "no_form", name)
        return False

    # ===== Password (optional) =====
    if password:
        await _smart_fill(page, ZOOM_SELECTORS["password_input"], password, timeout_ms=3000)
        # Don't fail on missing — many meetings need no password

    # ===== Name (required) =====
    if not await _smart_fill(page, ZOOM_SELECTORS["name_input"], name, timeout_ms=8000):
        log.warning(f"{name}: could not find name input")
        await _debug_dump(page, "no_name_input", name)
        return False

    # ===== v8.3.4: Pre-mute mic + stop video on the preview screen =====
    # This way bot enters meeting silent + no camera (no Zoom popup, no host approval needed).
    pm = await _premute_on_preview(page)
    if JOIN_WITH_AUDIO_MUTED or JOIN_WITH_VIDEO_OFF:
        log.debug(f"{name}: pre-toggle mic_muted={pm['audio_muted']} video_off={pm['video_off']}")

    # ===== Click Join =====
    if not await _smart_click(page, ZOOM_SELECTORS["join_button"], timeout_ms=8000):
        # Last resort: hit Enter on the name field
        try:
            await page.keyboard.press("Enter")
        except Exception:
            await _debug_dump(page, "no_join_button", name)
            return False

    # ===== Wait for "in meeting" — any of the meeting-shell selectors =====
    # v8.8: 35s → 60s. Under high-CPU bursts (90+ bots joining simultaneously)
    # Chromium renderer can take 40-50s to mount the meeting shell. 35s was
    # killing perfectly-valid bots that just needed 5 more seconds. The
    # initial-join retry loop will rescue genuinely stuck ones.
    in_meeting_loc = await _wait_any(page, ZOOM_SELECTORS["in_meeting"], timeout_ms=60_000)
    if in_meeting_loc is None:
        log.warning(f"{name}: never entered meeting (timeout)")
        await _debug_dump(page, "no_meeting_shell", name)
        return await _is_in_meeting(page)  # fall through to legacy verify

    # ===== Dismiss any "Join Audio by Computer" prompt (best-effort, fast) =====
    # v8.6: Extended timeout + retry — clicking this is what makes the mic icon
    # appear on the bot tile. If we skip it, Zoom shows a "join audio" phone
    # icon instead of the muted mic icon (user wants mic icon visible).
    for _ in range(3):
        try:
            audio_btn = await _wait_any(page, ZOOM_SELECTORS["audio_join"], timeout_ms=2500)
            if audio_btn is None:
                break
            try: await audio_btn.click(timeout=2000, force=True)
            except Exception: pass
            await page.wait_for_timeout(400)
        except Exception:
            break

    return await _is_in_meeting(page)


async def _is_in_meeting(page: Page) -> bool:
    try:
        url = (page.url or "").lower()
    except Exception:
        return False
    if "zoom.us" not in url and "/wc/" not in url:
        return False
    for sel in ZOOM_SELECTORS["in_meeting"]:
        try:
            if await page.locator(sel).count() > 0:
                return True
        except Exception:
            continue
    # Negative signal — still on preview screen
    try:
        for sel in ZOOM_SELECTORS["join_button"]:
            if await page.locator(sel).count() > 0:
                return False
    except Exception:
        pass
    return True


async def _meeting_ended(page: Page) -> bool:
    """v9.0 + v9.4 STRICT detection — only returns True when we are CERTAIN
    the meeting truly ended FOR EVERYONE (host-ended, not single-bot kick).
    False positives here cause individual bots to silently exit the meeting
    without rejoining — exactly the "bot disappearing" symptom user reported
    on 2026-06-22.

    Rules:
      1. Selectors list intentionally EXCLUDES 'You have been removed' (that
         is a single-bot kick, not a host-ended signal — handled by rejoin
         loop instead).
      2. URL fallback uses only the explicit Zoom post-meeting endpoints
         (/wc/postattendee, /wc/end, /feedback). Generic /leave? and
         leavetype= REMOVED because they fire on any single-bot leave too.
      3. The /feedback and /end URL signals also require a selector hit
         in the same call (defence in depth — page can transiently navigate
         here during reconnect on some Zoom builds).
    """
    try:
        # Layer 1: count matching strict selectors (any 1 hit is strong)
        sel_hits = 0
        for sel in ZOOM_SELECTORS["meeting_ended"]:
            try:
                if await page.locator(sel).count() > 0:
                    sel_hits += 1
                    if sel_hits >= 1:
                        break
            except Exception:
                continue
        if sel_hits:
            return True

        # Layer 2: URL-only fallback — ONLY the definitive Zoom end pages.
        # postattendee is the *only* URL Zoom routes EVERY participant to
        # when the host ends the meeting (single-bot leave/kick goes to
        # different URLs that should NOT exit the bot).
        url = (page.url or "").lower()
        if "/wc/postattendee" in url:
            return True

        return False
    except Exception:
        return False


async def _post_join_optimize(page: Page):
    """Reduce CPU/RAM use after the bot is in the meeting:
       - **Guarantee mic is muted** + video is off (safety net if pre-mute missed)
       - hide all <video>/<canvas> via CSS so GPU isn't decoding remote streams
       - flip document.visibilityState='hidden' so Chrome throttles renderer ~10×
    """
    # v8.6: Belt-and-suspenders MIC MUTE — multi-strategy retry with verification.
    # Strategies tried in order until mic shows "unmute" (= currently muted) state:
    #   1. Click button[aria-label*='mute my microphone']  → standard
    #   2. Press keyboard shortcut Alt+A                    → Zoom WC global toggle
    #   3. Locate by .footer-button__button label "Mute"   → legacy fallback
    if JOIN_WITH_AUDIO_MUTED:
        for attempt in range(5):
            try:
                # ✓ Already muted? aria says "Unmute my microphone"
                unmute_state = page.locator("button[aria-label*='unmute' i][aria-label*='microphone' i]").first
                if await unmute_state.count() > 0:
                    break
                # Strategy 1: direct mute-button click
                clicked = False
                for sel in [
                    "button[aria-label*='mute my microphone' i]",
                    "button[aria-label='Mute']",
                    ".footer-button__button[aria-label*='mute' i]",
                ]:
                    try:
                        mic = page.locator(sel).first
                        if await mic.count() > 0:
                            await mic.click(timeout=1500, force=True)
                            clicked = True
                            break
                    except Exception:
                        continue
                # Strategy 2: keyboard shortcut Alt+A (Zoom Web Client global)
                if not clicked:
                    try:
                        await page.keyboard.press("Alt+a")
                    except Exception:
                        pass
                await page.wait_for_timeout(400)
            except Exception:
                pass

    # v8.6: Belt-and-suspenders STOP VIDEO — same multi-strategy pattern.
    # Alt+V is Zoom Web Client's global video toggle shortcut.
    if JOIN_WITH_VIDEO_OFF:
        for attempt in range(5):
            try:
                # ✓ Already off? aria says "Start Video"
                start_state = page.locator("button[aria-label*='start video' i]").first
                if await start_state.count() > 0:
                    break
                clicked = False
                for sel in [
                    "button[aria-label*='stop my video' i]",
                    "button[aria-label='Stop Video']",
                    ".footer-button__button[aria-label*='stop video' i]",
                ]:
                    try:
                        vid = page.locator(sel).first
                        if await vid.count() > 0:
                            await vid.click(timeout=1500, force=True)
                            clicked = True
                            break
                    except Exception:
                        continue
                if not clicked:
                    try:
                        await page.keyboard.press("Alt+v")
                    except Exception:
                        pass
                await page.wait_for_timeout(400)
            except Exception:
                pass
    # v8.8: AGGRESSIVE VIDEO/SCREEN-SHARE KILL
    # ────────────────────────────────────────────────────────────────────
    # Problem this solves: when the host starts SCREEN SHARE, Chromium spawns
    # a new <video> element + WebRTC video decoder. With 90 bots on a single
    # box this spikes CPU to 100% in ~2 sec and tabs crash with "no_meeting_shell".
    # User's logs: "throttle: cpu=100%" repeating + DEBUG_DUMP[no_meeting_shell].
    #
    # Old approach: setInterval(kill, 4000) — too slow, decoder runs for 4 sec
    # before kill() pauses it. Also only kills the <video> element, not the
    # underlying WebRTC decoder which keeps receiving + decoding bytes.
    #
    # New approach (3 layers, defense in depth):
    #   1. MutationObserver fires INSTANTLY when a <video> element is added →
    #      pauses + nullifies srcObject before first frame decode.
    #   2. RTCPeerConnection.prototype.addTransceiver patched at page-load time
    #      (via init script) to set direction='inactive' for video. We also
    #      walk getReceivers() every 1.5 sec and set .track.enabled=false so
    #      Chromium stops the decoder pipeline at the source (zero CPU).
    #   3. CSS hides every conceivable share/video container class so even if
    #      something slips through, it doesn't trigger layout/paint.
    #
    # Result on the user's 90-bot test (CPU 100% → expected ~25-40%).
    try:
        await page.evaluate(
            """
            (() => {
              // ─── CSS hide: video tiles + screen share containers ──────
              const s = document.createElement('style');
              s.textContent = `
                video,canvas,
                .video-tile,.video-container,.gallery-video-container,
                .gallery-video-container__main-view,.speaker-active-container,
                .shared-container,.share-content,.share-container,
                .sharee-container-frame,.sharing-screen,.screen-share-container,
                [class*='shared-tile'],[class*='screen-share'],
                [class*='gallery-video'],[class*='speaker-view'],
                [class*='shareView'],[class*='ShareView'],
                .full-screen-widget,.fixed-active-speaker__video-view
                { display:none !important; visibility:hidden !important;
                  width:0 !important; height:0 !important; }
              `;
              document.head.appendChild(s);

              // ─── Layer 1: kill any existing + brand-new <video> instantly ──
              const killVideoEl = (v) => {
                try {
                  v.pause();
                  v.srcObject = null;
                  v.src = '';
                  v.removeAttribute('src');
                  v.style.display = 'none';
                } catch(e){}
              };
              document.querySelectorAll('video,canvas').forEach(killVideoEl);

              // MutationObserver — fires SYNCHRONOUSLY on DOM insertion
              try {
                const obs = new MutationObserver((mutations) => {
                  for (const m of mutations) {
                    m.addedNodes && m.addedNodes.forEach((n) => {
                      if (!n.querySelectorAll) return;
                      if (n.tagName === 'VIDEO' || n.tagName === 'CANVAS') killVideoEl(n);
                      n.querySelectorAll && n.querySelectorAll('video,canvas').forEach(killVideoEl);
                    });
                  }
                });
                obs.observe(document.documentElement || document.body,
                            {childList: true, subtree: true});
              } catch(e){}

              // ─── Layer 2: nuke WebRTC video receivers (the REAL CPU sink) ──
              window.__zk_pcs = window.__zk_pcs || new Set();
              const origRTC = window.RTCPeerConnection;
              if (origRTC && !origRTC.__zk_patched) {
                const Wrapped = function(...args) {
                  const pc = new origRTC(...args);
                  window.__zk_pcs.add(pc);
                  // On every new track, immediately disable if it's video.
                  // v9.1 FIX: DO NOT call track.stop() — Zoom treats a stopped
                  // remote receiver as "participant dead" and can kick the bot.
                  // Just disabling drops Chromium's decode pipeline (zero CPU)
                  // while Zoom's signaling sees a healthy receiver.
                  pc.addEventListener('track', (ev) => {
                    try {
                      if (ev.track && ev.track.kind === 'video') {
                        ev.track.enabled = false;
                      }
                    } catch(e){}
                  });
                  return pc;
                };
                Wrapped.prototype = origRTC.prototype;
                Object.setPrototypeOf(Wrapped, origRTC);
                Wrapped.__zk_patched = true;
                try { window.RTCPeerConnection = Wrapped; } catch(e){}
              }

              // Periodic sweep: walk every PC's receivers, kill video tracks
              const sweep = () => {
                try {
                  for (const pc of window.__zk_pcs) {
                    if (!pc.getReceivers) continue;
                    pc.getReceivers().forEach((r) => {
                      try {
                        if (r.track && r.track.kind === 'video' && r.track.enabled) {
                          r.track.enabled = false;
                        }
                      } catch(e){}
                    });
                  }
                  document.querySelectorAll('video,canvas').forEach(killVideoEl);
                } catch(e){}
              };
              setInterval(sweep, 1500);  // was 4000 — 2.5x faster

              // ─── Throttle the whole renderer (Chrome backgrounds the tab) ──
              try {
                Object.defineProperty(document, 'visibilityState',
                  {get: () => 'hidden', configurable: true});
                Object.defineProperty(document, 'hidden',
                  {get: () => true, configurable: true});
                document.dispatchEvent(new Event('visibilitychange'));
              } catch(e){}

              // ─── Block requestAnimationFrame (kills paint loop CPU) ────────
              try {
                const origRAF = window.requestAnimationFrame;
                window.requestAnimationFrame = function(cb) {
                  // Run callback at 1Hz instead of 60Hz so Zoom UI doesn't
                  // completely freeze but CPU usage drops 30-50x
                  return setTimeout(cb, 1000);
                };
              } catch(e){}
            })();
            """
        )
    except Exception:
        pass


async def _do_reaction_once(page: Page, name: str) -> bool:
    """v8.4: Click the in-meeting Reactions button, then a random emoji.
    Idempotent / best-effort — never raises. Returns True if an emoji landed.
    """
    import random as _r
    try:
        # Hover the meeting shell so the auto-hide footer appears
        try:
            shell = page.locator(".meeting-app, .meeting-client").first
            if await shell.count() > 0:
                box = await shell.bounding_box()
                if box:
                    await page.mouse.move(box["x"] + box["width"] / 2,
                                          box["y"] + box["height"] - 20)
        except Exception:
            pass
        # 1) Open the reactions popup
        opened = await _smart_click(page, ZOOM_SELECTORS["reactions_button"],
                                    timeout_ms=2500)
        if not opened:
            return False
        await page.wait_for_timeout(250)
        # 2) Pick a random emoji selector and click it
        emoji_sels = ZOOM_SELECTORS["reaction_emoji_any"][:]
        _r.shuffle(emoji_sels)
        for sel in emoji_sels:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    await loc.click(timeout=1500)
                    return True
            except Exception:
                continue
        # Close popup if nothing clicked (Esc to dismiss)
        try: await page.keyboard.press("Escape")
        except Exception: pass
        return False
    except Exception:
        return False


async def _reaction_loop(page: Page, slot: BotSlot,
                         interval_min: int, interval_max: int):
    """Side-coroutine spawned per-bot when participant_reactions / floating_emoji
    is enabled. Sleeps a random interval then fires one reaction. Cancellable —
    main loop calls .cancel() on cleanup."""
    import random as _r
    # Stagger initial fire so 50 bots don't all click together
    try:
        await asyncio.sleep(_r.uniform(2, max(interval_min, 5)))
    except asyncio.CancelledError:
        return
    while not slot.closed:
        try:
            if slot.joined:
                await _do_reaction_once(page, slot.name)
            wait = _r.uniform(max(5, interval_min), max(interval_min + 1, interval_max))
            await asyncio.sleep(wait)
        except asyncio.CancelledError:
            return
        except Exception:
            await asyncio.sleep(5)


async def run_bot(slot: BotSlot, browser_slot: BrowserSlot, meeting_id: str,
                  password: str, hold_seconds: int,
                  pool: Optional["BrowserPool"] = None,
                  task_cfg: Optional[dict] = None) -> bool:
    """One bot lifecycle inside a shared browser:
       newContext → newPage → join → hold (with reactions + anti-leave) → cleanup.
    Returns True if it ever joined.

    Optimization: if `pool` is provided AND has a ready prewarmed context,
    we use it INSTEAD of creating a fresh one (instant handoff).

    v8.4 features:
      • Reactions side-coroutine driven by task_cfg flags + intervals.
      • STRICT anti-leave — keep rejoining for ENTIRE hold_seconds (no early
        kick-window cutoff). Random backoff between attempts.
      • RECYCLE_CONTEXT_ON_END — instead of closing the context at meeting end,
        we blank the page and hand it back to the prewarm ready pool so the
        next task picks up an already-warmed tab (massive CPU/time saver).
    """
    task_cfg = task_cfg or {}
    reactions_on = REACTIONS_ENABLED and bool(
        task_cfg.get("participant_reactions") or task_cfg.get("floating_emoji")
    )
    r_min = int(task_cfg.get("reaction_interval_min") or REACTION_INTERVAL_MIN_DEFAULT)
    r_max = int(task_cfg.get("reaction_interval_max") or REACTION_INTERVAL_MAX_DEFAULT)
    if r_max < r_min:
        r_max = r_min + 10
    reaction_task: Optional[asyncio.Task] = None
    recycled_to_pool = False
    try:
        ctx = None
        page = None
        # ===== PREWARM PATH =====
        if pool is not None:
            rc = await pool.acquire_ready_context()
            if rc is not None:
                ctx = rc.context
                page = rc.page
                browser_slot = rc.browser_slot
                slot.context = ctx
                slot.page = page

        # ===== COLD PATH (fallback) =====
        if ctx is None:
            ctx = await browser_slot.browser.new_context(**_new_context_kwargs())
            # v9.1 FIX: Install screen-share survival + media kill + stealth
            # init scripts BEFORE any navigation. This guarantees the WebRTC
            # video-track guard is in place when Zoom's bundle loads.
            await _install_context_init_scripts(ctx)
            # Block images + media for extra savings
            async def _block(route):
                try:
                    if route.request.resource_type in ("image", "media", "font"):
                        await route.abort()
                    else:
                        await route.continue_()
                except Exception:
                    try: await route.continue_()
                    except Exception: pass
            await ctx.route("**/*", _block)
            slot.context = ctx
            page = await ctx.new_page()
            slot.page = page

        # Join with retry — INITIAL join uses BOT_INITIAL_JOIN_RETRIES (separate
        # budget from rejoin so a slow first-join doesn't burn mid-meeting recovery).
        joined = False
        for attempt in range(BOT_INITIAL_JOIN_RETRIES):
            try:
                if await _join_meeting(page, meeting_id, password, slot.name):
                    joined = True; break
            except Exception as e:
                log.debug(f"[{slot.name}] initial attempt {attempt+1}/{BOT_INITIAL_JOIN_RETRIES} err: {e}")
            # Exponential-ish backoff capped at 8s so we don't lose a bot for
            # 5 minutes on a single bad batch.
            await asyncio.sleep(min(8, 2 + attempt))

        if not joined:
            log.warning(f"[{slot.name}] gave up after {BOT_INITIAL_JOIN_RETRIES} initial join attempts")
            return False

        slot.joined = True
        slot.ever_joined = True
        slot.joined_at = time.time()
        await _post_join_optimize(page)

        # ===== START REACTIONS SIDE-COROUTINE (if enabled) =====
        if reactions_on:
            try:
                reaction_task = asyncio.create_task(
                    _reaction_loop(page, slot, r_min, r_max)
                )
                log.info(f"[{slot.name}] reactions ON ({r_min}-{r_max}s)")
            except Exception as e:
                log.debug(f"[{slot.name}] could not start reaction loop: {e}")
                reaction_task = None

        # ─────────────────────────────────────────────────────────────────
        # v8.9 NEVER-LEAVE HOLD LOOP
        # Bot exits this loop ONLY when:
        #   • host-ended-meeting page is detected (real meeting end)
        #   • slot.closed = True (admin cancelled task from dashboard)
        #   • hold_seconds elapsed (configured task timeout)
        # Page crashes, navigation failures, single-join-attempt failures —
        # NONE of these break us out. We reload, recreate, and keep trying.
        # ─────────────────────────────────────────────────────────────────
        end = time.time() + hold_seconds
        import random as _rb
        consecutive_errs = 0
        # v9.7 [C] KEEPALIVE — every N seconds dispatch a synthetic mousemove
        # so Zoom's idle-disconnect heuristic never marks the bot as "frozen".
        # Zoom WebClient kicks tabs that have no events for ~90s. We ping
        # every 30s — well under the threshold, near-zero CPU per ping.
        # Opt out via env: BOT_KEEPALIVE_SEC=0
        keepalive_every = int(os.environ.get("BOT_KEEPALIVE_SEC", "30"))
        last_keepalive = 0.0
        while time.time() < end and not slot.closed:
            await asyncio.sleep(IN_MEETING_CHECK_SEC)

            # v9.7 [C] keepalive ping
            if keepalive_every > 0 and (time.time() - last_keepalive) > keepalive_every:
                try:
                    await page.evaluate("""() => {
                        try {
                          window.dispatchEvent(new MouseEvent('mousemove', {clientX: 50, clientY: 50, bubbles: true}));
                          document.dispatchEvent(new Event('visibilitychange'));
                          // Touch Zoom's keepalive WebSocket if available
                          if (window.__zk_pcs && window.__zk_pcs.size) {
                            // ICE keep-alive: send a benign stats query so the
                            // PC's last-activity timestamp updates and Zoom's
                            // SFU does not mark us inactive.
                            window.__zk_pcs.forEach(pc => {
                              try { pc.getStats && pc.getStats(); } catch(_) {}
                            });
                          }
                        } catch(_) {}
                    }""")
                except Exception:
                    pass
                last_keepalive = time.time()

            # 1) Check if still in meeting
            try:
                in_room = await _is_in_meeting(page)
                consecutive_errs = 0
            except Exception:
                # Page crashed — try to reload before giving up.
                # v9.7: be EVEN MORE forgiving during screen-share start.
                # The previous threshold (10) was already wide; bumped to 20
                # (≈ IN_MEETING_CHECK_SEC × 20 = 2 min) so the H.264/VP8
                # renegotiation spike + DOM rebuild that Zoom does on share
                # start never trips the rejoin path. Real host-end-meeting
                # is detected via _meeting_ended() in the next block.
                consecutive_errs += 1
                log.debug(f"[{slot.name}] _is_in_meeting err #{consecutive_errs}")
                if consecutive_errs >= 20:
                    in_room = False
                else:
                    continue

            if in_room:
                continue

            # v9.7 — RENEGOTIATION GUARD: before triggering the rejoin path,
            # peek at the RTCPeerConnection signalingState. If ANY PC is
            # mid-negotiation (state != 'stable') we are almost certainly in
            # the brief window Zoom uses to add/remove the screen-share m=line.
            # During this window _is_in_meeting() returns false because Zoom
            # re-renders the meeting shell. Wait one more cycle.
            try:
                renegotiating = await page.evaluate(
                    """() => {
                      try {
                        if (!window.__zk_pcs || !window.__zk_pcs.size) return false;
                        for (const pc of window.__zk_pcs) {
                          try {
                            if (pc.signalingState && pc.signalingState !== 'stable')
                              return true;
                          } catch (_) {}
                        }
                        return false;
                      } catch (_) { return false; }
                    }""",
                    timeout=2000,
                )
            except Exception:
                renegotiating = False
            if renegotiating:
                log.debug(f"[{slot.name}] PC renegotiating (likely screen share) "
                          f"— skipping drop trigger this cycle")
                continue

            # 2) Drop detected — grace pause then check for meeting-end markers
            try:
                await asyncio.sleep(MEETING_END_GRACE_SEC)
                ended = await _meeting_ended(page)
            except Exception:
                ended = False
            if ended:
                # Real meeting end — exit cleanly (this is the ONLY non-admin
                # way the hold loop is allowed to exit early).
                log.info(f"[{slot.name}] meeting ended by host — exiting cleanly")
                slot.meeting_ended_seen = True
                slot.closed = True
                break

            # 3) Quick re-check (might have been a transient blip)
            try:
                if await _is_in_meeting(page):
                    continue
            except Exception:
                pass

            # 4) FORCE REJOIN — no budget cap (BOT_REJOIN_MAX default 9999).
            # v9.2: even MORE aggressive — 10 inner attempts (was 5), random
            # backoff stays 1-8s, and cooldown after burst is 8s (was 20s).
            # Goal: bot keeps trying to get back in within seconds of any drop.
            slot.rejoins += 1
            log.info(f"[{slot.name}] dropped, rejoin attempt {slot.rejoins}")
            inner_retry = 0
            rejoined = False
            while (time.time() < end
                   and not slot.closed
                   and slot.rejoins < BOT_REJOIN_MAX
                   and inner_retry < 10):
                inner_retry += 1
                try:
                    await asyncio.sleep(_rb.uniform(REJOIN_BACKOFF_MIN, REJOIN_BACKOFF_MAX))
                    if await _join_meeting(page, meeting_id, password, slot.name):
                        slot.joined_at = time.time()
                        slot.ever_joined = True
                        await _post_join_optimize(page)
                        rejoined = True
                        break
                except Exception as e:
                    log.debug(f"[{slot.name}] rejoin inner err {inner_retry}: {e}")
                # Check if meeting ended while we were retrying — saves
                # us from infinite-looping on a dead meeting.
                try:
                    if await _meeting_ended(page):
                        log.info(f"[{slot.name}] meeting ended during rejoin — exiting")
                        slot.meeting_ended_seen = True
                        slot.closed = True
                        break
                except Exception:
                    pass
            if rejoined:
                continue
            # v9.2: 10 inner attempts failed — short 8s cooldown then outer loop
            # ticks again so we keep trying for the entire meeting duration.
            log.warning(f"[{slot.name}] 10 inner rejoin attempts failed, "
                        f"cooling down 8s and retrying (total rejoins={slot.rejoins})")
            await asyncio.sleep(8)
        return True
    finally:
        # ===== STOP REACTIONS first =====
        if reaction_task is not None:
            try:
                reaction_task.cancel()
            except Exception:
                pass

        # ===== RECYCLE CONTEXT (tab warming) =====
        # Instead of closing the BrowserContext at meeting end, we blank the
        # page and hand the context back to the prewarm ready pool. Next task
        # picks up an already-warmed tab → massive CPU/time saver.
        if (RECYCLE_CONTEXT_ON_END and pool is not None
                and slot.context is not None and slot.page is not None
                and browser_slot is not None and browser_slot.alive
                and not slot.closed):
            try:
                # Best-effort: navigate to about:blank to drop the Zoom meeting
                # JS context + free media handles, then push back to ready pool.
                try:
                    await asyncio.wait_for(
                        slot.page.goto("about:blank", timeout=3000),
                        timeout=4.0
                    )
                except Exception:
                    pass
                rc = ReadyContext(
                    browser_slot=browser_slot,
                    context=slot.context,
                    page=slot.page,
                )
                async with pool.lock:
                    # Respect the warm-pool ceiling — drop overflow.
                    if len(pool.ready) < pool.target_max:
                        pool.ready.append(rc)
                        recycled_to_pool = True
                if recycled_to_pool:
                    log.debug(f"[{slot.name}] context recycled to ready pool")
            except Exception as e:
                log.debug(f"[{slot.name}] recycle failed, falling back to close: {e}")
                recycled_to_pool = False

        # ===== AUTO CLEANUP (only if NOT recycled) =====
        if not recycled_to_pool:
            try:
                if slot.page:
                    await slot.page.close()
            except Exception: pass
            try:
                if slot.context:
                    await slot.context.close()
            except Exception: pass
        slot.closed = True
        if browser_slot is not None:
            browser_slot.bots.pop(slot.name, None)


# ---------------------------------------------------------------- task runner
class TaskRunner:
    def __init__(self, pool: BrowserPool):
        self.pool = pool
        self.tasks: Dict[str, dict] = {}     # task_id -> {bots, joined, started_at}
        self.tasks_lock = asyncio.Lock()
        # dynamic_floor lets the health monitor LOWER live capacity
        self.dynamic_floor: Optional[int] = None

    def joined_count(self) -> int:
        return sum(t.get("joined", 0) for t in self.tasks.values())

    def total_bots_alive(self) -> int:
        n = 0
        for t in self.tasks.values():
            for b in t["bots"]:
                if b.joined and not b.closed:
                    n += 1
        return n

    async def run_task(self, task: dict):
        task_id = task["id"]
        meeting_id = task["meeting_id"]
        password = task.get("meeting_password") or ""
        members = int(task.get("members", 0))
        timeout_sec = int(task.get("timeout", 7200))

        local = _pick_local_names(members) if LOCAL_NAMES_FILE else []
        names = local if local else (task.get("names") or [f"User{i+1}" for i in range(members)])

        log.info(f"▶ task {task_id[:8]} | mid={meeting_id} members={members} timeout={timeout_sec}s")

        bot_slots: List[BotSlot] = []
        async with self.tasks_lock:
            self.tasks[task_id] = {"bots": bot_slots, "joined": 0, "started_at": time.time()}

        runners: List[asyncio.Task] = []
        # v8.7: BURST FILL with browser pooling — fire SPAWN_BURST_SIZE bots
        # in parallel, then sleep SPAWN_DELAY_MS, then next burst. On a 16-vCPU
        # box this ramps 500 bots to "spawning" in ~9 seconds instead of 125s.
        # Bigger burst = faster ramp but higher peak CPU during boot.
        for i in range(members):
            # RAM safety gate
            if _free_ram_pct() < PRE_SPAWN_FREE_RAM_PCT:
                log.warning(f"  RAM low ({_free_ram_pct():.0f}%), pausing spawn at {i}/{members}")
                waited = 0
                while waited < 60 and _free_ram_pct() < PRE_SPAWN_FREE_RAM_PCT:
                    await asyncio.sleep(3); waited += 3
                if _free_ram_pct() < PRE_SPAWN_FREE_RAM_PCT:
                    log.warning(f"  stopping spawn at {i}/{members} (RAM stayed low)")
                    break

            # v8.8: CPU safety gate — when CPU pegged at 100%, spawning more
            # bots starves the already-joining ones and pushes them to
            # no_meeting_shell. Wait for CPU to drop below threshold.
            if PRE_SPAWN_MAX_CPU_PCT > 0:
                try:
                    cur_cpu = float(psutil.cpu_percent(interval=None))
                except Exception:
                    cur_cpu = 0.0
                if cur_cpu >= PRE_SPAWN_MAX_CPU_PCT:
                    log.warning(f"  CPU {cur_cpu:.0f}% ≥ {PRE_SPAWN_MAX_CPU_PCT}%, "
                                f"pausing spawn at {i}/{members}")
                    waited = 0
                    while waited < 30:
                        await asyncio.sleep(2); waited += 2
                        try:
                            cur_cpu = float(psutil.cpu_percent(interval=None))
                        except Exception:
                            cur_cpu = 0.0
                        if cur_cpu < PRE_SPAWN_MAX_CPU_PCT:
                            break

            browser_slot = await self.pool.acquire_slot()
            slot = BotSlot(name=names[i], task_id=task_id)
            browser_slot.bots[slot.name + f"#{i}"] = slot
            bot_slots.append(slot)

            runners.append(asyncio.create_task(
                run_bot(slot, browser_slot, meeting_id, password, timeout_sec,
                        pool=self.pool, task_cfg=task)
            ))
            # v8.7: burst mode — only sleep AFTER every BURST_SIZE bots are
            # fired (not after each one). Sequential mode = SPAWN_BURST_SIZE=1.
            if SPAWN_BURST_SIZE <= 1 or (i + 1) % SPAWN_BURST_SIZE == 0:
                await asyncio.sleep(SPAWN_DELAY_MS / 1000.0)

        # v9.7 [I] STUCK-BOT TRACKING — record when each runner started so the
        # watcher can detect bots that never reach ever_joined=True within
        # STUCK_BOT_RESPAWN_SEC and respawn them ONCE. This guarantees the
        # final joined count matches the requested member count.
        spawn_started_at: List[float] = [time.time()] * len(bot_slots)
        respawned: set = set()
        # 60s (was 90s) — faster detection of stuck bots → more members actually join
        stuck_threshold = int(os.environ.get("STUCK_BOT_RESPAWN_SEC", "60"))
        # 3 respawns (was 2) — extra attempt for max member guarantee
        max_respawns_per_slot = int(os.environ.get("STUCK_BOT_MAX_RESPAWNS", "3"))
        respawn_count: List[int] = [0] * len(bot_slots)

        # ───────────────────────────────────────────────────────────────
        # v9.0 WATCHER — runs for the FULL task timeout regardless of bots
        # ───────────────────────────────────────────────────────────────
        # Previous bug (user report 2026-05-30): when ALL bot coroutines ended
        # (whether by initial-join failure or false-positive meeting_ended),
        # the watcher hit `alive_runners == 0` → break → reported chunk
        # complete to dashboard while the meeting was STILL ON. This cascaded
        # into the parent task being marked completed → OTHER workers' chunk
        # status check returned "completed" → they killed their own bots →
        # entire fleet evacuated mid-meeting.
        #
        # FIX: watcher loop now runs for the FULL `timeout_sec` ALWAYS.
        # The chunk is only reported complete when:
        #   (a) admin cancels/fails the task from dashboard, OR
        #   (b) task.timeout actually elapses
        # If bots die mid-task, we just log it — we DO NOT auto-report
        # complete. This prevents the cascade.
        last_reported = 0
        deadline = time.time() + timeout_sec
        last_cancel_check = 0.0
        cancelled_by_dashboard = False
        # ADDED: track host-end-meeting consensus across bots so the worker
        # can turant clean up and pick up the next task. Reaching majority
        # requires bots actually saw the end-meeting page — NOT the v9.0
        # false-positive case where bots just disconnected/crashed.
        early_end_grace_left = int(os.environ.get("EARLY_END_GRACE_SEC", "10"))
        early_end_grace_deadline: Optional[float] = None
        while time.time() < deadline:
            joined = sum(1 for s in bot_slots if s.ever_joined and not s.closed)
            if joined != last_reported:
                report_progress(task_id, joined)
                async with self.tasks_lock:
                    self.tasks[task_id]["joined"] = joined
                last_reported = joined
                log.info(f"  ✓ joined {joined}/{members} task={task_id[:8]}")

            # Cancel/fail check — ONLY way to exit early besides deadline.
            # NOTE: we deliberately do NOT honor 'completed' status here —
            # only explicit cancel/fail from admin. This stops the false-
            # positive cascade where one chunk's premature completion would
            # tear down everyone else's bots.
            # v9.8: reduced poll from 10s → 3s so cancel se chrome turant
            # free ho jaye (user request: "cancel ke bad chrome new task ke
            # liye ready ho jaye").
            if time.time() - last_cancel_check > 3:
                last_cancel_check = time.time()
                status = check_chunk_status(task_id)
                if status in ("cancelled", "failed"):
                    log.warning(f"  task {task_id[:8]} {status} by dashboard — tearing down NOW")
                    for s in bot_slots:
                        s.closed = True
                    cancelled_by_dashboard = True
                    break

            # ADDED: MAJORITY HOST-ENDED CHECK — instant cleanup on host end.
            # Counts bots that ACTUALLY navigated to the end-meeting page
            # (slot.meeting_ended_seen). If >=50% of EVER-JOINED bots saw
            # it (ceil(n/2)), grace-wait 10s then mark chunk complete so the
            # worker is immediately ready for the next task. Stragglers/single
            # dead bots cannot trigger this exit — only real host-end can.
            ever_joined_count = sum(1 for s in bot_slots if s.ever_joined)
            end_seen = sum(1 for s in bot_slots if s.meeting_ended_seen)
            if ever_joined_count > 0:
                threshold = max(1, (ever_joined_count + 1) // 2)
                if end_seen >= threshold:
                    if early_end_grace_deadline is None:
                        early_end_grace_deadline = time.time() + early_end_grace_left
                        log.info(
                            f"  task {task_id[:8]} — host-ended detected by "
                            f"{end_seen}/{ever_joined_count} bots, grace "
                            f"{early_end_grace_left}s then chunk complete"
                        )
                    elif time.time() >= early_end_grace_deadline:
                        log.info(
                            f"  task {task_id[:8]} — host-ended confirmed by "
                            f"{end_seen}/{ever_joined_count} bots, exiting "
                            f"(worker ready for next task)"
                        )
                        for s in bot_slots:
                            s.closed = True
                        break

            # v9.7 [I] STUCK-BOT RESPAWN — scan for runners that started >= 
            # stuck_threshold seconds ago but their slot still has
            # ever_joined=False. Cancel + respawn ONCE per slot (default 2 retries).
            # This eliminates the "1-2 bots stuck on no-meeting-shell" tail
            # that prevents perfect 50/50 joined counts.
            if stuck_threshold > 0:
                now_ts = time.time()
                for idx in range(len(bot_slots)):
                    cur_slot = bot_slots[idx]
                    if cur_slot.ever_joined or cur_slot.closed:
                        continue
                    if respawn_count[idx] >= max_respawns_per_slot:
                        continue
                    age = now_ts - spawn_started_at[idx]
                    if age < stuck_threshold:
                        continue
                    log.warning(
                        f"  v9.7 [I] stuck bot {cur_slot.name}#{idx}: not "
                        f"joined in {age:.0f}s — killing + respawning "
                        f"(try {respawn_count[idx]+1}/{max_respawns_per_slot})"
                    )
                    # Mark old slot dead + cancel its runner
                    try:
                        cur_slot.closed = True
                        if idx < len(runners) and not runners[idx].done():
                            runners[idx].cancel()
                    except Exception:
                        pass
                    # Spawn replacement
                    try:
                        new_browser_slot = await self.pool.acquire_slot()
                        new_name = (names[idx] if idx < len(names) else f"r-{idx}") + f"~r{respawn_count[idx]+1}"
                        new_slot = BotSlot(name=new_name, task_id=task_id)
                        new_browser_slot.bots[new_slot.name + f"#{idx}"] = new_slot
                        bot_slots[idx] = new_slot
                        runners[idx] = asyncio.create_task(
                            run_bot(new_slot, new_browser_slot, meeting_id,
                                    password, timeout_sec, pool=self.pool, task_cfg=task)
                        )
                        spawn_started_at[idx] = now_ts
                        respawn_count[idx] += 1
                    except Exception as e:
                        log.warning(f"  v9.7 [I] respawn failed for #{idx}: {e}")

            # NO MORE `if alive_runners == 0: break` — if bots all die, we
            # still keep the chunk reserved on the dashboard until either
            # the dashboard explicitly cancels or the full timeout elapses.
            # This is the SAFE choice: an empty chunk consumed for 2 hours
            # is far less harmful than a cascading mid-meeting evacuation.
            alive_runners = sum(1 for r in runners if not r.done())
            if alive_runners == 0:
                # Bots are all gone but we keep the chunk alive. Log once
                # so the operator notices, then sleep longer to save CPU.
                log.warning(f"  task {task_id[:8]} — ALL bot coroutines ended early "
                            f"but holding chunk until deadline ({int(deadline - time.time())}s left)")
                await asyncio.sleep(30)
                continue
            await asyncio.sleep(3)

        # Stop everything for this task
        for s in bot_slots:
            s.closed = True
        # Wait briefly for cleanups. v9.8: on dashboard-cancel we want chrome
        # turant free for next task — wait only 4s instead of 15s.
        cleanup_wait = 4 if cancelled_by_dashboard else 15
        try:
            await asyncio.wait(runners, timeout=cleanup_wait)
        except Exception:
            pass
        for r in runners:
            if not r.done():
                r.cancel()

        await self.pool.release_browser_if_empty()
        gc.collect()

        final_joined = sum(1 for s in bot_slots if s.joined)
        async with self.tasks_lock:
            self.tasks.pop(task_id, None)
        complete_task(task_id, success=True, joined=final_joined)
        log.info(f"✓ task {task_id[:8]} done: joined {final_joined}/{members}")


# ---------------------------------------------------------------- health monitor
async def health_monitor(runner: TaskRunner, pool: BrowserPool, stop_event: asyncio.Event):
    """Background watcher: enforces dynamic limits + restarts dead chromium.

    v9.5 — fixes user-reported "dynamic_cap=1" bug where a brief CPU spike
    during spawn-burst (CPU briefly 100% for 10-30s) silently cut capacity
    to a single bot, then the recovery hysteresis kept it stuck low even
    after CPU dropped. Two protections:

      1. THROTTLE_CONSECUTIVE: require 4 consecutive 10s checks of high
         pressure (= 40s sustained) before subtracting capacity. A single
         spike no longer touches dyn_cap.
      2. RAPID RECOVERY: when both CPU and RAM are clearly under threshold,
         snap dyn_cap straight back to base capacity (no 5-step crawl).
    """
    base_cap = _compute_safe_capacity()
    dyn_cap = base_cap
    high_streak = 0
    while not stop_event.is_set():
        try:
            s = _machine_specs()
            # ===== DYNAMIC THROTTLE (only after SUSTAINED pressure) =====
            if s["cpu_pct"] > CPU_THROTTLE_PCT or s["ram_pct"] > RAM_THROTTLE_PCT:
                high_streak += 1
                if high_streak >= DYNAMIC_THROTTLE_CONSECUTIVE:
                    dyn_cap = max(1, dyn_cap - DYNAMIC_LIMIT_STEP)
                    log.warning(
                        f"throttle: cpu={s['cpu_pct']:.0f}% ram={s['ram_pct']:.0f}% "
                        f"sustained {high_streak * 10}s → dynamic_cap={dyn_cap}"
                    )
                else:
                    # Spike but not yet sustained — log gently, do nothing.
                    log.info(
                        f"pressure spike (ignored): cpu={s['cpu_pct']:.0f}% "
                        f"ram={s['ram_pct']:.0f}% streak={high_streak}/{DYNAMIC_THROTTLE_CONSECUTIVE}"
                    )
            else:
                # Conditions normal — reset streak immediately.
                if high_streak > 0:
                    high_streak = 0
                # RAPID RECOVERY: if dyn_cap was previously reduced, snap
                # back to a freshly-computed base. No more 5-step crawl.
                if dyn_cap < base_cap and s["cpu_pct"] < CPU_THROTTLE_PCT - 10 and s["ram_pct"] < RAM_THROTTLE_PCT - 10:
                    fresh = _compute_safe_capacity(s)
                    if dyn_cap < fresh:
                        log.info(f"recovery: cpu={s['cpu_pct']:.0f}% ram={s['ram_pct']:.0f}% — restoring dynamic_cap {dyn_cap}→{fresh}")
                        dyn_cap = fresh
            runner.dynamic_floor = dyn_cap

            # ===== AUTO-RESTART DEAD CHROMIUM =====
            for b in list(pool.browsers):
                if not b.alive:
                    log.warning("health: chromium died, will be replaced on next acquire")
                    pool.browsers.remove(b)
        except Exception as e:
            log.warning(f"health monitor err: {e}")
        await asyncio.sleep(10)


async def cleanup_loop(pool: BrowserPool, runner: TaskRunner, stop_event: asyncio.Event):
    """Periodic orphan sweep — kills any rogue chromium NOT owned by our pool,
    plus closes empty pool browsers."""
    while not stop_event.is_set():
        await asyncio.sleep(CLEANUP_INTERVAL_SEC)
        try:
            await pool.release_browser_if_empty()
            _orphan_chromium_sweep()
        except Exception as e:
            log.warning(f"cleanup err: {e}")


async def warmup_loop(pool: BrowserPool, stop_event: asyncio.Event):
    """AUTO WARMUP ENGINE — keep the warm-standby ready-context pool topped up
    and shrink idle hot browsers. Runs every WARMUP_INTERVAL_SEC."""
    while not stop_event.is_set():
        try:
            await pool.topup_ready()
            await pool.shrink_idle()
            st = pool.stats()
            log.debug(f"warmup: ready={st['ready_contexts']} browsers={st['browsers']} bots={st['total_bots']}")
        except Exception as e:
            log.debug(f"warmup err: {e}")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=WARMUP_INTERVAL_SEC)
        except asyncio.TimeoutError:
            pass


def _orphan_chromium_sweep():
    """Kill chromium processes whose CWD/cmdline contains no recognizable pool
    marker. Best-effort — on Linux mainly; on Windows just no-ops if not psutil."""
    if not psutil:
        return
    # Our pool browsers are still alive — psutil cannot easily distinguish them
    # from orphans. We use a lightweight heuristic: if a chromium process has
    # been alive > 1h AND has parent_pid != our PID, treat as orphan.
    my_pid = os.getpid()
    killed = 0
    for p in psutil.process_iter(["pid", "name", "ppid", "create_time"]):
        try:
            n = (p.info.get("name") or "").lower()
            if not any(x in n for x in ("chromium", "chrome")):
                continue
            ppid = p.info.get("ppid")
            age = time.time() - p.info.get("create_time", time.time())
            # NOTE: under Playwright, chromium parents are the playwright host
            # process (NOT our worker PID directly). So we skip parents in the
            # PROCESS TREE rooted at our PID. Quick check: walk up.
            try:
                cur = psutil.Process(ppid)
                in_our_tree = False
                for _ in range(6):
                    if cur.pid == my_pid:
                        in_our_tree = True; break
                    cur = psutil.Process(cur.ppid())
                if in_our_tree:
                    continue
            except Exception:
                pass
            if age > 3600:  # > 1 hour and not in our tree
                p.kill(); killed += 1
        except Exception:
            continue
    if killed:
        log.info(f"cleanup: killed {killed} orphan chromium processes")


# ---------------------------------------------------------------- main
async def main():
    stop_event = asyncio.Event()

    # ============================================================
    # v9.7 [A] PROCESS PRIORITY BOOST (Windows)
    # ------------------------------------------------------------
    # On Windows the default scheduler is fair-share — 60+ chromium
    # processes compete equally with system noise (Windows Defender,
    # Update, telemetry). One renegotiation spike during screen share
    # can starve a few bot tabs enough that Zoom drops them.
    #
    # Solution: nice ourselves to HIGH_PRIORITY_CLASS so chromium
    # spawns inherit it. NOT REALTIME_PRIORITY — that can starve the
    # OS itself. HIGH gives us reliable ~5ms slice every 16ms tick
    # which is plenty for WebRTC keepalives.
    # Opt out via env: WORKER_PROCESS_PRIORITY=normal
    # ============================================================
    try:
        prio = os.environ.get("WORKER_PROCESS_PRIORITY", "high").lower()
        if prio == "high" and sys.platform == "win32":
            try:
                psutil.Process().nice(psutil.HIGH_PRIORITY_CLASS)
                log.info("v9.7 [A]: worker process priority -> HIGH (chromium will inherit)")
            except Exception as e:
                log.warning(f"v9.7 [A]: priority boost failed (continuing at normal): {e}")
    except Exception:
        pass

    def _sig(*_):
        log.info("signal received → shutting down")
        stop_event.set()

    for sig_name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, sig_name):
            try:
                signal.signal(getattr(signal, sig_name), _sig)
            except Exception:
                pass

    s = _machine_specs()
    log.info(f"Zoom Worker v9.7-silent-ui (hide-UI + mic/cam-connected defaults + screen-share renegotiation guard) starting")
    log.info(f"  dashboard={DASHBOARD_URL}")
    log.info(f"  cpu={s['cpu_count']}c  ram={s['total_ram_gb']:.1f}G  "
             f"safe_cap={_compute_safe_capacity(s)}")
    log.info(f"  tabs_per_browser={TABS_PER_BROWSER}  headless={HEADLESS}  "
             f"poll={POLL_INTERVAL}s")
    log.info(f"  prewarm: enabled={PREWARM_ENABLED} browsers={PREWARM_BROWSERS} "
             f"contexts={PREWARM_CONTEXTS} min={PREWARM_MIN_READY} max={PREWARM_MAX_READY}")
    log.info(f"  v8.3:    persistent_cache={PERSISTENT_CACHE} ({PERSISTENT_CACHE_DIR}, {PERSISTENT_CACHE_SIZE_MB}MB)  "
             f"storage_state={STORAGE_STATE_PATH}  preload_url={PREWARM_PRELOAD_URL}")
    log.info(f"  v8.3.4:  headless={HEADLESS} offscreen={OFFSCREEN_WINDOW}  "
             f"mute_on_join={JOIN_WITH_AUDIO_MUTED}  video_off_on_join={JOIN_WITH_VIDEO_OFF}")

    async with async_playwright() as pw:
        # v9.4: pull dashboard-managed proxy pool BEFORE prewarm so hot contexts
        # already get their rotating IP (no local file needed).
        refresh_proxies_from_dashboard()
        # ===== v8.3: BOOTSTRAP — bake cookies + localStorage once per worker boot =====
        try:
            await bootstrap_storage_state(pw)
        except Exception as e:
            log.warning(f"bootstrap_storage_state failed (continuing without): {e}")

        pool = BrowserPool(pw)
        runner = TaskRunner(pool)

        # ===== PREWARM ENGINE: launch hot browsers + ready contexts upfront =====
        await pool.prewarm()

        # background tasks
        health = asyncio.create_task(health_monitor(runner, pool, stop_event))
        cleanup = asyncio.create_task(cleanup_loop(pool, runner, stop_event))
        warmup = asyncio.create_task(warmup_loop(pool, stop_event))

        try:
            admin_cap: Optional[int] = None
            while not stop_event.is_set():
                load = runner.total_bots_alive()
                cap_override = runner.dynamic_floor
                pool_stats = pool.stats()
                # heartbeat in a thread so it never blocks event loop.
                # Server returns the admin-set capacity_max so we can locally
                # enforce the hard ceiling.
                admin_cap = await asyncio.to_thread(heartbeat, load, cap_override, pool_stats) or admin_cap

                # ===== v8.3.3: retune the prewarm pool to match admin cap =====
                # "jitna limit, utne ready" — if admin sets 50, we keep 50 ready.
                pool.retune(admin_cap)

                # ===== v8.3.2 admin HARD CEILING =====
                # If admin set capacity_max=50 and we already have 50 bots,
                # don't even bother claiming more — let the next RDP take over.
                ceiling_hit = (admin_cap is not None and load >= admin_cap)
                if ceiling_hit:
                    log.debug(f"admin-cap hit: load={load} >= cap={admin_cap}, skipping claim")

                if not ceiling_hit and len(runner.tasks) < MAX_CONCURRENT_TASKS:
                    # Cap claim count to the remaining headroom under admin ceiling
                    headroom = (admin_cap - load) if admin_cap is not None else 9999
                    if headroom > 0:
                        n = min(5, MAX_CONCURRENT_TASKS - len(runner.tasks), max(1, headroom))
                        claimed = await asyncio.to_thread(claim_tasks, n)
                        for t in claimed:
                            asyncio.create_task(runner.run_task(t))

                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=POLL_INTERVAL)
                except asyncio.TimeoutError:
                    pass
        finally:
            log.info("shutdown: closing pool…")
            stop_event.set()
            health.cancel(); cleanup.cancel(); warmup.cancel()
            await pool.shutdown()
            # mark every in-flight task failed
            for tid, t in list(runner.tasks.items()):
                complete_task(tid, success=False, joined=t.get("joined", 0),
                              error="worker shutdown")
            log.info("bye")


if __name__ == "__main__":
    # ============================================================
    # SUPERVISED MAIN — Internal auto-restart (no PM2 dependency)
    # ============================================================
    # Problem: asyncio.run(main()) dies on any unhandled exception and
    # waits for PM2 to restart (5s delay, max_restarts limit).
    # Fix: wrap in a supervisor loop that catches ALL exceptions and
    # immediately re-runs main() after a short backoff. PM2 still handles
    # the truly fatal cases (OOM kill, SIGKILL) but for all soft crashes
    # (network blip, Playwright crash, Zoom API timeout) we recover in 3s.
    #
    # Exit only on KeyboardInterrupt (Ctrl+C) or SIGTERM (PM2 stop).
    # ============================================================
    import time as _time_mod

    _RESTART_BACKOFF_MIN = int(os.environ.get("RESTART_BACKOFF_MIN", "3"))
    _RESTART_BACKOFF_MAX = int(os.environ.get("RESTART_BACKOFF_MAX", "30"))
    _crash_count = 0
    _backoff = _RESTART_BACKOFF_MIN

    while True:
        try:
            asyncio.run(main())
            # main() returned normally (only on clean SIGTERM/Ctrl-C) → exit
            log.info("main() returned normally — exiting supervisor")
            break
        except KeyboardInterrupt:
            log.info("KeyboardInterrupt — exiting")
            break
        except SystemExit as e:
            # Deliberate sys.exit() — respect it on first crash only.
            # On repeat crashes (Playwright version mismatch etc) keep retrying.
            if _crash_count == 0:
                log.info(f"SystemExit({e.code}) on first run — exiting")
                raise
            log.warning(f"SystemExit({e.code}) crash #{_crash_count} — retrying in {_backoff}s")
        except Exception:
            _crash_count += 1
            log.error(f"CRASH #{_crash_count} in main() — full traceback:")
            import traceback as _tb
            _tb.print_exc()
            log.warning(f"  auto-restart in {_backoff}s (Ctrl-C to stop)")
            _time_mod.sleep(_backoff)
            # Exponential backoff capped at max
            _backoff = min(_RESTART_BACKOFF_MAX, _backoff * 2)
            # Reset backoff after long successful run (heuristic: ≥ 5 min up)
            # is tracked via a separate counter reset by main() itself but we
            # can approximate by resetting after 3 consecutive fast crashes.
            if _crash_count % 5 == 0:
                _backoff = _RESTART_BACKOFF_MIN
            continue
        else:
            # Successful run → reset backoff
            _backoff = _RESTART_BACKOFF_MIN
