#!/usr/bin/env bash
# Đổi ExpressVPN sang một location cụ thể, rồi chỉ báo thành công khi public IPv4
# thực sự thay đổi và Telegram API còn truy cập được.
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

load_env() {
    [ -f "$1" ] || return 0
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in ''|\#*) continue ;; esac
        [ "${line#*=}" = "$line" ] && continue
        key="${line%%=*}"; val="${line#*=}"
        val="${val%\"}"; val="${val#\"}"; val="${val%\'}"; val="${val#\'}"
        [ -z "${!key:-}" ] && export "$key=$val"
    done < "$1"
}
load_env "$DIR/.env"

LOCATION="${1:-}"
[ -n "$LOCATION" ] || { echo "Dùng: $0 '<ExpressVPN location>'" >&2; exit 2; }

EXPRESSVPN_BIN="${EXPRESSVPN_BIN:-expressvpnctl}"
CURL_BIN="${CURL_BIN:-curl}"
VPN_CONNECT_TIMEOUT_SECS="${VPN_CONNECT_TIMEOUT_SECS:-90}"
VPN_CONNECT_SETTLE_SECS="${VPN_CONNECT_SETTLE_SECS:-30}"
IP_CHECK_URLS="${IP_CHECK_URLS:-https://api.ipify.org https://ifconfig.me/ip}"

[[ "$VPN_CONNECT_TIMEOUT_SECS" =~ ^[1-9][0-9]*$ ]] || { echo "VPN_CONNECT_TIMEOUT_SECS không hợp lệ" >&2; exit 2; }
[[ "$VPN_CONNECT_SETTLE_SECS" =~ ^[0-9]+$ ]] || { echo "VPN_CONNECT_SETTLE_SECS không hợp lệ" >&2; exit 2; }
command -v "$EXPRESSVPN_BIN" >/dev/null 2>&1 || { echo "Không tìm thấy $EXPRESSVPN_BIN" >&2; exit 2; }
command -v "$CURL_BIN" >/dev/null 2>&1 || { echo "Không tìm thấy $CURL_BIN" >&2; exit 2; }

valid_ipv4() {
    local ip="$1" part
    [[ "$ip" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || return 1
    local IFS=.
    read -r -a parts <<< "$ip"
    for part in "${parts[@]}"; do
        ((10#$part <= 255)) || return 1
    done
}

public_ip() {
    local url raw
    for url in $IP_CHECK_URLS; do
        raw="$("$CURL_BIN" -4 -fsS --connect-timeout 10 --max-time 20 "$url" 2>/dev/null || true)"
        raw="${raw//$'\r'/}"
        raw="${raw//$'\n'/}"
        if valid_ipv4 "$raw"; then
            printf '%s\n' "$raw"
            return 0
        fi
    done
    return 1
}

telegram_reachable() {
    # Không dùng URL chứa token: chỉ kiểm tra đường mạng tới Telegram.
    "$CURL_BIN" -4 -sS -o /dev/null --connect-timeout 10 --max-time 20 https://api.telegram.org
}

OLD_IP="${PREVIOUS_PUBLIC_IP:-}"
if [ -z "$OLD_IP" ]; then
    OLD_IP="$(public_ip)" || { echo "Không đọc được public IPv4 trước khi đổi VPN." >&2; exit 1; }
fi

echo "[vpn] Đổi ExpressVPN sang: $LOCATION (IP cũ: $OLD_IP)" >&2
"$EXPRESSVPN_BIN" background enable >/dev/null
"$EXPRESSVPN_BIN" disconnect >/dev/null 2>&1 || true

if command -v timeout >/dev/null 2>&1; then
    timeout --foreground "$VPN_CONNECT_TIMEOUT_SECS" "$EXPRESSVPN_BIN" connect "$LOCATION" >&2
else
    "$EXPRESSVPN_BIN" connect "$LOCATION" >&2
fi

sleep "$VPN_CONNECT_SETTLE_SECS"

NEW_IP=""
for _ in 1 2 3; do
    NEW_IP="$(public_ip || true)"
    [ -n "$NEW_IP" ] && break
    sleep 2
done
[ -n "$NEW_IP" ] || { echo "VPN kết nối nhưng không đọc được public IPv4 mới." >&2; exit 1; }
[ "$NEW_IP" != "$OLD_IP" ] || { echo "VPN location $LOCATION không đổi IP ($NEW_IP)." >&2; exit 1; }
telegram_reachable || { echo "IP mới không truy cập được Telegram API." >&2; exit 1; }

echo "[vpn] Thành công: $LOCATION | $OLD_IP -> $NEW_IP" >&2
printf '%s\n' "$NEW_IP"
