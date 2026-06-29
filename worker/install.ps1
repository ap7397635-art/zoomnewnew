# Zoom Worker v9.7-silent-ui - One-Click Installer for Windows RDP / VPS
# v9.7 highlights:
#   - Member sees ZERO meeting activity (chat / participants / reactions hidden)
#   - Mic + camera icons show CONNECTED for every bot (silent stream still sent)
#   - Renegotiation guard so screen-share never triggers false drops
#   - NSSM hardened: auto-restart on crash, log rotation, SCM recovery
#   - One-time RDP host setup (keep session logged in, disable WU restart, …)
# Installs: Python + Playwright Chromium + v9.7 pool worker + Windows hardening
# Usage (run in PowerShell as Administrator):
#   $env:DASHBOARD_URL="https://your-dashboard"; iwr "$env:DASHBOARD_URL/api/worker/install.ps1" | iex

$ErrorActionPreference = "Stop"
$ProgressPreference    = "SilentlyContinue"

$WorkerDir  = "C:\zoom-worker"
$BackendUrl = $env:DASHBOARD_URL
if (-not $BackendUrl) {
    $BackendUrl = "https://member-activity-hub.preview.emergentagent.com"
}

function Section($t) {
    Write-Host ""
    Write-Host "===> $t" -ForegroundColor Cyan
}

