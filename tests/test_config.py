"""Tests for agent.toml load/save and validation."""

import os
import tempfile
import unittest

from lesserv_agent import config as config_mod


def _base(tmp, **over):
    """Build a minimal valid config dict for tests."""
    data = {
        "cp_url": "https://plane.example.com",
        "node_id": "tokyo01",
        "token": "secret-token",
    }
    data.update(over)
    return data


class ConfigTest(unittest.TestCase):
    def test_save_then_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "agent.toml")
            config_mod.save_config(path, _base(tmp))
            cfg = config_mod.load_config(path)
            self.assertEqual(cfg["node_id"], "tokyo01")
            self.assertEqual(cfg["poll_interval"], 30)
            self.assertEqual(cfg["restart_mode"], "systemd")

    def test_missing_file_explains_enroll(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "nope.toml")
            with self.assertRaises(config_mod.ConfigError) as ctx:
                config_mod.load_config(path)
            self.assertIn("enroll", str(ctx.exception))

    def test_missing_token_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "agent.toml")
            with self.assertRaises(config_mod.ConfigError):
                config_mod.save_config(path, {"cp_url": "https://x", "node_id": "n"})

    def test_rejects_hmac_v1_for_now(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "agent.toml")
            with self.assertRaises(config_mod.ConfigError):
                config_mod.save_config(path, _base(tmp, auth="hmac-v1"))

    def test_cp_url_must_be_http(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "agent.toml")
            with self.assertRaises(config_mod.ConfigError):
                config_mod.save_config(path, _base(tmp, cp_url="not-a-url"))

    def test_token_never_written_to_world_readable(self):
        try:
            import posix  # noqa: F401
        except ImportError:
            self.skipTest("POSIX modes only")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "agent.toml")
            config_mod.save_config(path, _base(tmp))
            mode = oct(os.stat(path).st_mode & 0o777)
            self.assertEqual(mode, "0o600")


if __name__ == "__main__":
    unittest.main()
