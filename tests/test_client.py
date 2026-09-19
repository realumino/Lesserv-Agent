"""Tests for the bearer-v1 HTTP client."""

import json
import unittest
from unittest import mock

from lesserv_agent import client


def _cfg():
    """Return a minimal client config."""
    return {"cp_url": "https://plane.example.com", "node_id": "tokyo01",
            "token": "sekrit"}


class FakeResp(object):
    def __init__(self, status=200, body=b"{}"):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class ClientTest(unittest.TestCase):
    def test_headers_carry_identity(self):
        seen = {}

        def fake_urlopen(req, timeout=None):
            seen["auth"] = req.get_header("Authorization")
            seen["node"] = req.get_header("X-lesserv-node")
            return FakeResp(200, b'{"desired_hash":"h","actions":[]}')

        with mock.patch.object(client.urllib.request, "urlopen", fake_urlopen):
            client.do_heartbeat(_cfg(), {"applied_hash": ""})
        self.assertEqual(seen["auth"], "Bearer sekrit")
        self.assertEqual(seen["node"], "tokyo01")

    def test_heartbeat_requires_fields(self):
        with mock.patch.object(client.urllib.request, "urlopen",
                               lambda *a, **k: FakeResp(200, b'{"nope":1}')):
            with self.assertRaises(client.ClientError):
                client.do_heartbeat(_cfg(), {})

    def test_empty_body_is_network_failure(self):
        with mock.patch.object(client.urllib.request, "urlopen",
                               lambda *a, **k: FakeResp(200, b'')):
            with self.assertRaises(client.ClientError):
                client.do_heartbeat(_cfg(), {})

    def test_auth_failure_flagged(self):
        import urllib.error
        err = urllib.error.HTTPError("https://x/", 401, "unauth", {}, None)
        with mock.patch.object(client.urllib.request, "urlopen", side_effect=err):
            with self.assertRaises(client.ClientError) as ctx:
                client.do_heartbeat(_cfg(), {})
            self.assertTrue(ctx.exception.auth_failed)

    def test_stats_requires_boot_and_counters(self):
        with self.assertRaises(client.ClientError):
            client.do_stats(_cfg(), {"boot_id": "b"})

    def test_fetch_config_unwraps_config_key(self):
        body = json.dumps({"hash": "h",
                           "config": {"inbounds": []}}).encode()
        with mock.patch.object(client.urllib.request, "urlopen",
                               lambda *a, **k: FakeResp(200, body)):
            status, payload = client.fetch_config(_cfg(), "h")
            self.assertEqual(status, 200)
            self.assertEqual(payload, {"inbounds": []})

    def test_fetch_config_rejects_hash_mismatch(self):
        body = json.dumps({"hash": "other",
                           "config": {"inbounds": []}}).encode()
        with mock.patch.object(client.urllib.request, "urlopen",
                               lambda *a, **k: FakeResp(200, body)):
            with self.assertRaises(client.ClientError):
                client.fetch_config(_cfg(), "h")

    def test_protocol_version_sent(self):
        seen = {}

        def fake_urlopen(req, timeout=None):
            seen["body"] = json.loads(req.data.decode())
            return FakeResp(200, b'{}')

        with mock.patch.object(client.urllib.request, "urlopen", fake_urlopen):
            client.do_enroll(_cfg(), {"agent_version": "0.1.0"})
        self.assertEqual(seen["body"]["protocol"], 1)


if __name__ == "__main__":
    unittest.main()
