"""
Backend regression tests for v8.7 features:
- Task members 1-100 validation (bulk add cap)
- Forced even distribution + null pre_assignments
- Admin /admin/meetings live list (grouping, auth)
- Admin /admin/meetings/{id}/remove-all
- Admin /admin/system/reset-now preserves users + workers
- Daily 2 AM IST reset scheduler logs
- Multiple parallel tasks per worker (cancel one doesn't affect other)
"""
import os
import time
import requests
import pytest

def _load_base_url():
    url = os.environ.get("REACT_APP_BACKEND_URL")
    if url:
        return url.rstrip("/")
    # fallback: read from frontend/.env
    env_path = "/app/frontend/.env"
    if os.path.exists(env_path):
        for line in open(env_path):
            if line.startswith("REACT_APP_BACKEND_URL="):
                return line.split("=", 1)[1].strip().rstrip("/")
    raise RuntimeError("REACT_APP_BACKEND_URL is not set")

BASE_URL = _load_base_url()
ADMIN_EMAIL = "admin@finalzoom.com"
ADMIN_PASSWORD = "Admin@FinalZoom2026"


@pytest.fixture(scope="session")
def admin_session():
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, timeout=15)
    assert r.status_code == 200, f"Admin login failed: {r.status_code} {r.text}"
    data = r.json()
    assert data.get("role") == "admin", f"unexpected login response: {data}"
    # cookies are set on session by requests automatically
    return s


@pytest.fixture(scope="session")
def anon_session():
    return requests.Session()


# ---------------- Task validation ----------------
class TestTaskMembersValidation:
    def _payload(self, members, timeout=600):
        return {
            "meeting_id": "1234567890",
            "meeting_password": "abc123",
            "members": members,
            "timeout": timeout,
            "distribution_mode": "weighted",      # client tries to override
            "pre_assignments": {"abc": 5},        # client tries to pre-assign
        }

    def test_members_150_rejected(self, admin_session):
        r = admin_session.post(f"{BASE_URL}/api/tasks", json=self._payload(150))
        assert r.status_code in (400, 422), f"expected 4xx, got {r.status_code}: {r.text}"

    def test_members_101_rejected(self, admin_session):
        r = admin_session.post(f"{BASE_URL}/api/tasks", json=self._payload(101))
        assert r.status_code in (400, 422)

    def test_members_0_rejected(self, admin_session):
        r = admin_session.post(f"{BASE_URL}/api/tasks", json=self._payload(0))
        assert r.status_code in (400, 422)

    def test_members_100_accepted_and_forced_even(self, admin_session):
        r = admin_session.post(f"{BASE_URL}/api/tasks", json=self._payload(100, timeout=120))
        assert r.status_code == 200, r.text
        task = r.json()
        assert task["members"] == 100
        # backend stores distribution_mode='even' and pre_assignments=None
        # in the DB regardless of client input. TaskOut doesn't expose these
        # fields so we verify via Mongo directly.
        from pymongo import MongoClient
        mc = MongoClient(os.environ.get("MONGO_URL", "mongodb://localhost:27017"))
        try:
            doc = mc[os.environ.get("DB_NAME", "test_database")].tasks.find_one({"id": task["id"]})
            assert doc is not None, "task not persisted"
            assert doc.get("distribution_mode") == "even", \
                f"distribution_mode in DB not even: {doc.get('distribution_mode')}"
            assert doc.get("pre_assignments") in (None, {}), \
                f"pre_assignments in DB not nulled: {doc.get('pre_assignments')}"
        finally:
            mc.close()
        # cleanup
        admin_session.post(f"{BASE_URL}/api/tasks/{task['id']}/cancel")

    def test_members_1_accepted(self, admin_session):
        r = admin_session.post(f"{BASE_URL}/api/tasks", json=self._payload(1, timeout=60))
        assert r.status_code == 200, r.text
        task = r.json()
        assert task["members"] == 1
        admin_session.post(f"{BASE_URL}/api/tasks/{task['id']}/cancel")


# ---------------- Admin meetings live list ----------------
class TestAdminMeetings:
    def test_unauth_blocked(self, anon_session):
        r = anon_session.get(f"{BASE_URL}/api/admin/meetings")
        assert r.status_code in (401, 403), f"expected 401/403, got {r.status_code}"

    def test_list_running_meetings(self, admin_session):
        # Create one task so there is at least one running meeting
        payload = {"meeting_id": "5550001111", "meeting_password": "xyz",
                   "members": 10, "timeout": 300}
        r = admin_session.post(f"{BASE_URL}/api/tasks", json=payload)
        assert r.status_code == 200, r.text
        tid = r.json()["id"]
        try:
            r2 = admin_session.get(f"{BASE_URL}/api/admin/meetings")
            assert r2.status_code == 200, r2.text
            body = r2.json()
            assert "meetings" in body
            meetings = body["meetings"]
            mids = [m["meeting_id"] for m in meetings]
            assert "5550001111" in mids, f"created meeting not visible: {mids}"
            m = next(x for x in meetings if x["meeting_id"] == "5550001111")
            # required shape
            for key in ("total_members", "total_joined", "running_tasks", "status", "tasks"):
                assert key in m, f"missing key {key}"
            assert m["status"] == "running"
            assert m["total_members"] >= 10
            assert m["running_tasks"] >= 1
            assert isinstance(m["tasks"], list) and len(m["tasks"]) >= 1
        finally:
            admin_session.post(f"{BASE_URL}/api/tasks/{tid}/cancel")

    def test_remove_all_members(self, admin_session):
        # create 2 tasks against same meeting
        ids = []
        for _ in range(2):
            r = admin_session.post(f"{BASE_URL}/api/tasks", json={
                "meeting_id": "7770002222", "meeting_password": "p",
                "members": 5, "timeout": 300})
            assert r.status_code == 200
            ids.append(r.json()["id"])

        r = admin_session.post(f"{BASE_URL}/api/admin/meetings/7770002222/remove-all")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("ok") is True
        assert body.get("tasks_cancelled", 0) >= 2

        # verify affected tasks are cancelled
        active = admin_session.get(f"{BASE_URL}/api/tasks/active").json()
        prev = admin_session.get(f"{BASE_URL}/api/tasks/previous").json()
        all_tasks = {t["id"]: t for t in (active + prev)}
        for tid in ids:
            t = all_tasks.get(tid)
            assert t is not None, f"task {tid} missing from active+previous"
            assert t["status"] == "cancelled", f"task {tid} status={t['status']}"

    def test_remove_all_invalid_meeting(self, admin_session):
        r = admin_session.post(f"{BASE_URL}/api/admin/meetings/abc/remove-all")
        assert r.status_code == 400


