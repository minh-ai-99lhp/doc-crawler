#!/usr/bin/env bash
# ONE-TIME daily backfill: chạy đầy đủ từng ngày từ START_DATE đến ngày hiện tại.
# Nếu pha collect đã xong thì chuyển sang ngày kế tiếp dù pha tải còn lỗi; các URL
# pending/failed vẫn nằm trong queue.json và được lần chạy sau thử lại. Nếu collect
# chưa xong thì giữ nguyên ngày để retry. Giữa hai lượt nghỉ ngẫu nhiên quanh 50 phút.
#
# Có thể đổi cấu hình lúc chạy, ví dụ:
#   END_DATE=31/07/2026 MIN_GAP_SECS=7200 MAX_GAP_SECS=10800 ./run-backfill.sh

set -uo pipefail

START_DATE="${START_DATE:-29/04/2026}"
END_DATE="${END_DATE:-$(TZ=Asia/Ho_Chi_Minh date +%d/%m/%Y)}"
MIN_GAP_SECS="${MIN_GAP_SECS:-600}"    # 41 phút 40 giây
MAX_GAP_SECS="${MAX_GAP_SECS:-3000}"    # 58 phút 20 giây
DISPLAY_ID="${DISPLAY_ID:-:99}"
BACKFILL_JOB_TIMEOUT_SECS="${BACKFILL_JOB_TIMEOUT_SECS:-7500}"

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="$PROJECT_DIR/logs/backfill-daily.log"
SUMMARY="$PROJECT_DIR/logs/last-summary.json"
BACKFILL_LOCK="$PROJECT_DIR/logs/backfill.lock"
RUN_CONTAINER="doc-crawler-backfill"
COMPOSE=(docker compose -f "$PROJECT_DIR/.docker/docker-compose.yml")
X11_SOCKET="/tmp/.X11-unix/X${DISPLAY_ID#:}"

mkdir -p "$PROJECT_DIR/logs"

log() {
    echo "[$(TZ=Asia/Ho_Chi_Minh date)] $*" | tee -a "$LOG"
}

container_active=0

cleanup_run_container() {
    [ "$container_active" -eq 1 ] || return 0
    if docker container inspect "$RUN_CONTAINER" >/dev/null 2>&1; then
        log "Dọn container backfill còn chạy: $RUN_CONTAINER"
        docker container rm --force "$RUN_CONTAINER" >/dev/null 2>&1 || true
        # Chờ Docker thực sự nhả process/profile; không bắt đầu lượt mới khi khóa
        # browser của container cũ có thể vẫn còn hiệu lực.
        for _ in $(seq 1 20); do
            docker container inspect "$RUN_CONTAINER" >/dev/null 2>&1 || break
            sleep 1
        done
    fi
    container_active=0
}

handle_signal() {
    local exit_code="$1"
    local signal_name="$2"
    trap - INT TERM HUP EXIT
    log "Nhận $signal_name; đang dừng container hiện tại và giải phóng profile Camoufox."
    cleanup_run_container
    exit "$exit_code"
}

trap 'handle_signal 130 SIGINT' INT
trap 'handle_signal 143 SIGTERM' TERM
trap 'handle_signal 129 SIGHUP' HUP
trap cleanup_run_container EXIT

to_epoch() {
    date -d "${1:6:4}-${1:3:2}-${1:0:2}" +%s
}

add_days() {
    date -d "@$(( $(to_epoch "$1") + $2 * 86400 ))" +"%d/%m/%Y"
}

date_le() {
    [ "$(to_epoch "$1")" -le "$(to_epoch "$2")" ]
}

summary_stamp() {
    stat -c '%y:%s' "$SUMMARY" 2>/dev/null || echo "missing"
}

collect_completed_for() {
    local run_date="$1"
    jq -e --arg expected_range "$run_date..$run_date" \
        '.range == $expected_range and (.collect | type == "object")' \
        "$SUMMARY" >/dev/null 2>&1
}

random_gap() {
    echo $(( MIN_GAP_SECS + RANDOM % (MAX_GAP_SECS - MIN_GAP_SECS + 1) ))
}

format_duration() {
    local total="$1"
    printf '%02d:%02d:%02d' \
        $(( total / 3600 )) \
        $(( total % 3600 / 60 )) \
        $(( total % 60 ))
}

for command in docker date jq flock timeout; do
    if ! command -v "$command" >/dev/null 2>&1; then
        echo "Thiếu lệnh bắt buộc: $command" >&2
        exit 2
    fi
done

if ! docker compose version >/dev/null 2>&1; then
    echo "Docker Compose plugin chưa sẵn sàng." >&2
    exit 2
fi

if ! to_epoch "$START_DATE" >/dev/null 2>&1; then
    echo "START_DATE không hợp lệ: $START_DATE (cần dạng dd/mm/yyyy)." >&2
    exit 2
fi

if ! to_epoch "$END_DATE" >/dev/null 2>&1; then
    echo "END_DATE không hợp lệ: $END_DATE (cần dạng dd/mm/yyyy)." >&2
    exit 2
fi

if ! [[ "$MIN_GAP_SECS" =~ ^[0-9]+$ ]]; then
    echo "MIN_GAP_SECS phải là số giây nguyên không âm: $MIN_GAP_SECS" >&2
    exit 2
fi

if ! [[ "$MAX_GAP_SECS" =~ ^[0-9]+$ ]]; then
    echo "MAX_GAP_SECS phải là số giây nguyên không âm: $MAX_GAP_SECS" >&2
    exit 2
fi

