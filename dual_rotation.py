#!/usr/bin/env python3
"""Supervisor hai downloader dùng chung một kết nối ExpressVPN."""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
STOP_REQUESTED = False


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


load_env(ROOT / ".env")


def setting(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def integer(name: str, default: int, minimum: int = 0) -> int:
    try:
        value = int(setting(name, str(default)))
    except ValueError as exc:
        raise SystemExit(f"[dual-rotate] {name} phải là số nguyên") from exc
    if value < minimum:
        raise SystemExit(f"[dual-rotate] {name} phải >= {minimum}")
    return value


def number(name: str, default: float, minimum: float = 0) -> float:
    try:
        value = float(setting(name, str(default)))
    except ValueError as exc:
        raise SystemExit(f"[dual-rotate] {name} phải là số") from exc
    if value < minimum:
        raise SystemExit(f"[dual-rotate] {name} phải >= {minimum}")
    return value


def csv(name: str, default: str = "") -> list[str]:
    return [part.strip() for part in setting(name, default).split(",") if part.strip()]


def resolve_path(value: str) -> Path:
    path = Path(value).expanduser()
    # Không dùng Path.resolve(): python trong .venv là symlink; resolve symlink đó
    # thành /usr/bin/python sẽ làm mất virtualenv và thiếu Playwright/Camoufox.
    return Path(os.path.abspath(path if path.is_absolute() else ROOT / path))


STATE_DIR = resolve_path(setting("SITE_ROTATION_STATE_DIR", ".runtime/download-dual-rotation"))
OUT_DIR = resolve_path(setting("SITE_ROTATION_OUT_DIR", "downloads"))
QUEUES = [resolve_path(value) for value in csv(
    "SITE_ROTATION_QUEUE_ORDER",
    "queue/queue_1.json,queue/queue_2.json,queue/queue_3.json",
)]
USERS_A = csv("SITE_ROTATION_USERS_A")
USERS_B = csv("SITE_ROTATION_USERS_B")
LOCATIONS = csv("SITE_ROTATION_LOCATIONS")
ROTATION_SECONDS = integer("SITE_ROTATION_SECONDS", 5400, 1)
SOFT_COOLDOWN = integer("SITE_SOFT_BLOCK_COOLDOWN_SECONDS", 1800, 0)
MIN_VPN_DWELL = integer("SITE_MIN_VPN_DWELL_SECONDS", 900, 0)
BROWSER_RESTART_DELAY = integer("SITE_BROWSER_RESTART_DELAY_SECONDS", 60, 0)
LANE_STALL_TIMEOUT = integer("SITE_LANE_STALL_TIMEOUT_SECONDS", 300, 1)
TERM_GRACE = integer("SITE_ROTATION_TERM_GRACE_SECONDS", 30, 0)
CRAWLER_TIMEOUT_MS = integer("SITE_ROTATION_CRAWLER_TIMEOUT_MS", 50000, 1)
MIN_DELAY = number("SITE_ROTATION_MIN_DELAY", 10)
MAX_DELAY = number("SITE_ROTATION_MAX_DELAY", 18)
POLL_SECONDS = number("SITE_ROTATION_POLL_SECONDS", 1, 0.02)
PYTHON_BIN = resolve_path(setting("PYTHON_BIN", str(ROOT / ".venv/bin/python")))
CRAWLER_BIN = resolve_path(setting("CRAWLER_BIN", str(ROOT / "crawler.py")))
SWITCH_VPN_BIN = resolve_path(setting("SWITCH_VPN_BIN", str(ROOT / "switch_vpn.sh")))
EXPRESSVPN_BIN = resolve_path(setting("EXPRESSVPN_BIN", "expressvpnctl"))
SITE_PASS = setting("SITE_PASS")


def validate() -> None:
    if not USERS_A or len(USERS_A) != len(USERS_B):
        raise SystemExit("[dual-rotate] Hai danh sách user phải bằng nhau và không rỗng")
    if len(USERS_A) != len(LOCATIONS):
        raise SystemExit("[dual-rotate] Số cặp user và location phải bằng nhau")
    if len(set(QUEUES)) != len(QUEUES):
        raise SystemExit("[dual-rotate] Queue bị trùng")
    if any(not re.fullmatch(r"[A-Za-z0-9_.@-]+", user) for user in USERS_A + USERS_B):
        raise SystemExit("[dual-rotate] Username không hợp lệ")
    if any(a == b for a, b in zip(USERS_A, USERS_B)):
        raise SystemExit("[dual-rotate] Hai lane không được dùng cùng user")
    if not SITE_PASS:
        raise SystemExit("[dual-rotate] Thiếu SITE_PASS")
    if MIN_DELAY > MAX_DELAY:
        raise SystemExit("[dual-rotate] Delay phải thỏa 0 <= min <= max")
    for path in [*QUEUES, PYTHON_BIN, CRAWLER_BIN, SWITCH_VPN_BIN, EXPRESSVPN_BIN]:
        if not path.exists():
            raise SystemExit(f"[dual-rotate] Không tìm thấy: {path}")


def prepare_wsl_interop() -> None:
    if EXPRESSVPN_BIN.suffix.lower() != ".exe":
        return
    current = os.environ.get("WSL_INTEROP")
    if current and Path(current).exists():
        return
    candidates = []
    for path in Path("/run/WSL").glob("*_interop"):
        try:
            if stat_is_socket(path):
                candidates.append(path)
        except OSError:
            pass
    if not candidates:
        raise SystemExit("[dual-rotate] Không tìm thấy WSL interop socket")
    newest = max(candidates, key=lambda path: path.stat().st_mtime)
    os.environ["WSL_INTEROP"] = str(newest)


# Dùng stat chuẩn để nhận diện WSL interop socket.
import stat  # noqa: E402


def stat_is_socket(path: Path) -> bool:
    return stat.S_ISSOCK(path.stat().st_mode)


STATE_DIR.mkdir(parents=True, exist_ok=True)
OUT_DIR.mkdir(parents=True, exist_ok=True)
SUPERVISOR_LOG = STATE_DIR / "supervisor.log"
STATE_FILE = STATE_DIR / "state.json"


def log(message: str) -> None:
    line = f"[{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S%z')}] {message}"
    print(line, flush=True)
    with SUPERVISOR_LOG.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def read_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"[dual-rotate] State hỏng: {exc}") from exc


