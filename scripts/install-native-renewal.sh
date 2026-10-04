#!/usr/bin/env bash
# Keep the public domain certificate renewed and reload it after validation.
set -Eeuo pipefail
[[ "$EUID" == 0 ]]
root="${1:-/opt/ciallochat}"
lineage="${2:-ciallochat-domain}"
[[ "$root" =~ ^/[a-zA-Z0-9/_.-]+$ ]]
[[ "$lineage" =~ ^[a-zA-Z0-9_.-]+$ ]]
[[ -x /opt/ciallochat-certbot/bin/certbot ]]
cat > /etc/systemd/system/ciallochat-certbot-renew.service <<UNIT
[Unit]
Description=Renew CialloChat domain certificate
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
UMask=0077
ExecStart=/opt/ciallochat-certbot/bin/certbot renew --cert-name $lineage --quiet --deploy-hook "/bin/bash $root/scripts/certbot-deploy.sh $root"
UNIT
cat > /etc/systemd/system/ciallochat-certbot-renew.timer <<'UNIT'
[Unit]
Description=Check CialloChat certificate renewal twice daily

[Timer]
OnCalendar=*-*-* 00,12:00:00
RandomizedDelaySec=1800
Persistent=true

[Install]
WantedBy=timers.target
UNIT
systemd-analyze verify /etc/systemd/system/ciallochat-certbot-renew.service /etc/systemd/system/ciallochat-certbot-renew.timer
systemctl daemon-reload
systemctl enable --now ciallochat-certbot-renew.timer