if ! [[ "$BACKFILL_JOB_TIMEOUT_SECS" =~ ^[1-9][0-9]*$ ]]; then
    echo "BACKFILL_JOB_TIMEOUT_SECS phải là số giây nguyên dương: $BACKFILL_JOB_TIMEOUT_SECS" >&2
    exit 2
fi

if [ "$MIN_GAP_SECS" -gt "$MAX_GAP_SECS" ]; then
    echo "MIN_GAP_SECS ($MIN_GAP_SECS) không được lớn hơn MAX_GAP_SECS ($MAX_GAP_SECS)." >&2
    exit 2
fi

if ! date_le "$START_DATE" "$END_DATE"; then
    echo "START_DATE ($START_DATE) phải không muộn hơn END_DATE ($END_DATE)." >&2
    exit 2
fi

if [ ! -S "$X11_SOCKET" ]; then
    echo "Không thấy X11 display $DISPLAY_ID tại $X11_SOCKET." >&2
    echo "Hãy chạy Xvfb $DISPLAY_ID và x11vnc trước khi chạy backfill." >&2
    exit 2
fi

# Chỉ cho phép một vòng backfill điều phối tại một thời điểm. Khóa này độc lập
# với crawler.lock trong run.sh: một khóa bảo vệ vòng lặp ngày, một khóa bảo vệ browser.
exec 8>"$BACKFILL_LOCK"
if ! flock -n 8; then
    echo "Đã có một run-backfill.sh khác đang chạy (lock: $BACKFILL_LOCK)." >&2
    exit 4
fi

# Từ phiên bản này container có tên cố định. Nếu máy/script từng bị SIGKILL hoặc
# mất điện khiến trap không chạy, lượt kế tiếp có thể nhận diện và dọn đúng container
# do backfill quản lý; không đụng tới container crawler chạy tay hay service khác.
if docker container inspect "$RUN_CONTAINER" >/dev/null 2>&1; then
    container_active=1
    log "Phát hiện container backfill mồ côi từ lần trước; dọn trước khi tiếp tục."
    cleanup_run_container
fi

current="$START_DATE"
job=0

log "===== DAILY BACKFILL $START_DATE → $END_DATE | display=$DISPLAY_ID | gap ngẫu nhiên $(format_duration "$MIN_GAP_SECS")-$(format_duration "$MAX_GAP_SECS") ====="

while date_le "$current" "$END_DATE"; do
    job=$(( job + 1 ))
    log "RUN #$job: --from $current --to $current"

    cd "$PROJECT_DIR" || exit 1
    summary_before="$(summary_stamp)"
    container_active=1
    # --foreground giữ docker compose trong process group của terminal để Ctrl+C
    # tới cả script lẫn Compose; nếu không Compose có thể bị SIGTTOU/SIGTTIN và
    # chuyển sang trạng thái T (suspend), khiến Bash chờ mãi mới chạy trap dọn dẹp.
    timeout --foreground --signal=TERM --kill-after=60s "$BACKFILL_JOB_TIMEOUT_SECS" \
        "${COMPOSE[@]}" run --rm --name "$RUN_CONTAINER" \
            -e DISPLAY="$DISPLAY_ID" \
            -v /tmp/.X11-unix:/tmp/.X11-unix \
            crawler \
            ./run.sh \
            --from "$current" \
            --to "$current" >> "$LOG" 2>&1
    run_exit=$?
    cleanup_run_container
    summary_after="$(summary_stamp)"

    if [ "$run_exit" -eq 124 ]; then
        log "RUN #$job vượt quá ${BACKFILL_JOB_TIMEOUT_SECS}s; đã buộc dừng container để tránh treo và khóa profile."
    fi

    # Ctrl+C/SIGTERM là yêu cầu dừng thật sự của người vận hành, không tự sleep/retry.
    if [ "$run_exit" -eq 130 ] || [ "$run_exit" -eq 143 ]; then
        log "RUN #$job bị người vận hành dừng tại ngày $current (exit=$run_exit)."
        exit "$run_exit"
    fi

    collect_finished=0
    if [ "$summary_after" != "$summary_before" ] \
            && [ -s "$SUMMARY" ] \
            && collect_completed_for "$current"; then
        collect_finished=1
    fi

    if [ "$run_exit" -eq 0 ]; then
        log "RUN #$job hoàn tất ngày $current."
        next="$(add_days "$current" 1)"
    elif [ "$collect_finished" -eq 1 ]; then
        log "RUN #$job có lỗi pha tải (exit=$run_exit), nhưng collect ngày $current đã hoàn tất."
        log "Các URL pending/failed được giữ trong queue.json; chuyển sang ngày kế tiếp để lần sau retry."
        next="$(add_days "$current" 1)"
    else
        log "RUN #$job lỗi trước khi collect ngày $current hoàn tất (exit=$run_exit)."
        log "Giữ nguyên ngày $current để retry, không bỏ sót dữ liệu tìm kiếm."
        next="$current"
    fi

    if [ "$next" != "$current" ] && ! date_le "$next" "$END_DATE"; then
        break
    fi

    gap_secs="$(random_gap)"
    if [ "$next" = "$current" ]; then
        log "Nghỉ ngẫu nhiên $(format_duration "$gap_secs") (${gap_secs}s) rồi retry ngày $current."
    else
        log "Nghỉ ngẫu nhiên $(format_duration "$gap_secs") (${gap_secs}s) trước ngày $next."
    fi
    sleep "$gap_secs"
    current="$next"
done

log "BACKFILL HOÀN TẤT: $START_DATE → $END_DATE."
