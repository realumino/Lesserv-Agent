"""Reconcile loop: startup check, heartbeat, apply, backoff.

WHAT: The 30s pull loop plus the kill-safe startup rule.
WHY: Convergence by content hash needs no ordering: equal hashes and
a good last apply mean stop; anything else means fetch-and-apply.
The plane being down costs staleness and nothing else.
"""

import logging
import random
import time

from lesserv_agent import apply as apply_mod
from lesserv_agent import client, state as state_mod
from lesserv_agent import stats as stats_mod
from lesserv_agent import xray

LOG = logging.getLogger("lesserv_agent")

BASE_BACKOFF_S = 5
MAX_BACKOFF_S = 300


def compute_backoff(attempt):
    """Compute backoff delay for a consecutive-failure count.

    WHAT: Capped exponential 5s -> 300s plus small jitter.
    WHY: Protects a struggling plane from a thundering fleet while
    keeping the first retry fast. Jitter spreads synchronized nodes.
    """
    attempt = max(0, int(attempt))
    delay = min(MAX_BACKOFF_S, BASE_BACKOFF_S * (2 ** attempt))
    return delay + random.uniform(0, min(5, delay * 0.1))


def needs_apply(state, desired_hash):
    """Decide whether the desired hash requires work.

    WHAT: Compare desired vs applied plus last-apply health.
    WHY: Hash equality alone is not enough — a failed last apply
    means the node should run something it is not running.
    """
    if not desired_hash:
        return False
    if state.get("applied_hash") != desired_hash:
        return True
    return not state.get("last_apply_ok", True)


def _mark_broken(st, error):
    """Flag state as not-ok with a bounded error message.

    WHAT: Single place that sets last_apply_ok/error.
    WHY: Keeps every startup/apply failure path consistent.
    """
    st["last_apply_ok"] = False
    st["last_error"] = str(error)[:500]
    return st


def _repair_broken_live(cfg, st, code, out):
    """Restore last-good after a failed startup test.

    WHAT: Copy fallback over live and restart Xray.
    WHY: Split from startup_check so the main path stays readable;
    a missing fallback is reported, never hidden.
    """
    LOG.error("startup: live config failed test (exit %s); restoring last-good",
              code)
    try:
        restored = apply_mod.restore_last_good(cfg)
    except Exception as exc:
        restored = False
        out = "%s | restore failed: %s" % (out, exc)
    if restored:
        try:
            xray.restart_xray(cfg.get("xray_service", "xray"),
                              cfg.get("restart_mode", "systemd"))
        except Exception as exc:
            LOG.error("startup: restart after restore failed: %s", exc)
        return _mark_broken(st, "startup: live config broken, restored last-good")
    return _mark_broken(st, "startup: live config broken, no last-good: %s"
                        % (out or code))


def startup_check(cfg, st):
    """Test the live config on every agent start; repair if broken.

    WHAT: `xray test` the live file, restore last-good + restart on fail.
    WHY: This single rule makes kill -9 safe at any instant, including
    between swap and report: no bad config survives a restart.
    Returns the (possibly updated) state dict; never raises.
    """
    import os
    live = apply_mod.live_path(cfg)
    if not os.path.exists(live):
        LOG.warning("startup: no live config at %s", live)
        return _mark_broken(st, "live config missing at startup; awaiting plane")
    try:
        ok, code, out = xray.test_config(cfg.get("xray_binary", "xray"), live)
    except Exception as exc:
        return _mark_broken(st, "startup test error: %s" % exc)
    if ok:
        return st
    return _repair_broken_live(cfg, st, code, out)


def _heartbeat_payload(cfg, st, start_time):
    """Assemble the heartbeat body from local facts.

    WHAT: applied_hash/applied_at/liveness/uptime/last_error.
    WHY: The plane uses it for drift display only; absence is
    information, never an event. Keeps client.py free of xray imports.
    """
    running, pid = xray.service_running(cfg.get("xray_service", "xray"))
    return {
        "applied_hash": st.get("applied_hash", ""),
        "applied_at": st.get("applied_at", 0),
        "xray_running": bool(running),
        "xray_pid": int(pid or 0),
        "agent_uptime": int(time.time() - start_time),
        "last_error": st.get("last_error"),
    }


