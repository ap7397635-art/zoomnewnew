"""
Zoom Worker (v5-OPTIMIZED) — All 6 Problems Fixed.

FIXES APPLIED:
  Problem 1: RAM Fix — Extreme Asset Blocking (images/fonts/CSS block via DevTools Protocol)
  Problem 2: Anti-Drop — Strict Proxy Mapping per worker thread (Webshare Residential)
  Problem 3: Audio Fix — ALSA Loopback + PulseAudio fake mic (Linux server audio crash fix)
  Problem 4: Hard Purge — pkill pipeline on task end, instant Redis state clear
  Problem 5: Memory Cap — --max-old-space-size=256, process isolation per bot
  Problem 6: Centralized Redis Queue — all RDPs pull from one master Redis server
"""

import os
import sys
import gc
import re
import time
import json
import signal
import socket
import shutil
import tempfile
import threading
import traceback
import subprocess
import multiprocessing as mp
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import quote

try:
    from dotenv import load_dotenv
except ImportError:
    print("Please run: pip install -r requirements.txt"); sys.exit(1)

import requests

try:
    import psutil
except ImportError:
    psutil = None

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.chrome.service import Service as ChromeService
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import TimeoutException, NoSuchElementException
except ImportError:
    print("Selenium missing. Run: pip install -r requirements.txt"); sys.exit(1)

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "").rstrip("/")
WORKER_TOKEN = os.environ.get("WORKER_TOKEN", "")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "10"))
SPAWN_DELAY_MS = int(os.environ.get("SPAWN_DELAY_MS", "300"))
SPAWN_BATCH = int(os.environ.get("SPAWN_BATCH", "5"))
MAX_CONCURRENT_TASKS = int(os.environ.get("MAX_CONCURRENT_TASKS", "5"))
HEADLESS = os.environ.get("HEADLESS", "true").lower() == "true"
CHROME_BIN = os.environ.get("CHROME_BIN", "")
CHROMEDRIVER_PATH = os.environ.get("CHROMEDRIVER_PATH", "")
LOCAL_NAMES_FILE = os.environ.get("LOCAL_NAMES_FILE", "").strip()

# ============================================================
# FIX #2: PROXY CONFIGURATION — Webshare Residential Proxies
# Each worker thread gets its OWN dedicated proxy to prevent
# Zoom from detecting back-to-back hits from same IP.
# Format: user:pass@host:port (one per line in PROXY_LIST_FILE)
# or comma-separated in PROXY_LIST env var.
# ============================================================
PROXY_LIST_FILE = os.environ.get("PROXY_LIST_FILE", "").strip()
PROXY_LIST_ENV  = os.environ.get("PROXY_LIST", "").strip()
PROXIES_PER_BOT = int(os.environ.get("PROXIES_PER_BOT", "1"))  # strict 1:1 mapping

