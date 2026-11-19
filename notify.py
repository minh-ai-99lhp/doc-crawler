#!/usr/bin/env python3
"""Gửi thông báo Telegram — "năng lực" báo cáo của trợ lý theo dõi crawl site.

Đọc cấu hình từ .env (TELEGRAM_ENABLED / TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID).
Dùng chỉ thư viện chuẩn (urllib) — không cần cài thêm.

Cách dùng:
  python notify.py "nội dung báo cáo"     # gửi text từ tham số
  echo "nội dung" | python notify.py      # hoặc từ stdin
Trả mã 0 nếu gửi thành công, != 0 nếu lỗi/bị tắt.
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from datetime import datetime


def load_dotenv(path: Path = Path(__file__).with_name(".env")) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip().strip('"').strip("'")
        if v:
            os.environ.setdefault(k.strip(), v)


def send_telegram(text: str) -> bool:
    if os.environ.get("TELEGRAM_ENABLED", "0") != "1":
        print("[notify] TELEGRAM_ENABLED != 1 -> bỏ qua gửi.")
        return False
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        print("[notify] Thiếu TELEGRAM_BOT_TOKEN hoặc TELEGRAM_CHAT_ID.")
        return False

    # Telegram giới hạn 4096 ký tự/tin -> cắt bớt cho an toàn.
    text = text[:4000]
    data = urllib.parse.urlencode({
        "chat_id": chat,
        "text": text,
        "disable_web_page_preview": "true",
    }).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20) as r:
            resp = json.load(r)
        if resp.get("ok"):
            print("[notify] Đã gửi Telegram.")
            return True
        print("[notify] Telegram từ chối:", resp.get("description"))
        return False
    except Exception as e:
        print("[notify] Lỗi gửi:", e)
        return False


def build_summary_report(status_override: str | None = None) -> str:
    """Dựng báo cáo FIX CỨNG từ logs/last-summary.json (không cần AI)."""
    path = Path(__file__).with_name("logs") / "last-summary.json"
    try:
        s = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return "📊 BÁO CÁO CRAWL VĂN BẢN\nKhông đọc được logs/last-summary.json."

    if os.environ.get("BACKFILL_COMPACT_NOTIFY", "0") == "1":
        return build_compact_report(s, status_override)

    result = status_override or s.get("result", "?")
    blocked = bool(s.get("blocked"))          # Cloudflare chặn cứng — không phải lỗi code
    icon = "⛔" if blocked else ("✅" if result == "OK" else "⚠️")
    lines = [f"{icon} BÁO CÁO CRAWL VĂN BẢN — {s.get('run_at','?')}",
             f"Kết quả: {'BỊ CHẶN' if blocked else result}  "
             f"(mode={s.get('mode')}, ngày={s.get('range')})"]

    c = s.get("collect")
    if c:
        lines.append(f"• Thu thập: tìm thấy {c['found']}, mới {c['new']}, trùng {c['duplicate']}")
        if c["found"] == 0:
            lines.append("  → ngày này KHÔNG có văn bản mới.")
    else:
        lines.append("• Thu thập: (không chạy)")

    d = s.get("download")
    if d and not d.get("skipped"):
        pdf_upgrade = d.get("pdf", 0)
        dl_line = f"• Tải: {d['ok']} OK, {d['failed']} lỗi"
        if pdf_upgrade:
            dl_line += f", {pdf_upgrade} chỉ có PDF (đang chờ Word)"
        lines.append(dl_line)
    elif d and d.get("skipped"):
        lines.append("• Tải: bỏ qua (chưa đăng nhập)")
    else:
        lines.append("• Tải: (không chạy)")

    q = s.get("queue", {})
    pdf_only_q = q.get("pdf_only", 0)
    kho_line = (f"• Kho: {q.get('done',0)}/{q.get('total',0)} xong, "
                f"{q.get('pending',0)} chờ, {q.get('failed',0)} lỗi")
    if pdf_only_q:
        kho_line += f", {pdf_only_q} chờ Word (pdf_only)"
    lines.append(kho_line)

    dbd = s.get("downloads_by_date", {})
    if dbd:
        lines.append("• File theo ngày: " + ", ".join(f"{k}:{v}" for k, v in dbd.items()))

    probs = s.get("problems", [])
    lines.append("• Vấn đề: " + ("; ".join(probs) if probs else "không"))

    # Bị chặn: nói rõ đây KHÔNG phải hỏng code, và việc cần làm là ĐỢI — tránh người
    # vận hành (hoặc AI) lao vào chạy lại liên tục làm thời gian bị chặn dài thêm.
    if blocked:
        lines.append("")
        lines.append("⛔ Cloudflare CHẶN TRUY CẬP (không phải lỗi code, không có "
                     "captcha để giải). Nên NGHỈ 30-60 phút rồi để lần chạy sau tự "
                     "thử lại; ĐỪNG chạy lại liên tục. Hàng đợi giữ nguyên, không mất dữ liệu.")
        return "\n".join(lines)

    # Báo cáo fix cứng = KHÔNG có AI tự sửa -> nếu lỗi phải cảnh báo rõ.
    if result != "OK":
        lines.append("")
        lines.append("🚨 CHƯA ĐƯỢC TỰ SỬA (chế độ không AI). Cần người vào KIỂM TRA "
                     "& xử lý thủ công. Xem log ở máy: logs/latest.log")
    return "\n".join(lines)


def _compact_problem(problems: list[object]) -> str:
    if not problems:
        return "VĐ:0"
    raw = " ".join(str(problems[0]).split())
    # Giới hạn phần vấn đề, không cắt cả nhãn VĐ:.
    return "VĐ:" + raw[:20] + ("…" if len(raw) > 20 else "")


def _compact_file_line(s: dict) -> str:
    by_date = s.get("downloads_by_date") or {}
    start = str(s.get("range", "")).split("..", 1)[0]
    try:
        wanted = datetime.strptime(start, "%d/%m/%Y").strftime("%Y-%m-%d")
    except ValueError:
        wanted = ""
    keys = [k for k, v in by_date.items() if v]
    if not keys:
        return "F:0"
    key = wanted if wanted in by_date and by_date[wanted] else sorted(keys)[-1]
    extra = len([k for k in keys if k != key])
    text = f"F {key[5:]}:{by_date[key]}"
    if extra:
        text += f" +{extra} ngày"
    return text


def build_compact_report(s: dict, status_override: str | None = None) -> str:
    """Báo cáo cố định, ngắn cho các job backfill.

    Chỉ dùng số liệu trong last-summary.json; các biến BACKFILL_* chỉ cung cấp
    nhãn/cursor đã được runner truyền vào, không chứa credential.
    """
    label = os.environ.get("BACKFILL_LABEL", os.environ.get("BACKFILL_JOB_ID", "BACKFILL"))
    index = os.environ.get("BACKFILL_DAY_INDEX", "?")
    total = os.environ.get("BACKFILL_DAY_TOTAL", "?")
    attempt = os.environ.get("BACKFILL_ATTEMPT", "1")
    phase = os.environ.get("BACKFILL_PHASE", "daily")
    drain = os.environ.get("BACKFILL_DRAIN_ATTEMPT", "")
    blocked = bool(s.get("blocked"))
    problems = s.get("problems") or []

    if blocked:
        return f"{label} ⛔ Cloudflare · nghỉ 30–60p"

    try:
        stamp = datetime.strptime(str(s.get("range", "")).split("..", 1)[0], "%d/%m/%Y")
        date_text = stamp.strftime("%d/%m/%Y")
    except ValueError:
        date_text = str(s.get("range", "?"))
    result = status_override or s.get("result", "?")
    icon = "✅" if result == "OK" and not problems else "⚠️"
    progress = f"{index}/{total}"
    suffix = f" · vét {drain}/3" if phase == "drain" and drain else ""
    if phase == "daily" and str(attempt) != "1":
        suffix = f" · lần {attempt}"
    c = s.get("collect") or {}
    collect = (f"Thu {c.get('found', 0)}(+{c.get('new', 0)})"
               if s.get("collect") is not None else "Thu —")
    d = s.get("download") or {}
    if d.get("skipped"):
        download = "Tải —"
    else:
        download = f"Tải {d.get('ok', 0)}/{d.get('failed', 0)}"
        if d.get("pdf", 0):
            download += f" PDF {d['pdf']}"
    q = s.get("queue") or {}
    kho = f"Kho ✓{q.get('done', 0)} ⏳{q.get('pending', 0)} ✗{q.get('failed', 0)}"
    if q.get("pdf_only", 0):
        kho += f" PDF{q['pdf_only']}"
    return (f"{label} {icon} {progress} · {date_text}{suffix}\n"
            f"{collect} · {download} · {kho} · {_compact_file_line(s)} · "
            f"{_compact_problem(problems)}")


def main() -> None:
    load_dotenv()
    args = sys.argv[1:]
    # Chế độ báo cáo fix cứng: notify.py --summary [OK|FAIL]
    if args and args[0] == "--summary":
        override = args[1] if len(args) > 1 else None
        text = build_summary_report(override)
    else:
        text = " ".join(args).strip()
        if not text and not sys.stdin.isatty():
            text = sys.stdin.read().strip()
    if not text:
        print("[notify] Không có nội dung để gửi.")
        sys.exit(2)
    sys.exit(0 if send_telegram(text) else 1)


if __name__ == "__main__":
    main()
