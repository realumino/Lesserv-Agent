"""Control-plane HTTP client (bearer-v1).

WHAT: The five node endpoints: enroll, heartbeat, config, report, stats.
WHY: One module owns headers, version, timeouts, and error mapping so
callers treat "any error" uniformly: keep serving, change nothing.
Auth is a scheme (bearer-v1 today, hmac-v1 later), not an assumption.
"""

import json
import urllib.error
import urllib.parse
import urllib.request

from lesserv_agent import PROTOCOL_VERSION, __version__

TIMEOUT_S = 10


class ClientError(Exception):
    """Plane unreachable, error status, or malformed response."""

    def __init__(self, message, status=None, auth_failed=False):
        super(ClientError, self).__init__(message)
        self.status = status
        self.auth_failed = auth_failed


def _headers(cfg):
    """Build request headers for a node call.

    WHAT: Attach identity and auth.
    WHY: Every request carries node id + protocol version; the token
    travels only in Authorization and is never logged.
    """
    return {
        "Authorization": "Bearer %s" % cfg["token"],
        "X-Lesserv-Node": cfg["node_id"],
        "Content-Type": "application/json",
        "User-Agent": "lesserv-agent/%s" % __version__,
    }


def _send(cfg, method, path, query, payload):
    """Execute the HTTP round-trip, mapping transport errors.

    WHAT: Build URL, send JSON, return (status, raw bytes).
    WHY: Isolates urllib handling so _request stays about semantics
    (304/empty/malformed), not socket plumbing.
    """
    url = cfg["cp_url"] + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    req = urllib.request.Request(url, data=payload, method=method,
                                 headers=_headers(cfg))
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            return getattr(resp, "status", 200), resp.read()
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", "replace")[:500]
        except Exception:
            detail = ""
        raise ClientError("HTTP %d %s %s" % (exc.code, path, detail),
                          status=exc.code, auth_failed=exc.code in (401, 403))
    except Exception as exc:
        raise ClientError("%s %s failed: %s" % (method, path, exc))


def _decode(path, status, raw):
    """Validate status and parse the JSON body.

    WHAT: Map 304/non-2xx/empty/malformed to ClientError or payload.
    WHY: Only an explicit, complete payload may cause a config change.
    """
    if status == 304:
        return 304, None
    if not (200 <= status < 300):
        raise ClientError("HTTP %d %s" % (status, path), status=status,
                          auth_failed=status in (401, 403))
    if not raw:
        raise ClientError("empty response from %s" % path, status=status)
    try:
        return status, json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise ClientError("malformed JSON from %s: %s" % (path, exc),
                          status=status)


def _request(cfg, method, path, body=None, query=None):
    """Perform one JSON request against the control plane.

    WHAT: Send method+path, parse JSON, map errors to ClientError.
    WHY: Empty/malformed/5xx is a network failure by contract — only
    an explicit, complete payload may cause a config change.
    """
    payload = None
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
    status, raw = _send(cfg, method, path, query, payload)
    return _decode(path, status, raw)


def _require(data, fields, where):
    """Ensure a response carries every field we act on.

    WHAT: Reject incomplete payloads.
    WHY: An error is not a config — a missing field is treated like a
    network failure, never as permission to change the live config.
    """
    if not isinstance(data, dict):
        raise ClientError("malformed response from %s" % where)
    for field in fields:
        if field not in data:
            raise ClientError("response from %s missing %r" % (where, field))
    return data


def do_enroll(cfg, info):
    """First contact for an existing node.

    WHAT: Record agent facts the plane asks once.
    WHY: Idempotent; re-running after an agent.toml edit is the
    correct recovery. Returns node metadata dict.
    """
    body = {"protocol": PROTOCOL_VERSION}
    body.update(info or {})
    _, data = _request(cfg, "POST", "/api/node/enroll", body)
    return _require(data, [], "/api/node/enroll")


def do_heartbeat(cfg, hb):
    """Poll desired state. Must stay cheap.

    WHAT: Send applied_hash + liveness, learn desired_hash + actions.
    WHY: The whole loop is compare-hashes-and-reconcile; equality plus
    last_apply_ok means stop. Returns dict with desired_hash/actions.
    """
    body = {"protocol": PROTOCOL_VERSION}
    body.update(hb or {})
    _, data = _request(cfg, "POST", "/api/node/heartbeat", body)
    data = _require(data, ["desired_hash", "actions"], "/api/node/heartbeat")
    if not isinstance(data["actions"], list):
        raise ClientError("heartbeat actions must be a list")
    return data


def fetch_config(cfg, desired_hash):
    """Download the fully rendered runtime config for a hash.

    WHAT: GET the exact bytes Xray should run.
    WHY: The agent never transforms/merges; the returned hash must
    equal the requested one or the bytes are for a config we did not
    ask for — recording them under the wrong hash would hide drift.
    Returns (status, payload) where payload is dict or raw config.
    """
    status, data = _request(cfg, "GET", "/api/node/config",
                            query={"hash": desired_hash})
    if status == 304:
        return status, None
    if isinstance(data, dict) and "config" in data:
        if "hash" in data and data["hash"] != desired_hash:
            raise ClientError("config hash mismatch: asked %s, got %s"
                              % (desired_hash, data["hash"]))
        return status, data["config"]
    return status, data


def do_report(cfg, rep):
    """Report an apply attempt result.

    WHAT: Send hash/ok/stage/error for diagnosis and audit.
    WHY: Reports never drive correctness — a lost report re-syncs on
    the next heartbeat — but they make drift visible on the dashboard.
    """
    body = {"protocol": PROTOCOL_VERSION}
    body.update(rep or {})
    _request(cfg, "POST", "/api/node/report", body)
    return True


def do_stats(cfg, stats):
    """Report absolute traffic counters with a boot id.

    WHAT: Forward Xray's cumulative counters, untouched.
    WHY: Absolute-plus-boot-id makes retries safe; the plane (not the
    node) accumulates deltas and handles resets. Never compute deltas
    here — a retried delta would double-count.
    """
    body = {"protocol": PROTOCOL_VERSION}
    body.update(stats or {})
    if "boot_id" not in body or "counters" not in body:
        raise ClientError("stats need boot_id and counters")
    _request(cfg, "POST", "/api/node/stats", body)
    return True
