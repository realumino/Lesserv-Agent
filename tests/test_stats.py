"""Tests for absolute-counter stats payloads."""

import unittest

from lesserv_agent import stats as stats_mod


class StatsTest(unittest.TestCase):
    def test_collect_forwards_absolute_values(self):
        st = {"xray_boot_id": "boot-1"}
        payload = stats_mod.collect_stats(
            {}, st, reader=lambda: {"user>>>a>>>traffic>>>uplink": 1024})
        self.assertEqual(payload["boot_id"], "boot-1")
        self.assertEqual(payload["counters"]["user>>>a>>>traffic>>>uplink"], 1024)

    def test_reader_failure_yields_empty_not_crash(self):
        def boom():
            raise RuntimeError("no api")
        payload = stats_mod.collect_stats({}, {"xray_boot_id": "b"}, reader=boom)
        self.assertEqual(payload["counters"], {})

    def test_boot_ids_unique(self):
        self.assertNotEqual(stats_mod.new_boot_id(), stats_mod.new_boot_id())

    def test_non_int_counters_dropped(self):
        payload = stats_mod.collect_stats(
            {}, {"xray_boot_id": "b"}, reader=lambda: {"k": "nope", "j": 5})
        self.assertNotIn("k", payload["counters"])
        self.assertEqual(payload["counters"]["j"], 5)


if __name__ == "__main__":
    unittest.main()
