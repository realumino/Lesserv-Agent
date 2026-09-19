"""Command line: `enroll` and `run`.

WHAT: Operator entry points for provisioning and the systemd daemon.
WHY: enroll writes agent.toml once (token via stdin, never argv);
run owns the reconcile loop. Logging goes to stdout for journald.
"""

import argparse
import getpass
import logging
import sys

from lesserv_agent import __version__
from lesserv_agent import client, config as config_mod
from lesserv_agent import loop as loop_mod
from lesserv_agent import state as state_mod
from lesserv_agent import xray

LOG = logging.getLogger("lesserv_agent")


def _setup_logging(verbose=False):
    """Configure stdout logging for systemd capture.

    WHAT: INFO by default, DEBUG with --verbose.
    WHY: The daemon's only observability is journald; the token is
    never logged because only client.py sees it and it logs nothing.
    """
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(stream=sys.stdout, level=level,
                        format="%(asctime)s %(levelname)s %(message)s")


def _prompt_token():
    """Read the node token from stdin without echoing.

    WHAT: Hidden prompt for the pre-minted token.
    WHY: Arguments land in ps/history; stdin does not. Empty input
    aborts enroll rather than writing a useless config.
    """
    token = getpass.getpass("Node token (input hidden): ").strip()
    if not token:
        print("error: empty token; enroll aborted", file=sys.stderr)
        return None
    return token


def _save_enroll_config(args, token):
    """Persist agent.toml from enroll flags plus token.

    WHAT: Build config dict and write it 0600.
    WHY: Split from enroll RPC so validation errors exit before any
    network call. Returns the validated config.
    """
    cfg_path = args.config or config_mod.default_config_path()
    return config_mod.save_config(cfg_path, {
        "cp_url": args.cp,
        "node_id": args.node,
        "token": token,
        "poll_interval": args.poll_interval,
        "xray_binary": args.xray_binary,
        "config_path": args.config_path,
        "xray_service": args.xray_service,
        "restart_mode": args.restart_mode,
        "auth": "bearer-v1",
    })


def _post_enroll(cfg):
    """First-contact enroll RPC with local facts.

    WHAT: Send agent/xray/platform/ip to the plane.
    WHY: The plane records what it only asks once. Returns metadata
    or raises ClientError for the caller to report.
    """
    return client.do_enroll(cfg, {
        "agent_version": __version__,
        "xray_version": xray.get_xray_version(cfg["xray_binary"]),
        "platform": xray.get_platform(),
        "detected_ip": xray.get_detected_ip(),
    })


def cmd_enroll(args):
    """Provision this node: write agent.toml and first-contact enroll.

    WHAT: Prompt token on stdin, save 0600, POST enroll.
    WHY: No self-registration exists plane-side; the first request
    proves possession of the pre-minted token. Token via stdin keeps
    it out of `ps` output and shell history.
    """
    token = _prompt_token()
    if token is None:
        return 2
    try:
        cfg = _save_enroll_config(args, token)
    except config_mod.ConfigError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    try:
        meta = _post_enroll(cfg)
    except client.ClientError as exc:
        print("enroll request failed: %s" % exc, file=sys.stderr)
        print("(agent.toml was still written; fix the plane and re-run enroll)",
              file=sys.stderr)
        return 1
    print("enrolled node %s (plane says: %s)" % (cfg["node_id"], meta))
    return 0


def cmd_run(args):
    """Run the reconcile daemon (normally via systemd).

    WHAT: Startup-check the live config, then heartbeat forever.
    WHY: The startup rule is what makes kill -9 safe; the loop then
    converges by content hash until SIGTERM.
    """
    try:
        cfg = config_mod.load_config(args.config or None)
    except config_mod.ConfigError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    if cfg.get("restart_mode") == "docker":
        print('error: restart_mode "docker" is a later milestone', file=sys.stderr)
        return 2
    state_path = args.state or state_mod.default_state_path(cfg)
    st = state_mod.load_state(state_path)
    st = loop_mod.startup_check(cfg, st)
    state_mod.save_state(state_path, st)
    loop_mod.run_loop(cfg, state_path=state_path)
    return 0


def _add_enroll_args(sub):
    """Define enroll flags (no --token on purpose).

    WHAT: Register enroll subparser arguments.
    WHY: Keeping parser halves separate holds each function to one
    job and documents why the token flag must never exist.
    """
    enroll = sub.add_parser("enroll", help="provision this node")
    enroll.add_argument("--cp", required=True, help="control-plane base URL")
    enroll.add_argument("--node", required=True, help="node id")
    enroll.add_argument("--config", default=None, help="agent.toml path")
    enroll.add_argument("--poll-interval", type=int, default=30)
    enroll.add_argument("--xray-binary", default="xray")
    enroll.add_argument("--config-path", default="/var/lib/lesserv/config.json")
    enroll.add_argument("--xray-service", default="xray")
    enroll.add_argument("--restart-mode", default="systemd",
                        choices=["systemd", "docker"])
    enroll.add_argument("--verbose", action="store_true")
    enroll.set_defaults(func=cmd_enroll)


def _add_run_args(sub):
    """Define run flags.

    WHAT: Register run subparser arguments.
    WHY: Companion to _add_enroll_args; keeps build_parser tiny.
    """
    run = sub.add_parser("run", help="run the reconcile loop")
    run.add_argument("--config", default=None, help="agent.toml path")
    run.add_argument("--state", default=None, help="state.json path")
    run.add_argument("--verbose", action="store_true")
    run.set_defaults(func=cmd_run)


def build_parser():
    """Build the enroll/run argument parser.

    WHAT: Define CLI surface.
    WHY: Token has no flag on purpose — its absence from argv is the
    security property, enforced by not offering the option at all.
    """
    parser = argparse.ArgumentParser(prog="lesserv_agent",
                                     description="Lesserv node agent")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    _add_enroll_args(sub)
    _add_run_args(sub)
    return parser


def main(argv=None):
    """CLI dispatch.

    WHAT: Parse args, run enroll or daemon.
    WHY: Thin wrapper so `python -m lesserv_agent` works under systemd
    and in tests via injected argv.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(getattr(args, "verbose", False))
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
