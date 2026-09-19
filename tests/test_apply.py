"""Tests for the 8-step apply pipeline."""

import json
import os
import tempfile
import unittest

from lesserv_agent import apply as apply_mod


def _cfg(tmp):
    """Config pointing at a temp live file."""
    return {"config_path": os.path.join(tmp, "config.json")}


class ApplyTest(unittest.TestCase):
    def test_bad_test_leaves_live_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            with open(cfg["config_path"], "w") as fh:
                fh.write('{"live": true}')
            result = apply_mod.apply_to_hash(
                cfg, {"last_apply_ok": True}, "newhash",
                fetch_fn=lambda h: {"inbounds": []},
                test_fn=lambda p: (False, 1, "bad json"),
                restart_fn=lambda: (True, ""),
                verify_fn=lambda: True,
            )
            self.assertFalse(result["ok"])
            self.assertEqual(result["stage"], "test")
            with open(cfg["config_path"]) as fh:
                self.assertEqual(fh.read(), '{"live": true}')

    def test_good_apply_swaps_and_reports_started(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            with open(cfg["config_path"], "w") as fh:
                fh.write('{"old": 1}')
            calls = []
            result = apply_mod.apply_to_hash(
                cfg, {"last_apply_ok": True}, "newhash",
                fetch_fn=lambda h: {"new": 2},
                test_fn=lambda p: (True, 0, ""),
                restart_fn=lambda: (calls.append("restart"), (True, ""))[1],
                verify_fn=lambda: True,
            )
            self.assertTrue(result["ok"])
            self.assertEqual(calls, ["restart"])
            with open(cfg["config_path"]) as fh:
                self.assertEqual(json.load(fh), {"new": 2})
            self.assertTrue(os.path.exists(apply_mod.last_good_path(cfg)))

    def test_dead_after_restart_rolls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            with open(cfg["config_path"], "w") as fh:
                fh.write('{"good": 1}')
            # establish last-good manually (previous apply succeeded)
            with open(apply_mod.last_good_path(cfg), "w") as fh:
                fh.write('{"good": 1}')
            result = apply_mod.apply_to_hash(
                cfg, {"last_apply_ok": True}, "badhash",
                fetch_fn=lambda h: {"bad": 1},
                test_fn=lambda p: (True, 0, ""),
                restart_fn=lambda: (True, ""),
                verify_fn=lambda: False,
            )
            self.assertFalse(result["ok"])
            self.assertEqual(result["stage"], "started")
            with open(cfg["config_path"]) as fh:
                self.assertEqual(json.load(fh), {"good": 1})

    def test_snapshot_skipped_after_failed_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            with open(cfg["config_path"], "w") as fh:
                fh.write('{"live": 1}')
            self.assertFalse(apply_mod.snapshot_if_ok(cfg, {"last_apply_ok": False}))
            self.assertFalse(os.path.exists(apply_mod.last_good_path(cfg)))

    def test_fetch_failure_reports_fetched_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            def boom(h):
                raise RuntimeError("plane down")
            result = apply_mod.apply_to_hash(
                _cfg(tmp), {"last_apply_ok": True}, "h",
                fetch_fn=boom,
                test_fn=lambda p: (True, 0, ""),
                restart_fn=lambda: (True, ""),
                verify_fn=lambda: True,
            )
            self.assertEqual(result["stage"], "fetched")


if __name__ == "__main__":
    unittest.main()
