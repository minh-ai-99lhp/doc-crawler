#!/usr/bin/env bash
# Entrypoint chạy dưới ROOT (bắt buộc để khởi động cron daemon), nhưng công việc
# thật sự (run.sh / crawler.py / Camoufox) luôn chạy dưới user thường "crawler" —
# browser sandbox không nên (và nhiều bản không cho) chạy dưới root.
set -euo pipefail

# Volume mount lần đầu có thể do Docker tạo dưới quyền root -> luôn đưa về đúng
# chủ sở hữu trước khi user "crawler" đọc/ghi.
mkdir -p /app/.camoufox_profile /app/downloads /app/logs \
    /home/crawler/.claude

# An toàn khi lỡ quên `touch queue.json` trên host trước lần `docker compose up` đầu tiên:
# khi đó Docker daemon đã tự tạo NHẦM ./queue.json thành thư mục lúc TẠO container (trước
# khi entrypoint này chạy) -> không sửa được từ đây, chỉ báo lỗi rõ ràng rồi dừng, thay vì
# fail mù mờ (': /app/queue.json: Is a directory') hoặc âm thầm chạy sai.
if [ -e /app/queue.json ] && [ ! -f /app/queue.json ]; then
    echo "[entrypoint] LỖI: /app/queue.json là THƯ MỤC, không phải file — Docker tự tạo" >&2
    echo "  nhầm vì ./queue.json chưa tồn tại trên host lúc 'docker compose up'." >&2
    echo "  Sửa: dừng container rồi trên HOST chạy: rmdir queue.json && touch queue.json" >&2
    exit 1
fi
[ -f /app/queue.json ] || : > /app/queue.json
chown -R crawler:crawler /app/.camoufox_profile /app/downloads /app/logs \
    /home/crawler/.claude
chown crawler:crawler /app/queue.json
# /app/.env được mount :ro (chỉ đọc) -> không chown, chỉ cần user "crawler" đọc
# được là đủ (mặc định file mode đã cho phép "other" đọc).

if [ "${1:-cron}" = "cron" ]; then
    echo "[entrypoint] Container chạy nền, lịch cron (Asia/Ho_Chi_Minh):"
    grep -Ev '^\s*(#|$)' /etc/cron.d/crawler || true
    # cron daemon phải chạy dưới root -> tự chuyển sang user "crawler" khi tới giờ
    # (theo cột user khai trong /etc/cron.d/crawler), không setpriv ở đây.
    exec cron -f
else
    echo "[entrypoint] Chạy lệnh thủ công dưới user 'crawler': $*"
    export HOME=/home/crawler
    exec setpriv --reuid=crawler --regid=crawler --init-groups "$@"
fi
