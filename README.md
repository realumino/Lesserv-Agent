# Lesserv-Agent

The node-side half of Lesserv. A small Python program that runs on each
proxy node as a systemd service: it pulls the finished runtime Xray config
from the control plane, tests it, applies it, restarts Xray, and reports
what actually happened.

It contains no policy, no user knowledge, and no rendering. The config it
receives is complete and final.

## Install

Installed from this repo's releases. The control plane gives you a node id
and a token; the installer takes the control-plane URL and node id, and
prompts for the token.

```sh
curl -fsSL https://github.com/realumino/Lesserv-Agent/releases/latest/download/install.sh | sh
```

## Documents

| File | What it holds |
|---|---|
| [`AGENTS.md`](AGENTS.md) | Stable context: what this is, locked decisions, how to run it, conventions, gotchas |
| [`PLAN.md`](PLAN.md) | Volatile state: the milestone roadmap and current status |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | The walkthrough: the loop, the apply sequence, failure modes |

The contract with the control plane lives in the
[`Lesserv-Cloud`](https://github.com/realumino/Lesserv-Cloud) repo as
`docs/PROTOCOL.md` — this repo links to it rather than copying it.
