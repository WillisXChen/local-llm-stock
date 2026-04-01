#!/bin/bash
# ============================================================
#  自動產生自簽 SSL 憑證（開發/本地使用）
# ============================================================

SSL_DIR="$(cd "$(dirname "$0")/../ssl" && pwd)"
DAYS=365
DOMAIN="${SSL_DOMAIN:-localhost}"

echo "=== 產生 SSL 自簽憑證 ==="
echo "目錄：$SSL_DIR"
echo "域名：$DOMAIN"
echo "有效天數：$DAYS"
echo ""

# 產生隨機 CA 根憑證
openssl genrsa -out "$SSL_DIR/ca.key" 4096 2>/dev/null
openssl req -new -x509 -days "$DAYS" -key "$SSL_DIR/ca.key" \
  -out "$SSL_DIR/ca.crt" \
  -subj "/C=TW/ST=Taiwan/L=Taipei/O=LocalLLM/CN=LocalLLM-CA" 2>/dev/null

# 產生伺服器金鑰和 CSR
openssl genrsa -out "$SSL_DIR/server.key" 2048 2>/dev/null
openssl req -new -key "$SSL_DIR/server.key" \
  -out "$SSL_DIR/server.csr" \
  -subj "/C=TW/ST=Taiwan/L=Taipei/O=LocalLLM/CN=$DOMAIN" 2>/dev/null

# 建立 SAN 擴展設定（支援 localhost + IP）
cat > "$SSL_DIR/san.cnf" <<EOF
[req_ext]
subjectAltName = @alt_names

[alt_names]
DNS.1 = localhost
DNS.2 = $DOMAIN
DNS.3 = *.localhost
IP.1 = 127.0.0.1
IP.2 = ::1
EOF

# 使用 CA 簽署伺服器憑證
openssl x509 -req -days "$DAYS" \
  -in "$SSL_DIR/server.csr" \
  -CA "$SSL_DIR/ca.crt" -CAkey "$SSL_DIR/ca.key" -CAcreateserial \
  -out "$SSL_DIR/server.crt" \
  -extfile "$SSL_DIR/san.cnf" -extensions req_ext 2>/dev/null

# 合併完整憑證鏈
cat "$SSL_DIR/server.crt" "$SSL_DIR/ca.crt" > "$SSL_DIR/fullchain.crt"

# 產生 DH 參數（加強安全性）
openssl dhparam -out "$SSL_DIR/dhparam.pem" 2048 2>/dev/null

# 清理中間檔案
rm -f "$SSL_DIR/server.csr" "$SSL_DIR/ca.srl" "$SSL_DIR/san.cnf"

echo ""
echo "✅ SSL 憑證產生完成！"
echo "   CA 憑證：   $SSL_DIR/ca.crt"
echo "   伺服器金鑰：$SSL_DIR/server.key"
echo "   伺服器憑證：$SSL_DIR/server.crt"
echo "   完整憑證鏈：$SSL_DIR/fullchain.crt"
echo "   DH 參數：   $SSL_DIR/dhparam.pem"
echo ""
echo "💡 如需瀏覽器信任，將 ca.crt 加入系統鑰匙圈："
echo "   sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain $SSL_DIR/ca.crt"
