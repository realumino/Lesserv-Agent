"""The apply pipeline: fetch, test, snapshot, swap, restart, rollback.

WHAT: The only code path allowed to touch the live Xray config.
WHY: Every apply follows the same order so a bad config can never
take the service down and the agent is safe to kill at any moment.
"""

import json
import os
import shutil
import tempfile


def live_path(cfg):
    """Return the live config path from config.

    WHAT: Resolve what Xray is running now.
    WHY: Single accessor so tests and rollback agree on the file.
    """
    return cfg.get("config_path", "/var/lib/lesserv/config.json")


def last_good_path(cfg):
    """Derive the last-good snapshot path from the live path.

    WHAT: Map config.json -> config.last_good.
    WHY: One deterministic rule (matches ARCHITECTURE.md) so startup
    repair and rollback always find the same fallback file.
    """
    live = live_path(cfg)
    if live.endswith(".json"):
        return live[: -len(".json")] + ".last_good"
    return live + ".last_good"


def write_temp_config(cfg, config_data):
    """Write fetched config bytes to a temp file in the live dir.

    WHAT: Stage the candidate config beside the live file.
    WHY: Same filesystem => atomic rename later; Xray can never see
    a half-written file. Returns the temp path.
    """
    live = live_path(cfg)
    parent = os.path.dirname(live) or "."
    os.makedirs(parent, exist_ok=True)
    if isinstance(config_data, (dict, list)):
        payload = json.dumps(config_data, indent=2).encode("utf-8")
    elif isinstance(config_data, bytes):
        payload = config_data
    else:
        payload = str(config_data).encode("utf-8")
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".config.next.")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return tmp


def snapshot_if_ok(cfg, state):
    """Copy live config to last-good when the last apply succeeded.

    WHAT: Preserve the fallback before swapping.
    WHY: Snapshotting a broken config would preserve breakage as the
    fallback — precisely backwards. Skips when last apply failed or
    when no live config exists yet (first boot).
    """
    if not state.get("last_apply_ok", True):
        return False
    live = live_path(cfg)
    if not os.path.exists(live):
        return False
    dest = last_good_path(cfg)
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    shutil.copyfile(live, dest)
    return True


def swap_atomic(tmp, cfg):
    """Atomically rename the tested temp file over the live config.

    WHAT: Publish the new config in one syscall.
    WHY: Rename on the same filesystem is atomic; killing the agent
    here leaves a tested config on disk, which startup-check accepts.
    """
    os.replace(tmp, live_path(cfg))


def restore_last_good(cfg):
    """Restore the last-good snapshot over the live config.

    WHAT: Roll back after a failed start.
    WHY: Returns True when a fallback existed and was restored;
    False when there was nothing to restore (first-boot failure).
    """
    src = last_good_path(cfg)
    if not os.path.exists(src):
        return False
    os.makedirs(os.path.dirname(live_path(cfg)) or ".", exist_ok=True)
    shutil.copyfile(src, live_path(cfg))
    return True


def _fail(stage, error, code=None):
    """Build a failure result dict for one stage.

    WHAT: Single constructor for apply outcomes.
    WHY: Keeps every early return one line so the pipeline reads as
    stages, not dict literals.
    """
    return {"ok": False, "stage": stage, "error": (error or "")[:500],
            "xray_exit_code": code}


def _stage_fetch_to_tmp(cfg, desired_hash, fetch_fn):
    """Fetch config and stage it as a temp file.

    WHAT: Steps 2 + temp-write with fetch-stage error mapping.
    WHY: Isolates network/empty failures from test failures so the
    reported stage always names the real culprit.
    """
    try:
        config_data = fetch_fn(desired_hash)
    except Exception as exc:
        return None, _fail("fetched", str(exc))
    if config_data is None or (isinstance(config_data, dict) and not config_data):
        return None, _fail("fetched", "empty config for hash %s" % desired_hash)
    try:
        return write_temp_config(cfg, config_data), None
    except Exception as exc:
        return None, _fail("fetched", str(exc))


def _stage_test_snapshot_swap(cfg, state, tmp, test_fn):
    """Test, snapshot, and atomically swap the staged config.

    WHAT: Steps 3-5 with stage-accurate failures.
    WHY: Test failure must leave live untouched; snapshot must precede
    swap and only follow a healthy previous apply.
    """
    try:
        ok, exit_code, output = test_fn(tmp)
    except Exception as exc:
        _drop(tmp)
        return _fail("test", str(exc))
    if not ok:
        _drop(tmp)
        return _fail("test", output or "config test failed", exit_code)
    try:
        snapshot_if_ok(cfg, state)
    except Exception as exc:
        _drop(tmp)
        return _fail("test", "snapshot failed: %s" % exc)
    try:
        swap_atomic(tmp, cfg)
    except Exception as exc:
        _drop(tmp)
        return _fail("applied", "swap failed: %s" % exc)
    return None


def _stage_restart_verify(cfg, restart_fn, verify_fn):
    """Restart Xray, verify, and roll back on failure.

    WHAT: Steps 6-7 including last-good restore.
    WHY: A test pass cannot prove a live bind; only a surviving
    process counts, and a dead one must restore the fallback.
    """
    try:
        restart_ok, restart_err = restart_fn()
    except NotImplementedError as exc:
        return _fail("applied", str(exc))
    except Exception as exc:
        restart_ok, restart_err = False, str(exc)
    if not restart_ok:
        return _fail("applied", restart_err or "restart failed")
    try:
        alive = bool(verify_fn())
    except Exception:
        alive = False
    if alive:
        return {"ok": True, "stage": "started", "error": None,
                "xray_exit_code": None}
    rolled = _try_rollback(cfg, restart_fn)
    err = "xray failed to stay up after restart"
    err += " (rolled back to last-good)" if rolled else " (no last-good to restore)"
    return _fail("started", err)


def _try_rollback(cfg, restart_fn):
    """Restore last-good and restart; never raise.

    WHAT: Best-effort fallback after a dead start.
    WHY: Rollback failure is reported, not raised — the loop must
    survive to report the original stage.
    """
    try:
        rolled = restore_last_good(cfg)
        if rolled:
            try:
                restart_fn()
            except Exception:
                pass
        return rolled
    except Exception:
        return False


def apply_to_hash(cfg, state, desired_hash, fetch_fn, test_fn,
                  restart_fn, verify_fn):
    """Run steps 2-8 of the apply sequence for one hash.

    WHAT: Fetch, test, snapshot, swap, restart, verify/rollback.
    WHY: Injected callables keep Xray/HTTP out of this logic so unit
    tests cover every failure stage without a daemon. Returns a
    result dict {ok, stage, error, xray_exit_code}; the caller
    persists state and reports. Never raises on expected failures.
    """
    tmp, err = _stage_fetch_to_tmp(cfg, desired_hash, fetch_fn)
    if err:
        return err
    err = _stage_test_snapshot_swap(cfg, state, tmp, test_fn)
    if err:
        return err
    return _stage_restart_verify(cfg, restart_fn, verify_fn)


def _drop(path):
    """Remove a temp file, ignoring errors.

    WHAT: Clean up rejected candidates.
    WHY: A failed test must leave no residue; unlink failures are
    never apply failures.
    """
    try:
        os.remove(path)
    except OSError:
        pass
