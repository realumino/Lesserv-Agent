"""Traffic stats: absolute counters plus boot id.

WHAT: Read Xray's cumulative per-email counters for /api/node/stats.
WHY: Absolute-plus-boot-id makes retries safe. The plane stores the
last value per (node, email) and accumulates max(0, new-old); the
node never computes deltas (a retried delta would double-count) and
never interprets resets — a decrease or boot change is the plane's
signal, not an error.
"""

import uuid


def new_boot_id():
    """Mint a fresh Xray boot identifier.

    WHAT: Random hex marking one Xray lifetime.
    WHY: Must change on every verified start/restart so the plane can
    tell "counter reset because Xray restarted" from real anomalies.
    """
    return uuid.uuid4().hex


def collect_stats(cfg, state, reader=None):
    """Build the stats payload with absolute counters.

    WHAT: Return {boot_id, counters} for the stats endpoint.
    WHY: Best-effort and side-effect free; an empty counter map is
    valid (Xray API wiring is a seam, not a failure). `reader`, when
    given, supplies absolute counters for tests.
    """
    boot_id = state.get("xray_boot_id") or ""
    if reader is not None:
        try:
            counters = dict(reader() or {})
        except Exception:
            counters = {}
    else:
        counters = _read_xray_counters(cfg)
    if not isinstance(counters, dict):
        counters = {}
    clean = {}
    for key, val in counters.items():
        try:
            clean[str(key)] = int(val)
        except (TypeError, ValueError):
            continue
    return {"boot_id": boot_id, "counters": clean}


def _read_xray_counters(cfg):
    """Read counters from the local Xray API (v1 stub).

    WHAT: Query Xray's stats endpoint when configured.
    WHY: v1 ships the report path so history accumulates before the
    dashboard exists; without API wiring it returns {} rather than
    failing the loop. A future gRPC reader plugs in here without a
    protocol change.
    """
    _ = cfg
    return {}