def _apply_desired(cfg, st, desired):
    """Fetch, apply, persist, and report one desired hash.

    WHAT: Steps 2-8 plus state update and report.
    WHY: Split from run_once so heartbeat-vs-apply reads as two
    paragraphs, not one long function.
    """
    result = apply_mod.apply_to_hash(
        cfg, st, desired,
        fetch_fn=lambda h: _fetch_or_raise(cfg, h),
        test_fn=lambda p: xray.test_config(cfg.get("xray_binary", "xray"), p),
        restart_fn=lambda: xray.restart_xray(cfg.get("xray_service", "xray"),
                                             cfg.get("restart_mode", "systemd")),
        verify_fn=lambda: xray.verify_running(cfg.get("xray_service", "xray")),
    )
    st = _record_result(cfg, st, desired, result)
    try:
        client.do_report(cfg, {
            "hash": desired, "ok": result["ok"], "stage": result["stage"],
            "xray_exit_code": result.get("xray_exit_code"),
            "error": result.get("error"),
        })
    except client.ClientError as exc:
        LOG.warning("report failed (will re-sync next heartbeat): %s", exc)
    return st


def run_once(cfg, st, start_time):
    """Execute a single heartbeat-compare-act cycle.

    WHAT: Heartbeat, maybe apply, report, best-effort stats.
    WHY: Split from the infinite loop so tests drive one cycle with
    fakes. Returns (state, acted, desired_hash). Never raises on
    plane errors — those are backoff signals, not crashes.
    """
    hb = _heartbeat_payload(cfg, st, start_time)
    resp = client.do_heartbeat(cfg, hb)
    desired = resp.get("desired_hash", "")
    if "restart_xray" in (resp.get("actions", []) or []):
        _handle_restart_action(cfg)
    if not needs_apply(st, desired):
        return st, False, desired
    return _apply_desired(cfg, st, desired), True, desired


def _fetch_or_raise(cfg, desired_hash):
    """Fetch config or raise a descriptive error for the apply stage.

    WHAT: GET config, refusing bytes meant for another hash.
    WHY: A hash mismatch means the plane moved between heartbeat and
    fetch — applying those bytes under the old hash would hide drift,
    so it is a fetch-stage failure and the loop re-heartbeats.
    """
    status, payload = client.fetch_config(cfg, desired_hash)
    if status == 304 or payload is None:
        raise client.ClientError("config 304 for %s (already current?)" %
                                 desired_hash)
    if isinstance(payload, dict) and not payload:
        raise client.ClientError("empty config for %s" % desired_hash)
    return payload


def _record_result(cfg, st, desired_hash, result):
    """Persist apply outcome to state.

    WHAT: Update hash/ok/error/boot-id after a verified attempt.
    WHY: The hash advances only on verified start; every Xray
    (re)start mints a new boot id so stats resets stay explicable.
    """
    if result.get("ok"):
        st["applied_hash"] = desired_hash
        st["applied_at"] = int(time.time())
        st["last_apply_ok"] = True
        st["last_error"] = None
        st["xray_boot_id"] = stats_mod.new_boot_id()
    else:
        st["last_apply_ok"] = False
        st["last_error"] = (result.get("error") or
                            ("apply failed at %s" % result.get("stage")))[:500]
        if "rolled back" in (result.get("error") or "").lower():
            st["xray_boot_id"] = stats_mod.new_boot_id()
    return st


