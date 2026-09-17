# PLAN.md — Lesserv-Agent milestone roadmap

This file holds the project's volatile state: what is done, what is next.
Stable context lives in `AGENTS.md`; the walkthrough lives in
`ARCHITECTURE.md`; the contract with the control plane lives in
`Lesserv-Cloud/docs/PROTOCOL.md`.

## What "apply" means in this file

Applying is always the same seven behaviors, in this order, and no
milestone is allowed to take a shortcut through them:

1. Compare the plane's desired hash with the locally applied hash.
2. Fetch the rendered config and write it to a temporary file.
3. Test it with Xray's own config test — the live config is untouched if
   this fails.
4. Snapshot the current config as last-good, but only if the previous
   apply succeeded.
5. Rename the temporary file over the live config, atomically.
6. Restart Xray and verify it is actually running.
7. Roll back to last-good and report the failure if it is not.

Plus one rule that is not a step: on agent startup, test the live config
and restore last-good if it is broken. That rule is what makes the agent
safe to kill at any moment.

## Current status: not started

The agent is the node half of the control plane's milestone M3. The two
repos are developed against each other: `Lesserv-Cloud` serves this
protocol from a local process on a single VPS first, so everything here is
testable without any Cloudflare involvement.

| # | Milestone | Status |
|---|-----------|--------|
| 0 | Skeleton: CLI, `agent.toml`, logging, state file | not started |
| 1 | Client, enroll, heartbeat (`bearer-v1`) | not started |
| 2 | The apply pipeline (fetch, test, snapshot, swap, restart, rollback) | not started |
| 3 | Reporting, offline behavior, startup verification | not started |
| 4 | Packaging: systemd unit, installer, release artifact | not started |
| 5 | Stats collection and reporting | not started |
| — | `hmac-v1` request signing | later |
| — | Self-update from the control plane | later |
| — | `restart_mode: "docker"` (Xray in a container) | later |

## Milestones

### A0 — Skeleton

Done when: the CLI has `enroll` and `run`; `agent.toml` is read from a
known location with mode `0600` and a missing or malformed file produces a
clear message rather than a traceback; logging works to stdout for systemd
to capture; and the state file is read and written atomically, because a
half-written state file is worse than none.

### A1 — Client, enroll, heartbeat

Done when: `enroll` prompts for the token on stdin, writes `agent.toml`,
and performs the first authenticated request; the client sends the node id
and protocol version on every call and never logs the `Authorization`
header; `run` heartbeats on a 30-second interval and correctly concludes
"nothing to do" when the desired hash matches the applied hash.

### A2 — The apply pipeline

The core of the agent. Done when: a config that fails the test is rejected
with the live config untouched; a config that passes the test but kills
Xray on start triggers a rollback to last-good, a restart, and a failure
report at the right stage; the last-good snapshot is never taken from a
failed apply; the swap is atomic; and the applied hash is persisted only
after a verified start.

### A3 — Reporting, offline behavior, startup verification

Done when: success and every failure stage are reported and visible in the
plane; an unreachable plane changes nothing on the node and backs off from
5s to 300s with jitter; a broken live config found at startup is replaced
from last-good; and killing the agent at any point — including between the
swap and the report — then restarting it converges without human action.

### A4 — Packaging

Done when: a systemd unit runs the agent with restart-on-failure; an
installer script is published with each release and takes the control-plane
URL and node id while prompting for the token; the agent reports its own
version so the dashboard can show which nodes are behind; and the install
path is documented well enough that the control plane can link to it
instead of hosting anything.

### A5 — Stats collection and reporting

Done when: the agent polls the local Xray API for per-email counters and
reports absolute values with a boot identifier; counter resets are handled
by the plane, not by the agent; and an Xray restart produces a new boot id
rather than a negative delta. The dashboard is the control plane's problem,
not this repo's.

## Later

Three things are deliberately deferred, and each is a small, contained
change rather than a redesign:

- **`hmac-v1`** — signing requests so the secret is never transmitted. This
  is a new auth scheme in the client, selected by a field in `agent.toml`,
  switchable one node at a time. It is the only feature that makes the
  node's clock correctness matter.
- **Self-update** — the agent replacing itself from the control plane. It is
  a security-sensitive feature and gets its own design pass, not a line
  item.
- **`restart_mode: "docker"`** — Xray running in a container. The agent
  gains a second restart strategy and tests configs in a throwaway
  container from the same image Xray runs, so the test always matches the
  running version.
