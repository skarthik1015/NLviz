#!/usr/bin/env bash
# Dev/test only (issue J26): Jammi's MySQL source always verifies the server certificate against
# jammi-server's trust store (SSL_CERT_FILE), and a remote client cannot relax that. So the dev
# MySQL gets a certificate signed by a throwaway CA, and the CA is appended to the jammi-server
# container's bundle -- the same thing an operator does with the AWS RDS CA bundle in production.
#
#   bash scripts/dev/mysql_tls_setup.sh <out_dir>
# Writes ca.pem, server-cert.pem, server-key.pem (SAN: 127.0.0.1, localhost) into <out_dir>.
set -euo pipefail
out="${1:?out dir}"
mkdir -p "$out" && cd "$out"
openssl req -x509 -newkey rsa:2048 -nodes -days 30 -subj "/CN=NLviz dev test CA" \
  -keyout ca-key.pem -out ca.pem 2>/dev/null
openssl req -newkey rsa:2048 -nodes -subj "/CN=localhost" -keyout server-key.pem -out server.csr 2>/dev/null
printf "subjectAltName=IP:127.0.0.1,DNS:localhost\nbasicConstraints=CA:FALSE\n" > san.ext
openssl x509 -req -in server.csr -CA ca.pem -CAkey ca-key.pem -CAcreateserial -days 30 \
  -extfile san.ext -out server-cert.pem 2>/dev/null
chmod 644 server-key.pem
rm -f server.csr san.ext ca.srl
echo "wrote $(pwd)/{ca.pem,server-cert.pem,server-key.pem}"
