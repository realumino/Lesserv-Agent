# AGENTS.md — project context for Lesserv-Agent

The node-side half of Lesserv. A small Python program that runs on each
proxy node as a systemd service: it pulls the finished runtime Xray config
from the control plane, tests it, applies it, restarts Xray, and reports
what actually happened. The control plane lives in the `Lesserv-Cloud` repo,
and the contract between them lives in **that repo's `docs/PROTOCOL.md`** —
this repo links to it rather than copying it.

## Project state

Milestones and current status live in `PLAN.md`. The walkthrough (the loop,
the apply sequence, failure modes) lives in `ARCHITECTURE.md` — update it
in the same commit that changes code; wrong documentation is worse than
none.

## What the agent is (and is not)

It **is** the only thing on a node that ever touches Xray's config or
restarts Xray. It owns four local facts: the control-plane URL, its node
id, its token, and where Xray's config lives.

It is **not** a second control plane. It contains no policy, no user
knowledge, no allocator, no rendering, and no key management. The config it
receives is complete and final. If you find yourself adding logic that
decides *what* the config should contain, that logic belongs in
`Lesserv-Cloud`.

The one exception is validation that protects the node: `xray -test` before
swapping, and rollback to the last config that started successfully.

## Architecture decisions (locked)

- **Pull, every 30s.** No inbound port, no push channel, NAT-proof.
- **Convergence by content hash.** The node is in sync when its reported
  `applied_hash` matches the plane's `desired_hash`. There is no version
  counter and no ordering to get wrong.
- **Test before swap, snapshot before swap, roll back on failure.** A bad
  config can never take the service down, and the agent is safe to kill at
  any moment — including mid-apply.
- **The plane being down costs staleness and nothing else.** The node keeps
  serving its last config and backs off.
- **systemd-native first.** The agent is host software and lets systemd own
  the Xray process. `restart_mode: "systemd" | "docker"` is the seam that
  lets a node containerize Xray later without touching the protocol.
- **Installed from this repo's releases.** The control plane hands the
  admin a token and a link to the install instructions; it does not host
  the agent.
- **Auth is a scheme, not an assumption.** `bearer-v1` today; `hmac-v1`
  later, switchable per node, without a protocol version change.

## Run it

```
python -m lesserv_agent enroll --cp https://funky.example.com --node tokyo01
python -m lesserv_agent run            # normally via systemd
python -m unittest discover tests -v
```

`enroll` writes `/etc/lesserv/agent.toml` (mode `0600`) and prompts for the
token on stdin. The token is never taken as a command-line argument:
arguments are visible in `ps` output and land in shell history.

## Conventions (user requirement — non-negotiable)

- Every function: docstring explaining WHAT it does and WHY it exists.
- Functions under ~30 lines, one job each. Plain dicts/lists, no clever
  abstractions.
- Dependencies must be justifiable; keep them minimal. The standard library
  and an HTTP client should be enough.
- User reads every file before moving to the next milestone; explain code,
  don't just generate it.
- `ARCHITECTURE.md` is updated in the same commit as the code it describes.

## Gotchas

- **Never leave the node without a working config.** Every path that
  touches the live config is: write temp → test → snapshot → atomic swap →
  restart → verify → roll back on failure. There is no shortcut.
- **Verify, don't assume, on startup.** Test the live config at agent
  start and restore last-good if it fails. That is what makes
  `kill -9` safe.
- **The token is root on that node's secrets** — it grants the rendered
  config, which contains every user's UUID and the REALITY private key.
  `0600` on the config file, never log the `Authorization` header, and
  never echo the token.
- **An error is not a config.** A 5xx, an empty body, a truncated
  response, or a missing field is a network failure. Only an explicit,
  complete, newer config causes a change. Never delete or blank the config.
- **`BLOCK` is not an exit.** The rendered config guarantees it is the
  first outbound, which is Xray's fallback when no rule matches. The agent
  neither knows nor cares; it just must never reorder outbounds.
- **Clock skew matters** once `hmac-v1` arrives. Nodes need working NTP; a
  timestamp window that is too tight turns a drifting clock into an
  outage.
