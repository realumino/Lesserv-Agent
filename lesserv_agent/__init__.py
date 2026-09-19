"""Lesserv-Agent: node-side reconciler for Lesserv.

WHAT: Package marker holding the agent and protocol versions.
WHY: A single place for version truth so enroll/heartbeat/report
stay consistent and the dashboard can spot stale nodes.
"""

__version__ = "0.1.0"

#: Protocol version spoken by this agent. Binding contract lives in
#: Lesserv-Cloud/docs/PROTOCOL.md (v1). This repo never copies it.
PROTOCOL_VERSION = 1
