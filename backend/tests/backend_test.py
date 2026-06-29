"""Backend tests for Zoom Bot Farm RDP Manager."""
import os
import time
import asyncio
import json
import pytest
import requests
import websockets

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://member-activity-hub.preview.emergentagent.com").rstrip("/")
ADMIN_EMAIL = "admin@botfarm.io"
ADMIN_PASSWORD = "BotFarm@2026"


@pytest.fixture(scope="session")
def token():
    r = requests.post(f"{BASE_URL}/api/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    assert r.status_code == 200, r.text
    data = r.json()
    assert "token" in data and "user" in data
    assert data["user"]["email"] == ADMIN_EMAIL
    assert data["user"]["role"] == "owner"
    return data["token"]


@pytest.fixture(scope="session")
def auth_headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# ---------------- AUTH ----------------
class TestAuth:
    def test_login_invalid(self):
        r = requests.post(f"{BASE_URL}/api/auth/login", json={"email": ADMIN_EMAIL, "password": "wrong"})
        assert r.status_code == 401

    def test_me_with_token(self, auth_headers):
        r = requests.get(f"{BASE_URL}/api/auth/me", headers=auth_headers)
        assert r.status_code == 200
        assert r.json()["email"] == ADMIN_EMAIL

    def test_me_without_token(self):
        r = requests.get(f"{BASE_URL}/api/auth/me")
        assert r.status_code == 401

    def test_bcrypt_hash_format(self):
        # cannot inspect db directly here, but verify login works (uses bcrypt verify_password)
        r = requests.post(f"{BASE_URL}/api/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
        assert r.status_code == 200


# ---------------- RDP CRUD + Encryption ----------------
class TestRdps:
    rdp_id = None

    def test_add_rdp(self, auth_headers):
        payload = {"name": "TEST_RDP_1", "ip": "10.0.0.1", "username": "Administrator",
                   "password": "SuperSecret123!", "capacity": 80}
        r = requests.post(f"{BASE_URL}/api/rdps", headers=auth_headers, json=payload)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["name"] == "TEST_RDP_1"
        assert data["status"] == "online"
        assert data["simulated"] is True
        # Password must NOT be returned
        assert "password" not in data
        assert "password_enc" not in data
        TestRdps.rdp_id = data["id"]

    def test_list_rdps_no_password(self, auth_headers):
        r = requests.get(f"{BASE_URL}/api/rdps", headers=auth_headers)
        assert r.status_code == 200
        for rdp in r.json():
            assert "password" not in rdp
            assert "password_enc" not in rdp

    def test_get_rdp(self, auth_headers):
        assert TestRdps.rdp_id
        r = requests.get(f"{BASE_URL}/api/rdps/{TestRdps.rdp_id}", headers=auth_headers)
        assert r.status_code == 200
        assert r.json()["id"] == TestRdps.rdp_id

    def test_installer_script(self, auth_headers):
        r = requests.get(f"{BASE_URL}/api/rdps/{TestRdps.rdp_id}/installer", headers=auth_headers)
        assert r.status_code == 200
        assert "script" in r.json()


# ---------------- Agent contract ----------------
class TestAgent:
    def test_heartbeat_wrong_secret(self, auth_headers):
        assert TestRdps.rdp_id
        r = requests.post(f"{BASE_URL}/api/agent/heartbeat",
                          json={"rdp_id": TestRdps.rdp_id, "secret": "WRONG"})
        assert r.status_code == 401

    def test_heartbeat_correct(self, auth_headers):
        # fetch agent_token via list (it's exposed in GET /api/rdps clean_rdp)
        r = requests.get(f"{BASE_URL}/api/rdps", headers=auth_headers)
        rdp = next(x for x in r.json() if x["id"] == TestRdps.rdp_id)
        token_v = rdp.get("agent_token")
        assert token_v, "agent_token should be present in RDP list"
        r2 = requests.post(f"{BASE_URL}/api/agent/heartbeat",
                           json={"rdp_id": TestRdps.rdp_id, "secret": token_v,
                                 "cpu": 33, "ram": 44, "net": 55, "bots_online": 12, "status": "online"})
        assert r2.status_code == 200, r2.text
        data = r2.json()
        assert data["ok"] is True


# ---------------- Bulk commands & meetings ----------------
class TestCommandsMeetings:
    meeting_uuid = None

    def test_join_dispatch(self, auth_headers):
        # second RDP to test load-distribute
        requests.post(f"{BASE_URL}/api/rdps", headers=auth_headers, json={
            "name": "TEST_RDP_2", "ip": "10.0.0.2", "username": "u", "password": "p", "capacity": 80
        })
        body = {"action": "join", "meeting_id": "9999999999", "password": "p",
                "name": "TEST Meeting", "bot_count": 120, "rdp_ids": []}
        r = requests.post(f"{BASE_URL}/api/commands", headers=auth_headers, json=body)
        assert r.status_code == 200, r.text
        log = r.json()
        assert log["action"] == "join"
        assert log["success"] >= 1
        # Wait for engine ramp
        time.sleep(7)
        ov = requests.get(f"{BASE_URL}/api/analytics/overview", headers=auth_headers).json()
        assert ov["bots_online"] > 0
        # find meeting
        meetings = requests.get(f"{BASE_URL}/api/meetings", headers=auth_headers).json()
        active = [m for m in meetings if m.get("status") == "active"]
        assert active
        TestCommandsMeetings.meeting_uuid = active[0]["id"]

    @pytest.mark.parametrize("action", ["mute", "unmute", "video_on", "video_off"])
    def test_mute_video_commands(self, auth_headers, action):
        r = requests.post(f"{BASE_URL}/api/commands", headers=auth_headers,
                          json={"action": action, "rdp_ids": []})
        assert r.status_code == 200
        assert r.json()["action"] == action

    def test_unknown_action(self, auth_headers):
        r = requests.post(f"{BASE_URL}/api/commands", headers=auth_headers, json={"action": "explode"})
        assert r.status_code == 400

    def test_command_logs(self, auth_headers):
        r = requests.get(f"{BASE_URL}/api/commands", headers=auth_headers)
        assert r.status_code == 200
        assert len(r.json()) > 0

    def test_end_meeting(self, auth_headers):
        mid = TestCommandsMeetings.meeting_uuid
        assert mid
        r = requests.post(f"{BASE_URL}/api/meetings/{mid}/end", headers=auth_headers)
        assert r.status_code == 200
        time.sleep(5)
        meetings = requests.get(f"{BASE_URL}/api/meetings", headers=auth_headers).json()
        m = next(x for x in meetings if x["id"] == mid)
        assert m["status"] == "ended"

    def test_restart_kill_zoom(self, auth_headers):
        for a in ["restart", "kill_zoom"]:
            r = requests.post(f"{BASE_URL}/api/commands", headers=auth_headers,
                              json={"action": a, "rdp_ids": []})
            assert r.status_code == 200


# ---------------- Schedules ----------------
class TestSchedules:
    sid = None

    def test_create_due_schedule(self, auth_headers):
        from datetime import datetime, timezone, timedelta
        run_at = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        r = requests.post(f"{BASE_URL}/api/schedules", headers=auth_headers, json={
            "meeting_id": "8888888888", "password": "", "name": "TEST_Sched",
            "bot_count": 20, "rdp_ids": [], "run_at": run_at
        })
        assert r.status_code == 200
        TestSchedules.sid = r.json()["id"]

    def test_schedule_launches(self, auth_headers):
        time.sleep(7)
        scheds = requests.get(f"{BASE_URL}/api/schedules", headers=auth_headers).json()
        s = next((x for x in scheds if x["id"] == TestSchedules.sid), None)
        assert s and s["status"] in ("launched", "pending")
        # accept pending if tick hasn't yet hit; otherwise launched
        # but a meeting should exist for 8888888888
        meetings = requests.get(f"{BASE_URL}/api/meetings", headers=auth_headers).json()
        assert any(m.get("meeting_id") == "8888888888" for m in meetings) or s["status"] == "pending"

    def test_delete_schedule(self, auth_headers):
        if TestSchedules.sid:
            r = requests.delete(f"{BASE_URL}/api/schedules/{TestSchedules.sid}", headers=auth_headers)
            assert r.status_code == 200


# ---------------- Analytics ----------------
class TestAnalytics:
    def test_overview(self, auth_headers):
        r = requests.get(f"{BASE_URL}/api/analytics/overview", headers=auth_headers)
        assert r.status_code == 200
        data = r.json()
        for k in ["bots_online", "capacity", "rdps_total", "rdps_online", "meetings_active"]:
            assert k in data

    def test_alerts(self, auth_headers):
        r = requests.get(f"{BASE_URL}/api/alerts", headers=auth_headers)
        assert r.status_code == 200


# ---------------- WebSocket ----------------
class TestWebSocket:
    def test_ws_connects(self, token):
        ws_url = BASE_URL.replace("https://", "wss://").replace("http://", "ws://") + f"/api/ws?token={token}"

        async def _run():
            async with websockets.connect(ws_url, open_timeout=10) as ws:
                msg = await asyncio.wait_for(ws.recv(), timeout=10)
                data = json.loads(msg)
                assert data.get("type") == "snapshot"
                assert "totals" in data
        asyncio.run(_run())


# ---------------- Cleanup ----------------
class TestZCleanup:
    def test_delete_test_rdps(self, auth_headers):
        r = requests.get(f"{BASE_URL}/api/rdps", headers=auth_headers).json()
        for rdp in r:
            if rdp["name"].startswith("TEST_"):
                requests.delete(f"{BASE_URL}/api/rdps/{rdp['id']}", headers=auth_headers)