# ---------------- Manual reset preserves users + workers ----------------
class TestManualReset:
    def test_reset_preserves_users_and_workers(self, admin_session):
        # Ensure at least one worker exists (skip duplicate-name error)
        existing = admin_session.get(f"{BASE_URL}/api/workers").json()
        if not existing:
            import uuid as _u
            wr = admin_session.post(f"{BASE_URL}/api/workers",
                                    json={"name": f"TEST_W_{_u.uuid4().hex[:6]}"})
            assert wr.status_code == 200, wr.text

        # create a task so the reset has something to wipe
        admin_session.post(f"{BASE_URL}/api/tasks", json={
            "meeting_id": "9990003333", "meeting_password": "p",
            "members": 10, "timeout": 60})

        before_users = admin_session.get(f"{BASE_URL}/api/admin/users").json()
        before_workers = admin_session.get(f"{BASE_URL}/api/workers").json()
        assert len(before_users) > 0
        assert len(before_workers) > 0

        r = admin_session.post(f"{BASE_URL}/api/admin/system/reset-now")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert "tasks_deleted" in body
        assert "users_reset" in body
        assert "workers_load_reset" in body

        # users + workers must still exist
        after_users = admin_session.get(f"{BASE_URL}/api/admin/users").json()
        after_workers = admin_session.get(f"{BASE_URL}/api/workers").json()
        assert len(after_users) == len(before_users), "users were deleted by reset!"
        assert len(after_workers) == len(before_workers), "workers were deleted by reset!"

        # all tasks must be gone
        active = admin_session.get(f"{BASE_URL}/api/tasks/active").json()
        assert active == [] or len(active) == 0

        # usage zeroed for admin user
        me = admin_session.get(f"{BASE_URL}/api/auth/me").json()
        assert me["usage"] == 0

    def test_reset_requires_admin(self, anon_session):
        r = anon_session.post(f"{BASE_URL}/api/admin/system/reset-now")
        assert r.status_code in (401, 403)


# ---------------- Daily reset scheduler ----------------
class TestDailyResetScheduler:
    def test_log_says_ist_02(self):
        # Inspect backend log
        log_path = "/var/log/supervisor/backend.err.log"
        if not os.path.exists(log_path):
            pytest.skip("backend log not accessible from test env")
        with open(log_path, "r") as f:
            tail = f.readlines()[-500:]
        relevant = [ln for ln in tail if "daily-reset" in ln and "next run" in ln]
        assert relevant, "no daily-reset log line found"
        # the LATEST should say IST 02:00
        latest = relevant[-1]
        assert "IST 02:00" in latest, f"latest log line not IST 02:00: {latest!r}"


# ---------------- Multiple parallel tasks per worker ----------------
class TestParallelTasksPerWorker:
    def test_two_parallel_tasks_cancel_one_keeps_other(self, admin_session):
        # cleanup leftovers first
        active_before = admin_session.get(f"{BASE_URL}/api/tasks/active").json()
        for t in active_before:
            admin_session.post(f"{BASE_URL}/api/tasks/{t['id']}/cancel")
        time.sleep(0.5)

        r1 = admin_session.post(f"{BASE_URL}/api/tasks", json={
            "meeting_id": "1111111111", "meeting_password": "p",
            "members": 5, "timeout": 60})
        assert r1.status_code == 200, r1.text
        t1 = r1.json()

        r2 = admin_session.post(f"{BASE_URL}/api/tasks", json={
            "meeting_id": "2222222222", "meeting_password": "p",
            "members": 5, "timeout": 600})
        assert r2.status_code == 200, r2.text
        t2 = r2.json()

        active = admin_session.get(f"{BASE_URL}/api/tasks/active").json()
        active_ids = {t["id"] for t in active}
        assert t1["id"] in active_ids
        assert t2["id"] in active_ids

        # cancel first; second must remain active
        rc = admin_session.post(f"{BASE_URL}/api/tasks/{t1['id']}/cancel")
        assert rc.status_code == 200, rc.text

        active2 = admin_session.get(f"{BASE_URL}/api/tasks/active").json()
        active2_ids = {t["id"] for t in active2}
        assert t1["id"] not in active2_ids, "first task should be cancelled"
        assert t2["id"] in active2_ids, "second task must remain active untouched"

        # cleanup
        admin_session.post(f"{BASE_URL}/api/tasks/{t2['id']}/cancel")
