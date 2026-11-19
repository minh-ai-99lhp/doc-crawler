#!/usr/bin/env bash
#
# Lớp bọc cho cron: chạy crawler ở chế độ tự động (--ci), ghi log riêng cho mỗi
# lần chạy, phát hiện thành/bại qua EXIT CODE, và khi LỖI thì tự gọi AI
# vào kiểm tra/sửa. (Chạy tay bình thường không tốn token AI; lỗi mới gọi AI.)
#
# Dùng:
#   ./run.sh                              # cào hôm nay
#   ./run.sh --days-back 15               # cào cửa sổ trượt 15 ngày gần nhất (như cron)
#   ./run.sh --from 22/07/2026 --to 22/07/2026   # cào 1 ngày
# Cron (23:59 mỗi ngày) — quét 15 ngày gần nhất để bắt cả văn bản đăng trễ:
#   59 23 * * * /duong/dan/run.sh --days-back 15 >> /duong/dan/logs/cron.log 2>&1

set -uo pipefail

# Thư mục dự án = nơi đặt script này (chạy được dù ở /mnt/d hay /var/www/html).
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR" || exit 3

# Cron có PATH tối giản -> bổ sung để tìm thấy python/xvfb-run/claude.
export PATH="$HOME/.local/bin:$HOME/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

# Nạp .env làm MẶC ĐỊNH: biến đã set sẵn (vd `AI_ENABLED=0 ./run.sh`) được GIỮ, không bị đè.
load_env() {
    [ -f "$1" ] || return 0
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in ''|\#*) continue ;; esac      # bỏ dòng trống / comment
        [ "${line#*=}" = "$line" ] && continue          # bỏ dòng không có '='
        key="${line%%=*}"; val="${line#*=}"
        val="${val%\"}"; val="${val#\"}"; val="${val%\'}"; val="${val#\'}"
        [ -z "${!key:-}" ] && export "$key=$val"         # chỉ set nếu chưa có
    done < "$1"
}
load_env "$DIR/.env"

# Backfill là chế độ vận hành cố định: không cho phép AI tự sửa mã nguồn và
# không tự rơi sang một Xvfb khác khi display dùng chung bị mất. Chỉ các biến
# an toàn này được override bằng tham số môi trường; credential vẫn chỉ đọc từ
# file .env đã mount riêng của job.
if [ "${BACKFILL_MODE:-0}" = "1" ]; then
    export AI_ENABLED=0 AI_ON_FAIL=0 AI_ON_SUCCESS=0
    export TELEGRAM_ONLY_ON_FAIL=0
fi

LOG_DIR="$DIR/logs"
mkdir -p "$LOG_DIR"
TS="$(date +%Y%m%d-%H%M%S)"
LOG="$LOG_DIR/run-$TS.log"

# Báo lỗi rõ ràng trên máy chạy trực tiếp nếu thiếu công cụ bảo vệ, thay vì mở
# browser mà không có khóa/timeout.
for command in flock timeout setsid stat; do
    if ! command -v "$command" >/dev/null 2>&1; then
        echo "Thiếu lệnh bắt buộc: $command" >&2
        exit 2
    fi
done

# Mọi kiểu chạy (cron, backfill, chạy tay) đều dùng chung persistent profile.
# Hai Camoufox mở đồng thời sẽ tranh khóa profile và làm browser thoát/treo, nên
# khóa ở volume logs (được dùng chung giữa host và mọi container) trước khi mở browser.
CRAWLER_LOCK="$LOG_DIR/crawler.lock"
exec 9>"$CRAWLER_LOCK"
if ! flock -n 9; then
    echo "===== RUN $TS | args: $* =====" | tee -a "$LOG"
    echo "[run.sh] BỎ QUA: một crawler khác đang chạy và giữ profile Camoufox." | tee -a "$LOG"
    echo "[run.sh] Nếu không còn job hợp lệ, kiểm tra: docker ps --filter label=com.docker.compose.service=crawler" | tee -a "$LOG"
    exit 4
fi

# Chốt chặn cuối cùng nếu Playwright/Camoufox mất kết nối và timeout nội bộ không
# còn tác dụng. Có thể tăng qua .env cho ngày đặc biệt nhiều văn bản.
CRAWLER_MAX_RUNTIME_SECS="${CRAWLER_MAX_RUNTIME_SECS:-7200}"
if ! [[ "$CRAWLER_MAX_RUNTIME_SECS" =~ ^[1-9][0-9]*$ ]]; then
    echo "CRAWLER_MAX_RUNTIME_SECS phải là số giây nguyên dương: $CRAWLER_MAX_RUNTIME_SECS" >&2
    exit 2
