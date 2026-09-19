"""End-to-end pull test: real agent code against the real control plane.

WHAT: Launch Lesserv-Cloud locally, seed it over HTTP, then run the
agent's actual enroll / heartbeat / apply / report / stats path to
convergence, including rejection, rollback, kill-mid-apply, and
startup-repair.
WHY: Unit tests prove each side against fakes; only a live run proves
the two repos agree on the contract (paths, headers, field names,
status codes). Skips when the sibling checkout is absent so agent CI
stays hermetic. The xray binary / supervisor boundary is stubbed —
this box has neither Xray nor systemd, and that seam is the agent's
own unit-tested surface. Everything on the wire is real.
"""

import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

from lesserv_agent import apply as apply_mod
from lesserv_agent import client
from lesserv_agent import loop
from lesserv_agent import state as state_mod
from lesserv_agent import stats as stats_mod
from lesserv_agent import xray

NODE_ID = "e2e01"
BOOT_TIMEOUT_S = 60


def _cloud_dir():
    """Locate the sibling Lesserv-Cloud checkout, or None.

    WHAT: Prefer $LESSERV_CLOUD_DIR, else ../Lesserv-Cloud.
    WHY: The two repos are siblings on a dev box; CI without the
    sibling must skip, not fail.
    """
    env = os.environ.get("LESSERV_CLOUD_DIR")
    if env and os.path.isfile(os.path.join(env, "src", "local.py")):
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    cand = os.path.normpath(os.path.join(here, "..", "..", "Lesserv-Cloud"))
    if os.path.isfile(os.path.join(cand, "src", "local.py")):
        return cand
    return None


def _plane_python(cloud):
    """Return the interpreter that has the plane's deps, or None.

    WHAT: The cloud venv python (uvicorn + fastapi live there).
    WHY: The agent itself is stdlib-only; the plane is not. Reusing
    its venv avoids installing anything.
    """
    if os.name == "nt":
        cand = os.path.join(cloud, ".venv", "Scripts", "python.exe")
    else:
        cand = os.path.join(cloud, ".venv", "bin", "python")
    if os.path.isfile(cand):
        return cand
    return None


