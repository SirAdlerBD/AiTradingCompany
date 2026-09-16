#!/usr/bin/env bash
# One-time setup on the VPS. Run as root from the repo root.
set -euo pipefail
id -u desk >/dev/null 2>&1 || useradd --system --home /opt/desk --shell /usr/sbin/nologin desk
mkdir -p /opt/desk /var/lib/desk /var/lib/desk/reports /etc/desk
rsync -a --delete --exclude .venv --exclude .git --exclude '*.sqlite*' ./ /opt/desk/
# Record which commit is deployed; `desk` prints it and stores it on every run.
git rev-parse --short HEAD > /opt/desk/COMMIT 2>/dev/null || echo unknown > /opt/desk/COMMIT
chown -R desk:desk /opt/desk /var/lib/desk
if [ ! -f /etc/desk/desk.env ]; then
  cp deploy/desk.env.example /etc/desk/desk.env
  chown root:desk /etc/desk/desk.env; chmod 640 /etc/desk/desk.env
fi
[ -d /opt/desk/.venv ] || sudo -u desk python3 -m venv /opt/desk/.venv
sudo -u desk /opt/desk/.venv/bin/pip install -q -e '/opt/desk[charts]'
cp deploy/desk.service deploy/desk.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now desk.timer
echo "Deployed commit $(cat /opt/desk/COMMIT) to /opt/desk."
echo "Fill /etc/desk/desk.env, then:"
echo "  sudo -u desk --preserve-env=SAXO_SIM_MCP_TOKEN,SAXO_SIM_ACCOUNT_KEYS /opt/desk/.venv/bin/desk --config /opt/desk/config/desk.yaml guard"
echo "or simply:  systemctl start desk.service && journalctl -u desk -n 20"
