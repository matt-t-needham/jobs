#!/usr/bin/env bash
# One-time host setup for jobs.bix.computer: everything outside the apps tree.
# Run as root (`sudo bash install-host.sh`); safe to rerun.
#
#   ~/jobs/snapshot    state.json, written by the collector, mounted :ro into the
#                      `jobs` container. Outside the apps tree because it holds
#                      task names, and ai-router mounts the apps tree.
#   ~/jobs/spool       hook events (hook.py writes, the collector drains)
#   ~/jobs/context     per-task turn slices for the workbench viewport (the
#                      collector writes; the `jobs` container mounts :ro)
#   /etc/systemd/system/jobs-collector.service
#
# Enables and (re)starts the collector, so a rerun picks up an edited unit.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
U=matt

install -d -o $U -g $U -m 700 /home/$U/jobs /home/$U/jobs/snapshot /home/$U/jobs/spool /home/$U/jobs/context
for d in sessions jobs projects; do
    [[ -d /home/$U/.claude/$d ]] || install -d -o $U -g $U -m 700 /home/$U/.claude/$d
done

install -m 644 -o root -g root "$HERE/systemd/jobs-collector.service" /etc/systemd/system/jobs-collector.service
systemctl daemon-reload
systemctl enable jobs-collector.service
systemctl restart jobs-collector.service
sleep 3
systemctl --no-pager --lines=5 status jobs-collector.service
[[ -s /home/$U/jobs/snapshot/state.json ]] || { echo "FAILED: no snapshot written; see journalctl -u jobs-collector" >&2; exit 1; }

echo
echo "Done."
