#!/usr/bin/env bash
# Narrow INPUT rule for native MediaMTX; leaves SSH and established sessions alone.
set -Eeuo pipefail
[[ "$EUID" == 0 ]]
publish_port="${1:-1936}"
read_port="${2:-8554}"
[[ "$publish_port" =~ ^[0-9]+$ && "$read_port" =~ ^[0-9]+$ ]]
for firewall in iptables ip6tables; do
  command -v "$firewall" >/dev/null || continue
  "$firewall" -w -N CIALLOCHAT_NEW 2>/dev/null || true
  "$firewall" -w -F CIALLOCHAT_NEW
  "$firewall" -w -A CIALLOCHAT_NEW -m hashlimit --hashlimit-upto 12/minute \
    --hashlimit-burst 8 --hashlimit-mode srcip --hashlimit-name ciallochat-new \
    --hashlimit-htable-max 4096 --hashlimit-htable-expire 60000 -j RETURN
  "$firewall" -w -A CIALLOCHAT_NEW -j DROP
  if ! "$firewall" -w -C INPUT -p tcp -m multiport --dports "$publish_port,$read_port" \
      -m conntrack --ctstate NEW -j CIALLOCHAT_NEW 2>/dev/null; then
    "$firewall" -w -I INPUT 1 -p tcp -m multiport --dports "$publish_port,$read_port" \
      -m conntrack --ctstate NEW -j CIALLOCHAT_NEW
  fi
done
