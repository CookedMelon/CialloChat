#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
OUT="${1:?用法: make-test-cert.sh 空目标目录}"
mkdir -p "$OUT"
if [[ -n "$(find "$OUT" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
  echo '拒绝覆盖已有证书；请指定空目录。' >&2
  exit 1
fi
openssl req -x509 -newkey rsa:2048 -nodes -days 2 -keyout "$OUT/ca.key" -out "$OUT/ca.crt" -subj '/CN=CialloChat Test CA' -addext 'basicConstraints=critical,CA:TRUE' -addext 'keyUsage=critical,keyCertSign,cRLSign' 2>/dev/null
openssl req -newkey rsa:2048 -nodes -keyout "$OUT/server.key" -out "$OUT/server.csr" -subj '/CN=localhost' 2>/dev/null
cat > "$OUT/extensions.cnf" <<'EXT'
subjectAltName=DNS:localhost,IP:127.0.0.1
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
EXT
openssl x509 -req -in "$OUT/server.csr" -CA "$OUT/ca.crt" -CAkey "$OUT/ca.key" -CAcreateserial -out "$OUT/server.crt" -days 2 -extfile "$OUT/extensions.cnf" 2>/dev/null
rm -f "$OUT/server.csr" "$OUT/extensions.cnf" "$OUT/ca.srl"
chmod 700 "$OUT"
chmod 600 "$OUT"/*
echo "测试证书已写入 $OUT；仅用于 localhost，未安装到系统信任库。"