fi

# Một vài phiên Camoufox/Playwright có thể treo trong page.goto() dù timeout nội bộ
# đã hết. Watchdog dựa trên log là chốt chặn độc lập: 5 phút không có dòng mới thì
# dừng cả process group, giữ nguyên URL đang xử lý ở queue để lần chạy sau resume.
CRAWLER_IDLE_TIMEOUT_SECS="${CRAWLER_IDLE_TIMEOUT_SECS:-300}"
if ! [[ "$CRAWLER_IDLE_TIMEOUT_SECS" =~ ^[1-9][0-9]*$ ]]; then
    echo "CRAWLER_IDLE_TIMEOUT_SECS phải là số giây nguyên dương: $CRAWLER_IDLE_TIMEOUT_SECS" >&2
    exit 2
fi

ACTIVE_CRAWLER_PID=""
WATCHDOG_PID=""
stop_active_crawler() {
    trap - INT TERM HUP
    if [ -n "$ACTIVE_CRAWLER_PID" ] && kill -0 "$ACTIVE_CRAWLER_PID" 2>/dev/null; then
        echo "[run.sh] Nhận tín hiệu dừng; đang đóng crawler và Camoufox…" | tee -a "$LOG"
        # `setsid` ở run_crawler tạo PGID riêng, nên một tín hiệu dừng được gửi tới
        # timeout, Python, Playwright và toàn bộ process Camoufox của đúng lần chạy.
        kill -TERM -- "-$ACTIVE_CRAWLER_PID" 2>/dev/null || true
        wait "$ACTIVE_CRAWLER_PID" 2>/dev/null || true
    fi
    [ -n "$WATCHDOG_PID" ] && kill "$WATCHDOG_PID" 2>/dev/null || true
    exit 130
}
trap stop_active_crawler INT TERM HUP

run_crawler() {
    # Chạy trong session/process group riêng để Ctrl+C và watchdog không để lại
    # Python/Camoufox mồ côi. --foreground giúp timeout không tạo thêm process group.
    setsid timeout --foreground --signal=TERM --kill-after=30s "$CRAWLER_MAX_RUNTIME_SECS" \
        "$@" >>"$LOG" 2>&1 &
    ACTIVE_CRAWLER_PID=$!

    (
        while kill -0 "$ACTIVE_CRAWLER_PID" 2>/dev/null; do
            sleep 15
            last_log_at="$(stat -c %Y "$LOG" 2>/dev/null || echo 0)"
            now="$(date +%s)"
            if [ "$last_log_at" -gt 0 ] && [ $((now - last_log_at)) -ge "$CRAWLER_IDLE_TIMEOUT_SECS" ]; then
                echo "[run.sh] WATCHDOG: không có log mới trong ${CRAWLER_IDLE_TIMEOUT_SECS}s; dừng process treo để lần sau resume queue." >>"$LOG"
                kill -TERM -- "-$ACTIVE_CRAWLER_PID" 2>/dev/null || true
                exit 0
            fi
        done
    ) &
    WATCHDOG_PID=$!

    wait "$ACTIVE_CRAWLER_PID"
    local code=$?
    kill "$WATCHDOG_PID" 2>/dev/null || true
    wait "$WATCHDOG_PID" 2>/dev/null || true
    ACTIVE_CRAWLER_PID=""
    WATCHDOG_PID=""
    return "$code"
}

# Cờ điều khiển (có thể override qua env hoặc .env):
AI_ENABLED="${AI_ENABLED:-1}"        # 0 = KHÔNG dùng AI; báo cáo fix cứng từ last-summary.json
AI_ON_FAIL="${AI_ON_FAIL:-1}"        # (khi AI bật) lỗi -> gọi AI kiểm tra/sửa + báo cáo
AI_ON_SUCCESS="${AI_ON_SUCCESS:-1}"  # (khi AI bật) thành công -> gọi AI review + báo cáo
ONLY_ON_FAIL="${TELEGRAM_ONLY_ON_FAIL:-1}"   # 1 = chỉ báo Telegram khi lỗi; 0 = báo cả 2
CLAUDE_BIN="$(command -v claude || echo claude)"

notify()     { "$DIR/.venv/bin/python" "$DIR/notify.py" "$1" >>"$LOG" 2>&1; }
report_fixed() { "$DIR/.venv/bin/python" "$DIR/notify.py" --summary "$1" >>"$LOG" 2>&1; }  # báo cáo fix cứng

