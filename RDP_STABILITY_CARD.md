# RDP — Apply After v8.9 Deploy

> Bots ko aisa banaya gaya hai ki **bilkul drop nahi honge** jab tak:
> - Host meeting end na kare
> - Aap dashboard se task cancel na karein
> - Configured meeting timeout expire na ho
>
> Yeh card production deploy ke baad har RDP pe **ek baar** chalana hai.

---

## ⚡ 1. Worker code update (har RDP pe ek baar)

### Windows RDP (PowerShell admin)
```powershell
cd C:\zoom-worker
git pull
# Agar git nahi setup hai, re-run the install one-liner:
$env:DASHBOARD_URL = "https://quick-extract-16.emergent.host"
iwr "$env:DASHBOARD_URL/api/worker/install.ps1" | iex
pm2 restart all
```

### Linux RDP (root)
```bash
cd /opt/finalzoom-worker
git pull
sudo systemctl restart finalzoom-worker
```

> v8.9 ke saath har improved default code mein hard-coded hai. **`.env` mein
> kuch add karne ki zaroorat NAHI hai** — sab kuch already optimal hai.

---

## 🔧 2. OS-level tuning (sirf ek baar, lifetime)

### Linux RDP
```bash
# A. File descriptor + process ulimit raise
sudo tee -a /etc/security/limits.conf <<'EOF'
* soft nofile 131072
* hard nofile 131072
* soft nproc 32768
* hard nproc 32768
EOF

# B. Kernel network tune (rejoins → sockets churn fast)
sudo tee -a /etc/sysctl.d/99-finalzoom.conf <<'EOF'
net.ipv4.ip_local_port_range = 1024 65000
net.ipv4.tcp_tw_reuse = 1
net.ipv4.tcp_fin_timeout = 15
net.core.somaxconn = 4096
fs.file-max = 2097152
EOF
sudo sysctl -p /etc/sysctl.d/99-finalzoom.conf

# C. SWAP OFF (128GB box pe never want swap)
sudo swapoff -a
sudo sed -i '/ swap / s/^/#/' /etc/fstab

# D. Per-service ulimit
sudo systemctl edit finalzoom-worker
# Paste this and save:
#   [Service]
#   LimitNOFILE=131072
#   LimitNPROC=32768
sudo systemctl daemon-reload
sudo systemctl restart finalzoom-worker
```

### Windows RDP — open **PowerShell as Administrator**
```powershell
# A. TCP ephemeral port range raise
netsh int ipv4 set dynamicport tcp start=1024 num=64000
netsh int ipv4 set dynamicport udp start=1024 num=64000

# B. TIME_WAIT timeout 240s → 30s
Set-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters" `
  -Name "TcpTimedWaitDelay" -Value 30 -Type DWord

# C. Max user port (matches Linux ip_local_port_range)
Set-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters" `
  -Name "MaxUserPort" -Value 65000 -Type DWord -Force

# D. TCP receive auto-tuning ON
netsh int tcp set global autotuninglevel=normal

# E. Defender exclusions — without these, scan-on-execute slows every join
Add-MpPreference -ExclusionPath "C:\zoom-worker"
Add-MpPreference -ExclusionProcess "chromium.exe"
Add-MpPreference -ExclusionProcess "chrome.exe"
Add-MpPreference -ExclusionProcess "node.exe"
Add-MpPreference -ExclusionProcess "python.exe"

# F. Block Windows Update auto-reboot during meeting
sc.exe config wuauserv start=disabled
Stop-Service wuauserv -Force -ErrorAction SilentlyContinue

# G. Reboot once (TCP registry changes need restart)
Restart-Computer -Force
```

> **Yeh sab sirf EK BAAR karne hain RDP ki lifetime mein.** Reboot ke baad
> `pm2 list` se workers auto-started hone chahiye.

---

## 📋 3. Host meeting settings (Zoom UI)

Yeh Zoom **host** (meeting creator) ko apne side se karna hai. Sirf ek baar
meeting create karte time:

| Setting | Value |
|---|---|
| Mute participants upon entry | ✅ ON |
| Waiting Room | ❌ OFF |
| Authenticated users only | ❌ OFF |
| Allow removed participants to rejoin | ✅ ON |
| Disable "Join from your browser" link | ❌ OFF (must stay enabled) |
| Participant video on entry | ❌ OFF |
| Meeting duration | Set FULL planned time |

---

## 📊 4. Dashboard side checklist

Open https://quick-extract-16.emergent.host:

- [ ] Workers page → har RDP pe `capacity_max` set:
  - 4 GB / 2 vCPU  RDP → **20**
  - 8 GB / 4 vCPU  RDP → **35**
  - 16 GB / 8 vCPU RDP → **80**
  - 64 GB / 16 vCPU RDP → **200**
  - 128 GB / 16 vCPU RDP → **250** *(NOT 700 — beefy box bhi over-pack mat karo)*
- [ ] Create task time: Distribution Preview check karo
- [ ] Pre-assignment lock karo (manual override) — kisi RDP par cap se zyada na jaaye
- [ ] Task `timeout` field = meeting ki full planned duration (seconds)

---

## ✅ 5. Validation test

1. Small test: **30 bots** ek RDP pe daal ke ek 30-min meeting bana ke check karo
2. Host se **screen share** start karwao → dashboard live distribution dekho
3. Joined count **stable rehna chahiye** (zero drops)
4. Host meeting end kare → saare bots ek saath **clean exit** karenge (no rejoin loop)

Agar koi drop dikhe, RDP pe `tail -f /var/log/finalzoom-worker.log`:
- `dropped, rejoin attempt N` → normal, bot wapas aa raha hai
- `meeting ended by host — exiting cleanly` → meeting genuine end hua
- 5+ "rejoin attempts failed, cooling down" lagataar → us specific RDP pe problem, replace karo

---

## 🔥 v8.9 highlights jo isme already implement hai

1. **Rejoin attempts effectively infinite** (`BOT_REJOIN_MAX=9999`) —
   bot meeting end se pehle drop hi nahi hoga
2. **Tighter detection** — drop dikha to 6s ke andar rejoin start
3. **Faster rejoin** — 1-8s random backoff (was 3-30s)
4. **Inner retry loop** — agar pehla rejoin attempt fail, 5 baar aur try
   karta hai, phir 20s cooldown, fir bahar loop tick aata hai
5. **Page crash recovery** — even if Chromium tab itself crashes, hold loop
   reload + reconnect karega
6. **UA / viewport rotation** — har bot ka alag User-Agent + screen size,
   Zoom ko same-fingerprint kick trigger nahi hota
7. **WebRTC video pre-block** — screen share / participant video receiver
   chromium-level pe disable, CPU 100% spike khatam
8. **CPU spawn gate 80%** — burst CPU 100% nahi touch karega, already-joining
   bots ko CPU mil jayega clean join karne ke liye
9. **Wider end-meeting detection** — host end / "meeting ended" / kick page —
   sab markers cover hain
10. **Slow ramp (4 burst × 200ms)** — stability priority, drama-free join wave

Drops ab sirf TEEN cases mein ho sakte hain:
1. Host ne meeting end kiya (intended)
2. Aapne dashboard se task cancel kiya (intended)
3. Configured timeout pura hua (intended)

— Last updated 2026-05-30
