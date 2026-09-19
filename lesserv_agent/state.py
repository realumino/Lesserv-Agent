"""Node state: reading and writing state.json atomically.

WHAT: Persist the three facts the loop needs across restarts.
WHY: applied_hash + last_apply_ok decide "nothing to do"; a
half-written state file is worse than none, so all writes are
temp-file + atomic rename.
"""

import json
import os
import tempfile

DEFAULT_STATE_PATH = "/var/lib/lesserv/state.json"

DEFAULTS = {
    "applied_hash": "",
    "applied_at": 0,
    "last_apply_ok": True,
    "last_error": None,
    "xray_boot_id": "",
}


def default_state_path(config=None):
    """Resolve the state.json location.

    WHAT: Prefer explicit config, else env override, else default.
    WHY: Tests point at temp dirs; production uses the fixed path.
    """
    if config and config.get("state_path"):
        return config["state_path"]
    return os.environ.get("LESSERV_STATE_PATH", DEFAULT_STATE_PATH)


def load_state(path=None):
    """Load state.json, returning defaults when missing/corrupt.

    WHAT: Read reconciler memory from disk.
    WHY: Missing state is normal on first boot; corrupt state must not
    crash the loop — converge from defaults and let the next
    heartbeat re-sync reality.
    """
    path = path or default_state_path()
    if not os.path.exists(path):
        return dict(DEFAULTS)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return dict(DEFAULTS)
    state = dict(DEFAULTS)
    if isinstance(data, dict):
        for key in DEFAULTS:
            if key in data:
                state[key] = data[key]
    if not isinstance(state["applied_hash"], str):
        state["applied_hash"] = ""
    state["last_apply_ok"] = bool(state["last_apply_ok"])
    return state


def save_state(path, state):
    """Write state.json atomically.

    WHAT: Durably record applied_hash and last result.
    WHY: The hash is persisted only after a verified start; atomic
    rename makes kill-during-write safe (old or new, never half).
    """
    path = path or default_state_path()
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    slim = {key: state.get(key, DEFAULTS[key]) for key in DEFAULTS}
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".state.json.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(slim, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
    return slim