def _handle_restart_action(cfg):
    """Restart Xray unchanged for the v1 `restart_xray` action.

    WHAT: Supervisor restart + verify, no config change.
    WHY: The only sanctioned out-of-band behavior; everything else
    is a new rendered config. Failures only log — heartbeat shows it.
    """
    try:
        ok, err = xray.restart_xray(cfg.get("xray_service", "xray"),
                                    cfg.get("restart_mode", "systemd"))
        if ok and not xray.verify_running(cfg.get("xray_service", "xray")):
            LOG.warning("action restart_xray: xray not running after restart")
        elif not ok:
            LOG.warning("action restart_xray failed: %s", err)
    except Exception as exc:
        LOG.warning("action restart_xray error: %s", exc)


def _cycle_success(cfg, st, state_path, start_time):
    """Run one successful heartbeat cycle and return next delay.

    WHAT: run_once + persist, resetting failure count via delay.
    WHY: Keeps run_loop's try/except about backoff, not bookkeeping.
    Returns (state, poll_delay).
    """
    st, _acted, _desired = run_once(cfg, st, start_time)
    state_mod.save_state(state_path, st)
    return st, int(cfg.get("poll_interval", 30))


def _cycle_failure(cfg, failures, exc):
    """Map a heartbeat failure to backoff state and delay.

    WHAT: Log, harden backoff on auth failure, attempt last-good start.
    WHY: Auth rejection must never spin; any failure costs staleness
    only. Returns (new_failures, delay).
    """
    LOG.warning("heartbeat failed: %s", exc)
    if exc.auth_failed:
        LOG.error("token rejected; keeping serving, backing off hard")
        failures = max(failures + 2, 4)
    else:
        failures += 1
    try:
        _maybe_start_last_good(cfg)
    except Exception as start_exc:
        LOG.warning("last-good start attempt failed: %s", start_exc)
    return failures, compute_backoff(failures)


def run_loop(cfg, state_path=None, max_cycles=None):
    """Run the pull loop forever (or for max_cycles in tests).

    WHAT: Heartbeat every poll_interval; backoff 5s->300s on failure.
    WHY: Missing plane costs staleness only. Auth failures back off
    harder and never spin. Stats send best-effort on its own cadence.
    """
    start_time = time.time()
    failures = 0
    cycles = 0
    last_stats = 0.0
    state_path = state_path or state_mod.default_state_path(cfg)
    while True:
        st = state_mod.load_state(state_path)
        try:
            st, delay = _cycle_success(cfg, st, state_path, start_time)
            failures = 0
        except client.ClientError as exc:
            failures, delay = _cycle_failure(cfg, failures, exc)
        try:
            if _maybe_send_stats(cfg, st, last_stats):
                last_stats = time.time()
        except Exception as exc:
            LOG.debug("stats skipped: %s", exc)
        cycles += 1
        if max_cycles is not None and cycles >= max_cycles:
            return
        time.sleep(delay)


def _maybe_start_last_good(cfg):
    """Try to start last-good when Xray is down.

    WHAT: Supervisor liveness check + start attempt.
    WHY: Covers Xray-down + plane-unreachable: the node must keep
    serving its last config without plane help.
    """
    running, _ = xray.service_running(cfg.get("xray_service", "xray"))
    if running:
        return
    import os
    live = apply_mod.live_path(cfg)
    if not os.path.exists(live) and os.path.exists(apply_mod.last_good_path(cfg)):
        apply_mod.restore_last_good(cfg)
    xray.start_xray(cfg.get("xray_service", "xray"),
                    cfg.get("restart_mode", "systemd"))


def _maybe_send_stats(cfg, st, last_stats):
    """Send absolute counters when the stats interval elapsed.

    WHAT: Throttled best-effort stats report.
    WHY: History accumulates for a future dashboard; failures never
    disturb the reconcile loop. Returns True when a send was attempted.
    """
    interval = int(cfg.get("stats_interval", 60) or 60)
    if time.time() - last_stats < interval:
        return False
    payload = stats_mod.collect_stats(cfg, st)
    client.do_stats(cfg, payload)
    return True