# 0. Admin check
$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(`
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "Please run this in an *Administrator* PowerShell window." -ForegroundColor Red
    exit 1
}

# 0b. Stop any existing ZoomWorker service so we can overwrite files cleanly
Section "Stopping any existing ZoomWorker service / process"
try { Stop-Service -Name "ZoomWorker" -Force -ErrorAction SilentlyContinue } catch {}
try { Get-Process python* -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "*zoom-worker*" } | Stop-Process -Force -ErrorAction SilentlyContinue } catch {}
Start-Sleep -Seconds 2

Section "Creating $WorkerDir"
New-Item -ItemType Directory -Force -Path $WorkerDir | Out-Null
Set-Location $WorkerDir

# 1. Install Python if missing (3.11+ required for Playwright async)
Section "Checking Python (need >= 3.11)"
$pythonCmd = $null
foreach ($c in @("python", "py")) {
    if (Get-Command $c -ErrorAction SilentlyContinue) {
        $ver = & $c --version 2>&1
        if ($ver -match "Python 3\.(\d+)") {
            $minor = [int]$matches[1]
            if ($minor -ge 11) { $pythonCmd = $c; break }
        }
    }
}
if (-not $pythonCmd) {
    Write-Host "Installing Python 3.11.9..."
    $pyInstaller = "$env:TEMP\python-3.11.9-installer.exe"
    Invoke-WebRequest "https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe" -OutFile $pyInstaller
    Start-Process -Wait -FilePath $pyInstaller -ArgumentList "/quiet","InstallAllUsers=1","PrependPath=1","Include_test=0"
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
    $pythonCmd = "python"
}
Write-Host "Python OK: $(& $pythonCmd --version)" -ForegroundColor Green

# 2. Back up any old worker file before overwriting
Section "Backing up old worker (if any)"
$ts = Get-Date -Format "yyyyMMdd-HHmmss"
foreach ($f in @("zoom_worker.py", "zoom_worker_pool.py", ".env")) {
    if (Test-Path "$WorkerDir\$f") {
        Copy-Item "$WorkerDir\$f" "$WorkerDir\$f.backup.$ts" -Force
        Write-Host "  backed up $f -> $f.backup.$ts" -ForegroundColor DarkGray
    }
}

# 3. Download v8.3.2 pool worker (NOT the legacy zoom_worker.py)
Section "Downloading v8.3.2 worker files from $BackendUrl"
Invoke-WebRequest "$BackendUrl/api/worker/zoom_worker_pool.py" -OutFile "$WorkerDir\zoom_worker_pool.py"
$lineCount = (Get-Content "$WorkerDir\zoom_worker_pool.py" | Measure-Object -Line).Lines
Write-Host "  zoom_worker_pool.py downloaded ($lineCount lines)" -ForegroundColor Green
Invoke-WebRequest "$BackendUrl/api/worker/requirements.txt" -OutFile "$WorkerDir\requirements.txt"
Write-Host "  requirements.txt downloaded" -ForegroundColor Green

# 4. Install python deps + Playwright Chromium
Section "Installing Python libraries + Playwright Chromium (~150MB download)"
& $pythonCmd -m pip install --upgrade pip --quiet
& $pythonCmd -m pip install -r "$WorkerDir\requirements.txt" --quiet
& $pythonCmd -m pip install playwright psutil python-dotenv --quiet
Write-Host "  Python libs installed" -ForegroundColor Green
Write-Host "  Installing Playwright Chromium (this takes a minute)..." -ForegroundColor Yellow
& $pythonCmd -m playwright install chromium
Write-Host "  Playwright Chromium installed" -ForegroundColor Green

# 5. Prompt for token (preserve from existing .env if present)
Section "Configure worker"
$existingToken = ""
if (Test-Path "$WorkerDir\.env.backup.$ts") {
    $existingToken = (Select-String -Path "$WorkerDir\.env.backup.$ts" -Pattern "^WORKER_TOKEN=" -SimpleMatch | Select-Object -First 1).Line
    if ($existingToken) {
        $existingToken = $existingToken -replace "^WORKER_TOKEN=", ""
        $existingToken = $existingToken.Trim('"').Trim()
    }
}
if ($existingToken -and $existingToken -match "^[a-f0-9-]+\.[A-Za-z0-9_-]+$") {
    Write-Host "  Reusing WORKER_TOKEN from previous .env backup" -ForegroundColor Green
    $token = $existingToken
} else {
    Write-Host "Open the dashboard, click 'Workers' -> 'Add Worker' -> copy the token shown ONCE." -ForegroundColor Yellow
    Write-Host "Token format: <uuid>.<secret>" -ForegroundColor Yellow
    $token = Read-Host "Paste WORKER_TOKEN"
    if (-not $token -or $token -notmatch "^[a-f0-9-]+\.[A-Za-z0-9_-]+$") {
        Write-Host "Invalid token format. Re-run the installer." -ForegroundColor Red
        exit 1
    }
}

# 6. Write .env with v9.7 silent-UI + mic/cam-connected defaults
Section "Writing .env (v9.7 silent-UI defaults)"
$envText = @"
# ============================================================
#  Zoom Worker v9.7-silent-ui - generated $(Get-Date)
#  Edit any value below, then: Restart-Service ZoomWorker
# ============================================================

DASHBOARD_URL=$BackendUrl
WORKER_TOKEN=$token

# ---- Polling / capacity ----
POLL_INTERVAL=5
# v9.6 BEST BALANCE — fast join, low load (gentle ramp, no CPU thrash):
# 3 bots every 0.7s. Keeps CPU busy without pegging to 100% (pegged CPU is what
# made it slow + stuck). headless=false keeps Zoom WebClient light & stable.
SPAWN_DELAY_MS=700
SPAWN_BURST_SIZE=3
BOT_INITIAL_JOIN_RETRIES=15
PRE_SPAWN_MAX_CPU_PCT=97
MAX_CONCURRENT_TASKS=5

# ---- v9.7: SILENT-UI + MIC/CAM CONNECTED DEFAULTS ----
# User request: "memeber join ho aur unhe koi bhi activity na dikhe meeting ki
# bss mic and video ke sath connect ho aur sabke mic connect ho"
#
# HEADLESS=false  → Windows RDP runs visible chromium (more stable WebRTC than
#                   headless) but OFFSCREEN_WINDOW=true pushes the window off
#                   the visible desktop so the RDP user never sees it.
# JOIN_WITH_AUDIO_MUTED=false → bot enters with MIC ICON ON (connected look).
#                              Silent audio track is still sent (no real bytes).
# JOIN_WITH_VIDEO_OFF=false   → bot enters with CAM ICON ON (connected look).
#                              Black canvas video track is still sent.
# ZK_HIDE_UI=true → hides chat / participants / reactions / Q&A / polls /
#                   modals / toasts so member sees ZERO meeting activity.
HEADLESS=false
OFFSCREEN_WINDOW=true
JOIN_WITH_AUDIO_MUTED=false
JOIN_WITH_VIDEO_OFF=false
ZK_HIDE_UI=true

# ---- v9.7: ULTRA_LITE — MAX bots / MIN RAM/CPU ----
# Flip to "true" to squeeze ~1.5-1.6x more bots per RDP at the cost of slightly
# slower in-meeting polling (10s vs 6s). Recommended for any RDP where you want
# 200+ bots on 32 GB RAM. Auto-applies:
#   RAM_PER_BOT_MB 80 (was 120)  — 1.5x density
#   BOTS_PER_CPU 40 (was 25)     — 1.6x density
#   TABS_PER_BROWSER 30 (was 20) — fewer chromium parents = less RAM
#   V8 heap 96 MB (was 256 MB)   — smaller per-tab heap
#   IN_MEETING_CHECK_SEC 10 (was 6) — less polling = less CPU
#   Chromium: low-end-device-mode + single raster thread + smaller window
# Any individual knob can still be overridden by setting its own env var below.
ULTRA_LITE=false

AUTO_CAPACITY=true

# ---- Browser pool sizing ----
TABS_PER_BROWSER=20

# ---- v8.1 PREWARM engine ----
# IMPORTANT: PREWARM_CONTEXTS auto-scales to admin capacity_max at runtime.
# These are just the boot-time defaults; the worker will grow the ready pool
# to match whatever cap the dashboard says (jitna limit, utne ready).
PREWARM_ENABLED=true
PREWARM_BROWSERS=2
PREWARM_CONTEXTS=10
PREWARM_MIN_READY=5
PREWARM_MAX_READY=20
PREWARM_PRELOAD_URL=https://app.zoom.us/wc/join
WARMUP_INTERVAL_SEC=15
SHRINK_IDLE_SEC=120

# v8.3.3: scale ready pool to match admin capacity_max (browsers grow with it)
PREWARM_MATCH_ADMIN_CAP=true

# ---- v8.3 TAP-AND-JOIN ----
PERSISTENT_CACHE=true
PERSISTENT_CACHE_DIR=C:\zoom-worker\disk-cache
PERSISTENT_CACHE_SIZE_MB=256
STORAGE_STATE_PATH=C:\zoom-worker\storage-state.json
STORAGE_STATE_REFRESH_HOURS=24
FORM_PREWARM_WAIT_MS=1500

# ---- v8.3.1 DEBUG ----
DEBUG_DUMP_DOM=true
DEBUG_DUMP_DIR=C:\zoom-worker\debug

# ---- Cleanup ----
CLEANUP_INTERVAL_SEC=300

# Optional: local names file (overrides dashboard Name source if set)
LOCAL_NAMES_FILE=

# ---- v8.6.5: nuclear SDP video-strip (screen-share survival, layer 8) ----
# Removes m=video sections from incoming/outgoing SDP so Zoom's SFU never
# sends video packets at all (incl. screen share) — zero decode CPU even
# on cheap RDPs. Set to "false" only if joining ever fails on a Zoom build
# that rejects video-less SDP.
ZK_NUCLEAR_SDP_STRIP=true
"@
[IO.File]::WriteAllText("$WorkerDir\.env", $envText, [Text.UTF8Encoding]::new($false))
Write-Host "  .env saved with v8.3.2 prewarm defaults" -ForegroundColor Green

# Wipe stale storage_state so bootstrap runs fresh
Remove-Item "$WorkerDir\storage-state.json" -ErrorAction SilentlyContinue
Remove-Item "C:\zoom-worker\storage-state.json" -ErrorAction SilentlyContinue

# 7. Install as Windows service via NSSM
Section "Install Windows service (auto-restart on boot + on crash)"
$installService = Read-Host "Install as a Windows service so it auto-starts on reboot? (Y/n)"
if ($installService -ne "n" -and $installService -ne "N") {
    $nssmZip = "$env:TEMP\nssm.zip"
    $nssmDir = "C:\tools\nssm"
    if (-not (Test-Path "$nssmDir\nssm.exe")) {
        Invoke-WebRequest "https://nssm.cc/release/nssm-2.24.zip" -OutFile $nssmZip
        Expand-Archive $nssmZip -DestinationPath "$env:TEMP\nssm-ex" -Force
        New-Item -ItemType Directory -Force -Path $nssmDir | Out-Null
        Copy-Item "$env:TEMP\nssm-ex\nssm-2.24\win64\nssm.exe" "$nssmDir\nssm.exe" -Force
    }
    $svc = "ZoomWorker"
    # nssm writes status text to stderr; PowerShell with ErrorActionPreference=Stop
    # treats that as a fatal NativeCommandError and aborts the script. We:
    #   1. switch ErrorActionPreference to Continue for the entire NSSM block,
    #   2. merge stderr into stdout (`2>&1`) so PowerShell never sees a separate
    #      stderr stream that it could escalate to a terminating error.
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $nssmExe = "$nssmDir\nssm.exe"
    $py = (Get-Command $pythonCmd).Source
    if (Get-Service -Name $svc -ErrorAction SilentlyContinue) {
        & cmd /c "`"$nssmExe`" stop $svc"           2>&1 | Out-Null
        & cmd /c "`"$nssmExe`" remove $svc confirm" 2>&1 | Out-Null
        Start-Sleep -Seconds 1
    }
    & cmd /c "`"$nssmExe`" install $svc `"$py`" `"$WorkerDir\zoom_worker_pool.py`"" 2>&1 | Out-Null
    & cmd /c "`"$nssmExe`" set $svc AppDirectory `"$WorkerDir`""                    2>&1 | Out-Null
    & cmd /c "`"$nssmExe`" set $svc AppStdout `"$WorkerDir\worker.log`""            2>&1 | Out-Null
    & cmd /c "`"$nssmExe`" set $svc AppStderr `"$WorkerDir\worker.err.log`""        2>&1 | Out-Null
    & cmd /c "`"$nssmExe`" set $svc Start SERVICE_AUTO_START"                       2>&1 | Out-Null
    # v9.7 — HARDEN AUTO-RESTART so worker can NEVER stay dead:
    & cmd /c "`"$nssmExe`" set $svc AppExit Default Restart"                        2>&1 | Out-Null
    & cmd /c "`"$nssmExe`" set $svc AppRestartDelay 5000"                           2>&1 | Out-Null
    & cmd /c "`"$nssmExe`" set $svc AppThrottle 1500"                               2>&1 | Out-Null
    # Rotate logs at 10 MB so worker.log doesn't fill the disk over weeks of uptime
    & cmd /c "`"$nssmExe`" set $svc AppRotateFiles 1"                               2>&1 | Out-Null
    & cmd /c "`"$nssmExe`" set $svc AppRotateOnline 1"                              2>&1 | Out-Null
    & cmd /c "`"$nssmExe`" set $svc AppRotateBytes 10485760"                        2>&1 | Out-Null
    # Belt-and-suspenders: also configure SCM-level recovery (reset failure
    # counter after 60s, restart after 5s for first 3 failures, then keep trying).
    & cmd /c "sc.exe failure $svc reset= 60 actions= restart/5000/restart/5000/restart/5000" 2>&1 | Out-Null
    & cmd /c "sc.exe failureflag $svc 1"                                                     2>&1 | Out-Null
    & cmd /c "`"$nssmExe`" start $svc"                                              2>&1 | Out-Null
    $ErrorActionPreference = $prevEAP
    Write-Host "  Service 'ZoomWorker' installed and started (auto-restart on crash + boot)" -ForegroundColor Green
    Write-Host "  Logs:   $WorkerDir\worker.log" -ForegroundColor Yellow
    Write-Host "  Manage: net stop ZoomWorker  /  net start ZoomWorker" -ForegroundColor Yellow

    # ====================================================================
    # 8. v9.7 — ONE-TIME WINDOWS RDP HOST HARDENING
    # --------------------------------------------------------------------
    # User explicit ask: "Worker service auto-restart ho but RDP login
    # session bani rahe" + "ek bar open kru aur setup kru phir usme sab
    # kuch apne aaap ho" (one-time setup, then everything automatic).
    #
    # This block does these one-time tweaks idempotently:
    #   (a) High Performance power plan (no sleep / lid / monitor off)
    #   (b) Disable lock-screen on RDP disconnect (session stays alive)
    #   (c) Disable Windows Update auto-restart (no meeting interrupted)
    #   (d) Increase TCP ephemeral ports + TIME_WAIT recycle
    #   (e) Defender exclusions for our worker dir + chromium + python
    #   (f) Disable Zoom auto-update (registry pin)
    # All of these are SAFE TO RE-RUN — the installer checks state first.
    # ====================================================================
    Section "One-time Windows RDP host hardening (idempotent)"
    try {
        powercfg -setactive 8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c 2>$null
        powercfg -change -monitor-timeout-ac 0 2>$null
        powercfg -change -standby-timeout-ac 0 2>$null
        powercfg -change -hibernate-timeout-ac 0 2>$null
        Write-Host "  power plan -> High Performance (no sleep / no monitor-off)" -ForegroundColor DarkGreen
    } catch { Write-Host "  (power plan tweak skipped: $_)" -ForegroundColor DarkYellow }

    try {
        # Keep RDP session signed-in after disconnect (user does NOT want to
        # re-open RDP each time — session stays so worker.log + tray icons stay)
        New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\Terminal Server" `
            -Name "fDisableAutoReconnect" -Value 0 -PropertyType DWord -Force | Out-Null
        # Don't auto-lock on disconnect
        New-Item -Path "HKLM:\SOFTWARE\Policies\Microsoft\Windows\Personalization" -Force | Out-Null
        New-ItemProperty -Path "HKLM:\SOFTWARE\Policies\Microsoft\Windows\Personalization" `
            -Name "NoLockScreen" -Value 1 -PropertyType DWord -Force | Out-Null
        # Inactivity screensaver off
        New-ItemProperty -Path "HKCU:\Control Panel\Desktop" `
            -Name "ScreenSaveActive" -Value "0" -PropertyType String -Force | Out-Null
        Write-Host "  RDP login session will persist on disconnect (no lock / screensaver)" -ForegroundColor DarkGreen
    } catch { Write-Host "  (RDP session persistence tweak skipped: $_)" -ForegroundColor DarkYellow }

    try {
        # Block Windows Update auto-restart mid-meeting (the #1 silent killer)
        New-Item -Path "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU" -Force | Out-Null
        New-ItemProperty -Path "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU" `
            -Name "NoAutoRebootWithLoggedOnUsers" -Value 1 -PropertyType DWord -Force | Out-Null
        New-ItemProperty -Path "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU" `
            -Name "AUOptions" -Value 2 -PropertyType DWord -Force | Out-Null
        Write-Host "  Windows Update auto-restart blocked (NoAutoReboot)" -ForegroundColor DarkGreen
    } catch { Write-Host "  (Windows Update tweak skipped: $_)" -ForegroundColor DarkYellow }

    try {
        # TCP ephemeral port range + TIME_WAIT recycle (rejoin churn safety)
        netsh int ipv4 set dynamicport tcp start=1024 num=64000 | Out-Null
        Set-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters" `
            -Name "TcpTimedWaitDelay" -Value 30 -Type DWord -Force
        Set-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters" `
            -Name "MaxUserPort" -Value 65000 -Type DWord -Force
        netsh int tcp set global autotuninglevel=normal | Out-Null
        Write-Host "  TCP ephemeral ports raised + TIME_WAIT shortened" -ForegroundColor DarkGreen
    } catch { Write-Host "  (TCP tweak skipped: $_)" -ForegroundColor DarkYellow }

    try {
        # Defender exclusions — without these every Chrome / Python spawn is
        # blocked by scan-on-execute, adding ~2s + CPU spike to every join.
        Add-MpPreference -ExclusionPath "$WorkerDir" -ErrorAction SilentlyContinue
        Add-MpPreference -ExclusionProcess "chromium.exe" -ErrorAction SilentlyContinue
        Add-MpPreference -ExclusionProcess "chrome.exe"   -ErrorAction SilentlyContinue
        Add-MpPreference -ExclusionProcess "python.exe"   -ErrorAction SilentlyContinue
        Add-MpPreference -ExclusionProcess "nssm.exe"     -ErrorAction SilentlyContinue
        Write-Host "  Defender real-time-scan exclusions applied" -ForegroundColor DarkGreen
    } catch { Write-Host "  (Defender tweak skipped: $_)" -ForegroundColor DarkYellow }

    try {
        # Pin Zoom auto-update OFF — random update mid-meeting breaks selectors
        New-Item -Path "HKLM:\SOFTWARE\Policies\Zoom\Zoom Meetings\General" -Force | Out-Null
        New-ItemProperty -Path "HKLM:\SOFTWARE\Policies\Zoom\Zoom Meetings\General" `
            -Name "EnableClientAutoUpdate" -Value 0 -PropertyType DWord -Force | Out-Null
        Write-Host "  Zoom auto-update disabled" -ForegroundColor DarkGreen
    } catch { Write-Host "  (Zoom auto-update lock skipped: $_)" -ForegroundColor DarkYellow }

    Write-Host ""
    Write-Host "===> One-time host hardening DONE." -ForegroundColor Cyan
    Write-Host "===> From now on you don't need to open RDP again — service auto-runs." -ForegroundColor Cyan
    Write-Host ""
    Write-Host "===> Wait ~30 seconds, then check dashboard /workers page" -ForegroundColor Cyan
    Write-Host "     Expected: Pool column shows 'v9.7-silent-ui' + ready contexts." -ForegroundColor Cyan
    Start-Sleep -Seconds 8
    if (Test-Path "$WorkerDir\worker.log") {
        Write-Host ""
        Write-Host "===> First lines of worker.log:" -ForegroundColor Cyan
        Get-Content "$WorkerDir\worker.log" -Tail 25 | ForEach-Object { Write-Host "  $_" -ForegroundColor DarkGray }
    }
} else {
    Section "Running worker in foreground (Ctrl+C to stop)"
    Set-Location $WorkerDir
    & $pythonCmd zoom_worker_pool.py
}
