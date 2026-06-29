"""Quick verification of v9.8 fixes:
1. Task auto-marks 'completed' when ends_at passes (regardless of worker)
2. /tasks/{id}/distribution hides worker info for non-admin
3. Cancelled tasks appear in /tasks/previous (history)
"""
import requests, time, os, sys
from datetime import datetime, timezone, timedelta

API = os.environ.get("API", "http://localhost:8001/api")

def login(email, password):
    r = requests.post(f"{API}/auth/login", json={"email": email, "password": password}, timeout=10)
    r.raise_for_status()
    # token via cookies (per FastAPI auth flow) — check
    return r

# Login admin
admin_resp = login("admin@finalzoom.com", "Admin@FinalZoom2026")
admin_jar = admin_resp.cookies
print("✓ admin login OK")

# Create a regular user
import uuid
test_email = f"testuser_{uuid.uuid4().hex[:8]}@test.com"
r = requests.post(
    f"{API}/admin/users",
    cookies=admin_jar,
    json={"email": test_email, "password": "Test@1234", "name": "Test User"},
    timeout=10,
)
r.raise_for_status()
print(f"✓ created user {test_email}")

# Login user
user_resp = login(test_email, "Test@1234")
user_jar = user_resp.cookies
print("✓ user login OK")

# Create a task with very short timeout (10s) so we can verify auto-completion
r = requests.post(
    f"{API}/tasks",
    cookies=user_jar,
    json={
        "meeting_id": "1234567890",
        "meeting_password": "",
        "members": 2,
        "name_source": "NamesIn",
        "meeting_type": "Normal Participants",
        "timeout": 10,
        "floating_emoji": False,
        "participant_reactions": False,
    },
    timeout=10,
)
print(f"  create task status: {r.status_code}")
if r.status_code != 200:
    print("ERR:", r.text)
    sys.exit(1)
task = r.json()
tid = task["id"]
print(f"✓ created task {tid[:8]}, status={task['status']}, ends_at={task['ends_at']}")

# Verify it's in /tasks/active
r = requests.get(f"{API}/tasks/active", cookies=user_jar, timeout=10)
assert any(t["id"] == tid for t in r.json()), "task not in active list"
print("✓ task is in /tasks/active")

# Verify /tasks/{id}/distribution: non-admin should see empty workers list
r = requests.get(f"{API}/tasks/{tid}/distribution", cookies=user_jar, timeout=10)
dist = r.json()
print(f"  user-side distribution workers: {len(dist.get('workers', []))}")
assert dist.get("workers") == [], "non-admin should see empty workers list"
print("✓ Distribution endpoint hides workers from non-admin")

# Admin side distribution can see workers (may be empty if no workers)
r = requests.get(f"{API}/tasks/{tid}/distribution", cookies=admin_jar, timeout=10)
print(f"  admin-side distribution workers field type: {type(r.json().get('workers'))}")
assert isinstance(r.json().get("workers"), list), "admin should see workers array"
print("✓ Admin distribution endpoint returns workers array")

# Wait for task timeout + poller cycle (5s poller + 10s timeout = ~16s)
print("Waiting 18s for auto-completion...")
time.sleep(18)

# Verify it's gone from /tasks/active
r = requests.get(f"{API}/tasks/active", cookies=user_jar, timeout=10)
assert not any(t["id"] == tid for t in r.json()), f"task still in active list! {r.json()}"
print("✓ task removed from /tasks/active after timeout")

# Verify it's in /tasks/previous with status=completed
today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
r = requests.get(f"{API}/tasks/previous?date={today}", cookies=user_jar, timeout=10)
match = [t for t in r.json() if t["id"] == tid]
assert match, f"task not in previous list! {r.json()}"
assert match[0]["status"] == "completed", f"expected completed, got {match[0]['status']}"
print(f"✓ task in /tasks/previous with status=completed")

# ===== Test cancel-then-history =====
r = requests.post(
    f"{API}/tasks",
    cookies=user_jar,
    json={
        "meeting_id": "9876543210", "meeting_password": "",
        "members": 1, "name_source": "NamesIn",
        "meeting_type": "Normal Participants", "timeout": 600,
    },
    timeout=10,
)
tid2 = r.json()["id"]
print(f"✓ created second task {tid2[:8]} for cancel test")

# Cancel it
r = requests.post(f"{API}/tasks/{tid2}/cancel", cookies=user_jar, timeout=10)
assert r.status_code == 200, r.text
assert r.json()["status"] == "cancelled"
print("✓ cancel returns status=cancelled")

# Verify it's in /tasks/previous
r = requests.get(f"{API}/tasks/previous?date={today}", cookies=user_jar, timeout=10)
match2 = [t for t in r.json() if t["id"] == tid2]
assert match2, f"cancelled task not in previous! {r.json()}"
assert match2[0]["status"] == "cancelled"
print("✓ cancelled task appears in /tasks/previous (complete history)")

# ===== Test admin remove-all =====
r = requests.post(
    f"{API}/tasks",
    cookies=user_jar,
    json={
        "meeting_id": "5555555555", "meeting_password": "",
        "members": 1, "name_source": "NamesIn",
        "meeting_type": "Normal Participants", "timeout": 600,
    },
    timeout=10,
)
tid3 = r.json()["id"]
print(f"✓ created third task {tid3[:8]} for remove-all test")

r = requests.post(f"{API}/admin/meetings/5555555555/remove-all", cookies=admin_jar, timeout=10)
print(f"  remove-all: {r.json()}")
assert r.status_code == 200

# Verify it's in user's previous as cancelled
r = requests.get(f"{API}/tasks/previous?date={today}", cookies=user_jar, timeout=10)
match3 = [t for t in r.json() if t["id"] == tid3]
assert match3, f"remove-all task not in previous! {r.json()}"
assert match3[0]["status"] == "cancelled"
print("✓ admin remove-all → task in user's /tasks/previous as cancelled")

# Test preview-distribution is admin-only
r = requests.post(f"{API}/tasks/preview-distribution", cookies=user_jar,
                  json={"members": 10}, timeout=10)
assert r.status_code == 403, f"expected 403, got {r.status_code}"
print("✓ /tasks/preview-distribution blocks non-admin (403)")

r = requests.post(f"{API}/tasks/preview-distribution", cookies=admin_jar,
                  json={"members": 10}, timeout=10)
assert r.status_code == 200, f"admin should access: got {r.status_code}: {r.text[:200]}"
print("✓ /tasks/preview-distribution allows admin")

print("\n🎉 ALL VERIFICATIONS PASSED")
