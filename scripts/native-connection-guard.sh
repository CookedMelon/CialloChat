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
  # Remove only our own old jumps when the public ports change. Otherwise an
  # old 1936/8554 rule rate-limits every backend connection as the same proxy IP.
  mapfile -t input_rules < <("$firewall" -w -S INPUT)
  for line in "${input_rules[@]}"; do
    read -r -a rule <<< "$line"
    if [[ "${rule[0]}" == '-A' && "${rule[-1]}" == CIALLOCHAT_NEW ]]; then
      "$firewall" -w -D "${rule[@]:1}"
    fi
  done
  "$firewall" -w -I INPUT 1 ! -i lo -p tcp -m multiport --dports "$publish_port,$read_port" \
    -m conntrack --ctstate NEW -j CIALLOCHAT_NEW
done