def _free_port():
    """Return an ephemeral localhost port for the test plane.

    WHAT: Bind port 0, read the assignment, close.
    WHY: Fixed ports collide on shared CI runners; the tiny
    bind-close-race is acceptable for a local test.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def _http(base, method, path, body=None):
    """One JSON admin call against the test plane.

    WHAT: Send method+path, return (status, parsed body).
    WHY: Seeding (nodes, configs, users, tokens) uses the admin
    surface; the agent's own client covers the node surface.
    """
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8") or "null"
            return resp.status, json.loads(raw)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")[:500]


def _authored_config(extra=None):
    """Minimal authored config the plane renders deterministically."""
    config = {
        "inbounds": [
            {"tag": "reality", "protocol": "vless", "port": 443,
             "settings": {"clients": []}},
        ],
        "outbounds": [{"tag": "niigata", "protocol": "freedom"}],
    }
    if extra:
        config.update(extra)
    return config


def _access(user="ed"):
    """One-user access payload for the e2e node."""
    return {"username": user, "access": {
        NODE_ID: {"allowed_inbounds": ["reality"],
                  "allowed_outbounds": ["niigata"]}}}


class TestE2EPull(unittest.TestCase):
    """The M3 proof: real loop, real plane, converged hashes."""

    @classmethod
    def setUpClass(cls):
        cloud = _cloud_dir()
        if not cloud:
            raise unittest.SkipTest("no Lesserv-Cloud sibling checkout")
        py = _plane_python(cloud)
        if not py:
            raise unittest.SkipTest("no plane interpreter (cloud .venv)")
        probe = subprocess.run([py, "-m", "uvicorn", "--version"],
                               capture_output=True, timeout=60)
        if probe.returncode != 0:
            raise unittest.SkipTest("uvicorn missing from cloud .venv")
        cls.root = tempfile.mkdtemp(prefix="lesserv-e2e-")
        cls.port = _free_port()
        cls.base = "http://127.0.0.1:%d" % cls.port
        log_path = os.path.join(cls.root, "plane.log")
        cls.log = open(log_path, "wb")
        cls.plane = subprocess.Popen(
            [py, "-m", "uvicorn", "local:app",
             "--app-dir", os.path.join(cloud, "src"),
             "--host", "127.0.0.1", "--port", str(cls.port)],
            cwd=cls.root, stdout=cls.log, stderr=subprocess.STDOUT)
        try:
            cls._wait_for_plane()
            cls.token = cls._seed()
        except Exception:
            cls.tearDownClass()
            raise
        cls.agent_root = os.path.join(cls.root, "agent")
        os.makedirs(cls.agent_root)
        cls.cfg = {
            "cp_url": cls.base, "node_id": NODE_ID, "token": cls.token,
            "config_path": os.path.join(cls.agent_root, "config.json"),
            "poll_interval": 30, "xray_binary": "xray",
            "xray_service": "xray", "restart_mode": "systemd",
            "auth": "bearer-v1", "stats_interval": 60,
        }
        cls.state_path = os.path.join(cls.agent_root, "state.json")

    @classmethod
    def _wait_for_plane(cls):
        """Poll /api/health until the test plane answers or time runs out."""
        deadline = time.time() + BOOT_TIMEOUT_S
        while time.time() < deadline:
            if cls.plane.poll() is not None:
                raise AssertionError("plane exited during boot (see plane.log)")
            try:
                status, _ = _http(cls.base, "GET", "/api/health")
                if status == 200:
                    return
            except Exception:
                pass
            time.sleep(1)
        raise AssertionError("plane did not boot in %ds" % BOOT_TIMEOUT_S)

    @classmethod
    def _seed(cls):
        """Create node + config + user, return a minted token."""
        status, _ = _http(cls.base, "POST", "/api/admin/nodes", {
            "id": NODE_ID, "label": "E2E", "address": "127.0.0.1"})
        assert status == 201, status
        status, _ = _http(cls.base, "PUT",
                           "/api/admin/nodes/%s/config" % NODE_ID,
                           _authored_config())
        assert status == 200, status
        status, _ = _http(cls.base, "POST", "/api/admin/users",
                           _access("ed"))
        assert status == 201, status
        status, data = _http(cls.base, "POST",
                             "/api/admin/nodes/%s/token" % NODE_ID)
        assert status == 201, (status, data)
        return data["token"]

    @classmethod
    def tearDownClass(cls):
        try:
            if getattr(cls, "plane", None) and cls.plane.poll() is None:
                cls.plane.terminate()
                try:
                    cls.plane.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    cls.plane.kill()
        finally:
            try:
                cls.log.close()
            except Exception:
                pass
            shutil.rmtree(cls.root, ignore_errors=True)

    def setUp(self):
        self.verify_up = True
        self.restarts = []
        self._patches = [
            mock.patch.object(xray, "test_config",
                              side_effect=self._fake_test),
            mock.patch.object(xray, "restart_xray",
                              side_effect=self._fake_restart),
            mock.patch.object(xray, "verify_running",
                              side_effect=lambda *a: self.verify_up),
            mock.patch.object(xray, "service_running",
                              return_value=(False, 0)),
        ]
        for patcher in self._patches:
            patcher.start()
        self.addCleanup(self._stop_patches)

    def _stop_patches(self):
        """Stop xray-boundary stubs in reverse order."""
        for patcher in reversed(self._patches):
            patcher.stop()

    def _fake_test(self, binary, path, timeout=15):
        """Stand in for `xray test`: fail only on the BAD marker.

        WHAT: Read the staged file, reject the poisoned config.
        WHY: The plane is opaque to config semantics, so a marker key
        survives the render and lets us drive test-stage failure
        through the real fetch path.
        """
        with open(path, "rb") as handle:
            data = handle.read()
        if b"E2E-BAD" in data:
            return False, 1, "E2E-BAD marker rejected"
        return True, 0, "ok"

    def _fake_restart(self, service, mode="systemd"):
        """Record a restart attempt and succeed."""
        self.restarts.append((service, mode))
        return True, ""

    def _sync(self):
        """Read the plane's drift view for the e2e node."""
        status, data = _http(self.base, "GET",
                             "/api/admin/nodes/%s/sync" % NODE_ID)
        assert status == 200, (status, data)
        return data

    def _cycle(self, st):
        """Run one real reconcile cycle and persist state like run_loop."""
        st, acted, desired = loop.run_once(self.cfg, st, time.time())
        state_mod.save_state(self.state_path, st)
        return st, acted, desired

    def _live_bytes(self):
        """Read the agent's live config file as bytes."""
        with open(self.cfg["config_path"], "rb") as handle:
            return handle.read()

    def test_pull_lifecycle(self):
        meta = client.do_enroll(self.cfg, {
            "agent_version": "0.1.0", "xray_version": "test",
            "platform": "test", "detected_ip": None})
        self.assertEqual(meta["state"], "active")
        self.assertTrue(meta["desired_hash"])

        st = state_mod.load_state(self.state_path)
        st, acted, first = self._cycle(st)
        self.assertTrue(acted)
        self.assertEqual(st["applied_hash"], first)
        sync = self._sync()
        self.assertTrue(sync["in_sync"])
        self.assertEqual(sync["applied_hash"], first)
        _, acted_again, _ = self._cycle(st)
        self.assertFalse(acted_again)

        self._put_user("fay")
        st, _, second = self._cycle(st)
        self.assertNotEqual(second, first)
        self.assertTrue(self._sync()["in_sync"])

        good = self._live_bytes()
        self._put_config({"comment": "E2E-BAD"})
        st, acted_bad, _ = self._cycle(st)
        self.assertTrue(acted_bad)
        self.assertFalse(st["last_apply_ok"])
        self.assertEqual(self._live_bytes(), good)
        sync = self._sync()
        self.assertFalse(sync["in_sync"])
        self.assertIn("test", sync["health"])
        self.assertEqual(sync["applied_hash"], second)

        self._put_config()
        self._put_user("gus")
        with open(apply_mod.last_good_path(self.cfg), "rb") as handle:
            fallback = handle.read()
        self.verify_up = False
        st, _, _ = self._cycle(st)
        self.assertFalse(st["last_apply_ok"])
        self.assertEqual(self._live_bytes(), fallback)
        self.assertNotIn(b"gus@", self._live_bytes())
        sync = self._sync()
        self.assertIn("started", sync["health"])
        self.verify_up = True
        st, _, fourth = self._cycle(st)
        self.assertTrue(st["last_apply_ok"])
        self.assertTrue(self._sync()["in_sync"])

        self._put_user("hal")
        hb = client.do_heartbeat(self.cfg, {"applied_hash": st["applied_hash"]})
        fifth = hb["desired_hash"]
        self._crash_between_swap_and_report(fifth)
        st = state_mod.load_state(self.state_path)
        st = loop.startup_check(self.cfg, st)
        st, _, _ = self._cycle(st)
        self.assertTrue(st["last_apply_ok"])
        self.assertEqual(st["applied_hash"], fifth)
        self.assertTrue(self._sync()["in_sync"])
        self.assertNotEqual(fourth, fifth)

        with open(self.cfg["config_path"], "wb") as handle:
            handle.write(b"E2E-BAD live")
        st = loop.startup_check(self.cfg, state_mod.load_state(self.state_path))
        self.assertFalse(st["last_apply_ok"])
        st, _, _ = self._cycle(dict(st, last_apply_ok=False,
                                    applied_hash=st["applied_hash"]))
        self.assertTrue(st["last_apply_ok"])
        self.assertTrue(self._sync()["in_sync"])

    def _put_user(self, username):
        """Grant one more user access on the e2e node."""
        status, data = _http(self.base, "POST", "/api/admin/users",
                             _access(username))
        self.assertEqual(status, 201, (status, data))

    def _put_config(self, extra=None):
        """Replace the e2e node's authored config."""
        status, data = _http(self.base, "PUT",
                             "/api/admin/nodes/%s/config" % NODE_ID,
                             _authored_config(extra))
        self.assertEqual(status, 200, (status, data))

    def _crash_between_swap_and_report(self, desired_hash):
        """Swap a fetched config without reporting (simulated kill -9).

        WHAT: Fetch + atomic swap, skipping state persist and report.
        WHY: The exact crash window — tested config on disk, nobody
        told. Restart must converge without human action.
        """
        _, payload = client.fetch_config(self.cfg, desired_hash)
        tmp = apply_mod.write_temp_config(self.cfg, payload)
        apply_mod.swap_atomic(tmp, self.cfg)

    def test_stats_report_path(self):
        st = state_mod.load_state(self.state_path)
        payload = stats_mod.collect_stats(
            self.cfg, st, reader=lambda: {
                "user>>>ed@%s-niigata>>>traffic>>>uplink" % NODE_ID: 1024})

        self.assertTrue(client.do_stats(self.cfg, payload))

    def test_wrong_token_fails_closed(self):
        bad = dict(self.cfg, token="wrong")

        with self.assertRaises(client.ClientError) as ctx:
            client.do_heartbeat(bad, {"applied_hash": ""})

        self.assertTrue(ctx.exception.auth_failed)


if __name__ == "__main__":
    unittest.main()