def queue_remaining(path: Path) -> int:
    # Queue được crawler checkpoint trong lúc supervisor poll. Ghi nguyên tử ở
    # crawler ngăn file rỗng mới, còn retry này giúp tự phục hồi nếu đang đọc một
    # checkpoint kiểu cũ hoặc filesystem Windows/WSL trả lỗi thoáng qua.
    last_error: Exception | None = None
    for attempt in range(10):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("root JSON của queue không phải object")
            return sum(
                isinstance(value, dict)
                and value.get("status") in {"pending", "failed"}
                for value in data.values()
            )
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            last_error = exc
            if attempt < 9:
                time.sleep(0.2)
    raise RuntimeError(f"Không đọc được queue {path} sau 10 lần thử: {last_error}")


def total_remaining() -> int:
    return sum(queue_remaining(path) for path in QUEUES)


@dataclass
class Lane:
    name: str
    user: str
    queue: Path | None = None
    process: subprocess.Popen | None = None
    log_handle: object | None = None
    log_path: Path | None = None
    work_dir: Path | None = None
    started_at: float = 0
    last_output_at: float = 0
    retry_at: float = 0
    soft_retries: int = 0
    session_resets: int = 0
    browser_retries: int = 0
    stall_restarts: int = 0
    status: str = "idle"
    last_exit_code: int | None = None
    stop_reason: str | None = None

    def state(self) -> dict:
        return {
            "user": self.user,
            "queue": str(self.queue) if self.queue else None,
            "status": self.status,
            "retry_at": self.retry_at or None,
            "soft_retries": self.soft_retries,
            "session_resets": self.session_resets,
            "browser_retries": self.browser_retries,
            "stall_restarts": self.stall_restarts,
            "last_output_at": self.last_output_at or None,
            "last_exit_code": self.last_exit_code,
            "stop_reason": self.stop_reason,
        }


