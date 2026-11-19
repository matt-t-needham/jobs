#!/usr/bin/env bash
# One-time host setup for the workbench broker (workbench plan, phase 3). Run as
# root (`sudo bash install-broker.sh`); safe to rerun.
#
#   ~/jobs/broker/       the sockets (web and approval), audit.log, and the per-run
#                        MCP configs (which hold approval tickets). Owner-only.
#   /etc/systemd/system/jobs-broker.service
#
# The unit is enabled and started only when config/broker.json has an `aud`. The
# workbench is part of jobs.bix.computer, so that aud is the jobs Access app's.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
U=matt

install -d -o $U -g $U -m 700 /home/$U/jobs /home/$U/jobs/broker
install -m 644 -o root -g root "$HERE/systemd/jobs-broker.service" /etc/systemd/system/jobs-broker.service
systemctl daemon-reload

if python3 -c "import json,sys; sys.exit(0 if json.load(open('$HERE/config/broker.json')).get('aud') else 1)"; then
    systemctl enable jobs-broker.service
    systemctl restart jobs-broker.service
    sleep 3
    systemctl --no-pager --lines=5 status jobs-broker.service
    runuser -u $U -- python3 -I "$HERE/broker/probe.py" --state /home/$U/jobs/broker \
        || { echo "FAILED: probe checks; see above" >&2; exit 1; }
else
    echo "config/broker.json has no aud yet: the unit is installed but not started."
    echo "Set aud in config/broker.json (the jobs Access app's audience), then rerun this script."
fi
