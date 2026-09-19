"""Xray process helpers: test, restart, verify.

WHAT: Thin wrappers around the Xray binary and systemd.
WHY: Xray is the authority on config validity (the agent has no
opinion), and systemd owns the process (the agent only reconciles).
restart_mode is the seam for a future docker strategy.
"""

import platform
import shutil
import socket
import subprocess
import time

TEST_TIMEOUT_S = 15
VERIFY_GRACE_S = 3


def _run(cmd, timeout=TEST_TIMEOUT_S):
    """Run a subprocess, capturing output without raising.

    WHAT: Uniform (returncode, combined-output) runner.
    WHY: Callers decide what failure means; missing binaries report
    as failures, not tracebacks, so the loop can back off cleanly.
    """
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=timeout)
    except FileNotFoundError:
        return 127, "command not found: %s" % cmd[0]
    except subprocess.TimeoutExpired:
        return 124, "timed out: %s" % " ".join(cmd)
    except Exception as exc:
        return 1, str(exc)
    try:
        out = proc.stdout.decode("utf-8", "replace")
    except Exception:
        out = ""
    return proc.returncode, out[-2000:]


def test_config(binary, path, timeout=TEST_TIMEOUT_S):
    """Test a config file with Xray's own checker.

    WHAT: Run `xray test -c <file>` (legacy `-test` fallback).
    WHY: A non-zero exit must stop the apply before the live config
    is touched. Returns (ok, exit_code, output_tail).
    """
    code, out = _run([binary, "test", "-c", path], timeout=timeout)
    if code == 0:
        return True, code, out
    if "unknown" in out.lower() and "test" in out.lower():
        code2, out2 = _run([binary, "-test", "-config", path], timeout=timeout)
        return (code2 == 0), code2, out2
    return False, code, out


def _systemctl():
    """Locate systemctl, or None on non-systemd hosts.

    WHAT: Detect supervisor availability.
    WHY: Dev machines/Windows lack systemd; returning None lets
    callers degrade to "not running" instead of crashing.
    """
    return shutil.which("systemctl")


def service_running(service):
    """Check whether a systemd unit is active.

    WHAT: Query supervisor state + MainPID.
    WHY: Restart-and-verify needs ground truth, not assumptions.
    Returns (running, pid).
    """
    binpath = _systemctl()
    if not binpath:
        return False, 0
    code, _ = _run([binpath, "is-active", "--quiet", service], timeout=10)
    if code != 0:
        return False, 0
    code, out = _run([binpath, "show", service, "-p", "MainPID"], timeout=10)
    pid = 0
    if code == 0:
        for line in out.splitlines():
            if line.startswith("MainPID="):
                try:
                    pid = int(line.split("=", 1)[1])
                except ValueError:
                    pid = 0
    return True, pid


def restart_xray(service, mode="systemd"):
    """Restart Xray through its supervisor.

    WHAT: `systemctl restart <service>`.
    WHY: The agent never manages PIDs itself; systemd keeps journal
    and restart semantics. docker mode is a later milestone.
    """
    if mode == "docker":
        raise NotImplementedError('restart_mode "docker" is a later milestone')
    if mode != "systemd":
        raise ValueError("unknown restart_mode %r" % mode)
    binpath = _systemctl()
    if not binpath:
        return False, "systemctl not found (non-systemd host?)"
    code, out = _run([binpath, "restart", service], timeout=30)
    if code != 0:
        return False, out or ("systemctl restart exited %d" % code)
    return True, ""


def start_xray(service, mode="systemd"):
    """Start Xray if it is down (offline-recovery path).

    WHAT: `systemctl start <service>`.
    WHY: Xray-down + plane-unreachable still tries last-good; this is
    that attempt. Separate from restart for clearer reports.
    """
    if mode == "docker":
        raise NotImplementedError('restart_mode "docker" is a later milestone')
    binpath = _systemctl()
    if not binpath:
        return False, "systemctl not found (non-systemd host?)"
    code, out = _run([binpath, "start", service], timeout=30)
    if code != 0:
        return False, out or ("systemctl start exited %d" % code)
    return True, ""


def verify_running(service, grace_s=VERIFY_GRACE_S):
    """Confirm Xray survived restart after a grace period.

    WHAT: Sleep, then check supervisor state.
    WHY: A config can pass `test` yet die on bind (port in use,
    missing file, perms). Only a live process counts as success.
    """
    time.sleep(max(0, grace_s))
    running, _ = service_running(service)
    return running


def get_xray_version(binary):
    """Report the Xray binary version for enroll.

    WHAT: Best-effort `xray version` first line.
    WHY: The dashboard shows stale nodes; a failure here must never
    block enroll, so unknown versions report as "unknown".
    """
    code, out = _run([binary, "version"], timeout=10)
    if code != 0 or not out.strip():
        return "unknown"
    return out.strip().splitlines()[0][:120]


def get_platform():
    """Report linux/arch for enroll.

    WHAT: Normalize platform string.
    WHY: Cheap admin context; derived locally, never from the plane.
    """
    machine = platform.machine() or "unknown"
    return "linux/%s" % machine


def get_detected_ip():
    """Guess the node's public egress address.

    WHAT: UDP-connect trick, no packets sent.
    WHY: A suggestion for the admin to accept, never adopted
    silently. Returns None when offline rather than failing enroll.
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
        finally:
            sock.close()
    except Exception:
        return None