def write_state(phase: str, index: int, location: str, public_ip: str,
                connected_at: float, deadline: float, lanes: list[Lane], message: str,
                queue_cursor: int) -> None:
    data = {
        "version": 5,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "phase": phase,
        "next_index": index,
        "location": location or None,
        "public_ip": public_ip or None,
        "connected_at": connected_at or None,
        "slice_deadline": deadline or None,
        "queue_cursor": queue_cursor,
        "message": message,
        "lanes": {lane.name: lane.state() for lane in lanes},
    }
    fd, temporary = tempfile.mkstemp(prefix="dual-state.", suffix=".json", dir=STATE_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, STATE_FILE)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def current_public_ip() -> str:
    custom = setting("SITE_PUBLIC_IP_BIN")
    if custom:
        result = subprocess.run([custom], text=True, capture_output=True, timeout=20, check=False)
        candidate = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
        return candidate if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", candidate) else ""
    curl = setting("CURL_BIN", "curl")
    urls = setting("IP_CHECK_URLS", "https://api.ipify.org https://ifconfig.me/ip").split()
    for url in urls:
        result = subprocess.run(
            [curl, "-4", "-fsS", "--connect-timeout", "10", "--max-time", "20", url],
            text=True, capture_output=True, timeout=25, check=False,
        )
        candidate = result.stdout.strip()
        if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", candidate):
            return candidate
    return ""


def current_vpn_location() -> str:
    result = subprocess.run(
        [str(EXPRESSVPN_BIN), "status"], text=True, capture_output=True,
        timeout=20, check=False,
    )
    match = re.search(r"Connected to\s+([^\r\n]+)", result.stdout, re.IGNORECASE)
    return match.group(1).strip() if match else ""


def switch_vpn(location: str, previous_ip: str) -> str:
    env = os.environ.copy()
    env["PREVIOUS_PUBLIC_IP"] = previous_ip
    result = subprocess.run(
        [str(SWITCH_VPN_BIN), location], env=env, text=True,
        capture_output=True, check=False,
    )
    if result.stderr:
        with SUPERVISOR_LOG.open("a", encoding="utf-8") as handle:
            handle.write(result.stderr)
    candidate = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    if result.returncode or not re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", candidate):
        raise RuntimeError(f"Đổi VPN {location} thất bại (exit={result.returncode})")
    return candidate


def next_queue(excluded: Path | None = None) -> Path | None:
    for queue in QUEUES:
        if queue != excluded and queue_remaining(queue) > 0:
            return queue
    return None


def select_slice_queues(cursor: int, limit: int = 2) -> list[Path]:
    """Chọn tối đa hai queue theo vòng tròn và bỏ qua queue đã hết."""
    selected: list[Path] = []
    for offset in range(len(QUEUES)):
        queue = QUEUES[(cursor + offset) % len(QUEUES)]
        if queue_remaining(queue) > 0:
            selected.append(queue)
            if len(selected) >= limit:
                break
    return selected


