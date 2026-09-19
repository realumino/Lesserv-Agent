# ARCHITECTURE.md — how Lesserv-Agent works, explained

A human-readable walkthrough of the node-side half of Lesserv: the loop it
runs, how a config reaches Xray, and what happens when things go wrong.

**Maintenance rule: this file is updated in the same commit that changes
the code.** Treat it like code — wrong documentation is worse than none.

## What the agent is

One small Python program per node, running as a systemd service. It is the
only thing on the machine that writes Xray's config or restarts Xray.

It does not know who the users are, which exits exist, or what the node is
called in any topology. It knows its own node id, its token, and a URL. The
config it receives is complete: qualified, with clients, routing rules, and
REALITY private keys already in it.

That asymmetry is the design. Every decision about who may use what lives
in the control plane, so the node holds no policy to drift out of date and
nothing to leak beyond the config it was given.

## The loop

```
  ┌─ every 30s ────────────────────────────────────────────────┐
  │                                                            │
  │  heartbeat ──► desired_hash                                │
  │      │                                                     │
  │      ├── equal to applied_hash, last apply ok → do nothing │
  │      │                                                     │
  │      └── different → fetch → test → snapshot → swap →      │
  │                       restart → verify → report            │
  └────────────────────────────────────────────────────────────┘
```

There is no queue, no push channel, and no ordering to maintain. The plane
never instructs the node to do anything; it only answers "here is the hash
of what this node should be running". The agent compares that with what it
last applied successfully, and reconciles the difference itself.

This is why the protocol is resilient in ways that are hard to get
otherwise: a reboot, an agent crash, a duplicated poll, a control-plane
restart, or two admins editing at once all resolve to the same thing —
compare hashes, apply if different, report what happened.

## The apply sequence

Applying is the only dangerous operation the agent performs, so every path
through it is the same path.

**1. Decide.** Compare `desired_hash` from the heartbeat with the stored
`applied_hash`. If they match *and* the last apply succeeded, stop. A
failed last apply matters even when the hashes match: it means the node is
supposed to be running something it is not.

**2. Fetch.** Request the config for that hash. A `409` means the plane
moved on between heartbeat and fetch, so the loop re-heartbeats instead
of applying bytes it did not ask for; the returned hash is verified
against the requested one for the same reason. Anything that is not a
complete, well-formed config for the requested hash is treated as a
network failure.

**3. Test.** Write the config to a temporary file and run Xray's own
config test against it. Xray is the authority on whether a config is valid
— the agent deliberately has no opinion, because having an opinion would
mean duplicating the control plane's knowledge. A non-zero exit stops here.
The live config was never touched.

**4. Snapshot.** Copy the currently running config to `config.last_good` —
but only if the previous apply succeeded. Snapshotting a broken config
would preserve the breakage as the fallback, which is precisely backwards.

**5. Swap.** Rename the temporary file over the live config. Rename is
atomic on the same filesystem, so Xray can never observe a partially
written file. If the agent dies here, the node is running a config that was
already tested.

**6. Restart and verify.** Restart Xray, then wait a grace period and
confirm the process is actually alive. A config that passes the test can
still die at startup — a port already in use, a missing file referenced by
the config, a permission problem.

**7. Roll back or report.** If Xray is not running, restore `last_good`,
restart, and report the failure with the stage it failed at. Otherwise
persist the applied hash and report success.

**The startup rule.** On every agent start, test the *live* config; if it
fails, restore `last_good` and restart Xray. Combined with the sequence
above, this makes the agent safe to `kill -9` at any instant, including
between the swap and the report. There is no window in which a bad config
can persist across a restart.

## Local files

```
/etc/lesserv/agent.toml          cp_url, node_id, token, poll_interval,
                                 xray_binary, config_path, restart_mode,
                                 xray_service, stats_interval, auth
/var/lib/lesserv/state.json      applied_hash, applied_at, last_apply_ok,
                                 last_error, xray_boot_id
/var/lib/lesserv/config.json     what Xray is running now
/var/lib/lesserv/config.last_good  the last config that started successfully
```

`agent.toml` is `0600`. It is the only file an operator ever edits by hand,
and the only thing it contains that matters is the token.

`state.json` is written atomically and is deliberately minimal: the hash
the node believes it is running, when it applied it (`applied_at`), whether
the last apply worked, the last error, and the current Xray boot id (minted
fresh on every verified start so stats resets stay explicable). Everything
else can be recomputed from the plane or from the config on disk.

