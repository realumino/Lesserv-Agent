"""Tests for atomic state.json handling."""

import os
import tempfile
import unittest

from lesserv_agent import state as state_mod


class StateTest(unittest.TestCase):
    def test_missing_state_returns_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = state_mod.load_state(os.path.join(tmp, "state.json"))
            self.assertEqual(st["applied_hash"], "")
            self.assertTrue(st["last_apply_ok"])

    def test_corrupt_state_returns_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            with open(path, "w") as fh:
                fh.write("{not json")
            st = state_mod.load_state(path)
            self.assertEqual(st["applied_hash"], "")

    def test_save_then_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            state_mod.save_state(path, {
                "applied_hash": "ab12", "applied_at": 123,
                "last_apply_ok": False, "last_error": "boom",
                "xray_boot_id": "b1",
            })
            st = state_mod.load_state(path)
            self.assertEqual(st["applied_hash"], "ab12")
            self.assertFalse(st["last_apply_ok"])
            self.assertEqual(st["last_error"], "boom")

    def test_save_ignores_extra_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            state_mod.save_state(path, {"applied_hash": "x", "evil": 1,
                                        "last_apply_ok": True})
            st = state_mod.load_state(path)
            self.assertNotIn("evil", st)


if __name__ == "__main__":
    unittest.main()