def start_lane(lane: Lane, deadline: float) -> None:
    if lane.queue is None:
        lane.status = "idle"
        return
    lane.work_dir = STATE_DIR / "lanes" / lane.name / "users" / lane.user
    (lane.work_dir / "logs").mkdir(parents=True, exist_ok=True)
    (lane.work_dir / "staging").mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    log_path = lane.work_dir / "logs" / f"{lane.queue.name}-{stamp}.log"
    lane.log_path = log_path
    lane.log_handle = log_path.open("a", encoding="utf-8")
    env = os.environ.copy()
    env.update({
        "PYTHONUNBUFFERED": "1",
        "SITE_USER": lane.user,
        "SITE_PASS": SITE_PASS,
        "SITE_DOWNLOAD_STAGING_DIR": str(lane.work_dir / "staging"),
    })
    command = [
        str(PYTHON_BIN), str(CRAWLER_BIN), "--download-only",
        "--queue-file", str(lane.queue), "--out-dir", str(OUT_DIR), "--ci",
        "--timeout", str(CRAWLER_TIMEOUT_MS), "--min-delay", str(MIN_DELAY),
        "--max-delay", str(MAX_DELAY),
    ]
    lane.process = subprocess.Popen(
        command, cwd=lane.work_dir, env=env, stdout=lane.log_handle,
        stderr=subprocess.STDOUT, start_new_session=True,
    )
    lane.started_at = time.time()
    lane.last_output_at = lane.started_at
    lane.retry_at = 0
    lane.status = "running"
    lane.last_exit_code = None
    lane.stop_reason = None
    log(f"Lane {lane.name}: user={lane.user}, queue={lane.queue.name}, còn {max(0, int(deadline-time.time()))}s.")


def lane_stalled(lane: Lane, now: float) -> bool:
    """True khi crawler còn sống nhưng không ghi thêm log quá lâu.

    Mọi bước dài hợp lệ hiện đều có ngân sách tối đa khoảng 120 giây. Dùng mtime
    của log làm heartbeat tránh phụ thuộc Playwright: kể cả RPC/browser kẹt mà PID
    vẫn sống, supervisor vẫn tự phục hồi lane đó.
    """
    if lane.process is None or lane.process.poll() is not None:
        return False
    if lane.log_path is not None:
        try:
            lane.last_output_at = max(lane.last_output_at, lane.log_path.stat().st_mtime)
        except OSError:
            pass
    return now - lane.last_output_at >= LANE_STALL_TIMEOUT


def clear_lane_browser_session(lane: Lane) -> None:
    """Xóa cookie/login/browser state nhưng giữ fingerprint identity.json."""
    if lane.process is not None:
        raise RuntimeError(f"Lane {lane.name} vẫn còn process khi clear session")
    if lane.work_dir is None:
        raise RuntimeError(f"Lane {lane.name} chưa có work_dir")
    expected_work_dir = STATE_DIR / "lanes" / lane.name / "users" / lane.user
    if lane.work_dir != expected_work_dir:
        raise RuntimeError(f"Từ chối clear session ngoài lane runtime: {lane.work_dir}")
    session_dir = lane.work_dir / ".camoufox_profile" / "browser"
    if session_dir.is_symlink():
        session_dir.unlink()
    elif session_dir.exists():
        shutil.rmtree(session_dir)
    identity = lane.work_dir / ".camoufox_profile" / "identity.json"
    log(
        f"Lane {lane.name}: đã clear browser session của {lane.user}; "
        f"fingerprint identity.json {'được giữ nguyên' if identity.exists() else 'sẽ được tạo lại'}."
    )


def stop_lane(lane: Lane) -> None:
    process = lane.process
    if process is not None and process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=TERM_GRACE)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
    if lane.log_handle:
        lane.log_handle.close()
    lane.process = None
    lane.log_handle = None
    if lane.status == "running":
        lane.status = "stopped"