`config.last_good` is the safety net. It is the reason a failed apply is an
inconvenience rather than an outage.

## The client and auth

Every request carries the node id, the protocol version, and a bearer
token. The token arrived from the control plane at node-creation time,
shown once, and is compared server-side against a stored hash.

Auth is a *scheme*, selected by `agent.toml`, not a hardcoded behavior.
`bearer-v1` sends the token in a header over TLS. A later `hmac-v1` will
sign the method, path, timestamp, nonce, and body hash with a shared secret
and send no secret at all — which is strictly better in one narrow sense
(the secret is never transmitted, so nothing that logs headers can leak it)
and worse in one practical sense (it makes clock correctness load-bearing).
Because the scheme is a field, a fleet can migrate one node at a time.

The token is equivalent to root on that node's config. It must never be
logged, never echoed, and never passed as a command-line argument, because
command-line arguments appear in `ps` output and in shell history.

## Restart strategies

`restart_mode` is the seam that lets the node change shape without the
protocol changing:

- **`systemd`** (v1) — the agent writes the config and asks systemd to
  restart the Xray unit. systemd owns the process, so `journalctl` works
  and restarts survive the agent. This is the idiomatic Linux split: a
  supervisor supervises, a reconciler reconciles.
- **`docker`** (later) — the agent writes the config to a shared volume,
  tests it in a throwaway container built from the *same image tag Xray
  runs* (testing against a binary of a different version would prove
  nothing), and restarts the container through the Docker socket.

Because the agent runs on the host rather than inside a container, moving
Xray into a container later introduces no new trust problem: the agent
already has host access, so the socket never has to be mounted anywhere.

## Failure modes

| Situation | What happens | What the node serves |
|---|---|---|
| Plane unreachable | Nothing changes; backoff 5s→300s with jitter | Current config |
| Plane returns an error | Nothing changes; treated as unreachable | Current config |
| Response is empty or malformed | Nothing changes; treated as a network failure | Current config |
| Config fails the test | Report `stage: "test"`; live config untouched | Current config |
| Xray dies after restart | Restore last-good, restart, report `stage: "started"` | Last good config |
| Agent killed mid-apply | Startup rule tests the live config and repairs it | Tested config or last-good |
| Token rejected | Keep serving; back off harder; surface locally | Current config |
| Xray down, plane reachable | Apply normally — the desired state is what matters | New config |

The invariant behind the table: **a node never degrades itself because it
lost contact with the control plane.** Staleness is the entire cost of an
outage.

## Stats collection

When enabled, the agent polls Xray's local API for per-email traffic
counters and reports them. Two properties matter:

- **Absolute values, not deltas.** Reports can be retried, and a retried
  delta would double-count. The plane stores the last value it saw and
  accumulates the difference, so retries are harmless. Handling resets is
  also the plane's job, not the node's.
- **A boot identifier travels with the counters,** changing every time Xray
  restarts. That is what lets the plane distinguish "counter went backwards
  because Xray restarted" from "counter went backwards because something is
  wrong".

The agent does not aggregate, does not store history, and does not decide
what any of it means. It reads numbers and forwards them, which is the same
division of labor as the config itself.

## Concepts that were confusing (and their answers)

- **Why the agent tests a config it did not write.** Because testing is not
  the same as understanding. Xray's own test is the authority on validity,
  and using it means the agent never has to know what a REALITY key or a
  routing rule is.
- **Why snapshot before swap, not after.** If the new config fails and the
  snapshot happened after the swap, the fallback would be the broken
  config. Order matters more than it looks.
- **Why "nothing to do" needs two conditions.** Hash equality alone is not
  enough: it says the node has the right config, not that the right config
  is running. A failed last apply breaks that assumption.
- **Why the protocol version is in every request.** Two repos, independent
  release cadences. The version is what lets the plane serve an old agent
  faithfully instead of guessing what it understands.
- **Why actions are almost never used.** Everything is easier to reason
  about when the only way to change a node is to change its config. The
  action list exists for the rare case that does not fit, and staying short
  is the point.

## Conventions

Same as `AGENTS.md` — this is binding: docstrings explaining what and why,
functions under ~30 lines with one job, plain dicts and lists, minimal
dependencies, and this file updated in the same commit as the code.
