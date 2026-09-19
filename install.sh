#!/bin/sh
# Lesserv-Agent installer: host setup + enroll, from this repo's releases.
# WHAT: Install the agent, write agent.toml paths, enable systemd unit.
# WHY: The control plane hands the admin a token + link; it never hosts
# the agent. Token is prompted on stdin, never passed as an argument.
set -eu

CP_URL="${1:-}"
NODE_ID="${2:-}"
SRC_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

usage() {
  echo "usage: install.sh --cp <control-plane-url> --node <node-id>" >&2
  echo "  prompts for the node token on stdin (never pass it as an argument)" >&2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --cp) CP_URL="${2:-}"; shift 2 ;;
    --node) NODE_ID="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

if [ -z "$CP_URL" ] || [ -z "$NODE_ID" ]; then
  usage; exit 2
fi

if [ "$(id -u)" -ne 0 ]; then
  echo "error: run as root (needs /etc/lesserv and systemd)" >&2
  exit 1
fi

command -v python3 >/dev/null || { echo "error: python3 not found" >&2; exit 1; }
command -v systemctl >/dev/null || { echo "error: systemctl not found" >&2; exit 1; }

mkdir -p /etc/lesserv /var/lib/lesserv /usr/local/lib/lesserv-agent
chmod 755 /etc/lesserv /var/lib/lesserv

# Install package (release tarball unpacks next to this script).
cp -r "$SRC_DIR/lesserv_agent" /usr/local/lib/lesserv-agent/
chmod -R a=rX /usr/local/lib/lesserv-agent
cp "$SRC_DIR/systemd/lesserv-agent.service" /etc/systemd/system/lesserv-agent.service
systemctl daemon-reload

# Enroll writes /etc/lesserv/agent.toml (0600) and first-contacts the plane.
PYTHONPATH=/usr/local/lib/lesserv-agent \
  python3 -m lesserv_agent enroll --cp "$CP_URL" --node "$NODE_ID"

systemctl enable --now lesserv-agent.service
echo "lesserv-agent installed and started (journalctl -u lesserv-agent)"