ai_active() {
    [ "$AI_ENABLED" = "1" ] || return 1
    if command -v "$CLAUDE_BIN" >/dev/null 2>&1 || [ -x "$CLAUDE_BIN" ]; then
        return 0
    fi
    echo "[run.sh] Không tìm thấy CLI 'claude' -> dùng báo cáo fix cứng." | tee -a "$LOG"
    return 1
}

ask_ai() {
    "$CLAUDE_BIN" -p --permission-mode bypassPermissions "$1" >>"$LOG" 2>&1
}

# Vai trò cố định cho AI mỗi lần được gọi:
PERSONA="Bạn là TRỢ LÝ theo dõi việc crawl trang nguồn (project tại $DIR), báo cáo cho nhóm vận hành qua Telegram.
NGUỒN SỐ LIỆU DUY NHẤT: $DIR/logs/last-summary.json (số liệu THẬT của lần chạy: collect.found/new/duplicate, download.ok/failed, queue, downloads_by_date, problems, result). Log chi tiết: $LOG.
QUY TẮC BÁO CÁO: chỉ thuật lại con số từ last-summary.json — TUYỆT ĐỐI không bịa/không suy diễn con số không có trong đó. Nếu 'collect.found=0' thì nói thẳng 'ngày này không có văn bản', đừng chế ra văn bản. Nếu số liệu bất thường (vd văn bản năm cũ ở tìm kiếm ngày mới, new=0 mà vẫn có gì đó lạ) thì nêu nghi vấn, đừng tô hồng.
QUY TẮC AN TOÀN: nếu problems/log cho thấy site blocked, Cloudflare chặn, tài khoản bị khoá hoặc login thành công nhưng tab tải báo chưa đăng nhập thì KHÔNG retry tải, KHÔNG tìm cách bypass anti-bot và KHÔNG sửa queue.json, downloads/ hay .env; chỉ báo cần cooldown/manual calibration. Chỉ sửa code khi nguyên nhân thật sự nằm ở logic/selector/parser/session handling; nếu test chỉ --collect-only hoặc tối đa 1 link.
Gửi báo cáo bằng lệnh:  $DIR/.venv/bin/python $DIR/notify.py '<nội dung tiếng Việt, ngắn gọn, có số liệu>'"

echo "===== RUN $TS | ai=claude | args: $* =====" | tee -a "$LOG"

# Chạy crawler (chế độ --ci: không chờ người, thoát != 0 nếu có vấn đề).
# WSLg/desktop đã có DISPLAY thật thì dùng luôn; gọi xvfb-run chồng lên WSLg vừa thừa,
# vừa có thể lỗi quyền /tmp/.X11-unix. Cron/server không có display vẫn dùng Xvfb.
# X server/X11 socket đôi khi còn tồn tại nhưng không trả lời. Nếu gọi `xdpyinfo`
# trần ở đây, run sẽ đứng trước cả crawler và chỉ còn đúng dòng tiêu đề trong log
# cho tới khi timeout ngoài (backfill hiện là 7.500 giây) buộc dừng. Giới hạn riêng
# probe này để có thể chuyển sang Xvfb nội bộ ngay khi display dùng chung bị kẹt.
if [ -n "${DISPLAY:-}" ] && command -v xdpyinfo >/dev/null 2>&1 \
        && timeout --signal=TERM --kill-after=2s 10s \
            xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then
    echo "[run.sh] Dùng display hiện có: $DISPLAY" | tee -a "$LOG"
    run_crawler "$DIR/.venv/bin/python" -u "$DIR/crawler.py" --ci "$@"
else
    if [ "${BACKFILL_REQUIRE_DISPLAY:-0}" = "1" ]; then
        echo "[run.sh] BACKFILL_REQUIRE_DISPLAY=1 nhưng display ${DISPLAY:-<trống>} không phản hồi." | tee -a "$LOG"
        CODE=2
        echo "exit_code=$CODE" | tee -a "$LOG"
        ln -sf "$LOG" "$LOG_DIR/latest.log"
        echo "FAIL $TS (exit=$CODE) log=$LOG" >> "$LOG_DIR/last-status.txt"
        report_fixed FAIL
        exit "$CODE"
    fi
    echo "[run.sh] Không có display thật -> dùng Xvfb." | tee -a "$LOG"
    run_crawler xvfb-run -a "$DIR/.venv/bin/python" -u "$DIR/crawler.py" --ci "$@"