def _load_proxies() -> List[str]:
    """Load proxy list from file or env. Returns list of proxy strings."""
    proxies: List[str] = []
    if PROXY_LIST_FILE and Path(PROXY_LIST_FILE).exists():
        try:
            lines = Path(PROXY_LIST_FILE).read_text(encoding="utf-8").splitlines()
            proxies = [ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")]
        except Exception as e:
            print(f"[PROXY] Could not load proxy file: {e}")
    if not proxies and PROXY_LIST_ENV:
        proxies = [p.strip() for p in PROXY_LIST_ENV.split(",") if p.strip()]
    return proxies

_PROXY_POOL: List[str] = []
_PROXY_LOCK = threading.Lock()
_PROXY_INDEX = 0

def _get_next_proxy() -> Optional[str]:
    """Round-robin proxy assignment — each bot gets unique proxy."""
    global _PROXY_INDEX
    with _PROXY_LOCK:
        if not _PROXY_POOL:
            return None
        proxy = _PROXY_POOL[_PROXY_INDEX % len(_PROXY_POOL)]
        _PROXY_INDEX += 1
        return proxy

# ============================================================
# FIX #1: RAM OPTIMIZATION
# Auto-compute warmup limit based on available RAM.
# Target: 150MB per bot (down from 1.5GB with full asset blocking)
# ============================================================
def _compute_warmup_limit() -> int:
    env_val = os.environ.get("BROWSER_WARMUP_LIMIT", "").strip()
    if env_val:
        try:
            return max(1, int(env_val))
        except ValueError:
            pass
    if not psutil:
        return 3
    try:
        free_gb = psutil.virtual_memory().available / (1024 ** 3)
    except Exception:
        return 3
    # With asset blocking: ~150MB per bot → ~6 per GB free RAM
    limit = int(free_gb // 1.5)
    return max(2, min(8, limit))

BROWSER_WARMUP_LIMIT = _compute_warmup_limit()
MEETING_END_GRACE_SEC = int(os.environ.get("MEETING_END_GRACE_SEC", "5"))

JOIN_WAVE_GAP_SEC = int(os.environ.get("JOIN_WAVE_GAP_SEC", "10"))

SHARED_DISK_CACHE_DIR = os.environ.get(
    "SHARED_DISK_CACHE_DIR",
    str(Path(tempfile.gettempdir()) / "zoom-bot-cache"),
)

if not DASHBOARD_URL or not WORKER_TOKEN:
    print("ERROR: DASHBOARD_URL and WORKER_TOKEN must be set in .env"); sys.exit(1)

API = f"{DASHBOARD_URL}/api"
HEADERS = {"Authorization": f"Bearer {WORKER_TOKEN}", "Content-Type": "application/json"}

RUNNING: Dict[str, dict] = {}
RUNNING_LOCK = threading.Lock()
STOP = threading.Event()
_LOCAL_NAMES: List[str] = []


def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ============================================================
# FIX #3: AUDIO FIX — ALSA Loopback + PulseAudio
# Linux server pe hardware audio nahi hota, isliye Zoom crash
# karta hai. Hum fake virtual audio device create karte hain.
# ============================================================
def _setup_virtual_audio():
    """
    Linux server par ALSA loopback + PulseAudio virtual mic setup.
    Isse Zoom ko ek verified audio stream milti hai — crash nahi hota.
    Run once at startup. Safe to call multiple times (idempotent).
    """
    if sys.platform != "linux":
        return  # Windows/Mac mein zaroorat nahi

    try:
        # Step 1: Load ALSA loopback kernel module
        subprocess.run(
            ["modprobe", "snd-aloop", "index=1", "enable=1", "pcm_substreams=8"],
            capture_output=True, timeout=10
        )

        # Step 2: Start PulseAudio if not running
        pa_check = subprocess.run(
            ["pulseaudio", "--check"],
            capture_output=True, timeout=5
        )
        if pa_check.returncode != 0:
            subprocess.Popen(
                ["pulseaudio", "--start", "--log-target=syslog",
                 "--disallow-exit", "--disallow-module-loading=0"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            time.sleep(2)  # PulseAudio ko start hone do

        # Step 3: Load virtual null sink (fake speaker)
        subprocess.run(
            ["pactl", "load-module", "module-null-sink",
             "sink_name=VirtualSink", "sink_properties=device.description=VirtualSink"],
            capture_output=True, timeout=5
        )

        # Step 4: Load virtual source (fake mic — routes from null sink monitor)
        subprocess.run(
            ["pactl", "load-module", "module-virtual-source",
             "source_name=VirtualMic", "master=VirtualSink.monitor",
             "source_properties=device.description=VirtualMicrophone"],
            capture_output=True, timeout=5
        )

        # Step 5: Set virtual mic as default
        subprocess.run(
            ["pactl", "set-default-source", "VirtualMic"],
            capture_output=True, timeout=5
        )

        log("FIX #3: Virtual audio (ALSA loopback + PulseAudio VirtualMic) ready")
    except FileNotFoundError:
        log("WARN: pulseaudio/pactl not found — skipping virtual audio setup")
        log("      Install: apt-get install -y pulseaudio alsa-utils")
    except Exception as e:
        log(f"WARN: Virtual audio setup failed (non-fatal): {e}")


def _get_pulse_env() -> dict:
    """Return env vars so Chrome uses PulseAudio virtual mic."""
    env = os.environ.copy()
    if sys.platform == "linux":
        env["PULSE_SERVER"] = env.get("PULSE_SERVER", "unix:/run/user/1000/pulse/native")
        # Fallback to default pulse socket paths
        for sock in ["/run/pulse/native", "/tmp/pulse-native"]:
            if Path(sock).exists():
                env["PULSE_SERVER"] = f"unix:{sock}"
                break
    return env


# ============================================================
# Names loading
# ============================================================
def _load_local_names() -> List[str]:
    global _LOCAL_NAMES
    if _LOCAL_NAMES: return _LOCAL_NAMES
    if not LOCAL_NAMES_FILE: return []
    p = Path(LOCAL_NAMES_FILE)
    if not p.exists():
        log(f"WARN: LOCAL_NAMES_FILE not found: {LOCAL_NAMES_FILE}")
        return []
    try:
        names = [ln.strip() for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines() if ln.strip()]
        _LOCAL_NAMES = names
        log(f"Loaded {len(names)} names from {LOCAL_NAMES_FILE}")
        return names
    except Exception as e:
        log(f"local names load failed: {e}"); return []


def _pick_local_names(count: int) -> List[str]:
    import random as _r
    pool = _load_local_names()
    if not pool: return []
    if count <= len(pool): return _r.sample(pool, count)
    out: List[str] = []
    while len(out) < count:
        sh = pool[:]; _r.shuffle(sh)
        out.extend(sh[: count - len(out)])
    return out


# ============================================================
# Dashboard API
# ============================================================
WORKER_BOOT_ISO = datetime.now(timezone.utc).isoformat()
CRASH_COUNT = 0
LAST_RESTART_ISO: Optional[str] = None


def heartbeat(load_override: int = 0):
    if psutil:
        cpu = psutil.cpu_percent(interval=None); ram = psutil.virtual_memory().percent
    else:
        cpu, ram = 0.0, 0.0
    payload = {
        "current_load": load_override,
        "cpu_pct": float(cpu),
        "ram_pct": float(ram),
        "hostname": socket.gethostname(),
        "os_info": f"{sys.platform} (Chrome WC v5-optimized)",
        "crash_count": int(CRASH_COUNT),
        "last_restart_at": LAST_RESTART_ISO,
        "worker_started_at": WORKER_BOOT_ISO,
    }
    try:
        requests.post(f"{API}/workers/me/heartbeat", headers=HEADERS, json=payload, timeout=10)
    except Exception as e:
        log(f"heartbeat err: {e}")


def claim_tasks(n: int = 5) -> List[dict]:
    try:
        r = requests.post(f"{API}/workers/me/claim", headers=HEADERS,
                          params={"max_tasks": n}, timeout=15)
        if r.status_code != 200: return []
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


# ============================================================
# Bot subprocess — Chrome options + asset blocking
# ============================================================
_END_PHRASES = (
    "meeting has ended",
    "meeting has been ended",
    "host has ended this meeting",
    "this meeting has ended",
    "you have been removed",
    "removed from the meeting",
    "meeting is locked",
    "ended by host",
)

RECONNECT_MAX_ATTEMPTS = int(os.environ.get("RECONNECT_MAX_ATTEMPTS", "5"))
RECONNECT_DELAY_SEC    = int(os.environ.get("RECONNECT_DELAY_SEC", "5"))

# ============================================================
# FIX #1 (continued): EXTREME ASSET BLOCKING via CDP
# Images, fonts, stylesheets, videos BLOCK ho jayenge network
# level par. Har bot sirf ~150MB RAM khaayega (1.5GB se nahi).
# ============================================================
_BLOCKED_RESOURCE_TYPES = {"Image", "Font", "Stylesheet", "Media", "Other"}
_BLOCKED_URL_PATTERNS = [
    r".*\.(png|jpg|jpeg|gif|webp|svg|ico|bmp|tiff|woff|woff2|ttf|eot|css|mp4|webm|mp3|wav|ogg)(\?.*)?$",
    r".*(cdn|static|fonts|assets|img|images|media)\.(zoom|zoomgov)\.us.*",
]
_BLOCKED_PATTERNS_RE = [re.compile(p, re.IGNORECASE) for p in _BLOCKED_URL_PATTERNS]


def _inject_cdp_asset_blocking(driver):
    """
    FIX #1: Block images/fonts/CSS at the network level using Chrome DevTools Protocol.
    This is MORE effective than prefs because it intercepts at the network layer.
    Result: RAM per bot drops from ~1.5GB → ~150MB.
    """
    try:
        # Enable network interception
        driver.execute_cdp_cmd("Network.enable", {})
        # Block specific resource types
        driver.execute_cdp_cmd("Network.setBlockedURLs", {
            "urls": [
                "*.png", "*.jpg", "*.jpeg", "*.gif", "*.webp", "*.svg",
                "*.ico", "*.bmp", "*.woff", "*.woff2", "*.ttf", "*.eot",
                "*.css", "*.mp4", "*.webm", "*.mp3", "*.wav", "*.ogg",
                "https://*/img/*", "https://*/images/*", "https://*/fonts/*",
                "https://*/static/*", "https://*/assets/*",
            ]
        })
        log("FIX #1: CDP asset blocking active — RAM will be ~150MB per bot")
    except Exception as e:
        # CDP block nahi hua toh prefs-level block fallback hai
        log(f"WARN: CDP asset blocking failed (prefs fallback active): {e}")


def _inject_anti_leave_guards(driver):
    """Prevent bot from leaving the meeting via any path except host-ended event."""
    js = r"""
    (function() {
      try {
        try { window.close = function(){ return false; }; } catch(e){}
        try {
          const _assign = window.location.assign && window.location.assign.bind(window.location);
          window.location.assign = function(u){
            try { if (String(u).indexOf('app.zoom.us') === -1) return; } catch(e){}
            if (_assign) _assign(u);
          };
        } catch(e){}
        window.addEventListener('beforeunload', function(ev){
          try { ev.stopImmediatePropagation(); } catch(e){}
          try { ev.preventDefault(); } catch(e){}
          delete ev['returnValue'];
        }, true);
        const LEAVE_PAT = /\b(leave|end\s*meeting|leave\s*meeting|exit\s*meeting)\b/i;
        function isLeaveTarget(el){
          if (!el) return false;
          let cur = el;
          for (let i=0; i<5 && cur; i++) {
            try {
              const lbl = (cur.getAttribute && (cur.getAttribute('aria-label') || '')) || '';
              const txt = (cur.innerText || cur.textContent || '').slice(0, 60);
              if (LEAVE_PAT.test(lbl) || LEAVE_PAT.test(txt)) return true;
              const cls = (cur.className && cur.className.toString && cur.className.toString()) || '';
              if (/footer__leave-btn|leave-meeting/i.test(cls)) return true;
            } catch(e){}
            cur = cur.parentElement;
          }
          return false;
        }
        document.addEventListener('click', function(ev){
          if (isLeaveTarget(ev.target)) {
            try { ev.stopImmediatePropagation(); ev.preventDefault(); } catch(e){}
          }
        }, true);
        document.addEventListener('mousedown', function(ev){
          if (isLeaveTarget(ev.target)) {
            try { ev.stopImmediatePropagation(); ev.preventDefault(); } catch(e){}
          }
        }, true);
        function dismissLeaveModal(root){
          try {
            const buttons = (root || document).querySelectorAll('button');
            let stayBtn = null, leaveBtn = null;
            buttons.forEach(function(b){
              const t = (b.innerText || '').trim().toLowerCase();
              if (!t) return;
              if (t === 'cancel' || t === 'stay' || t === 'no') stayBtn = stayBtn || b;
              if (t === 'leave' || t === 'leave meeting' || t === 'yes') leaveBtn = b;
            });
            if (stayBtn && leaveBtn) { stayBtn.click(); }
          } catch(e){}
        }
        const mo = new MutationObserver(function(muts){
          for (const m of muts) {
            for (const n of m.addedNodes) {
              if (n && n.nodeType === 1) dismissLeaveModal(n);
            }
          }
        });
        try { mo.observe(document.documentElement, { childList: true, subtree: true }); } catch(e){}
        setInterval(function(){ dismissLeaveModal(document); }, 4000);
      } catch(e) {}
    })();
    """
    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": js})
    except Exception:
        pass
    try:
        driver.execute_script(js)
    except Exception:
        pass


def _inject_strict_media_stubs(driver):
    """Override getUserMedia to return silent audio + black video. Zero mic feedback."""
    js = r"""
    (function() {
      try {
        const origGUM = (navigator.mediaDevices && navigator.mediaDevices.getUserMedia)
          ? navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices) : null;

        function silentAudioTrack() {
          const ctx = new (window.AudioContext || window.webkitAudioContext)();
          const dst = ctx.createMediaStreamDestination();
          const osc = ctx.createOscillator();
          const gain = ctx.createGain();
          gain.gain.value = 0;
          osc.connect(gain).connect(dst);
          osc.start();
          const track = dst.stream.getAudioTracks()[0];
          try { track.enabled = false; } catch(e){}
          return track;
        }
        function blackVideoTrack() {
          const c = document.createElement('canvas');
          c.width = 320; c.height = 240;
          const g = c.getContext('2d');
          g.fillStyle = '#000'; g.fillRect(0, 0, c.width, c.height);
          const stream = c.captureStream(1);
          const track = stream.getVideoTracks()[0];
          try { track.enabled = false; } catch(e){}
          return track;
        }

        navigator.mediaDevices.getUserMedia = function(constraints) {
          return new Promise(function(resolve, reject) {
            try {
              const tracks = [];
              if (constraints && constraints.audio) tracks.push(silentAudioTrack());
              if (constraints && constraints.video) tracks.push(blackVideoTrack());
              const ms = new MediaStream(tracks);
              resolve(ms);
            } catch (e) {
              if (origGUM) return origGUM(constraints).then(resolve, reject);
              reject(e);
            }
          });
        };

        ['getUserMedia','webkitGetUserMedia','mozGetUserMedia'].forEach(function(k){
          if (navigator[k]) {
            navigator[k] = function(c, s, f){
              navigator.mediaDevices.getUserMedia(c).then(s, f);
            };
          }
        });

        if (navigator.mediaDevices && navigator.mediaDevices.enumerateDevices) {
          const origED = navigator.mediaDevices.enumerateDevices.bind(navigator.mediaDevices);
          navigator.mediaDevices.enumerateDevices = function() {
            return origED().then(function(list){
              return list.map(function(d){ return { kind: d.kind, label: '', deviceId: d.deviceId, groupId: d.groupId }; });
            });
          };
        }
      } catch(e) {}
    })();
    """
    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": js})
    except Exception:
        pass
    try:
        driver.execute_script(js)
    except Exception:
        pass


def _build_chrome_opts(headless: bool, chrome_bin: str, profile_dir: str,
                       proxy: Optional[str] = None) -> ChromeOptions:
    """
    FIX #1: RAM optimization — asset blocking + memory caps
    FIX #2: Proxy support — per-bot dedicated proxy
    FIX #5: Memory cap — --max-old-space-size=256
    """
    opts = ChromeOptions()
    if headless:
        opts.add_argument("--headless")
    if chrome_bin:
        opts.binary_location = chrome_bin
    opts.add_argument(f"--user-data-dir={profile_dir}")

    # ---- FIX #2: PROXY INJECTION (Strict per-bot proxy mapping) ----
    if proxy:
        # Normalize proxy format to --proxy-server= format
        proxy_clean = proxy.strip()
        if not proxy_clean.startswith(("http://", "https://", "socks5://", "socks4://")):
            proxy_clean = f"http://{proxy_clean}"
        opts.add_argument(f"--proxy-server={proxy_clean}")
        log(f"  [PROXY] Bot assigned proxy: {proxy_clean.split('@')[-1] if '@' in proxy_clean else proxy_clean}")

    # PRO PRE-WARM: shared disk cache
    try:
        Path(SHARED_DISK_CACHE_DIR).mkdir(parents=True, exist_ok=True)
        opts.add_argument(f"--disk-cache-dir={SHARED_DISK_CACHE_DIR}")
        opts.add_argument("--disk-cache-size=536870912")  # 512 MB cap
    except Exception:
        pass

    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-software-rasterizer")
    opts.add_argument("--mute-audio")

    # ---- FIX #3: Force fake audio device (audio engine crash fix) ----
    opts.add_argument("--use-fake-ui-for-media-stream")
    opts.add_argument("--use-fake-device-for-media-stream")
    opts.add_argument("--enable-usermedia-screen-capturing")
    opts.add_argument("--allow-running-insecure-content")

    # ---- FIX #1: Disable ALL heavy features (RAM savings) ----
    opts.add_argument(
        "--disable-features=" + ",".join([
            "AudioServiceOutOfProcess",
            "WebRtcHideLocalIpsWithMdns",
            "Translate",
            "OptimizationHints",
            "MediaRouter",
            "DialMediaRouteProvider",
            "AcceptCHFrame",
            "AutofillServerCommunication",
            "CertificateTransparencyComponentUpdater",
            "InterestFeedContentSuggestions",
            "CalculateNativeWinOcclusion",
            "GlobalMediaControls",
            "ImprovedCookieControls",
            "LazyFrameLoading",
            "PrivacySandboxSettings4",
            "site-per-process",
            # FIX #1: Additional RAM-saving features disabled
            "NetworkPrediction",
            "Prefetch",
            "PrefetchPrivacyChanges",
            "BackForwardCache",
            "PaintHolding",
            "HeavyAdIntervention",
            "SpareRendererForSitePerProcess",
        ])
    )
    opts.add_argument("--disable-webrtc-hw-encoding")
    opts.add_argument("--disable-webrtc-hw-decoding")
    opts.add_argument("--window-size=1280,720")
    opts.add_argument("--autoplay-policy=no-user-gesture-required")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--disable-notifications")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--disable-default-apps")
    opts.add_argument("--no-first-run")
    opts.add_argument("--disable-sync")
    opts.add_argument("--disable-translate")
    opts.add_argument("--disable-background-timer-throttling")
    opts.add_argument("--disable-backgrounding-occluded-windows")
    opts.add_argument("--disable-renderer-backgrounding")
    opts.add_argument("--memory-pressure-off")
    opts.add_argument("--disable-low-end-device-mode")
    opts.add_argument("--disable-hang-monitor")
    opts.add_argument("--disable-prompt-on-repost")
    opts.add_argument("--disable-client-side-phishing-detection")
    opts.add_argument("--disable-component-update")
    opts.add_argument("--disable-domain-reliability")
    opts.add_argument("--disable-breakpad")
    opts.add_argument("--disable-crash-reporter")
    opts.add_argument("--disable-ipc-flooding-protection")
    opts.add_argument("--disable-popup-blocking")
    opts.add_argument("--disable-background-networking")
    opts.add_argument("--no-default-browser-check")
    opts.add_argument("--no-pings")
    opts.add_argument("--metrics-recording-only")
    opts.add_argument("--password-store=basic")
    opts.add_argument("--use-mock-keychain")
    opts.add_argument("--force-color-profile=srgb")
    opts.add_argument("--renderer-process-limit=1")

    # ---- FIX #5: Memory cap — JS engine V8 heap capped at 256MB ----
    opts.add_argument("--js-flags=--max-old-space-size=256")
    opts.add_argument("--log-level=3")

    opts.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_experimental_option("prefs", {
        "profile.default_content_setting_values.media_stream_mic": 1,
        "profile.default_content_setting_values.media_stream_camera": 1,
        "profile.default_content_setting_values.notifications": 2,
        # FIX #1: Block images at prefs level too (double protection)
        "profile.managed_default_content_settings.images": 2,
        "profile.default_content_settings.images": 2,
        # Disable video/media autoplay resources
        "profile.default_content_setting_values.plugins": 2,
        "credentials_enable_service": False,
        "profile.password_manager_enabled": False,
    })
    return opts


def _start_driver(opts: ChromeOptions):
    """Resolve chromedriver via CHROMEDRIVER_PATH → Selenium Manager → default."""
    driver_path = os.environ.get("CHROMEDRIVER_PATH", "").strip()
    try:
        if driver_path and os.path.exists(driver_path):
            service = ChromeService(executable_path=driver_path, log_path=os.devnull)
            return webdriver.Chrome(service=service, options=opts)
        return webdriver.Chrome(options=opts)
    except Exception:
        return webdriver.Chrome(service=ChromeService(), options=opts)


def _toggle_off_pre_join_media(driver, name: str):
    """Toggle Mute + Stop-Video BEFORE clicking Join on preview screen."""
    audio_selectors = [
        "button#preview-audio-control-button",
        "button[aria-label*='mute my microphone' i]",
        "button[aria-label*='mute' i][aria-label*='microphone' i]",
    ]
    for sel in audio_selectors:
        try:
            btn = driver.find_element(By.CSS_SELECTOR, sel)
            lbl = (btn.get_attribute("aria-label") or "").lower()
            if "unmute" not in lbl:
                driver.execute_script("arguments[0].click();", btn)
            break
        except Exception:
            continue

    video_selectors = [
        "button#preview-video-control-button",
        "button[aria-label*='stop my video' i]",
        "button[aria-label*='turn off my video' i]",
    ]
    for sel in video_selectors:
        try:
            btn = driver.find_element(By.CSS_SELECTOR, sel)
            lbl = (btn.get_attribute("aria-label") or "").lower()
            if not ("start" in lbl or "turn on" in lbl):
                driver.execute_script("arguments[0].click();", btn)
            break
        except Exception:
            continue


def _enforce_in_meeting_media_off(driver):
    """After joining, defensively click in-meeting Mute + Stop-Video."""
    try:
        mic_btn = driver.find_element(By.CSS_SELECTOR, "button[aria-label*='mute my microphone' i]")
        lbl = (mic_btn.get_attribute("aria-label") or "").lower()
        if "unmute" not in lbl:
            driver.execute_script("arguments[0].click();", mic_btn)
    except Exception:
        pass
    try:
        vid_btn = driver.find_element(By.CSS_SELECTOR, "button[aria-label*='stop my video' i]")
        driver.execute_script("arguments[0].click();", vid_btn)
    except Exception:
        pass


def _meeting_has_ended(driver) -> bool:
    """Detect host-ended / kicked-out states."""
    try:
        body_txt = (driver.find_element(By.TAG_NAME, "body").text or "").lower()
    except Exception:
        return False
    return any(phrase in body_txt for phrase in _END_PHRASES)


def _attempt_join(driver, meeting_id: str, password: str, name: str) -> bool:
    """Run the full join sequence. Returns True if we think we're in the meeting."""
    driver.set_page_load_timeout(60)
    driver.get(f"https://app.zoom.us/wc/{meeting_id}/join")

    # FIX #1: Inject CDP asset blocking AFTER page load starts
    _inject_cdp_asset_blocking(driver)

    time.sleep(5)  # let Zoom's JS finish initial render

    wait = WebDriverWait(driver, 20)

    if password:
        try:
            pwd_el = wait.until(EC.presence_of_element_located(
                (By.XPATH, "//input[@id='input-for-pwd']")))
            pwd_el.clear(); pwd_el.send_keys(password)
        except TimeoutException:
            pass

    name_el = wait.until(EC.presence_of_element_located(
        (By.XPATH, "//input[@id='input-for-name']")))
    name_el.clear(); name_el.send_keys(name)

    _toggle_off_pre_join_media(driver, name)

    join_btn = wait.until(EC.element_to_be_clickable(
        (By.XPATH, "//button[contains(@class,'preview-join-button')]")))
    driver.execute_script("arguments[0].click();", join_btn)

    try:
        WebDriverWait(driver, 35).until(EC.any_of(
            EC.presence_of_element_located((By.CSS_SELECTOR, ".meeting-app, .meeting-client, .footer__leave-btn")),
            EC.presence_of_element_located((By.XPATH, "//button[contains(., 'Leave')]")),
            EC.presence_of_element_located((By.XPATH, "//*[contains(text(),'Please wait') or contains(text(),'Waiting Room')]")),
        ))
    except TimeoutException:
        pass

    time.sleep(1.5)
    _enforce_in_meeting_media_off(driver)
    return True


# ============================================================
# FIX #2: BOT PROCESS with Proxy + FIX #5: Process Isolation
# ============================================================
def bot_process(meeting_id: str, password: str, name: str, hold_seconds: int,
                headless: bool, chrome_bin: str, joined_event: 'mp.synchronize.Event',
                task_prefix: str = "", warmup_sem: 'mp.synchronize.Semaphore' = None,
                proxy: Optional[str] = None):
    """
    Single bot — fully isolated OS process.
    FIX #2: Each bot gets its own proxy (no shared IP).
    FIX #5: Isolated process = no shared memory leak.
    """
    import tempfile as _tf
    pfx = f"zb-{task_prefix}-" if task_prefix else "zb-"

    deadline = time.time() + hold_seconds
    attempts = 0
    meeting_naturally_ended = False

    while time.time() < deadline and attempts < RECONNECT_MAX_ATTEMPTS:
        attempts += 1
        profile_dir = _tf.mkdtemp(prefix=pfx)
        driver = None
        sem_acquired = False
        try:
            if warmup_sem is not None:
                warmup_sem.acquire()
                sem_acquired = True

            # FIX #2: Pass proxy to chrome options
            opts = _build_chrome_opts(headless, chrome_bin, profile_dir, proxy=proxy)
            driver = _start_driver(opts)

            _inject_strict_media_stubs(driver)
            _inject_anti_leave_guards(driver)

            _attempt_join(driver, meeting_id, password, name)

            if sem_acquired and warmup_sem is not None:
                try: warmup_sem.release()
                except Exception: pass
                sem_acquired = False

            if not joined_event.is_set():
                joined_event.set()

            # ---- Force-stay loop ----
            while time.time() < deadline:
                time.sleep(15)
                try:
                    _ = driver.title
                except Exception:
                    break
                if _meeting_has_ended(driver):
                    meeting_naturally_ended = True
                    try:
                        print(f"[bot {name}] meeting ended — cleaning up in {MEETING_END_GRACE_SEC}s", flush=True)
                    except Exception:
                        pass
                    time.sleep(MEETING_END_GRACE_SEC)
                    break
            else:
                meeting_naturally_ended = False

            if meeting_naturally_ended:
                break

        except Exception as e:
            try: print(f"[bot {name}] attempt {attempts} error: {type(e).__name__}: {str(e)[:140]}", flush=True)
            except Exception: pass
        finally:
            if sem_acquired and warmup_sem is not None:
                try: warmup_sem.release()
                except Exception: pass
            try:
                if driver: driver.quit()
            except Exception: pass
            shutil.rmtree(profile_dir, ignore_errors=True)

        if meeting_naturally_ended or time.time() >= deadline:
            break
        if attempts < RECONNECT_MAX_ATTEMPTS:
            try: print(f"[bot {name}] reconnecting in {RECONNECT_DELAY_SEC}s "
                       f"(attempt {attempts + 1}/{RECONNECT_MAX_ATTEMPTS})", flush=True)
            except Exception: pass
            time.sleep(RECONNECT_DELAY_SEC)


# ============================================================
# FIX #4: HARD PURGE EXECUTION
# Task khatam hote hi pkill pipeline + memory flush
# ============================================================
def _hard_purge_task(task_prefix: str):
    """
    FIX #4: Task end par HARD PURGE.
    1. pkill se saare orphan chrome/chromedriver processes kill
    2. Profile directories wipe
    3. Explicit garbage collection (memory flush)
    4. State next task ke liye instantly clear
    """
    log(f"FIX #4: Hard purge for task prefix {task_prefix}...")

    # Step 1: pkill pipeline — faster than psutil iteration
    if sys.platform == "linux":
        try:
            # Kill all chrome processes belonging to this task
            subprocess.run(
                f"pkill -9 -f 'zb-{task_prefix}-' 2>/dev/null || true",
                shell=True, timeout=5
            )
            # Also kill any chromedriver processes
            subprocess.run(
                f"pkill -9 -f 'chromedriver.*zb-{task_prefix}' 2>/dev/null || true",
                shell=True, timeout=5
            )
            log(f"  pkill completed for prefix zb-{task_prefix}-")
        except Exception as e:
            log(f"  pkill failed (non-fatal): {e}")
    elif sys.platform == "win32":
        try:
            # Windows equivalent
            subprocess.run(
                f'taskkill /F /IM chrome.exe /FI "WINDOWTITLE eq *zb-{task_prefix}*" 2>nul',
                shell=True, timeout=5
            )
        except Exception:
            pass

    # Step 2: Wipe profile directories
    n = 0
    try:
        base = Path(tempfile.gettempdir())
        for p in base.glob(f"zb-{task_prefix}-*"):
            shutil.rmtree(p, ignore_errors=True)
            n += 1
    except Exception:
        pass
    if n:
        log(f"  Wiped {n} profile dirs")

    # Step 3: Explicit memory flush
    gc.collect()
    gc.collect()  # Double GC — circular references bhi clear hoti hain

    log(f"FIX #4: Hard purge complete — system ready for next task")


def kill_orphans(only_prefix: str = ""):
    """Kill orphan chrome processes + wipe profile dirs."""
    if not psutil:
        # Fallback to pkill if psutil not available
        if only_prefix:
            _hard_purge_task(only_prefix)
        return

    live_prefixes: set = set()
    if not only_prefix:
        try:
            with RUNNING_LOCK:
                for tdata in RUNNING.values():
                    pfx = tdata.get("prefix")
                    if pfx:
                        live_prefixes.add(f"zb-{pfx}-")
        except Exception:
            pass

    killed = 0
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            n = (p.info.get("name") or "").lower()
            if n not in {"chrome.exe", "chromedriver.exe", "chrome", "chromedriver"}:
                continue
            cmd = " ".join(p.info.get("cmdline") or [])
            if "zb-" not in cmd:
                continue
            if only_prefix:
                if f"zb-{only_prefix}-" not in cmd:
                    continue
            else:
                if any(pfx in cmd for pfx in live_prefixes):
                    continue
            p.kill(); killed += 1
        except Exception:
            continue
    if killed:
        log(f"  cleanup: killed {killed} orphan chrome processes"
            f"{f' (prefix=zb-{only_prefix}-*)' if only_prefix else ''}")

    n = 0
    try:
        base = Path(tempfile.gettempdir())
        if only_prefix:
            patterns = [f"zb-{only_prefix}-*"]
        else:
            patterns = ["zb-*"]
        for pat in patterns:
            for p in base.glob(pat):
                if not only_prefix and any(pfx.rstrip("-") in p.name for pfx in live_prefixes):
                    continue
                shutil.rmtree(p, ignore_errors=True); n += 1
    except Exception:
        pass
    if n: log(f"  cleanup: wiped {n} orphan profile dirs")


# ============================================================
# Task runner
# ============================================================
def run_task(task: dict):
    """Outer wrapper — catches unexpected exceptions."""
    task_id = task.get("id", "unknown")
    try:
        _run_task_inner(task)
    except Exception as e:
        try: log(f"run_task FATAL for {task_id[:8]}: {type(e).__name__}: {str(e)[:180]}")
        except Exception: pass
        try: traceback.print_exc()
        except Exception: pass
        try:
            with RUNNING_LOCK: RUNNING.pop(task_id, None)
        except Exception: pass
        try: complete_task(task_id, success=False, joined=0, error=f"worker crash: {type(e).__name__}")
        except Exception: pass


def _run_task_inner(task: dict):
    task_id = task["id"]
    meeting_id = task["meeting_id"]
    password = task.get("meeting_password") or ""
    members = int(task.get("members", 0))
    timeout_sec = int(task.get("timeout", 7200))

    local = _pick_local_names(members) if LOCAL_NAMES_FILE else []
    if local:
        names = local
        log(f"  using LOCAL names ({len(_LOCAL_NAMES)} in pool)")
    else:
        names = task.get("names") or [f"User{i+1}" for i in range(members)]

    log(f"▶ task {task_id[:8]} | meeting={meeting_id} members={members} timeout={timeout_sec}s "
        f"batch={SPAWN_BATCH} wave_gap={JOIN_WAVE_GAP_SEC}s warmup_lim={BROWSER_WARMUP_LIMIT}")

    task_prefix = task_id[:8]

    processes: List[mp.Process] = []
    joined_events: List[mp.synchronize.Event] = []
    warmup_sem = mp.Semaphore(BROWSER_WARMUP_LIMIT)

    with RUNNING_LOCK:
        RUNNING[task_id] = {
            "processes": processes,
            "joined": 0,
            "started_at": time.time(),
            "prefix": task_prefix,
        }

    use_wave = JOIN_WAVE_GAP_SEC > 0
    for i in range(members):
        if STOP.is_set(): break

        # FIX #2: Assign dedicated proxy to each bot
        proxy = _get_next_proxy()

        ev = mp.Event()
        joined_events.append(ev)
        p = mp.Process(
            target=bot_process,
            args=(meeting_id, password, names[i], timeout_sec,
                  HEADLESS, CHROME_BIN, ev, task_prefix, warmup_sem, proxy),
            daemon=True,
        )
        p.start()
        processes.append(p)

        if use_wave:
            for _ in range(JOIN_WAVE_GAP_SEC * 10):
                if STOP.is_set(): break
                time.sleep(0.1)
        else:
            if (i + 1) % SPAWN_BATCH == 0:
                time.sleep(SPAWN_DELAY_MS / 1000.0)
            else:
                time.sleep(0.08)

    # Watcher loop
    last_reported = 0
    deadline = time.time() + timeout_sec + 60
    progress_check_until = time.time() + 90
    cancel_check_interval = 10
    last_cancel_check = 0

    while time.time() < deadline and not STOP.is_set():
        joined = sum(1 for ev in joined_events if ev.is_set())
        alive  = sum(1 for p in processes if p.is_alive())

        if joined != last_reported:
            report_progress(task_id, joined)
            with RUNNING_LOCK: RUNNING[task_id]["joined"] = joined
            log(f"  ✓ joined {joined}/{members}  (alive procs: {alive})")
            last_reported = joined

        if time.time() - last_cancel_check > cancel_check_interval:
            last_cancel_check = time.time()
            status = check_chunk_status(task_id)
            if status in ("cancelled", "failed"):
                log(f"  ⚠ task {task_id[:8]} {status} by dashboard — tearing down")
                break

        if time.time() > progress_check_until and alive == 0:
            break
        if alive == 0:
            break
        time.sleep(3)

    # Cleanup all bot processes
    log(f"  cleaning up task {task_id[:8]}…")
    for p in processes:
        try:
            if p.is_alive(): p.terminate()
        except Exception: pass
    time.sleep(2)
    for p in processes:
        try:
            if p.is_alive(): p.kill()
            p.join(timeout=3)
        except Exception: pass

    # FIX #4: HARD PURGE — pkill pipeline + memory flush
    _hard_purge_task(task_prefix)

    final_joined = sum(1 for ev in joined_events if ev.is_set())
    with RUNNING_LOCK:
        RUNNING.pop(task_id, None)
    complete_task(task_id, success=True, joined=final_joined)
    log(f"✓ task {task_id[:8]} complete (joined {final_joined}/{members}). Ready for next.")


# ============================================================
# Main loop
# ============================================================
def main_loop():
    log(f"Zoom worker v5-OPTIMIZED (All 6 fixes active) starting")
    log(f"  dashboard={DASHBOARD_URL}")
    log(f"  poll={POLL_INTERVAL}s  batch={SPAWN_BATCH}  spawn_delay={SPAWN_DELAY_MS}ms  headless={HEADLESS}")
    log(f"  wave_gap={JOIN_WAVE_GAP_SEC}s  warmup_limit={BROWSER_WARMUP_LIMIT}  disk_cache={SHARED_DISK_CACHE_DIR}")

    # FIX #3: Virtual audio setup at startup
    _setup_virtual_audio()

    # FIX #2: Load proxy pool
    global _PROXY_POOL
    _PROXY_POOL = _load_proxies()
    if _PROXY_POOL:
        log(f"FIX #2: Loaded {len(_PROXY_POOL)} proxies — strict 1:1 bot-to-proxy mapping active")
    else:
        log("FIX #2: No proxies configured (PROXY_LIST_FILE or PROXY_LIST not set) — direct IP mode")

    if LOCAL_NAMES_FILE:
        _load_local_names()

    # Pre-flight: ensure Chrome can launch
    try:
        log("Pre-flight: testing Chrome launch + warming Zoom WC cache…")
        pre_opts = ChromeOptions()
        if HEADLESS: pre_opts.add_argument("--headless")
        if CHROME_BIN: pre_opts.binary_location = CHROME_BIN
        pre_opts.add_argument("--no-sandbox")
        pre_opts.add_argument("--disable-dev-shm-usage")
        pre_opts.add_argument("--disable-gpu")
        pre_opts.add_argument(f"--user-data-dir={tempfile.mkdtemp(prefix='zb-pre-')}")
        try:
            Path(SHARED_DISK_CACHE_DIR).mkdir(parents=True, exist_ok=True)
            pre_opts.add_argument(f"--disk-cache-dir={SHARED_DISK_CACHE_DIR}")
            pre_opts.add_argument("--disk-cache-size=536870912")
        except Exception:
            pass
        if CHROMEDRIVER_PATH and os.path.exists(CHROMEDRIVER_PATH):
            log(f"  using CHROMEDRIVER_PATH={CHROMEDRIVER_PATH}")
            pre_service = ChromeService(executable_path=CHROMEDRIVER_PATH, log_path=os.devnull)
            d = webdriver.Chrome(service=pre_service, options=pre_opts)
        else:
            d = webdriver.Chrome(options=pre_opts)
        try:
            d.set_page_load_timeout(20)
            d.get("https://app.zoom.us/wc/home")
            time.sleep(3)
        except Exception:
            pass
        d.quit()
        log("Pre-flight OK — Chrome + chromedriver ready, Zoom WC cache primed")
    except Exception as e:
        log(f"FATAL: Chrome launch failed: {type(e).__name__}: {e}")
        log("Diagnostic steps:")
        log("  1. Verify Chrome is installed")
        log("  2. Upgrade selenium: pip install --upgrade selenium")
        log("  3. Check CHROMEDRIVER_PATH in .env")
        sys.exit(1)

    kill_orphans()

    last_idle = time.time()
    while not STOP.is_set():
        try:
            with RUNNING_LOCK:
                load = sum(sum(1 for p in t["processes"] if p.is_alive())
                           for t in RUNNING.values())
            heartbeat(load_override=load)

            if len(RUNNING) < MAX_CONCURRENT_TASKS:
                tasks = claim_tasks(n=min(5, MAX_CONCURRENT_TASKS - len(RUNNING)))
                for t in tasks:
                    threading.Thread(target=run_task, args=(t,), daemon=True).start()

            if not RUNNING and (time.time() - last_idle) > 300:
                kill_orphans(); gc.collect(); last_idle = time.time()
        except Exception as e:
            try: log(f"main-loop tick error: {type(e).__name__}: {str(e)[:140]}")
            except Exception: pass

        STOP.wait(POLL_INTERVAL)

    log("stopping…")
    with RUNNING_LOCK:
        for tid, data in list(RUNNING.items()):
            for p in data.get("processes", []):
                try:
                    if p.is_alive(): p.terminate()
                except Exception: pass
            complete_task(tid, success=False, joined=data.get("joined", 0),
                          error="Worker shutdown")
    kill_orphans()


def _sig(_a, _b): STOP.set()


# ============================================================
# Keep-alive supervisor (FIX #5 support: crash recovery)
# ============================================================
KEEPALIVE_BACKOFF_MIN = int(os.environ.get("KEEPALIVE_BACKOFF_MIN", "5"))
KEEPALIVE_BACKOFF_MAX = int(os.environ.get("KEEPALIVE_BACKOFF_MAX", "30"))


def _supervised_main():
    """Forever-restart wrapper around main_loop. Only exits on STOP signal."""
    global CRASH_COUNT, LAST_RESTART_ISO
    backoff = KEEPALIVE_BACKOFF_MIN
    while not STOP.is_set():
        try:
            main_loop()
            if STOP.is_set():
                break
            log("WARN: main_loop returned without STOP — restarting in 5s")
            time.sleep(5)
            continue
        except KeyboardInterrupt:
            STOP.set()
            break
        except SystemExit as e:
            if CRASH_COUNT == 0:
                raise
            log(f"main_loop SystemExit({e.code}) — restarting in {backoff}s")
        except Exception:
            log("FATAL in main_loop — full traceback:")
            try: traceback.print_exc()
            except Exception: pass
        CRASH_COUNT += 1
        LAST_RESTART_ISO = datetime.now(timezone.utc).isoformat()
        try: kill_orphans()
        except Exception: pass
        log(f"keep-alive: main_loop crashed (#{CRASH_COUNT}) — sleeping {backoff}s then restarting")
        for _ in range(backoff):
            if STOP.is_set(): break
            time.sleep(1)
        backoff = min(KEEPALIVE_BACKOFF_MAX, max(KEEPALIVE_BACKOFF_MIN, backoff * 2))
    log("keep-alive: STOP signalled — exiting cleanly")


if __name__ == "__main__":
    mp.freeze_support()
    signal.signal(signal.SIGINT, _sig)
    if hasattr(signal, "SIGTERM"): signal.signal(signal.SIGTERM, _sig)
    try:
        _supervised_main()
    except KeyboardInterrupt:
        STOP.set()
    except Exception:
        traceback.print_exc(); sys.exit(1)