def stop_all(lanes: list[Lane]) -> None:
    # Gửi TERM cho cả hai trước, rồi mới chờ, để grace period không cộng dồn.
    for lane in lanes:
        if lane.process is not None and lane.process.poll() is None:
            try:
                os.killpg(lane.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    deadline = time.time() + TERM_GRACE
    for lane in lanes:
        process = lane.process
        if process is not None:
            try:
                process.wait(timeout=max(0, deadline - time.time()))
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
        if lane.log_handle:
            lane.log_handle.close()
        lane.process = None
        lane.log_handle = None
        if lane.status == "running":
            lane.status = "stopped"


def summary_reason(lane: Lane) -> str:
    if lane.work_dir is None:
        return ""
    path = lane.work_dir / "logs" / "last-summary.json"
    try:
        if path.stat().st_mtime + 1 < lane.started_at:
            return ""
        return json.loads(path.read_text(encoding="utf-8")).get("stop_reason") or ""
    except (OSError, json.JSONDecodeError):
        return ""


def reap_lane(lane: Lane) -> tuple[int, str]:
    assert lane.process is not None
    code = lane.process.wait()
    if lane.log_handle:
        lane.log_handle.close()
    lane.process = None
    lane.log_handle = None
    reason = summary_reason(lane)
    if not reason:
        reason = {2: "exception", 3: "hard_block", 4: "soft_cloudflare_streak"}.get(code, "")
    lane.last_exit_code = code
    lane.stop_reason = reason or None
    return code, reason


def signal_handler(_signum, _frame) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True


def wait_until(timestamp: float) -> None:
    while not STOP_REQUESTED and time.time() < timestamp:
        time.sleep(min(POLL_SECONDS, max(0.02, timestamp - time.time())))


def run() -> int:
    validate()
    prepare_wsl_interop()
    lock_path = ROOT / ".runtime/vpn-rotation.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_handle = lock_path.open("w")
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("[dual-rotate] Một supervisor VPN khác đang chạy")

    previous = read_state()
    index = previous.get("next_index", 0)
    if not isinstance(index, int) or not 0 <= index < len(LOCATIONS):
        raise SystemExit("[dual-rotate] state next_index không hợp lệ")
    queue_cursor = previous.get("queue_cursor", 0)
    if not isinstance(queue_cursor, int) or not 0 <= queue_cursor < len(QUEUES):
        raise SystemExit("[dual-rotate] state queue_cursor không hợp lệ")
    public_ip = previous.get("public_ip") or ""
    connected_at = float(previous.get("connected_at") or 0)
    slice_deadline = float(previous.get("slice_deadline") or 0)
    first_cycle = True
    no_progress_slices = 0

    while not STOP_REQUESTED:
        before_total = total_remaining()
        if before_total == 0:
            write_state("success", index, "", public_ip, connected_at, slice_deadline, [],
                        "Tất cả queue đã hoàn tất", queue_cursor)
            log("Tất cả queue đã hết pending/failed; dừng thành công.")
            return 0

        location = LOCATIONS[index]
        now = time.time()
        observed_location = current_vpn_location() if first_cycle else ""
        observed_ip = current_public_ip() if first_cycle else ""
        can_resume = (
            first_cycle and previous.get("location") == location and public_ip
            and connected_at > 0 and slice_deadline > now
            and (observed_location == location or observed_ip == public_ip)
        )
        if can_resume:
            if observed_ip:
                public_ip = observed_ip
            log(f"Resume cặp {index + 1}/{len(LOCATIONS)} tại {location}, IP={public_ip}; không reconnect VPN.")
        else:
            placeholder = [Lane("A", USERS_A[index]), Lane("B", USERS_B[index])]
            write_state("switching_vpn", index, location, public_ip, connected_at,
                        slice_deadline, placeholder, "Đang đổi VPN", queue_cursor)
            log(f"Đổi cặp {index + 1}/{len(LOCATIONS)}: {USERS_A[index]} + {USERS_B[index]}, VPN={location}, remaining={before_total}")
            try:
                public_ip = switch_vpn(location, public_ip)
            except RuntimeError as exc:
                log(str(exc))
                index = (index + 1) % len(LOCATIONS)
                no_progress_slices += 1
                if no_progress_slices >= len(LOCATIONS):
                    return 1
                first_cycle = False
                continue
            connected_at = time.time()
            slice_deadline = connected_at + ROTATION_SECONDS
            log(f"VPN sẵn sàng: {location}, IP={public_ip}.")
        first_cycle = False

        selected_queues = select_slice_queues(queue_cursor)
        lane_a = Lane("A", USERS_A[index], selected_queues[0] if selected_queues else None)
        lane_b = Lane(
            "B", USERS_B[index], selected_queues[1] if len(selected_queues) > 1 else None
        )
        # Lưu con trỏ của lượt kế tiếp ngay khi pair này bắt đầu. Restart giữa phiên
        # cũng chuyển sang pair kế tiếp thay vì luôn quay lại cặp queue đầu.
        queue_cursor = (queue_cursor + min(2, len(QUEUES))) % len(QUEUES)
        lanes = [lane_a, lane_b]
        for lane in lanes:
            if lane.queue:
                start_lane(lane, slice_deadline)
        write_state("running", index, location, public_ip, connected_at, slice_deadline,
                    lanes, "Downloader đang chạy", queue_cursor)
        rotate_reason = ""
        fatal = False

        last_state_write = 0.0
        while not STOP_REQUESTED:
            now = time.time()
            if now >= slice_deadline:
                rotate_reason = f"Đủ {ROTATION_SECONDS}s"
                break

            for lane in lanes:
                if lane.process is not None and lane.process.poll() is not None:
                    code, reason = reap_lane(lane)
                    remaining = queue_remaining(lane.queue) if lane.queue else 0
                    log(f"Lane {lane.name} kết thúc exit={code}, reason={reason or 'none'}, remaining={remaining}.")
                    if remaining == 0:
                        other = lane_b if lane is lane_a else lane_a
                        lane.queue = next_queue(other.queue)
                        lane.soft_retries = lane.session_resets = lane.browser_retries = 0
                        if lane.queue:
                            start_lane(lane, slice_deadline)
                        else:
                            lane.status = "complete"
                    elif code == 3 or reason == "hard_block":
                        lane.status = "hard_block"
                        rotate_reason = f"Lane {lane.name} hard block"
                        break
                    elif code == 4 or reason == "soft_cloudflare_streak":
                        if lane.soft_retries < 1:
                            lane.soft_retries += 1
                            lane.retry_at = now + SOFT_COOLDOWN
                            lane.status = "soft_cooldown"
                            log(f"Lane {lane.name} soft-block; giữ nguyên IP/user, nghỉ {SOFT_COOLDOWN}s.")
                        elif lane.session_resets < 1:
                            try:
                                clear_lane_browser_session(lane)
                            except (OSError, RuntimeError) as exc:
                                lane.status = "soft_exhausted"
                                log(f"Lane {lane.name} không clear được session: {exc}; nghỉ đến rotation kế tiếp.")
                            else:
                                lane.session_resets += 1
                                lane.soft_retries = 0
                                lane.browser_retries = 0
                                lane.retry_at = now + SOFT_COOLDOWN
                                lane.status = "session_reset_cooldown"
                                log(
                                    f"Lane {lane.name} soft-block lần hai; đã clear session, "
                                    f"nghỉ {SOFT_COOLDOWN}s rồi thử thêm tối đa hai lượt."
                                )
                        else:
                            lane.status = "soft_exhausted"
                            log(f"Lane {lane.name} vẫn soft-block sau khi clear session; nghỉ đến rotation kế tiếp.")
                    elif code == 2 or reason == "exception":
                        if lane.browser_retries < 1:
                            lane.browser_retries += 1
                            lane.retry_at = now + BROWSER_RESTART_DELAY
                            lane.status = "browser_cooldown"
                            log(f"Lane {lane.name} lỗi browser; restart cùng IP/user sau {BROWSER_RESTART_DELAY}s.")
                        else:
                            lane.status = "exception_exhausted"
                    elif reason == "file_server_streak":
                        lane.retry_at = now + SOFT_COOLDOWN
                        lane.status = "server_cooldown"
                    else:
                        lane.status = "idle_until_rotation"
                        log(f"Lane {lane.name} không làm đổi VPN; chờ rotation kế tiếp.")
            if rotate_reason:
                break

            # PID/browser có thể vẫn sống trong khi một Playwright RPC treo vô hạn.
            # Chỉ restart lane im lặng; lane kia và VPN hiện tại tiếp tục bình thường.
            now = time.time()
            for lane in lanes:
                if not lane_stalled(lane, now):
                    continue
                silent_seconds = int(now - lane.last_output_at)
                stop_lane(lane)
                lane.stall_restarts += 1
                lane.last_exit_code = -signal.SIGTERM
                lane.stop_reason = "stalled"
                lane.retry_at = time.time() + BROWSER_RESTART_DELAY
                lane.status = "stalled_cooldown"
                log(
                    f"Lane {lane.name} không có log mới {silent_seconds}s; "
                    f"restart riêng lane cùng IP/user sau {BROWSER_RESTART_DELAY}s "
                    f"(lần {lane.stall_restarts})."
                )

            now = time.time()
            for lane in lanes:
                if lane.retry_at and lane.retry_at <= now:
                    other = lane_b if lane is lane_a else lane_a
                    lane.retry_at = 0
                    if lane.queue and lane.queue != other.queue and queue_remaining(lane.queue) > 0:
                        start_lane(lane, slice_deadline)

            if total_remaining() == 0:
                rotate_reason = "Tất cả queue đã hoàn tất"
                break

            if all(lane.status == "soft_exhausted" for lane in lanes):
                rotate_reason = "Cả hai lane soft-block sau retry"
                break
            if all(lane.status == "exception_exhausted" for lane in lanes):
                fatal = True
                break
            if now - last_state_write >= 30:
                write_state("running", index, location, public_ip, connected_at,
                            slice_deadline, lanes, "Giữ VPN; lane chạy/cooldown độc lập",
                            queue_cursor)
                last_state_write = now
            time.sleep(POLL_SECONDS)

        stop_all(lanes)
        if STOP_REQUESTED:
            write_state("stopped", index, location, public_ip, connected_at, slice_deadline,
                        lanes, "Dừng bởi người vận hành/systemd", queue_cursor)
            log("Đã dừng cả hai downloader; giữ nguyên VPN/profile/queue.")
            return 130
        if fatal:
            write_state("failed", index, location, public_ip, connected_at, slice_deadline,
                        lanes, "Cả hai lane lỗi browser hai lần", queue_cursor)
            log("Cả hai lane lỗi browser hai lần; dừng service, không đổi VPN.")
            return 1

        after_total = total_remaining()
        if after_total == 0:
            write_state("success", index, location, public_ip, connected_at, slice_deadline,
                        lanes, "Tất cả queue đã hoàn tất", queue_cursor)
            log("Tất cả queue đã hoàn tất.")
            return 0
        no_progress_slices = 0 if after_total < before_total else no_progress_slices + 1
        early = time.time() < slice_deadline
        if early:
            earliest_switch = connected_at + MIN_VPN_DWELL
            if time.time() < earliest_switch:
                write_state("vpn_dwell", index, location, public_ip, connected_at, slice_deadline, lanes,
                            f"{rotate_reason}; chờ đủ dwell trước khi đổi VPN", queue_cursor)
                log(f"{rotate_reason}; chờ đến đủ {MIN_VPN_DWELL}s trên IP hiện tại.")
                wait_until(earliest_switch)
                if STOP_REQUESTED:
                    write_state("stopped", index, location, public_ip, connected_at,
                                slice_deadline, lanes, "Dừng trong VPN dwell", queue_cursor)
                    return 130
        index = (index + 1) % len(LOCATIONS)
        previous = {}
        if no_progress_slices >= len(LOCATIONS):
            log("Cả một vòng không làm queue tiến triển; dừng.")
            return 1

    return 130


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGHUP, signal_handler)
    try:
        exit_code = run()
    except SystemExit as exc:
        if isinstance(exc.code, str):
            print(exc.code, file=sys.stderr)
            raise SystemExit(2)
        raise
    except KeyboardInterrupt:
        raise SystemExit(130)
    raise SystemExit(exit_code)