fi
CODE=$?

if [ "$CODE" -eq 124 ]; then
    echo "[run.sh] TIMEOUT: crawler vượt quá ${CRAWLER_MAX_RUNTIME_SECS}s; đã dừng cả Camoufox để giải phóng profile." | tee -a "$LOG"
fi

echo "exit_code=$CODE" | tee -a "$LOG"
ln -sf "$LOG" "$LOG_DIR/latest.log"           # con trỏ tới log mới nhất

# ===== THÀNH CÔNG =====
if [ "$CODE" -eq 0 ]; then
    echo "OK   $TS (exit=0)" >> "$LOG_DIR/last-status.txt"
    echo "[run.sh] Thành công." | tee -a "$LOG"
    if [ "$ONLY_ON_FAIL" = "1" ]; then
        exit 0                                # cấu hình: không báo khi thành công
    fi
    if ai_active && [ "$AI_ON_SUCCESS" = "1" ]; then
        echo "[run.sh] Gọi Claude tổng hợp & báo cáo…" | tee -a "$LOG"
        ask_ai "$PERSONA
Lần chạy TỰ ĐỘNG vừa rồi THÀNH CÔNG (exit=0). Đọc logs/last-summary.json rồi GỬI BÁO CÁO cho nhóm vận hành qua notify.py: khoảng ngày, thu thập bao nhiêu (found/new/duplicate), tải ok/failed bao nhiêu, gom vào ngày nào (downloads_by_date), tổng kho (queue). Nếu collect.found=0 thì báo 'ngày này không có văn bản mới'. Đừng chạy tải thêm, đừng bịa số." \
            || { echo "[run.sh] Claude lỗi (hết quota/chưa đăng nhập/mạng?) -> lùi về báo cáo fix cứng." | tee -a "$LOG"; report_fixed OK; }
    else
        echo "[run.sh] Báo cáo FIX CỨNG (không AI)…" | tee -a "$LOG"
        report_fixed OK
    fi
    exit 0
fi

# ===== BỊ CHẶN (exit=3) =====
# Cloudflare chặn cứng KHÔNG phải lỗi code: gọi AI đi "sửa" chỉ tốn token và có nguy cơ
# nó tự retry làm bị chặn nặng thêm. Chỉ báo cáo fix cứng rồi dừng, để người vận hành
# cho nghỉ 30-60 phút (lần cron sau tự chạy lại; queue.json giữ nguyên nên không mất gì).
if [ "$CODE" -eq 3 ]; then
    echo "BLOCKED $TS (exit=3) log=$LOG" >> "$LOG_DIR/last-status.txt"
    echo "[run.sh] BỊ CHẶN (Cloudflare) -> báo cáo, KHÔNG gọi AI, KHÔNG chạy lại." | tee -a "$LOG"
    report_fixed FAIL
    exit 3
fi

# ===== THẤT BẠI =====
echo "FAIL $TS (exit=$CODE) log=$LOG" >> "$LOG_DIR/last-status.txt"
echo "[run.sh] LỖI (exit=$CODE)." | tee -a "$LOG"

if ai_active && [ "$AI_ON_FAIL" = "1" ]; then
    echo "[run.sh] Gọi Claude kiểm tra/sửa & báo cáo…" | tee -a "$LOG"
    ask_ai "$PERSONA
Lần chạy TỰ ĐỘNG vừa rồi LỖI (exit=$CODE). Hãy: (1) đọc logs/last-summary.json (mục problems) + log để xác định nguyên nhân; (2) kiểm tra crawler.py và SỬA nếu khắc phục được trong code (ĐỪNG tải hàng loạt để test — chỉ --collect-only hoặc 1 link); (3) GỬI BÁO CÁO cho nhóm vận hành qua notify.py: đã xảy ra gì (theo problems), đã sửa được chưa, người vận hành cần làm gì (nếu là khoá tài khoản/đổi mật khẩu/site đổi giao diện/Cloudflare siết thì nói rõ). Chỉ dùng số liệu thật, không suy diễn." \
        || { echo "[run.sh] Claude lỗi (hết quota/chưa đăng nhập/mạng?) -> lùi về báo cáo fix cứng." | tee -a "$LOG"; report_fixed FAIL; }
else
    echo "[run.sh] Báo cáo LỖI FIX CỨNG (không AI)…" | tee -a "$LOG"
    report_fixed FAIL
fi

exit "$CODE"
