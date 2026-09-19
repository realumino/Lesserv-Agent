"""Tests for backoff, apply gating, startup check, and one cycle."""

import os
import tempfile
import unittest
from unittest import mock

from lesserv_agent import loop as loop_mod


class BackoffTest(unittest.TestCase):
    def test_backoff_grows_then_caps(self):
        first = loop_mod.compute_backoff(0)
        later = loop_mod.compute_backoff(3)
        capped = loop_mod.compute_backoff(20)
        self.assertTrue(4 <= first <= 11)
        self.assertTrue(later > first)
        self.assertTrue(capped <= 305)

    def test_needs_apply_two_conditions(self):
        self.assertFalse(loop_mod.needs_apply(
            {"applied_hash": "h", "last_apply_ok": True}, "h"))
        self.assertTrue(loop_mod.needs_apply(
            {"applied_hash": "h", "last_apply_ok": False}, "h"))
        self.assertTrue(loop_mod.needs_apply(
            {"applied_hash": "old", "last_apply_ok": True}, "new"))
        self.assertFalse(loop_mod.needs_apply(
            {"applied_hash": "h", "last_apply_ok": True}, ""))


class StartupTest(unittest.TestCase):
    def test_missing_live_marks_not_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {"config_path": os.path.join(tmp, "config.json"),
                   "xray_binary": "xray"}
            st = loop_mod.startup_check(cfg, {"last_apply_ok": True})
            self.assertFalse(st["last_apply_ok"])

    def test_broken_live_restores_last_good(self):
        with tempfile.TemporaryDirectory() as tmp:
            from lesserv_agent import apply as apply_mod
            cfg = {"config_path": os.path.join(tmp, "config.json"),
                   "xray_binary": "xray", "xray_service": "xray",
                   "restart_mode": "systemd"}
            with open(cfg["config_path"], "w") as fh:
                fh.write("broken")
            with open(apply_mod.last_good_path(cfg), "w") as fh:
                fh.write('{"good": true}')
            with mock.patch("lesserv_agent.loop.xray.test_config",
                            return_value=(False, 1, "bad")), \
                 mock.patch("lesserv_agent.loop.xray.restart_xray",
                            return_value=(True, "")):
                st = loop_mod.startup_check(cfg, {"last_apply_ok": True})
            self.assertFalse(st["last_apply_ok"])
            with open(cfg["config_path"]) as fh:
                self.assertEqual(fh.read(), '{"good": true}')

    def test_healthy_live_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {"config_path": os.path.join(tmp, "config.json"),
                   "xray_binary": "xray"}
            with open(cfg["config_path"], "w") as fh:
                fh.write('{"ok": true}')
            with mock.patch("lesserv_agent.loop.xray.test_config",
                            return_value=(True, 0, "")):
                st = loop_mod.startup_check(
                    cfg, {"last_apply_ok": True, "applied_hash": "h"})
            self.assertEqual(st["applied_hash"], "h")


class RunOnceTest(unittest.TestCase):
    def test_nothing_to_do_skips_apply(self):
        cfg = {"config_path": "/tmp/x.json", "xray_binary": "xray",
               "xray_service": "xray", "restart_mode": "systemd"}
        st = {"applied_hash": "same", "applied_at": 1, "last_apply_ok": True,
              "last_error": None, "xray_boot_id": "b"}
        with mock.patch("lesserv_agent.loop.xray.service_running",
                        return_value=(True, 100)), \
             mock.patch("lesserv_agent.loop.client.do_heartbeat",
                        return_value={"desired_hash": "same", "actions": []}):
            out, acted, desired = loop_mod.run_once(cfg, dict(st), 0)
        self.assertFalse(acted)
        self.assertEqual(desired, "same")
        self.assertEqual(out["applied_hash"], "same")

    def test_differing_hash_applies_and_reports(self):
        cfg = {"config_path": "/tmp/x.json", "xray_binary": "xray",
               "xray_service": "xray", "restart_mode": "systemd"}
        st = {"applied_hash": "old", "applied_at": 1, "last_apply_ok": True,
              "last_error": None, "xray_boot_id": "b"}
        with mock.patch("lesserv_agent.loop.xray.service_running",
                        return_value=(True, 100)), \
             mock.patch("lesserv_agent.loop.client.do_heartbeat",
                        return_value={"desired_hash": "new", "actions": []}), \
             mock.patch("lesserv_agent.loop.apply_mod.apply_to_hash",
                        return_value={"ok": True, "stage": "started",
                                      "error": None, "xray_exit_code": None}), \
             mock.patch("lesserv_agent.loop.client.do_report") as rep:
            out, acted, _ = loop_mod.run_once(cfg, dict(st), 0)
        self.assertTrue(acted)
        self.assertEqual(out["applied_hash"], "new")
        self.assertTrue(out["last_apply_ok"])
        rep.assert_called_once()


if __name__ == "__main__":
    unittest.main()
