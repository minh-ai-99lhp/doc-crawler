#!/usr/bin/env python3
"""
Crawler tải văn bản (.doc) từ cổng văn bản nguồn (SITE_BASE_URL) bằng Camoufox.

Nguyên tắc:
- Dùng trình duyệt THẬT (headed), bạn tự đăng nhập lần đầu -> tự qua Cloudflare/captcha.
- Session được lưu vào user_data_dir nên các lần sau thường không phải login lại.
- Không giả mạo Cloudflare, không xoay IP, có delay ngẫu nhiên -> hạn chế bị khoá acc.
- Bắt sự kiện download (page.expect_download) nên không lệ thuộc selector cứng.

Chế độ:
  auto   : script tự bấm nút tải (cần --download-selector đúng).
  assist : script mở từng văn bản rồi CHỜ bạn tự bấm "Tải về"; nó tự bắt file.

Hai pha: (1) THU THẬP link từ trang tìm kiếm theo ngày (phân trang + dedup, lưu
queue.json), (2) TẢI CHI TIẾT từng văn bản (chậm như người). Đăng nhập tự động.

Cách chạy:
  python3 -m venv .venv && source .venv/bin/activate
  pip install camoufox==0.5.4 pillow pytesseract
  python -m camoufox fetch

  # Khai báo tài khoản (để tự đăng nhập): tạo auth.json theo auth.example.json,
  # hoặc: export SITE_USER="..." SITE_PASS="..."

  # Cào văn bản HÔM NAY (mặc định):
  xvfb-run -a python crawler.py

  # Cron 23:59 mỗi ngày — quét CỬA SỔ TRƯỢT N ngày gần nhất để bắt cả VB đăng trễ
  # (VB ban hành ngày D có thể vài ngày sau mới lên site); queue.json dedup nên không tải lại:
  xvfb-run -a python crawler.py --days-back 15

  # Cào LẠI QUÁ KHỨ theo khoảng ngày (chạy 1 lần):
  xvfb-run -a python crawler.py --from 01/07/2026 --to 23/07/2026

  # Tách pha khi cần:
  python crawler.py --collect-only      # chỉ gom link vào queue.json
  python crawler.py --download-only     # chỉ tải các link pending trong queue

Ghi chú:
  - PHẢI chạy headed (Cloudflare chặn headless). Trong WSL/không màn hình: xvfb-run.
  - queue.json giữ trạng thái pending/done/failed -> chống trùng & resume được.
  - Chưa khai báo tài khoản: script chờ bạn đăng nhập tay 1 lần
    (session lưu trong .camoufox_profile).
"""

import argparse
from contextlib import contextmanager
import json
import os
import random
import re
import sys
import tempfile
import time
import traceback
import urllib.parse as urlparse
from datetime import date, timedelta
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeout

PWTimeout = (PlaywrightTimeout,)

# Domain nguồn KHÔNG hardcode — đọc từ biến môi trường SITE_BASE_URL (.env), xem configure_site().
BASE = ""
# Trang gốc để mở/kiểm tra đăng nhập (trang tìm văn bản, không phải trang login).
START_URL = ""
CAMOUFOX_DIR = Path("./.camoufox_profile")
CAMOUFOX_PROFILE_DIR = CAMOUFOX_DIR / "browser"
CAMOUFOX_IDENTITY_FILE = CAMOUFOX_DIR / "identity.json"
OUT_DIR = Path("./downloads")            # nơi lưu file .doc
AUTH_FILE = Path("./auth.json")          # {"username": "...", "password": "..."} (gitignore!)
QUEUE_FILE = Path("./queue.json")        # HÀNG ĐỢI: url -> {status, added, ...} (dedup + state)
QUEUE_PRUNE_DONE_DAYS = 30                # xoá URL 'done' cũ hơn N ngày khỏi queue.json (khỏi phình
                                           # to); 'pending'/'failed' KHÔNG BAO GIỜ tự xoá — còn cần
                                           # giữ để lần chạy sau (ngày mới) tự thử lại.
try:
    CF_STREAK_ABORT = max(1, int(os.environ.get("CF_STREAK_ABORT", "3")))
except ValueError:
    CF_STREAK_ABORT = 3
                                           # bấy nhiêu link LIÊN TIẾP bị Cloudflare challenge
                                           # thì dừng pha tải sớm: 1-2 lần là chuyện thường,
                                           # liên tiếp nhiều = site đang siết, cố nữa chỉ tổ
                                           # tụt điểm. Link chưa tải giữ nguyên cho lần sau.
CAPTCHA_STREAK_ABORT = 3                   # CAPTCHA ảnh site có bộ đếm/ngưỡng riêng,
                                           # không ghi nhầm thành lỗi Cloudflare.
MAX_RELOGINS_PER_RUN = 3                   # tránh hai máy dùng cùng tài khoản đá nhau vô hạn;
                                           # hết ngưỡng thì dừng pha tải, giữ nguyên queue.
SESSION_PROBE_DOCUMENTS = 2                # số VB đã tải thành công dùng để phân biệt
                                           # mất phiên thật với một VB bị khóa riêng.
PDF_RETRY_DAYS = 30                        # re-check 'pdf_only' tối đa N ngày (tính từ 'added');
                                           # PHẢI ≤ QUEUE_PRUNE_DONE_DAYS để prune dọn đúng lúc.
LOGS_DIR = Path("./logs")                # log + last-summary.json (số liệu thật cho AI)
LOGIN_URL = ""


def download_staging_dir() -> Path:
    """Thư mục tạm cho download manager của Firefox/Camoufox.

    Browser ghi luồng tải ở filesystem tạm trước; Playwright chỉ chép file đã
    hoàn tất về ``OUT_DIR``. Điều này giữ phần tải dở tách khỏi kho kết quả và
    đặc biệt ổn định khi output nằm trên mount Windows của WSL. Có thể đổi thư
    mục này khi cần qua ``SITE_DOWNLOAD_STAGING_DIR``.
    """
    root = os.environ.get("SITE_DOWNLOAD_STAGING_DIR")
    base = Path(root).expanduser() if root else Path(tempfile.gettempdir())
    return base / "doc-crawler-downloads"

# Template trang tìm kiếm theo khoảng ngày (bdate/edate dạng dd/MM/yyyy). Phân trang: &page=N.
SEARCH_TMPL = ""
# Host của server file (link tải có thể redirect sang đây); mặc định files.<domain nguồn>.
FILE_SERVER_DOC_URL = ""


def configure_site() -> None:
    """Dựng các URL nguồn từ SITE_BASE_URL (gọi sau load_dotenv())."""
    global BASE, START_URL, LOGIN_URL, SEARCH_TMPL, FILE_SERVER_DOC_URL
    BASE = os.environ.get("SITE_BASE_URL", "").rstrip("/")
    if not BASE:
        sys.exit("[!] Chưa cấu hình SITE_BASE_URL trong .env (vd: SITE_BASE_URL=https://example.com).")
    START_URL = f"{BASE}/page/tim-van-ban.aspx?keyword=&match=True&area=0"
    LOGIN_URL = f"{BASE}/page/login.aspx"
    SEARCH_TMPL = (f"{BASE}/page/tim-van-ban.aspx?"
                   "keyword=&area=0&type=0&status=0&lan=1&org=0&signer=0&match=True&sort=1"
                   "&bdate={bdate}&edate={edate}")
    host = urlparse.urlparse(BASE).netloc.removeprefix("www.")
    FILE_SERVER_DOC_URL = os.environ.get("SITE_FILE_SERVER", f"files.{host}").lower() + "/doc.aspx"

# Trong tab 'Tải về' của trang chi tiết, thử các nhãn tải theo thứ tự ưu tiên này.
# "Tải Văn bản gốc" = PDF gốc (bản chính thức); (docx) = bản Word; còn lại = .doc.
DOWNLOAD_LABELS = (
    "Tải Văn bản tiếng Việt (docx)",   # Word (ưu tiên)
    "Tải Văn bản tiếng Việt",          # .doc
    "Tải Văn bản gốc",                 # PDF gốc
)
PDF_LABEL = DOWNLOAD_LABELS[2]             # "Tải Văn bản gốc" — chỉ có PDF
FILE_SERVER_DISK_ERROR = "there is not enough space on the disk"

# Dấu hiệu ĐANG bị challenge (phải chính xác, vì trang thật cũng có thể nhúng widget
# turnstile). Chỉ tin: tiêu đề "chờ/just a moment" và token __cf_chl trên URL.
# KHÔNG để "attention required" ở đây: đó là tiêu đề trang BỊ CHẶN CỨNG (xem
# CF_BLOCK_MARKERS) — coi nó là challenge sẽ khiến code ngồi click cái checkbox
# không tồn tại rồi reload nhiều lần vào một trang đang chặn, càng bị chặn lâu hơn.
CF_TITLE_MARKERS = ("just a moment", "chờ một chút", "checking your browser")
CF_URL_MARKERS = ("__cf_chl", "cf_chl_opt")

# Dấu hiệu BỊ CHẶN CỨNG (khác challenge: KHÔNG có gì để giải, không có checkbox).
# Trang thật: tiêu đề "Attention Required! | Cloudflare", nội dung "Sorry, you have
# been blocked". Gặp cái này phải DỪNG NGAY, không reload — theo ghi chú vận hành thì
# cần nghỉ 30-60 phút. Mã 1015 = rate limit, 1020 = firewall rule chặn.
CF_BLOCK_MARKERS = ("you have been blocked", "error 1015", "error 1020",
                    "attention required", "bạn đã bị chặn")

# CAPTCHA ảnh riêng của site (khác Cloudflare Turnstile). Site hiện dùng mã 6 ký tự.
CAPTCHA_LENGTH = 6
CAPTCHA_MAX_ATTEMPTS = 5
CAPTCHA_IMAGE_SELECTORS = (
    "img[src='/RegistImage.aspx']",
    "img[src*='RegistImage']",
    "#ctl00_Content_pnlLoginTemplate img",
)
CAPTCHA_INPUT_SELECTORS = (
    "#ctl00_Content_txtSecCode",
    "input[name*='SecCode' i]",
)
CAPTCHA_SUBMIT_SELECTORS = (
    "#ctl00_Content_CheckButton",
    "#ctl00_Content_cmdLogin",
    "button:has-text('Xác nhận')",
    "button:has-text('Đồng ý')",
    "input[type='submit']",
)

def safe_name(url: str, suggested: str | None) -> str:
    if suggested:
        return suggested
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", url.split("/")[-1]) or "vanban"
    return f"{slug}.doc"


def looks_like_cloudflare(page) -> bool:
    """True chỉ khi ĐANG kẹt ở trang challenge (không phải trang thật có nhúng widget)."""
    try:
        url = (page.url or "").lower()
    except Exception:
        url = ""
    if any(m in url for m in CF_URL_MARKERS):
        return True

    # `page.title()` không nhận timeout. Trên interstitial Cloudflare, document có
    # thể liên tục bị thay thế trong vòng xác minh; Juggler khi đó có thể
    # chờ lệnh title vô hạn, làm vô hiệu cả deadline của handler lẫn
    # timeout từng trang. Locator có timeout thật nên nếu document đang đổi,
    # ta nhường vòng hiện tại và thử lại thay vì treo cả crawler.
    try:
        title = (page.locator("title").text_content(timeout=1500) or "").lower()
    except Exception:
        return False
    return any(m in title for m in CF_TITLE_MARKERS)


def is_hard_blocked(page) -> bool:
    """True khi Cloudflare CHẶN CỨNG (trang 'Sorry, you have been blocked').

    Khác `looks_like_cloudflare()`: challenge thì còn giải được (chờ/click checkbox),
    còn cái này thì KHÔNG có gì để giải — mọi retry chỉ làm thời gian chặn dài thêm.
    Nhận diện bằng NỘI DUNG trang (chắc chắn hơn tiêu đề, phòng khi CF đổi title), và
    chỉ soi 3000 ký tự đầu để không kéo cả trang thật (trang site thật rất nặng)."""
    try:
        body = (page.locator("body").inner_text(timeout=1500) or "")[:3000]
    except Exception:
        body = ""
    low = body.lower()
    if any(m in low for m in CF_BLOCK_MARKERS):
        return True
    try:
        title = page.locator("title").text_content(timeout=1500) or ""
        return "attention required" in title.lower()
    except Exception:
        return False


def cf_ray_id(page) -> str:
    """Ray ID trên trang chặn của Cloudflare — mã này để báo cho người vận hành (và để
    liên hệ chủ site nếu cần). Không lấy được thì trả chuỗi rỗng."""
    try:
        body = page.locator("body").inner_text(timeout=1500) or ""
        match = re.search(r"Ray ID:?\s*([0-9a-f]{8,})", body, re.IGNORECASE)
        return match.group(1) if match else ""
    except Exception:
        return ""


class AccessChallengeError(RuntimeError):
    """Không thể đi tiếp vì Cloudflare hoặc CAPTCHA ảnh của site."""


class AccessBlockedError(AccessChallengeError):
    """Cloudflare CHẶN CỨNG. Khác challenge: KHÔNG retry, KHÔNG reload, KHÔNG nhờ người
    bấm (chẳng có gì để bấm) — dừng hẳn run và để người vận hành cho nghỉ 30-60 phút."""


class CaptchaBypassError(AccessChallengeError):
    """CAPTCHA ảnh site không thể giải tự động; dừng run để lần sau resume."""


class SessionExpiredError(RuntimeError):
    """Phiên đăng nhập site đã hết hoặc bị một phiên đăng nhập khác đá ra."""


def raise_if_blocked(page) -> None:
    """Gặp trang chặn cứng -> ném AccessBlockedError kèm Ray ID (để người vận hành tra
    cứu / báo chủ site). Gọi ở MỌI chỗ vừa điều hướng xong, TRƯỚC khi chờ hay reload."""
    if not is_hard_blocked(page):
        return
    ray = cf_ray_id(page)
    raise AccessBlockedError(
        "Cloudflare CHẶN CỨNG (trang 'Sorry, you have been blocked') tại "
        f"{page.url}{f' — Ray ID {ray}' if ray else ''}. "
        "KHÔNG phải challenge: không có checkbox để bấm. Dừng run, nghỉ 30-60 phút "
        "rồi chạy lại; đừng thử liên tục vì càng bị chặn lâu hơn."
    )


def _first_visible(page, selectors):
    """Trả locator visible đầu tiên trong danh sách selector, hoặc None."""
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.count() and locator.is_visible():
                return locator
        except Exception:
            continue
    return None


def looks_like_site_captcha(page) -> bool:
    """Nhận diện trang CAPTCHA ảnh của site, không nhầm với Turnstile."""
    try:
        url = (page.url or "").lower()
        title = (page.title() or "").lower()
    except Exception:
        return False

    if "captcha.aspx" in url or "/page/captcha" in url or "captcha" in title:
        return True
    if _first_visible(page, CAPTCHA_INPUT_SELECTORS) is not None:
        return True
    if (_first_visible(page, CAPTCHA_IMAGE_SELECTORS) is not None
            and _first_visible(page, CAPTCHA_SUBMIT_SELECTORS) is not None):
        return True

    for text in ("Bạn đã tìm kiếm với tốc độ của Robot", "Vui lòng nhập mã để tiếp tục"):
        try:
            locator = page.get_by_text(text, exact=False).first
            if locator.count() and locator.is_visible():
                return True
        except Exception:
            continue
    return False


def configure_tesseract(pytesseract_module) -> None:
    """Cho phép Windows chỉ rõ tesseract.exe; Docker/Linux dùng executable trong PATH."""
    configured = os.environ.get("TESSERACT_CMD", "").strip()
    if configured:
        pytesseract_module.pytesseract.tesseract_cmd = configured


def ocr_captcha_code(image_bytes: bytes) -> str | None:
    """Đọc mã CAPTCHA 6 ký tự từ nhiều biến thể ảnh và chọn kết quả ổn định nhất."""
    try:
        from collections import Counter
        import io
        from PIL import Image, ImageEnhance, ImageFilter, ImageOps
        import pytesseract
    except ImportError as exc:
        raise CaptchaBypassError(
            "Thiếu Pillow/pytesseract; cần rebuild image trước khi xử lý CAPTCHA ảnh."
        ) from exc

    configure_tesseract(pytesseract)
    source = Image.open(io.BytesIO(image_bytes)).convert("L")
    width, height = source.size
    inverted = ImageOps.invert(source)
    scaled_4 = inverted.resize((width * 4, height * 4), Image.Resampling.LANCZOS)
    scaled_3 = inverted.resize((width * 3, height * 3), Image.Resampling.LANCZOS)
    scaled_5 = inverted.resize((width * 5, height * 5), Image.Resampling.LANCZOS)
    variants = (
        scaled_4.point(lambda value: 0 if value < 100 else 255),
        scaled_3.point(lambda value: 0 if value < 128 else 255).filter(ImageFilter.SHARPEN),
        ImageEnhance.Contrast(scaled_4).enhance(2.0).point(
            lambda value: 0 if value < 120 else 255),
        scaled_5.point(lambda value: 0 if value < 110 else 255),
        scaled_3.filter(ImageFilter.MedianFilter(3)).point(
            lambda value: 0 if value < 128 else 255),
    )

    candidates: list[str] = []
    whitelists = ("0123456789", "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    for image in variants:
        for psm in (7, 8, 6):
            for whitelist in whitelists:
                try:
                    raw = pytesseract.image_to_string(
                        image,
                        config=f"--psm {psm} -c tessedit_char_whitelist={whitelist}",
                    )
                except pytesseract.TesseractNotFoundError as exc:
                    raise CaptchaBypassError(
                        "Không tìm thấy Tesseract OCR; kiểm tra image hoặc TESSERACT_CMD."
                    ) from exc
                code = "".join(char for char in raw.upper() if char.isalnum())
                if len(code) == CAPTCHA_LENGTH:
                    candidates.append(code)

    if not candidates:
        return None
    return Counter(candidates).most_common(1)[0][0]


def _refresh_captcha(page) -> None:
    image = _first_visible(page, CAPTCHA_IMAGE_SELECTORS)
    if image is not None:
        try:
            image.click(timeout=2000, force=True)
        except Exception:
            pass
    page.wait_for_timeout(1500)


def solve_site_captcha(page) -> bool:
    """OCR, điền và submit CAPTCHA ảnh site tối đa CAPTCHA_MAX_ATTEMPTS lần."""
    print("[!] Phát hiện CAPTCHA ảnh site — đang thử OCR tự động.")
    for attempt in range(1, CAPTCHA_MAX_ATTEMPTS + 1):
        # URL captcha xuất hiện ngay từ navigation commit, trước khi ảnh và ô nhập
        # được render. Chờ DOM thật sự sẵn sàng; không tính thời gian chờ này thành
        # một lần OCR thất bại.
        ready_deadline = time.time() + 15
        image = input_box = None
        while time.time() < ready_deadline:
            if not looks_like_site_captcha(page):
                return True
            image = _first_visible(page, CAPTCHA_IMAGE_SELECTORS)
            input_box = _first_visible(page, CAPTCHA_INPUT_SELECTORS)
            if image is not None and input_box is not None:
                break
            page.wait_for_timeout(400)
        if image is None or input_box is None:
            print("[!] Trang CAPTCHA đã tải nhưng chưa thấy ảnh hoặc ô nhập sau 15 giây.")
            return False

        try:
            code = ocr_captcha_code(image.screenshot())
        except CaptchaBypassError:
            raise
        except Exception as exc:
            print(f"[!] OCR CAPTCHA lỗi ở lần {attempt}/{CAPTCHA_MAX_ATTEMPTS}: {exc}")
            code = None

        if not code:
            print(f"[!] OCR không đọc được mã ở lần {attempt}/{CAPTCHA_MAX_ATTEMPTS}.")
            _refresh_captcha(page)
            continue

        print(f"[i] Đã đọc CAPTCHA, đang gửi lần {attempt}/{CAPTCHA_MAX_ATTEMPTS}.")
        try:
            input_box.fill(code, timeout=5000)
        except Exception as exc:
            if not looks_like_site_captcha(page):
                print("[✓] CAPTCHA đã tự chuyển trang trước khi điền mã.")
                return True
            print(f"[!] Ô nhập CAPTCHA đổi DOM ở lần {attempt}: {exc}")
            continue
        page.wait_for_timeout(random.randint(300, 800))
        submitted = False
        for selector in CAPTCHA_SUBMIT_SELECTORS:
            try:
                button = page.locator(selector).first
                if button.count() and button.is_visible():
                    button.click(timeout=2000, force=True, no_wait_after=True)
                    submitted = True
                    break
            except Exception:
                continue
        if not submitted:
            try:
                input_box.press("Enter")
                submitted = True
            except Exception:
                pass
        if not submitted:
            return False

        # Chờ chuyển trang sau submit. Site có thể điều hướng chậm; ba giây cố định
        # trước đây dễ kết luận sai rồi refresh khi request vẫn đang xử lý.
        submit_deadline = time.time() + 8
        while time.time() < submit_deadline:
            if not looks_like_site_captcha(page):
                print("[✓] Đã vượt CAPTCHA ảnh site.")
                return True
            page.wait_for_timeout(400)
        print(f"[!] CAPTCHA chưa đúng ở lần {attempt}/{CAPTCHA_MAX_ATTEMPTS}.")
        _refresh_captcha(page)
    return False


# ----- Trạng thái để báo thành/bại cho lớp ngoài (cron/wrapper) -----
CI_MODE = False              # True: chạy tự động, KHÔNG chờ người; gặp bí -> ghi lỗi.
PROBLEMS: list[str] = []     # gom các vấn đề gặp trong 1 lần chạy.
RUN_STOP_REASON: str | None = None


def report_problem(msg: str) -> None:
    PROBLEMS.append(msg)
    print(f"[PROBLEM] {msg}")


# ----- Chuẩn hoá quan hệ 'Lược đồ' -----
# Nhãn tiếng Việt trên trang (đã bỏ "Văn bản " + "- [n]") -> key cố định để code dùng về
# sau không bị lệch (site có thể đổi cách viết hoa/dấu câu; site khác có thể diễn đạt khác
# nhưng cùng ý nghĩa). Không "-> _by"/passive = văn bản NÀY chủ động làm hành động đó; "_by"
# = văn bản NÀY bị/được văn bản khác tác động.
LUOC_DO_RELATION_MAP: dict[str, str] = {
    "được hướng dẫn": "guides",                              # VB này hướng dẫn thi hành VB kia
    "hướng dẫn": "guided_by",                                # VB này được VB kia hướng dẫn thi hành
    "quy định chi tiết, hướng dẫn thi hành": "guided_by",
    "quy định chi tiết / hướng dẫn thi hành": "guided_by",
    "được hợp nhất": "consolidates",                         # VB này hợp nhất (là bản hợp nhất của) VB kia
    "hợp nhất": "consolidated_into",                         # VB này bị hợp nhất vào 1 VB hợp nhất khác
    "áp dụng": "applies",
    "bị sửa đổi bổ sung": "amended_by",
    "sửa đổi bổ sung": "amends",
    "bị đính chính": "corrected_by",
    "đính chính": "corrects",
    "bị thay thế": "replaced_by",
    "thay thế": "replaces",
    "bị bãi bỏ": "repealed_by",
    "bãi bỏ": "repeals",
    "bị tạm ngừng hiệu lực": "suspended_by",
    "tạm ngừng hiệu lực": "suspends",
    "được dẫn chiếu": "references",
    "được căn cứ": "based_on",
    "liên quan ngôn ngữ": "related_language",
    "liên quan cùng nội dung": "related_content",
}
LUOC_DO_KEYS = sorted(set(LUOC_DO_RELATION_MAP.values()))
LUOC_DO_SKIP_LABELS = {"đang xem"}         # chính văn bản hiện tại, không phải quan hệ với VB khác.
UNKNOWN_LUOC_DO_LOG = LOGS_DIR / "unknown_luoc_do_labels.json"   # ghi nhận nhãn lạ để người vận hành xem lại.
_luoc_do_unknown_seen: set[str] = set()    # dedupe cảnh báo trong 1 lần chạy (khỏi spam problems).

# Lược đồ là một request AJAX riêng cho MỖI văn bản.  Không lấy nó trong luồng
# crawl hiện tại để giảm đáng kể thời gian và số request; vẫn giữ schema metadata
# với các mảng rỗng để dữ liệu cũ và downstream không bị vỡ.
SKIP_LUOC_DO = True


def _log_unknown_luoc_do_label(label: str, url: str) -> None:
    """Ghi bền (ngoài PROBLEMS chỉ sống trong 1 lần chạy) nhãn quan hệ lược đồ chưa biết,
    kèm số lần gặp + VB ví dụ, để người vận hành xem lại và bổ sung LUOC_DO_RELATION_MAP."""
    try:
        data = json.loads(UNKNOWN_LUOC_DO_LOG.read_text(encoding="utf-8")) if UNKNOWN_LUOC_DO_LOG.exists() else {}
    except Exception:
        data = {}
    entry = data.setdefault(label, {"first_seen": time.strftime("%Y-%m-%d %H:%M:%S"), "count": 0, "example_url": url})
    entry["count"] += 1
    entry["last_seen"] = time.strftime("%Y-%m-%d %H:%M:%S")
    LOGS_DIR.mkdir(exist_ok=True)
    UNKNOWN_LUOC_DO_LOG.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _luoc_do_key(raw_label: str, url: str) -> str | None:
    """Chuẩn hoá 1 nhãn nhóm quan hệ thô (còn '- [n]') -> key cố định trong LUOC_DO_KEYS.
    Nhãn lạ (chưa có trong LUOC_DO_RELATION_MAP): vẫn giữ dữ liệu (không âm thầm bỏ mất)
    dưới key 'unmapped:<nhãn>', đồng thời ghi log bền + báo problem (-> Telegram) 1 lần/nhãn
    mỗi lần chạy, để người vận hành quyết định xếp vào quan hệ nào."""
    label = re.sub(r"\s*-\s*\[\d+\]\s*$", "", raw_label).strip()
    label = re.sub(r"^Văn bản\s+", "", label, flags=re.IGNORECASE).strip()
    norm = label.lower()
    if norm in LUOC_DO_SKIP_LABELS:
        return None
    key = LUOC_DO_RELATION_MAP.get(norm)
    if key:
        return key
    if norm not in _luoc_do_unknown_seen:
        _luoc_do_unknown_seen.add(norm)
        _log_unknown_luoc_do_label(label, url)
        report_problem(f"Lược đồ: gặp loại quan hệ MỚI chưa định nghĩa: '{label}' "
                        f"(vd: {url}) — cần bổ sung vào LUOC_DO_RELATION_MAP.")
    return f"unmapped:{label}"


def wait_for_human(page, msg: str) -> None:
    """Chờ người thao tác trên browser rồi Enter. Ở CI_MODE (cron): KHÔNG chờ,
    chỉ ghi nhận là 1 vấn đề để lớp ngoài biết cần AI vào xử lý."""
    print(f"\n[⏸  CẦN BẠN] {msg}")
    if CI_MODE:
        report_problem(f"Cần thao tác tay nhưng đang chạy tự động: {msg}")
        return
    print("   -> Thao tác trên cửa sổ Camoufox, xong quay lại đây bấm Enter.")
    try:
        input()
    except EOFError:
        report_problem("Không có stdin để thao tác tay (chạy không tương tác).")


def _camoufox_identity() -> dict:
    """Tạo một fingerprint Windows rồi giữ cố định cùng profile Linux.

    Cookie Cloudflare gắn với danh tính trình duyệt. Camoufox mặc định sinh fingerprint
    mới mỗi lần mở, vì vậy ta lưu cấu hình đã sinh và chỉ tạo lại khi phiên bản browser
    thay đổi (lúc đó UA bắt buộc phải đổi theo phiên bản thật).
    """
    from camoufox import DefaultAddons
    from camoufox.pkgman import installed_verstr
    from camoufox.utils import launch_options

    CAMOUFOX_DIR.mkdir(exist_ok=True)
    version = installed_verstr()
    try:
        saved = json.loads(CAMOUFOX_IDENTITY_FILE.read_text(encoding="utf-8"))
        if saved.get("browser_version") == version and isinstance(saved.get("config"), dict):
            config = saved["config"]
            # Addon là cấu hình lúc launch, không phải fingerprint. Các bản cũ đã vô
            # tình lưu đường dẫn uBlock vào identity; bỏ nó để challenge không bị content
            # blocker can thiệp và để `exclude_addons` bên dưới có hiệu lực.
            config.pop("addons", None)
            return config
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass

    # Sinh cấu hình hoàn chỉnh một lần. `fingerprint_preset=True` dùng mẫu Firefox
    # Windows thật; các seed canvas/audio/font cũng nằm trong config được lưu lại.
    generated = launch_options(
        os="windows",
        locale=["vi-VN", "vi", "en-US", "en"],
        fingerprint_preset=True,
        humanize=True,
        exclude_addons=[DefaultAddons.UBO],
        headless=False,
    )
    env = generated.get("env") or {}
    chunks = sorted(
        (int(key.rsplit("_", 1)[1]), value)
        for key, value in env.items()
        if key.startswith("CAMOU_CONFIG_")
    )
    if not chunks:
        raise RuntimeError("Camoufox không sinh được fingerprint config.")
    config = json.loads("".join(value for _, value in chunks))
    CAMOUFOX_IDENTITY_FILE.write_text(
        json.dumps({"browser_version": version, "config": config}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[i] Đã tạo fingerprint Windows cố định cho Linux (Camoufox {version}).")
    return config


def open_camoufox_context(pw, headless: bool, downloads_path: Path | None = None):
    """Mở Camoufox trên Linux với fingerprint Windows đồng bộ ở cấp browser."""
    try:
        from camoufox import DefaultAddons
        from camoufox.sync_api import NewBrowser
        from camoufox.utils import launch_options
    except ImportError as exc:
        raise RuntimeError(
            "Linux cần Camoufox: chạy `.venv/bin/pip install camoufox==0.5.4` rồi "
            "`.venv/bin/python -m camoufox fetch`."
        ) from exc

    CAMOUFOX_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    config = _camoufox_identity()
    options = launch_options(
        config=config,
        os="windows",
        locale=["vi-VN", "vi", "en-US", "en"],
        humanize=True,
        # Camoufox hỗ trợ riêng tùy chọn này để thao tác Turnstile trong iframe chéo
        # nguồn. Không nạp uBlock vì content blocker có thể cản challenge-platform.
        disable_coop=True,
        exclude_addons=[DefaultAddons.UBO],
        headless=headless,
        i_know_what_im_doing=True,
        user_data_dir=str(CAMOUFOX_PROFILE_DIR),
        accept_downloads=True,
        no_viewport=True,
    )
    if downloads_path is not None:
        # Firefox/Playwright phải biết thư mục staging ngay từ lúc launch. Nếu chỉ
        # gọi download.save_as() sau khi nhận event thì browser vẫn có thể chọn một
        # thư mục mặc định khác trước khi event download được phát ra.
        downloads_path.mkdir(parents=True, exist_ok=True)
        options["downloads_path"] = str(downloads_path.resolve())
    ctx = NewBrowser(pw, from_options=options, persistent_context=True)
    ctx.set_default_timeout(60000)
    detail = f", download-tmp={downloads_path}" if downloads_path is not None else ""
    print(f"[i] Browser Linux: Camoufox headed, profile={CAMOUFOX_PROFILE_DIR}{detail}")
    return ctx


@contextmanager
def browser_context(headless: bool, downloads_path: Path | None = None):
    """Mở duy nhất Camoufox persistent context và luôn đóng browser an toàn."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("Thiếu dependency Playwright đi kèm Camoufox.") from exc
    with sync_playwright() as pw:
        ctx = open_camoufox_context(pw, headless, downloads_path=downloads_path)
        try:
            yield ctx
        finally:
            close_context(ctx)


def close_context(ctx) -> None:
    """Đóng context an toàn — bỏ qua nếu cửa sổ đã bị đóng tay (TargetClosedError)."""
    try:
        ctx.close()
    except Exception:
        pass


def human_pause(a: float = 0.4, b: float = 1.6) -> None:
    """Nghỉ ngắn ngẫu nhiên giữa các thao tác cho giống người."""
    time.sleep(random.uniform(a, b))


def human_scroll(page) -> None:
    """Cuộn trang vài nhịp thất thường -> mô phỏng người đang đọc."""
    try:
        for _ in range(random.randint(2, 5)):
            page.mouse.wheel(0, random.randint(300, 900))
            human_pause(0.3, 1.1)
    except Exception:
        pass


def load_dotenv(path: Path = Path("./.env")) -> None:
    """Nạp biến từ file .env vào os.environ (KHÔNG ghi đè biến đã có sẵn).
    Định dạng KEY=VALUE mỗi dòng; bỏ dòng trống và dòng bắt đầu bằng '#'."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.strip().strip('"').strip("'")
        if val:
            os.environ.setdefault(key.strip(), val)


def load_credentials() -> tuple[str | None, str | None]:
    """Đọc tài khoản: ưu tiên biến môi trường (kể cả từ .env), rồi tới auth.json."""
    u, p = os.environ.get("SITE_USER"), os.environ.get("SITE_PASS")
    if u and p:
        return u, p
    if AUTH_FILE.exists():
        try:
            data = json.loads(AUTH_FILE.read_text(encoding="utf-8"))
            return data.get("username"), data.get("password")
        except Exception as e:
            print(f"[!] Lỗi đọc {AUTH_FILE}: {e}")
    return None, None


def _find_checkbox_in_image(image, regions=None) -> tuple[int, int, int, int] | None:
    """Nhận diện viền checkbox thật; loại các component tròn như spinner."""
    width, height = image.size
    pixels = image.load()
    if regions:
        bounds = []
        for left, top, region_w, region_h in regions:
            x0 = max(0, int(left))
            y0 = max(0, int(top))
            x1 = min(width, int(left + region_w))
            y1 = min(height, int(top + region_h))
            if x1 > x0 and y1 > y0:
                bounds.append((x0, y0, x1, y1))
    else:
        bounds = [(int(width * 0.05), int(height * 0.10),
                   int(width * 0.65), int(height * 0.70))]

    # Viền checkbox Turnstile là một component gần vuông 18-30px. Giới hạn vùng quét
    # vào widget challenge. Bốn cạnh/góc là điều kiện bắt buộc để spinner tròn dù có
    # cùng bounding box cũng không thể bị nhận nhầm thành checkbox.
    points = {
        (x, y)
        for x0, y0, x1, y1 in bounds
        for y in range(y0, y1)
        for x in range(x0, x1)
        # Ảnh thật của Turnstile có lõi viền khoảng gray=74 và anti-alias tới ~168.
        # Ngưỡng cũ bắt đầu ở 90 đã cắt rời lõi viền, khiến ô 24x24 không còn thành
        # một component kín. Các spinner vẫn bị loại bằng tỷ lệ bốn cạnh bên dưới.
        if 55 <= pixels[x, y] <= 210
    }
    candidates: list[tuple[int, int, int, int, int]] = []
    while points:
        start = points.pop()
        stack = [start]
        component = [start]
        while stack:
            x, y = stack.pop()
            for neighbor in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if neighbor in points:
                    points.remove(neighbor)
                    stack.append(neighbor)
                    component.append(neighbor)
        if not 30 <= len(component) <= 350:
            continue
        xs = [point[0] for point in component]
        ys = [point[1] for point in component]
        left, top = min(xs), min(ys)
        box_w, box_h = max(xs) - left + 1, max(ys) - top + 1
        if not (18 <= box_w <= 32 and 18 <= box_h <= 32 and abs(box_w - box_h) <= 3):
            continue

        component_set = set(component)
        # Với hình vuông, phần lớn pixel của component nằm sát một trong bốn cạnh
        # bounding box. Spinner tròn có cùng kích thước vẫn chỉ chạm bounding box ở
        # bốn cung ngắn nên tỷ lệ này thấp. Đây là tiêu chí chính để không bấm khi
        # biểu tượng còn đang quay.
        border_pixels = sum(
            1
            for x, y in component_set
            if min(
                x - left,
                left + box_w - 1 - x,
                y - top,
                top + box_h - 1 - y,
            ) <= 1
        )
        if border_pixels / len(component_set) < 0.70:
            continue

        band = 1
        top_x = {x for x, y in component_set if y <= top + band}
        bottom_x = {x for x, y in component_set if y >= top + box_h - 1 - band}
        left_y = {y for x, y in component_set if x <= left + band}
        right_y = {y for x, y in component_set if x >= left + box_w - 1 - band}
        if min(len(top_x) / box_w, len(bottom_x) / box_w,
               len(left_y) / box_h, len(right_y) / box_h) < 0.65:
            continue

        corner_size = 3
        corners = (
            (left, top),
            (left + box_w - corner_size, top),
            (left, top + box_h - corner_size),
            (left + box_w - corner_size, top + box_h - corner_size),
        )
        if any(not any(
            cx <= x < cx + corner_size and cy <= y < cy + corner_size
            for x, y in component_set
        ) for cx, cy in corners):
            continue

        score = abs(box_w - 24) + abs(box_h - 24)
        candidates.append((score, left, top, box_w, box_h))

    if not candidates:
        return None
    _, left, top, box_w, box_h = min(candidates)
    return left, top, box_w, box_h


def _turnstile_regions(page) -> list[tuple[float, float, float, float]]:
    """Lấy vùng iframe để thu hẹp nhận diện; không dùng vùng này làm tọa độ click."""
    regions = []
    try:
        frames = [
            frame for frame in page.frames
            if "challenges.cloudflare.com" in (frame.url or "")
        ]
    except Exception:
        return regions
    for frame in frames:
        try:
            bb = frame.frame_element().bounding_box()
            if bb:
                regions.append((bb["x"], bb["y"], bb["width"], bb["height"]))
        except Exception:
            continue
    return regions


def _find_turnstile_checkbox(page) -> tuple[int, int, int, int] | None:
    """Tìm ô vuông checkbox trên screenshot, kể cả iframe dùng closed shadow DOM."""
    try:
        from io import BytesIO
        from PIL import Image

        screenshot = page.screenshot()
        image = Image.open(BytesIO(screenshot)).convert("L")
    except Exception as exc:
        print(f"[!] Không chụp/đọc được ảnh challenge Cloudflare: {exc}")
        return None
    regions = _turnstile_regions(page)

    # Ảnh chẩn đoán một lần, chỉ khi operator chủ động cấu hình đường dẫn. Mở bằng
    # mode "xb" để hai lane/những lần retry sau không ghi đè hay tạo ảnh liên tục.
    debug_path = os.environ.get("SITE_CF_DEBUG_SCREENSHOT", "").strip()
    if debug_path:
        try:
            destination = Path(debug_path).expanduser()
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("xb") as handle:
                handle.write(screenshot)
            print(f"[i] Đã lưu ảnh chẩn đoán Cloudflare: {destination}; "
                  f"image={image.size}, iframe_regions={regions}")
        except FileExistsError:
            pass
        except OSError as exc:
            print(f"[!] Không lưu được ảnh chẩn đoán Cloudflare: {exc}")

    return _find_checkbox_in_image(image, regions or None)


def _click_turnstile_with_camoufox(page, x: float, y: float, source: str) -> bool:
    """Click một lần qua Mouse API để Camoufox tự sinh toàn bộ quỹ đạo C++.

    Không truyền ``steps``: từ bản 152.0.4-beta.28, mỗi mousemove đã được Camoufox
    humanize thành nhiều điểm. Thêm nội suy của Playwright lên trên sẽ tạo hai tầng
    chuyển động. Tách down/up để giữ nút một khoảng ngắn thay vì click tức thời.
    """
    button_down = False
    try:
        page.mouse.move(x, y)
        time.sleep(random.uniform(0.35, 0.70))
        page.mouse.down()
        button_down = True
        time.sleep(random.uniform(0.08, 0.15))
        page.mouse.up()
        button_down = False
        print(
            f"[i] Đã click checkbox Cloudflare bằng Camoufox ({source}) "
            f"tại ({x:.0f}, {y:.0f})."
        )
        return True
    except Exception as exc:
        if button_down:
            try:
                page.mouse.up()
            except Exception:
                pass
        print(f"[!] Click checkbox Cloudflare bằng Camoufox thất bại ({source}): {exc}")
        return False


def solve_turnstile(page, checkbox=None) -> bool:
    """Click checkbox đã được xác nhận bằng ảnh; không có fallback theo tọa độ."""
    checkbox = checkbox if checkbox is not None else _find_turnstile_checkbox(page)
    if checkbox is not None:
        left, top, box_w, box_h = checkbox
        # Chọn điểm khác nhau trong phần lõi của checkbox. Chừa ít nhất 4px quanh viền
        # để ngẫu nhiên hóa mà không bao giờ click lệch sang chữ hoặc mép widget.
        margin_x = max(4.0, box_w * 0.22)
        margin_y = max(4.0, box_h * 0.22)
        cx = random.uniform(left + margin_x, left + box_w - margin_x)
        cy = random.uniform(top + margin_y, top + box_h - margin_y)
        return _click_turnstile_with_camoufox(page, cx, cy, "ô vuông ổn định")
    return False


def _same_checkbox(first, second, tolerance: int = 2) -> bool:
    if first is None or second is None:
        return False
    return all(abs(a - b) <= tolerance for a, b in zip(first, second))


def _positive_float_setting(name: str, default: float) -> float:
    try:
        return max(0.1, float(os.environ.get(name, str(default))))
    except ValueError:
        return default


def _positive_int_setting(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


def handle_cloudflare(page, wait_s: float | None = None, reloads: int = 0) -> None:
    """Chỉ click khi ô vuông ổn định; spinner/unknown không bao giờ bị click.

    Watchdog đo thời gian không có *chuyển đổi trạng thái* có ý nghĩa. Nó chỉ quyết
    định thoát soft-block, tuyệt đối không kích hoạt click. Sau click đầu, widget phải
    qua ít nhất ba frame không-checkbox rồi hiện lại ô vuông ổn định mới được click lần
    hai. Điều này hỗ trợ chuỗi spinner -> checkbox -> spinner -> checkbox của Turnstile.
    """
    del reloads
    stalled_timeout = wait_s if wait_s is not None else _positive_float_setting(
        "SITE_CF_STALLED_TIMEOUT_SECONDS", 120.0
    )
    stable_frames_required = _positive_int_setting("SITE_CF_STABLE_FRAMES", 3)
    frame_interval = _positive_float_setting("SITE_CF_FRAME_INTERVAL_SECONDS", 0.5)
    raise_if_blocked(page)
    if not looks_like_cloudflare(page):
        return

    click_count = 0
    clear_since: float | None = None
    last_progress_at = time.time()
    last_visual_state = "waiting"
    stable_checkbox = None
    stable_checkbox_frames = 0
    non_checkbox_frames = 0
    reset_seen_after_click = False

    while time.time() - last_progress_at < stalled_timeout:
        if page.is_closed():
            raise AccessChallengeError("Browser đã đóng khi đang xử lý Cloudflare.")

        if not looks_like_cloudflare(page):
            # Challenge đôi khi đổi document/title trong chớp mắt rồi hiện lại. Chỉ
            # công nhận thành công khi trạng thái sạch giữ liên tục ba giây.
            clear_since = clear_since or time.time()
            if time.time() - clear_since >= 3:
                if click_count:
                    print(f"[✓] Cloudflare đã qua sau {click_count} lần click Camoufox.")
                return
            time.sleep(frame_interval)
            continue

        clear_since = None
        raise_if_blocked(page)
        checkbox = _find_turnstile_checkbox(page)
        if checkbox is None:
            stable_checkbox = None
            stable_checkbox_frames = 0
            non_checkbox_frames += 1
            if click_count and non_checkbox_frames >= stable_frames_required:
                reset_seen_after_click = True
        else:
            non_checkbox_frames = 0
            if _same_checkbox(stable_checkbox, checkbox):
                stable_checkbox_frames += 1
            else:
                stable_checkbox = checkbox
                stable_checkbox_frames = 1

        ready = stable_checkbox_frames >= stable_frames_required
        # Checkbox mới chỉ là tiến triển khi đã ổn định đủ số frame. Một nét vuông
        # chớp tắt hoặc detector dao động không được kéo dài watchdog vô hạn.
        if ready:
            visual_state = "checkbox"
        elif reset_seen_after_click:
            visual_state = "waiting_after_click"
        else:
            visual_state = "waiting"

        if visual_state != last_visual_state:
            # Spinner animation bên trong cùng trạng thái không reset watchdog. Chỉ
            # spinner/unknown <-> checkbox hoặc click mới là tiến triển có ý nghĩa.
            last_progress_at = time.time()
            last_visual_state = visual_state

        may_click = click_count == 0 or (click_count == 1 and reset_seen_after_click)
        if ready and may_click and click_count < 2 and solve_turnstile(page, stable_checkbox):
            click_count += 1
            last_progress_at = time.time()
            last_visual_state = "clicked"
            stable_checkbox = None
            stable_checkbox_frames = 0
            non_checkbox_frames = 0
            reset_seen_after_click = False
            print(f"[i] Đang chờ Cloudflare xác minh sau lần {click_count}/2; "
                  "spinner/unknown sẽ không bị click.")
        time.sleep(frame_interval)

    if looks_like_cloudflare(page):
        action = (f"đã click {click_count} lần nhưng chưa được xác nhận"
                  if click_count else "không tìm thấy checkbox")
        wait_for_human(
            page,
            f"Cloudflare {action} sau {stalled_timeout:.0f}s không tiến triển — "
            "kiểm tra trên cửa sổ Camoufox; code đã dừng tự click.",
        )
        if CI_MODE:
            return
        # Người vận hành có thể vừa thao tác xong trước khi bấm Enter; chờ chuyển trang.
        clear_since = None
        for _ in range(30):
            if not looks_like_cloudflare(page):
                clear_since = clear_since or time.time()
                if time.time() - clear_since >= 3:
                    return
            else:
                clear_since = None
            time.sleep(0.5)


def handle_access_challenge(page) -> None:
    """Chặn cứng -> dừng ngay; còn lại giữ cách xử lý Cloudflare hiện tại, sau đó giải
    CAPTCHA ảnh nếu site yêu cầu."""
    raise_if_blocked(page)
    handle_cloudflare(page)
    if looks_like_cloudflare(page):
        raise AccessChallengeError(f"Cloudflare vẫn chặn truy cập: {page.url}")
    if not looks_like_site_captcha(page):
        return
    if not solve_site_captcha(page):
        raise CaptchaBypassError(
            f"Không vượt được CAPTCHA ảnh site: {page.url}"
        )
    # CAPTCHA thường xuất hiện khi nhịp request vừa rồi đã quá dày.  Đừng chuyển
    # ngay sang thao tác/navigation tiếp theo khi vừa qua CAPTCHA: để trang ổn
    # định và cho server thấy một khoảng đọc thực tế trước request kế tiếp.
    cooldown = random.uniform(8.0, 14.0)
    print(f"[i] Đã qua CAPTCHA; nghỉ hồi nhịp {cooldown:.1f}s trước khi tiếp tục.")
    time.sleep(cooldown)
    # Sau khi submit CAPTCHA, site đôi khi chuyển tiếp qua một challenge Cloudflare mới
    # (hoặc chặn cứng luôn nếu submit sai nhiều lần -> handle_cloudflare tự dừng).
    handle_cloudflare(page)
    if looks_like_cloudflare(page):
        raise AccessChallengeError(
            f"Cloudflare chặn lại sau khi submit CAPTCHA ảnh site: {page.url}"
        )
    if looks_like_site_captcha(page):
        raise CaptchaBypassError(f"CAPTCHA ảnh site vẫn còn sau khi submit: {page.url}")


def goto_with_access_challenge(page, url: str, timeout_ms: int = 60000) -> None:
    """Điều hướng mà không để ``DOMContentLoaded`` chặn mất handler Cloudflare.

    Challenge ở trang chi tiết có thể đã vẽ checkbox nhưng cố ý chưa phát
    ``DOMContentLoaded``. Chờ ``commit`` trước, rồi luân phiên kiểm tra load-state và
    challenge để checkbox được xử lý ngay khi xuất hiện.
    """
    page.goto(url, wait_until="commit", timeout=timeout_ms)
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        raise_if_blocked(page)
        # Chỉ Cloudflare cần xử lý từ navigation commit vì interstitial của nó có
        # thể cố ý không phát DOMContentLoaded. CAPTCHA ảnh site phải chờ DOM xong,
        # nếu không URL đã đổi nhưng ảnh/ô nhập chưa tồn tại và OCR thoát giả.
        if looks_like_cloudflare(page):
            challenge_started = time.time()
            handle_cloudflare(page)
            # ``timeout_ms`` là ngân sách để trang thật tải, không phải ngân sách giải
            # Turnstile. Một chu kỳ reset có thể hợp lệ mất hơn 60 giây; nếu vẫn tính
            # thời gian đó vào deadline thì crawler báo timeout ngay sau khi vừa qua CF.
            deadline += time.time() - challenge_started
            if looks_like_cloudflare(page):
                raise AccessChallengeError(f"Cloudflare vẫn chặn truy cập: {page.url}")
            # Thời gian giải managed challenge (tối đa 50 giây) không được ăn vào
            # timeout chờ trang đích tải DOM. Nếu challenge vừa qua sau mốc 60 giây,
            # document site thường còn đang redirect và lần chờ DOM đầu tiên sẽ
            # timeout ngắn; cấp lại trọn timeout cho navigation hậu challenge.
            deadline = time.time() + timeout_ms / 1000
        try:
            page.wait_for_load_state("domcontentloaded", timeout=1000)
            # Challenge có thể xuất hiện đúng lúc load-state vừa hoàn tất.
            handle_access_challenge(page)
            return
        except PWTimeout:
            continue
    raise AccessChallengeError(
        f"Trang chưa tải xong và không vượt được challenge sau {timeout_ms}ms: {page.url}"
    )


def is_logged_in(page) -> bool:
    """Đã đăng nhập khi (đã xác thực trên site): có link tới 'logout.aspx' đang HIỂN THỊ,
    hoặc nhãn 'Tài khoản :' hiển thị ở header. Lưu ý link đăng xuất ghi là 'Thoát'."""
    try:
        return bool(page.evaluate("""() => {
            const hasLogout = [...document.querySelectorAll('a[href*="logout"]')]
                .some(e => e.offsetParent);
            const hasAcc = [...document.querySelectorAll('span,div')]
                .some(e => e.offsetParent && /tài khoản\\s*:/i.test(e.innerText || ''));
            return hasLogout || hasAcc;
        }"""))
    except Exception:
        return False


def session_is_valid(page) -> bool:
    """Xác minh phiên trên một trang chung, không dựa riêng vào trang văn bản hiện tại.

    Một số văn bản site vẫn render ``Bạn Chưa Đăng Nhập Thành Viên`` và bỏ hẳn
    header tài khoản dù cùng phiên đó mở các văn bản khác bình thường. Vì vậy không
    được coi riêng DOM của văn bản ấy là bằng chứng tài khoản đã bị đá.
    """
    if is_logged_in(page):
        return True

    current_url = normalize_url(page.url)
    probe = page.context.new_page()
    try:
        goto_with_access_challenge(probe, START_URL)
        if not is_logged_in(probe):
            return False

        # Header trang chủ có thể vẫn dựa trên cookie cũ trong khi máy chủ văn bản
        # đã thu hồi quyền tải. Xác minh sâu bằng vài văn bản đã tải thành công trước
        # đây; một số VB cá biệt có thể luôn báo chưa đăng nhập nên cần hơn một mẫu.
        try:
            queue = load_queue()
        except Exception:
            queue = {}
        candidates = [
            url for url, state in queue.items()
            if state.get("status") == "done" and normalize_url(url) != current_url
        ][:SESSION_PROBE_DOCUMENTS]
        if not candidates:
            return True

        checked = 0
        for url in candidates:
            goto_with_access_challenge(probe, cache_bust_url(url))
            try:
                tab8_text = probe.evaluate("""() => {
                    const tab = document.querySelector('#tab8');
                    return tab ? (tab.innerText || '') : '';
                }""")
            except Exception:
                continue
            if not tab8_text:
                continue
            checked += 1
            if "chưa đăng nhập" not in tab8_text.lower():
                return True
        return True if checked == 0 else False
    finally:
        try:
            probe.close()
        except Exception:
            pass


def ensure_login(page, force: bool = False) -> bool:
    """Đảm bảo đã đăng nhập; ``force=True`` luôn tạo lại phiên sau khi bị đá ra."""
    if not force and is_logged_in(page):
        print("[✓] Đã đăng nhập sẵn (session cũ).")
        return True

    user, pwd = load_credentials()
    if not user or not pwd:
        print("[i] Chưa có tài khoản lưu sẵn (env SITE_USER/SITE_PASS hoặc auth.json).")
        wait_for_human(page, "Hãy ĐĂNG NHẬP TAY 1 lần trên cửa sổ Camoufox (session sẽ được lưu).")
        return is_logged_in(page)

    if force:
        print("[i] Phiên site đã hết/bị đá — đang đăng nhập lại bằng tài khoản đã lưu…")
    else:
        print("[i] Đang tự đăng nhập bằng tài khoản đã lưu…")
    goto_with_access_challenge(page, LOGIN_URL)
    if force:
        # Cookie cũ đôi khi làm /page/login.aspx redirect về trang chủ dù quyền tải
        # văn bản đã bị thu hồi. Logout rồi mở lại form để chắc chắn tạo phiên mới.
        login_field = page.locator("#UserName")
        try:
            has_login_form = bool(login_field.count() and login_field.first.is_visible())
        except Exception:
            has_login_form = False
        if not has_login_form:
            print("[i] Trang đăng nhập bị phiên cũ chuyển hướng — đang làm mới phiên site…")
            goto_with_access_challenge(page, f"{BASE}/logout.aspx")
            human_pause(1.0, 2.0)
            goto_with_access_challenge(page, LOGIN_URL)
    try:
        page.fill("#UserName", user, timeout=15000)
        human_pause()
        page.fill("#Password", pwd, timeout=15000)
        human_pause()
        page.click("#Button1")
        time.sleep(5)
        handle_access_challenge(page)
        dismiss_session_warning(page)     # bấm 'Đồng ý' nếu tài khoản chỉ cho 1 phiên
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
    except AccessChallengeError:
        raise
    except Exception as e:
        print(f"[!] Lỗi khi tự đăng nhập: {e}")

    if is_logged_in(page):
        print("[✓] Tự đăng nhập thành công.")
        return True
    report_problem("Đăng nhập thất bại (sai mật khẩu/OTP/khoá phiên?).")
    wait_for_human(page, "Tự đăng nhập chưa chắc thành công. Kiểm tra tay.")
    return is_logged_in(page)


def dismiss_session_warning(page) -> bool:
    """Nếu hiện cảnh báo 'Tài khoản đã đủ 1 người dùng cùng thời điểm' -> bấm 'Đồng ý'
    (phiên đăng nhập kia sẽ bị đá ra). Trả True nếu có bấm."""
    btn = page.locator("xpath=//button[normalize-space()='Đồng ý']")
    try:
        if btn.count() and btn.first.is_visible():
            print("[i] Có cảnh báo phiên -> bấm 'Đồng ý' (phiên khác sẽ bị đăng xuất).")
            btn.first.click()
            time.sleep(3)
            return True
    except Exception:
        pass
    return False


def normalize_url(url: str) -> str:
    """Bỏ tham số 'dll' (vd ?dll=true). URL có dll=true là chế độ 'direct download link'
    -> trang HIỆN BẢN CHƯA ĐĂNG NHẬP, không tải được. Bỏ đi thì trang nhận session."""
    parts = urlparse.urlsplit(url)
    q = [(k, v) for k, v in urlparse.parse_qsl(parts.query) if k.lower() != "dll"]
    return urlparse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlparse.urlencode(q), parts.fragment))


def cache_bust_url(url: str) -> str:
    """Tạo URL đọc mới sau đổi phiên, không để Firefox dùng HTML anonymous đã cache."""
    parts = urlparse.urlsplit(normalize_url(url))
    q = [(k, v) for k, v in urlparse.parse_qsl(parts.query)
         if k != "_session_ts"]
    q.append(("_session_ts", str(int(time.time() * 1000))))
    return urlparse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlparse.urlencode(q), parts.fragment))


def pick_download_link(page):
    """Tìm nút tải trong tab 'Tải về' theo thứ tự ưu tiên nhãn DOWNLOAD_LABELS."""
    for label in DOWNLOAD_LABELS:
        cand = page.locator(f'xpath=//a[normalize-space()="{label}"]')
        if cand.count():
            return cand.first, label
    return None, None


def has_file_server_disk_error(page) -> bool:
    """Nhận ra HTML lỗi từ server file site, không nhầm với lỗi ổ đĩa local.

    Một số URL tải của site redirect về ``files.<domain>/doc.aspx`` (FILE_SERVER_DOC_URL).
    Khi server đó không tạo được file, nó trả HTTP 200 với trang HTML mang thông
    báo "There is not enough space on the disk." thay vì phát browser download.
    Đây là lỗi tạm thời phía site; không nên chờ hết timeout hoặc đánh dấu URL
    là failed vĩnh viễn.
    """
    try:
        if FILE_SERVER_DOC_URL not in page.url.lower():
            return False
        text = page.evaluate(
            "() => (document.body && document.body.innerText || '').toLowerCase()")
        return FILE_SERVER_DISK_ERROR in str(text).lower()
    except Exception:
        return False


# Nhãn thuộc tính (tiếng Việt) -> khoá snake_case trong file .json.
META_MAP = {
    "Số hiệu": "so_hieu",
    "Loại văn bản": "loai_van_ban",
    "Nơi ban hành": "noi_ban_hanh",
    "Người ký": "nguoi_ky",
    "Ngày ban hành": "ngay_ban_hanh",
    "Ngày hiệu lực": "ngay_hieu_luc",
    "Ngày công báo": "ngay_cong_bao",
    "Số công báo": "so_cong_bao",
    "Tình trạng": "tinh_trang",
}


def extract_metadata(page, url: str) -> dict:
    """Trích thuộc tính văn bản từ trang chi tiết (bảng 2 cột nhãn:giá trị)."""
    try:
        raw = page.evaluate(r"""(labels) => {
            const norm = s => (s||'').trim().replace(/\s+/g,' ').replace(/:$/,'').trim();
            const cells = [...document.querySelectorAll('td,th')];
            const out = {};
            for (let i=0;i<cells.length;i++){
                const k = norm(cells[i].innerText);
                if (labels.includes(k) && cells[i+1] && !out[k]) {
                    const v = norm(cells[i+1].innerText);
                    if (v) out[k]=v;
                }
            }
            out.__title = (document.title||'').replace(/\s+/g,' ').trim();
            return out;
        }""", list(META_MAP.keys()))
    except Exception:
        raw = {}

    meta = {"url": normalize_url(url), "doc_id": doc_id_from_url(url), "title": raw.get("__title", "")}
    for vi, key in META_MAP.items():
        meta[key] = raw.get(vi, "")
    return meta


def extract_scheme(page, url: str) -> dict:
    """Trích khu 'Lược đồ' (#tab4): văn bản liên quan, gom theo quan hệ CHUẨN HOÁ (key cố
    định trong LUOC_DO_KEYS, xem LUOC_DO_RELATION_MAP) chứ không dùng thẳng nhãn tiếng Việt
    thô của site — để code dùng về sau không bị lệch key giữa các lần chạy/site khác nhau.
    #tab4 rỗng (chỉ có loader) tới khi bấm #aLuocDo mới tải nội dung qua AJAX -> phải bấm
    rồi chờ '.ghd' xuất hiện. Chỉ lấy docID (số cuối URL) làm metadata, không lưu title/url.

    Trả về {key: [docID, ...]} với ĐỦ mọi key trong LUOC_DO_KEYS (kể cả [] nếu văn bản này
    không có quan hệ đó) để phía dùng dữ liệu có schema ổn định. Nhãn lạ chưa có trong
    LUOC_DO_RELATION_MAP: không bỏ mất — giữ dưới key 'unmapped:<nhãn>' + ghi log bền
    (UNKNOWN_LUOC_DO_LOG) + report_problem() 1 lần/nhãn để báo qua Telegram cho người vận
    hành quyết định xếp loại."""
    # Không truy cập tab "Lược đồ": đây là AJAX request phụ cho từng văn bản.
    # Vẫn trả đủ schema 19 key để metadata của các job mới tương thích tuyệt đối
    # với dữ liệu cũ.
    empty_schema: dict = {k: [] for k in LUOC_DO_KEYS}
    if SKIP_LUOC_DO:
        return empty_schema

    result: dict = empty_schema
    try:
        page.click("#aLuocDo", timeout=15000)
        page.wait_for_selector("#tab4 .ghd", timeout=15000)
    except Exception:
        return result

    try:
        raw = page.evaluate(r"""() => {
            const pane = document.getElementById('tab4');
            if (!pane) return {};
            const out = {};
            for (const hdr of pane.querySelectorAll('.ghd')) {
                const label = hdr.textContent.trim().replace(/\s+/g, ' ');
                const box = hdr.nextElementSibling;
                if (!box) continue;
                const seen = new Set();
                const ids = [];
                for (const dgc of box.querySelectorAll('.dgc')) {
                    const a = dgc.querySelector('div > a[href*="/van-ban/"]');
                    if (!a) continue;
                    const m = a.href.match(/-(\d+)\.aspx/i);
                    if (!m || seen.has(m[1])) continue;
                    seen.add(m[1]);
                    ids.push(m[1]);
                }
                out[label] = ids;   // giữ cả rỗng; lọc + chuẩn hoá key ở phía Python
            }
            return out;
        }""")
    except Exception:
        raw = {}

    for raw_label, ids in raw.items():
        if not ids:
            continue
        key = _luoc_do_key(raw_label, url)
        if key is None:
            continue
        bucket = result.setdefault(key, [])
        bucket.extend(i for i in ids if i not in bucket)
    return result


def doc_id_from_url(url: str) -> str:
    m = re.search(r"-(\d+)\.aspx", url)
    return m.group(1) if m else ""


def date_folder(ngay_ban_hanh: str | None) -> str:
    """'dd/MM/yyyy' -> 'yyyy-mm-dd' để gom thư mục. Không đọc được -> 'khong-ro-ngay'."""
    m = re.match(r"\s*(\d{1,2})/(\d{1,2})/(\d{4})", ngay_ban_hanh or "")
    if not m:
        return "khong-ro-ngay"
    d, mo, y = m.groups()
    return f"{y}-{int(mo):02d}-{int(d):02d}"


def find_previous_pdf_files(dest_dir: Path, doc_id: str) -> set[Path]:
    """Tìm PDF cũ của đúng văn bản trước khi metadata mới có thể ghi đè metadata cũ.

    Tên file site thường chứa thêm số hiệu, ví dụ ``67_NQ-CP_m_699021.pdf``, nên
    không thể giả định file có tên ``699021.pdf``. Ưu tiên metadata ``doc_id`` và
    dùng token số trong tên file làm fallback cho dữ liệu cũ thiếu metadata.
    """
    if not doc_id:
        return set()

    candidates: set[Path] = set()
    for meta_path in dest_dir.glob("*.json"):
        try:
            old_meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if str(old_meta.get("doc_id", "")) != doc_id:
            continue
        old_name = Path(str(old_meta.get("file", ""))).name
        old_file = dest_dir / old_name
        if old_file.suffix.lower() == ".pdf":
            candidates.add(old_file)

    # Fallback cho dữ liệu cũ thiếu/hỏng metadata: safe_name của site đặt doc_id ở
    # cuối stem (``..._699021`` hoặc ``..._m_699021``). Không match số ở giữa tên
    # để tránh xóa nhầm khi doc_id vô tình trùng với số hiệu văn bản khác.
    doc_id_suffix = re.compile(rf"(?:^|_){re.escape(doc_id)}$")
    candidates.update(
        old_pdf for old_pdf in dest_dir.glob("*.pdf")
        if doc_id_suffix.search(old_pdf.stem)
    )
    return candidates


def remove_previous_pdf_files(candidates: set[Path], keep_meta: Path) -> list[str]:
    """Xóa các PDF đã xác định sau khi Word + metadata mới lưu thành công."""
    removed: list[str] = []
    for old_pdf in sorted(candidates):
        if old_pdf.exists():
            old_pdf.unlink()
            removed.append(old_pdf.name)
        old_meta_path = old_pdf.with_suffix(".json")
        if old_meta_path != keep_meta and old_meta_path.exists():
            old_meta_path.unlink()
            removed.append(old_meta_path.name)
    return removed


def download_one(page, url: str, mode: str, selector: str | None, timeout_ms: int,
                 retry_pdf: bool = False, _challenge_retry: bool = False,
                 _session_retry: bool = False) -> str:
    """Tải một URL. Trả 'done' (Word/bất kỳ), 'pdf' (chỉ có PDF gốc), hoặc 'failed'.
    retry_pdf=True: đây là lần re-check entry pdf_only — nếu site vẫn chỉ có PDF thì
    KHÔNG tải lại (đã có rồi), chỉ trả 'pdf'; khi tải được Word thì xóa PDF cũ."""
    target_url = cache_bust_url(url) if _session_retry else normalize_url(url)
    goto_with_access_challenge(page, target_url)   # bỏ ?dll=true; đổi URL sau relogin
    human_pause(0.8, 2.2)

    # Trích metadata (Ngày ban hành dùng để gom thư mục; kèm file .json).
    meta = extract_metadata(page, url)
    meta["luoc_do"] = extract_scheme(page, url)   # văn bản liên quan theo quan hệ (#tab4)

    # Mở tab 'Tải về' (xử lý client-side, không phải link) rồi mới có nút tải.
    try:
        page.click("#aTabTaiVe", timeout=15000)
        human_pause(1.0, 2.0)
        handle_access_challenge(page)
    except AccessChallengeError:
        raise
    except Exception:
        print("[!] Không thấy tab 'Tải về' (#aTabTaiVe) trên trang này.")

    # Chỉ lấy text primitive trong browser, tránh locator.count() phải serialize DOM node
    # qua browser protocol và có thể lỗi "CBOR: stack limit exceeded" trên DOM phức tạp.
    try:
        tab8_text = page.evaluate("""() => {
            const tab = document.querySelector('#tab8');
            return tab ? (tab.innerText || '') : '';
        }""")
    except Exception as exc:
        print(f"[!] Không kiểm tra được #tab8, bỏ qua kiểm tra đăng nhập: {exc}")
        tab8_text = ""

    if "chưa đăng nhập" in tab8_text.lower():
        # Thử chính văn bản bằng URL mới trước. Phần lớn trường hợp là HTML anonymous
        # trong cache Firefox; làm vậy tránh mở thêm 1-5 văn bản probe và giảm nguy cơ
        # Site bật CAPTCHA do tần suất request.
        if not _session_retry:
            print(
                "[i] Trang báo chưa đăng nhập — tải lại chính văn bản bằng URL mới "
                "trước khi kiểm tra toàn bộ phiên."
            )
            return download_one(
                page, url, mode, selector, timeout_ms,
                retry_pdf=retry_pdf, _challenge_retry=_challenge_retry,
                _session_retry=True,
            )
        if session_is_valid(page):
            print(
                "[!] Riêng văn bản này vẫn báo chưa đăng nhập nhưng phiên site "
                "còn hiệu lực; bỏ qua link, không đăng nhập lại toàn bộ phiên."
            )
            return "failed"
        raise SessionExpiredError(
            f"Phiên site đã hết hoặc bị tài khoản khác đá ra tại: {url}"
        )

    selected_label: str | None = None
    if mode == "auto":
        if selector:
            el, label = page.locator(selector).first, selector
        else:
            el, label = pick_download_link(page)
            selected_label = label
        if el is None:
            print("[!] Không tìm thấy nút tải nào trong tab 'Tải về'.")
            return "failed"
        # Phải kiểm tra TRƯỚC expect_download; nếu return bên trong context manager,
        # Browser vẫn chờ một download không bao giờ xảy ra rồi timeout.
        if retry_pdf and selected_label == PDF_LABEL:
            print(f"    -> vẫn chỉ có PDF, chờ Word: {url}")
            return "pdf"

    def retry_after_download_challenge() -> str:
        print("[i] Nút tải gặp Cloudflare — đang giải challenge trước khi thử lại.")
        handle_access_challenge(page)
        if not _challenge_retry:
            return download_one(
                page, url, mode, selector, timeout_ms,
                retry_pdf=retry_pdf, _challenge_retry=True,
                _session_retry=_session_retry,
            )
        raise AccessChallengeError(
            f"Cloudflare xuất hiện lại sau khi thử tải lần hai: {page.url}"
        )

    try:
        downloads = []

        def capture_download(download) -> None:
            downloads.append(download)

        page.on("download", capture_download)
        try:
            if mode == "auto":
                print(f"    -> bấm '{label}'")
                el.scroll_into_view_if_needed(timeout=timeout_ms)
                el.hover(timeout=timeout_ms)
                human_pause(0.3, 1.0)
                # Link tải có thể redirect sang trang HTML lỗi của file server.
                # Không để Playwright tự chờ navigation; vòng phía dưới sẽ bắt
                # download thật hoặc nhận diện HTML lỗi ngay sau cú click.
                el.click(timeout=timeout_ms, no_wait_after=True)
            else:  # assist
                wait_for_human(page, f"Bấm nút tải trên trang:\n   {url}")

            deadline = time.time() + timeout_ms / 1000
            challenge_after_click = False
            file_server_error = False
            while not downloads and time.time() < deadline:
                if is_hard_blocked(page) or looks_like_cloudflare(page):
                    challenge_after_click = True
                    break
                if has_file_server_disk_error(page):
                    file_server_error = True
                    break
                # wait_for_timeout tiếp tục bơm event Playwright để callback download chạy.
                page.wait_for_timeout(500)
        finally:
            try:
                page.remove_listener("download", capture_download)
            except Exception:
                pass

        if challenge_after_click:
            return retry_after_download_challenge()
        if file_server_error:
            print("[!] Server file site tạm thời không tạo được file (báo thiếu dung lượng); "
                  "giữ link để thử lại sau.")
            return "retry"
        if not downloads:
            # Phiên có thể bị phiên khác đá đúng lúc đang chờ response tải (như log
            # văn bản 51): trang chưa kịp hiện thông báo trong #tab8 nhưng header đã
            # mất trạng thái tài khoản. Chuyển sang nhánh đăng nhập lại thay vì chỉ
            # timeout rồi để văn bản kế tiếp mới phát hiện.
            if not session_is_valid(page):
                raise SessionExpiredError(
                    f"Phiên site mất trong lúc chờ file tải xuống tại: {url}"
                )
            print(f"[✗] Không bắt được file trong {timeout_ms}ms: {url}")
            return "failed"
        download = downloads[0]
        fname = safe_name(url, download.suggested_filename)

        # Gom theo Ngày ban hành: downloads/YYYY-MM-DD/<file> + <file>.json
        sub = date_folder(meta.get("ngay_ban_hanh"))
        dest_dir = OUT_DIR / sub
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / fname
        download.save_as(str(dest))
        # Với persistent Firefox context, file tạm do download manager tạo có thể
        # còn nằm lại sau save_as. Xóa đúng bản staging sau khi copy thành công để
        # không nhân đôi dung lượng theo số lượng văn bản.
        try:
            download.delete()
        except Exception as exc:
            print(f"[!] Không dọn được file tạm của browser: {exc}")

        is_pdf = selected_label == PDF_LABEL or fname.lower().endswith(".pdf")
        old_pdf_files = (
            find_previous_pdf_files(dest_dir, doc_id_from_url(url))
            if retry_pdf and not is_pdf else set()
        )

        meta["file"] = fname
        meta["downloaded_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        meta_path = dest.with_suffix(".json")
        meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

        # Chỉ dọn PDF cũ SAU KHI Word và metadata mới đã lưu thành công.
        if retry_pdf and not is_pdf:
            for old_name in remove_previous_pdf_files(
                    old_pdf_files, keep_meta=meta_path):
                print(f"    -> xóa file cũ: {old_name}")

        print(f"[✓] {url}\n      -> {dest}  (+ .json)")
        return "pdf" if is_pdf else "done"
    except PWTimeout:
        # Nút tải đôi khi điều hướng tab hiện tại sang challenge thay vì phát download.
        # Giải challenge tại đúng văn bản rồi thử lại trọn luồng đúng một lần.
        challenge_after_click = is_hard_blocked(page) or looks_like_cloudflare(page)
        if challenge_after_click:
            return retry_after_download_challenge()
        if not session_is_valid(page):
            raise SessionExpiredError(
                f"Phiên site mất trong lúc thao tác nút tải tại: {url}"
            )
        print(f"[✗] Không bắt được file trong {timeout_ms}ms: {url}")
        return "failed"


# ----- Hàng đợi (queue.json): kiểm soát dedup + trạng thái -----

def load_queue() -> dict:
    if QUEUE_FILE.exists():
        try:
            return json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
        except Exception:
            print(f"[!] {QUEUE_FILE} hỏng — coi như rỗng.")
    return {}


def save_queue(q: dict) -> None:
    QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Ghi sang file tạm cùng thư mục rồi replace nguyên tử. Nếu supervisor đọc
    # đúng lúc checkpoint, nó sẽ luôn thấy bản JSON cũ hoặc bản JSON mới hoàn
    # chỉnh, không bao giờ thấy khoảng trống do write_text() vừa truncate file.
    fd, temporary = tempfile.mkstemp(
        prefix=f".{QUEUE_FILE.name}.", suffix=".tmp", dir=QUEUE_FILE.parent,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(q, handle, ensure_ascii=False, indent=1)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, QUEUE_FILE)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def enqueue(urls: list[str], src_range: str) -> int:
    """Thêm URL mới vào hàng đợi (bỏ qua URL đã có = dedup). Trả số link mới thêm."""
    q = load_queue()
    today = time.strftime("%Y-%m-%d")
    added = 0
    for u in urls:
        if u not in q:
            q[u] = {"status": "pending", "added": today, "range": src_range}
            added += 1
    save_queue(q)
    return added


def _pdf_retry_due(v: dict) -> bool:
    """Trả True nếu entry 'pdf_only' còn trong hạn re-check (tuổi < PDF_RETRY_DAYS).
    Lỗi parse ngày -> True (an toàn, thà re-check thừa hơn bỏ sót)."""
    ts = v.get("added") or v.get("at") or ""
    try:
        fmt = "%Y-%m-%d %H:%M:%S" if " " in ts else "%Y-%m-%d"
        age_days = (time.time() - time.mktime(time.strptime(ts, fmt))) / 86400
        return age_days < PDF_RETRY_DAYS
    except Exception:
        return True


def prune_done(q: dict, days: int) -> tuple[dict, int]:
    """Xoá khỏi hàng đợi các URL 'done'/'pdf_only' đã quá `days` ngày — để queue.json
    không phình to vô hạn. 'done' tính theo 'at'; 'pdf_only' tính theo 'added' (vì 'at'
    bị bump mỗi lần retry). 'pending'/'failed' KHÔNG BAO GIỜ tự xoá. Ngày không parse
    được -> giữ lại cho an toàn."""
    cutoff = time.time() - days * 86400
    kept: dict = {}
    removed = 0
    for u, v in q.items():
        status = v.get("status")
        if status == "done":
            ts = v.get("at") or v.get("added") or ""
        elif status == "pdf_only":
            ts = v.get("added") or v.get("at") or ""
        else:
            kept[u] = v
            continue
        try:
            fmt = "%Y-%m-%d %H:%M:%S" if " " in ts else "%Y-%m-%d"
            if time.mktime(time.strptime(ts, fmt)) < cutoff:
                removed += 1
                continue
        except Exception:
            pass
        kept[u] = v
    return kept, removed


# ----- Số liệu THẬT cho báo cáo (last-summary.json) -----

def queue_stats() -> dict:
    q = load_queue()
    st = {"total": len(q), "pending": 0, "done": 0, "failed": 0, "pdf_only": 0}
    for v in q.values():
        s = v.get("status")
        if s in st:
            st[s] += 1
    return st


def downloads_stats() -> dict:
    """Đếm số file văn bản (.docx/.doc/.pdf) theo từng thư mục ngày trong downloads/."""
    stats = {}
    if OUT_DIR.exists():
        for sub in sorted(OUT_DIR.iterdir()):
            if sub.is_dir():
                n = sum(1 for f in sub.iterdir()
                        if f.suffix.lower() in (".docx", ".doc", ".pdf"))
                if n:
                    stats[sub.name] = n
    return stats


def write_summary(data: dict) -> None:
    """Ghi số liệu thật của lần chạy ra logs/last-summary.json để AI báo cáo dựa vào."""
    try:
        LOGS_DIR.mkdir(exist_ok=True)
        (LOGS_DIR / "last-summary.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[!] Không ghi được last-summary.json: {e}")


def cmd_preflight() -> None:
    """Kiểm tra profile và đăng nhập đúng credential, không collect/download.

    Preflight luôn ép login lại sau khi mở trang login.  Vì vậy một profile còn
    session của tài khoản khác sẽ không được coi là hợp lệ chỉ vì header đang có
    link logout.  Kết quả xác nhận chỉ được ghi bởi launcher bên ngoài.
    """
    user, pwd = load_credentials()
    if not user or not pwd:
        raise RuntimeError("Preflight thiếu SITE_USER/SITE_PASS trong crawler.env")

    print(f"[i] PREFLIGHT: kiểm tra đăng nhập cho tài khoản {user}")
    with browser_context(headless=False) as ctx:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        goto_with_access_challenge(page, START_URL)
        if not ensure_login(page, force=True):
            raise RuntimeError("Preflight đăng nhập thất bại")
        if not is_logged_in(page):
            raise RuntimeError("Preflight chưa xác nhận trạng thái đã đăng nhập")
    print("[✓] PREFLIGHT LOGIN OK")


# ----- Cấu hình ngày & thu thập listing -----

def resolve_dates(frm: str | None, to: str | None, days_back: int = 0) -> tuple[str, str]:
    """Chuẩn hoá From-To (dd/MM/yyyy). None hoặc 'today' -> ngày hôm nay (giờ VN).

    days_back > 0 (VÀ không truyền --from): quét CỬA SỔ TRƯỢT N ngày gần nhất
    -> bdate = hôm nay - (N-1) ngày, edate = hôm nay (đủ N ngày tính cả hai đầu).
    Dùng cho cron: văn bản ban hành
    ngày D có thể vài ngày sau mới lên site, nên quét lùi lại để không bỏ sót
    (queue.json dedup nên VB đã tải không tải lại). --from tường minh LUÔN thắng
    days_back (giữ nguyên chế độ backfill 1 lần theo khoảng ngày cụ thể)."""
    today_d = date.today()                       # máy ở Asia/Ho_Chi_Minh
    today = today_d.strftime("%d/%m/%Y")

    def norm(x):
        return today if (not x or x.strip().lower() == "today") else x.strip()

    edate = norm(to)
    if frm is None and days_back > 0:
        bdate = (today_d - timedelta(days=days_back - 1)).strftime("%d/%m/%Y")
    else:
        bdate = norm(frm)
    return bdate, edate


def extract_detail_urls(page) -> list[str]:
    """Lấy URL trang chi tiết CHỈ trong khối kết quả tìm kiếm '#block-info-advan'
    (mỗi kết quả là p.nqTitle > a). Bỏ query, dedup. Quan trọng: KHÔNG quét toàn trang
    vì popup tour/banner thông báo/'văn bản liên quan' cũng chứa link /van-ban/ -> nếu
    quét cả trang sẽ vơ nhầm (vd trang 0 kết quả vẫn ra Luật Đất đai...). Khối rỗng ->
    trả [] (đúng với 'Không tìm thấy văn bản nào')."""
    try:
        return page.evaluate("""() => {
            const box = document.querySelector('#block-info-advan')
                     || document.querySelector('#block-info');
            if (!box) return [];
            const s = new Set();
            for (const a of box.querySelectorAll("a[href*='/van-ban/']")) {
                const h = a.href.split('?')[0].split('#')[0];
                if (/\\/van-ban\\/.+\\.aspx$/i.test(h)) s.add(h);
            }
            return [...s];
        }""")
    except Exception:
        return []


def last_page_number(page) -> int:
    """Mốc trang lớn nhất *đang hiện* trong pager, chỉ dùng để hiển thị.

    Site dùng pager trượt: trên trang đầu chỉ hiện một cụm (thường tới ``page=11``),
    dù còn nhiều trang kết quả.  Vì vậy giá trị này tuyệt đối không được dùng để
    quyết định điểm dừng của việc thu thập.
    """
    try:
        nums = page.evaluate("""() => {
            const out = [];
            for (const a of document.querySelectorAll('.cmPager a')) {
                const m = (a.getAttribute('href') || '').match(/[?&]page=(\\d+)/i);
                if (m) out.push(parseInt(m[1], 10));
            }
            return out;
        }""")
        return max(nums) if nums else 1
    except Exception:
        return 1


def collect_listing(page, bdate: str, edate: str, max_pages: int,
                    min_d: float, max_d: float, checkpoint=None,
                    start_page: int = 1) -> list[str]:
    """Duyệt listing theo khoảng ngày, qua từng trang, gom URL chi tiết (dedup).

    Không tin ``page`` lớn nhất trong pager vì site chỉ hiển thị cửa sổ trang ngắn.
    Chỉ dừng khi trang không còn kết quả, khi server lặp lại toàn bộ trang cuối, hoặc
    khi chạm ``max_pages`` (cầu chì an toàn).

    Nếu có ``checkpoint``, gọi nó ngay sau mỗi trang có URL mới. Callback nhận batch
    URL mới của riêng trang đó và phải trả về số URL thực sự được thêm vào queue. Nhờ
    vậy CAPTCHA/lỗi ở trang sau không làm mất kết quả của các trang đã đọc.
    """
    # Mỗi trang listing là một navigation độc lập.  Nhịp cũ chỉ chờ 4--9 giây
    # giữa các URL, còn request đầu tiên được bắn ngay sau trang khởi động; đó là
    # một mẫu rất đều và dày, dễ làm site đưa CAPTCHA.  Giữ tối thiểu 10--18 giây
    # (hoặc giá trị lớn hơn do caller truyền), đồng thời cuộn/đọc trang trước khi
    # rời đi.  Các khoảng ngẫu nhiên này chỉ áp dụng cho pha collect, không làm
    # thay đổi tốc độ download do người vận hành đã cấu hình riêng.
    pause_min = max(10.0, min_d)
    pause_max = max(pause_min + 8.0, max_d)

    def read_then_pause(stage: str) -> None:
        human_scroll(page)
        delay = random.uniform(pause_min, pause_max)
        print(f"    …đọc trang và nghỉ {delay:.1f}s ({stage})")
        time.sleep(delay)

    base = SEARCH_TMPL.format(bdate=bdate, edate=edate)
    seen: set[str] = set()
    found: list[str] = []
    pager_hint = None
    full_page_size = 0
    p = start_page
    # Tránh bắn URL tìm kiếm đầu tiên ngay sau START_URL/Cloudflare challenge.
    read_then_pause("trước trang listing đầu tiên")
    while p <= max_pages:
        goto_with_access_challenge(page, f"{base}&page={p}")
        human_pause(1.0, 2.5)                    # đọc listing như người
        docs = extract_detail_urls(page)
        if not docs:
            break
        full_page_size = max(full_page_size, len(docs))
        if pager_hint is None:
            pager_hint = last_page_number(page)
            print(f"[i] {bdate} → {edate}: pager đang hiển thị tới trang "
                  f"{pager_hint}; tiếp tục tới khi hết kết quả.")
        new = [u for u in docs if u not in seen]
        seen.update(docs)
        found.extend(new)
        print(f"    trang {p}: {len(docs)} VB ({len(new)} mới) — tổng {len(found)}")
        if new and checkpoint is not None:
            added = checkpoint(new)
            print(f"    [✓] Đã checkpoint trang {p}: +{added} queue"
                  + (f", {len(new) - added} trùng" if added != len(new) else ""))
        # Khi site clamp page vượt trang cuối về trang cuối, toàn bộ URL đã thấy.
        # Dừng ở đây thay vì tin pageMax/pager và thay vì quét đến max_pages.
        if not new:
            print(f"[i] Trang {p} không có URL mới; đã tới cuối kết quả.")
            break
        # Listing site hiện trả 20 kết quả trên các trang đầy. Khi đã thấy một
        # trang 20 kết quả mà trang hiện tại ngắn hơn, đây là trang cuối thật.
        # Dừng ngay sau checkpoint thay vì gọi page+1 chỉ để phát hiện trang
        # cuối; request thừa đó dễ gặp Cloudflare mà không mang thêm URL nào.
        if full_page_size >= 20 and len(docs) < full_page_size:
            print(f"[i] Trang {p} chỉ có {len(docs)}/{full_page_size} VB; đã tới cuối kết quả.")
            break
        p += 1
        read_then_pause("trước trang listing kế tiếp")
    if p > max_pages:
        print(f"[!] Đã chạm giới hạn an toàn --max-pages={max_pages}.")
    return found


def download_queue(page, args) -> tuple[int, int, int, str]:
    """Tải queue và trả (ok, fail, pdf_n, stop_reason)."""
    q = load_queue()
    # Ưu tiên lượng công việc mới. Link từng lỗi hoặc chỉ có PDF vẫn được thử lại,
    # nhưng không được làm nghẽn toàn bộ pending khi server file site đang chập chờn.
    pending = [u for u, v in q.items() if v.get("status") == "pending"]
    failed = [u for u, v in q.items() if v.get("status") == "failed"]
    retry_pdf = [u for u, v in q.items()
                 if v.get("status") == "pdf_only" and _pdf_retry_due(v)]
    todo = pending + failed + retry_pdf
    if args.max_downloads:
        todo = todo[:args.max_downloads]
    pdf_only_n = sum(1 for u in todo if q[u].get("status") == "pdf_only")
    print(f"\n[i] Hàng đợi: {len(todo)} link cần tải "
          f"(tổng {len(q)}, pdf_only re-check {pdf_only_n}, "
          f"xong {sum(1 for v in q.values() if v.get('status')=='done')}).")

    ok_n = fail_n = pdf_n = 0
    cf_streak = 0            # số link liên tiếp bị Cloudflare challenge
    captcha_streak = 0       # số link liên tiếp bị CAPTCHA ảnh riêng của site
    file_server_error_streak = 0
    file_server_retry_n = 0
    relogin_attempts = 0
    stop_downloads = False
    stop_reason = "completed_pass"
    for i, url in enumerate(todo, 1):
        print(f"\n--- [{i}/{len(todo)}] {url}")
        is_retry = (q.get(url, {}).get("status") == "pdf_only")
        session_retry = False
        while True:
            try:
                res = download_one(
                    page, url, args.mode, args.download_selector, args.timeout,
                    retry_pdf=is_retry, _session_retry=session_retry,
                )
                cf_streak = 0
                captcha_streak = 0
                break
            except SessionExpiredError as exc:
                # Mất đăng nhập là lỗi của cả phiên, không phải lỗi của văn bản. Không
                # được đánh dấu link failed rồi tiếp tục đốt toàn bộ queue.
                if relogin_attempts >= MAX_RELOGINS_PER_RUN:
                    report_problem(
                        f"Đã đăng nhập lại {relogin_attempts} lần trong một run nhưng vẫn "
                        "bị đá; dừng pha tải để tránh hai phiên giành tài khoản liên tục."
                    )
                    stop_downloads = True
                    stop_reason = "session_error"
                    break

                relogin_attempts += 1
                print(
                    f"[!] {exc}\n"
                    f"[i] Đăng nhập lại site ({relogin_attempts}/{MAX_RELOGINS_PER_RUN}) "
                    "rồi thử lại đúng văn bản này."
                )
                try:
                    login_ok = ensure_login(page, force=True)
                except AccessBlockedError:
                    raise
                except AccessChallengeError as login_exc:
                    report_problem(
                        "Không thể đăng nhập lại vì challenge chưa qua; dừng pha tải, "
                        f"giữ nguyên queue. ({login_exc})"
                    )
                    stop_downloads = True
                    stop_reason = "session_error"
                    break
                if not login_ok:
                    report_problem(
                        "Đăng nhập lại site thất bại; dừng pha tải, giữ nguyên link hiện "
                        "tại và các link còn lại."
                    )
                    stop_downloads = True
                    stop_reason = "session_error"
                    break

                print(f"[✓] Đăng nhập lại thành công — thử lại [{i}/{len(todo)}].")
                session_retry = True
                human_pause(2.0, 4.0)
                continue
            except AccessBlockedError:
                # Chặn cứng = vấn đề của cả phiên, không phải của riêng link này -> dừng hẳn,
                # để lớp ngoài báo "cần nghỉ". Link vẫn ở pending/failed nên lần sau tự thử lại.
                raise
            except CaptchaBypassError as e:
                captcha_streak += 1
                cf_streak = 0
                print(
                    f"[!] Bỏ qua link này do CAPTCHA ảnh site chưa qua "
                    f"({captcha_streak} link liên tiếp): {e}"
                )
                res = "failed"
                if captcha_streak >= CAPTCHA_STREAK_ABORT:
                    report_problem(
                        f"{captcha_streak} link LIÊN TIẾP bị CAPTCHA ảnh site -> dừng "
                        "pha tải sớm; các link còn lại giữ nguyên cho lần chạy sau."
                    )
                    q = load_queue()
                    q.setdefault(url, {})
                    q[url]["status"] = "failed"
                    q[url]["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                    save_queue(q)
                    fail_n += 1
                    stop_downloads = True
                    stop_reason = "captcha_streak"
                    break
                time.sleep(random.uniform(args.min_delay * 3, args.max_delay * 3))
                break
            except AccessChallengeError as e:
                # Cloudflare ở MỘT trang chi tiết: đánh dấu riêng link này failed,
                # nghỉ dài hơn cho site hạ nhiệt, rồi đi tiếp.
                cf_streak += 1
                captcha_streak = 0
                print(
                    f"[!] Bỏ qua link này do challenge chưa qua "
                    f"({cf_streak} link liên tiếp): {e}"
                )
                res = "failed"
                if cf_streak >= CF_STREAK_ABORT:
                    report_problem(
                        f"{cf_streak} link LIÊN TIẾP bị Cloudflare challenge -> dừng pha tải "
                        "sớm (site đang siết); các link còn lại giữ nguyên cho lần chạy sau."
                    )
                    q = load_queue()
                    q.setdefault(url, {})
                    q[url]["status"] = "failed"
                    q[url]["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                    save_queue(q)
                    fail_n += 1
                    stop_downloads = True
                    stop_reason = "soft_cloudflare_streak"
                    break
                time.sleep(random.uniform(args.min_delay * 3, args.max_delay * 3))
                break

        if stop_downloads:
            break

        if res == "retry":
            # HTML "not enough space" là phản hồi của server site, không phải
            # download failure trên máy local. Không đổi status/at để link còn
            # nguyên pending (hoặc failed cũ) cho lần retry sau.
            file_server_error_streak += 1
            file_server_retry_n += 1
            print("    -> giữ nguyên trạng thái queue; chuyển sang văn bản kế tiếp.")
            if file_server_error_streak >= 5:
                report_problem(
                    "Server file site trả lỗi thiếu dung lượng ở 5 văn bản liên tiếp; "
                    "dừng sớm và giữ nguyên các link còn lại để thử lại sau."
                )
                stop_reason = "file_server_streak"
                break
            delay = random.uniform(args.min_delay, args.max_delay)
            print(f"    …nghỉ {delay:.1f}s")
            time.sleep(delay)
            continue

        file_server_error_streak = 0

        q = load_queue()                          # nạp lại phòng chạy song song
        q.setdefault(url, {})
        if is_retry:
            q[url]["status"] = "done" if res == "done" else "pdf_only"
        else:
            q[url]["status"] = {"done": "done", "pdf": "pdf_only", "failed": "failed"}[res]
        q[url]["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        save_queue(q)
        if res == "done":
            ok_n += 1
        elif res == "pdf":
            pdf_n += 1
        else:
            fail_n += 1
        delay = random.uniform(args.min_delay, args.max_delay)
        print(f"    …nghỉ {delay:.1f}s")          # request thật từ từ như người
        time.sleep(delay)

    if file_server_retry_n:
        report_problem(
            f"{file_server_retry_n} link nhận lỗi tạm thời từ server file site; "
            "đã giữ nguyên queue để retry."
        )
    if fail_n:
        report_problem(f"Có {fail_n}/{len(todo)} link tải THẤT BẠI (status=failed).")
    return ok_n, fail_n, pdf_n, stop_reason


def cmd_run(args) -> None:
    global RUN_STOP_REASON
    bdate, edate = resolve_dates(args.from_, args.to, args.days_back)
    rng = f"{bdate}..{edate}"
    mode = ("collect-only" if args.collect_only
            else "download-only" if args.download_only else "full")

    # Số liệu thật của lần chạy -> logs/last-summary.json (AI báo cáo dựa vào, không suy diễn).
    summary = {
        "run_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "range": rng, "mode": mode,
        "logged_in": None, "collect": None, "download": None,
        "stop_reason": None,
    }
    try:
        with browser_context(
                headless=args.headless,
                downloads_path=None if args.collect_only else download_staging_dir(),
        ) as ctx:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            # Vào trang gốc -> qua Cloudflare.
            goto_with_access_challenge(page, START_URL)

            # Đăng nhập CHỈ khi sẽ tải. --collect-only KHÔNG cần login -> bỏ qua để
            # tránh fail giả và tránh 'đá' phiên đăng nhập vô ích. logged=None = chưa kiểm tra.
            logged = None
            if not args.collect_only:
                logged = ensure_login(page)
            summary["logged_in"] = logged

            # PHA 1: thu thập link (trừ khi --download-only) — không cần đăng nhập.
            if not args.download_only:
                print(f"\n===== THU THẬP LINK: {rng} =====")
                # Ghi ngay sau từng trang, thay vì chờ quét xong cả khoảng ngày. Nếu
                # Site ném CAPTCHA ở trang sau thì các trang trước vẫn ở queue để
                # chạy --download-only hoặc collect lại (dedup) vào lần sau.
                collect = {"found": 0, "new": 0, "duplicate": 0,
                           "pages_checkpointed": 0, "complete": False}
                summary["collect"] = collect

                def checkpoint_page(urls: list[str]) -> int:
                    added = enqueue(urls, rng)
                    collect["found"] += len(urls)
                    collect["new"] += added
                    collect["duplicate"] += len(urls) - added
                    collect["pages_checkpointed"] += 1
                    return added

                urls = collect_listing(page, bdate, edate, args.max_pages,
                                       args.min_delay, args.max_delay,
                                       checkpoint=checkpoint_page,
                                       start_page=args.start_page)
                # ``urls`` và số liệu checkpoint phải luôn khớp khi collect hoàn tất;
                # dùng số liệu checkpoint làm nguồn báo cáo để trường hợp lỗi giữa
                # chừng vẫn có số thật của các trang đã kịp lưu.
                collect["complete"] = True
                print(f"[✓] Thu thập {len(urls)} link; thêm mới {collect['new']} vào hàng đợi "
                      f"(còn lại là trùng, đã bỏ qua).")

            # PHA 2: tải chi tiết (trừ khi --collect-only) — cần đăng nhập.
            if not args.collect_only:
                print(f"\n===== TẢI CHI TIẾT =====")
                if not logged:
                    report_problem("Chưa đăng nhập -> bỏ qua pha tải (mọi link sẽ khoá).")
                    summary["download"] = {"ok": 0, "failed": 0, "skipped": True}
                    summary["stop_reason"] = "login_failed"
                else:
                    ok_n, fail_n, pdf_n, stop_reason = download_queue(page, args)
                    summary["download"] = {"ok": ok_n, "failed": fail_n, "pdf": pdf_n, "skipped": False}
                    summary["stop_reason"] = stop_reason

            print("\n[✓] Hoàn tất.")
    except AccessBlockedError as e:
        # Chặn cứng KHÔNG phải lỗi code -> đánh dấu riêng để lớp ngoài (run.sh/AI) biết
        # là chỉ cần nghỉ, đừng đi sửa crawler.py hay retry.
        summary["blocked"] = True
        summary["stop_reason"] = "hard_block"
        report_problem(str(e))
        raise
    except AccessChallengeError as e:
        # Managed challenge/Turnstile chưa qua không phải browser crash. Trả về
        # soft-block để supervisor cooldown lane này, giữ nguyên VPN/user/profile
        # rồi thử lại theo lịch thay vì exhaust sau một lần restart ngắn.
        summary["stop_reason"] = "soft_cloudflare_streak"
        report_problem(str(e))
    except Exception as e:
        summary["stop_reason"] = "exception"
        report_problem(f"Ngoại lệ khi chạy: {e}")
        raise
    finally:
        q, pruned_n = prune_done(load_queue(), QUEUE_PRUNE_DONE_DAYS)
        if pruned_n:
            save_queue(q)
            print(f"[i] Dọn hàng đợi: xoá {pruned_n} URL 'done' đã quá "
                  f"{QUEUE_PRUNE_DONE_DAYS} ngày.")
        summary["queue"] = queue_stats()
        summary["queue"]["pruned"] = pruned_n
        summary["downloads_by_date"] = downloads_stats()
        summary["problems"] = list(PROBLEMS)
        summary["result"] = "FAIL" if PROBLEMS else "OK"
        RUN_STOP_REASON = summary.get("stop_reason")
        write_summary(summary)


def main() -> None:
    load_dotenv()   # nạp cấu hình (.env): SITE_USER/SITE_PASS, TELEGRAM_*…

    # In log ra NGAY từng dòng (kể cả khi ghi ra file/pipe), khỏi bị gom buffer.
    # encoding="utf-8": BẮT BUỘC cho Windows — console/redirect ở đó mặc định là cp1252,
    # in chữ Việt hay ký tự '✓' là ném UnicodeEncodeError và CHẾT GIỮA CHỪNG (gặp thật
    # ngày 12/08/2026: run hỏng ngay khi in dòng [PROBLEM]). errors="replace" để dù gặp
    # ký tự lạ cũng chỉ hiện '?' chứ không bao giờ làm sập cả lần chạy vì một dòng log.
    try:
        sys.stdout.reconfigure(line_buffering=True, encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(line_buffering=True, encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser(
        description="Cào cổng văn bản nguồn theo ngày: thu thập link (phân trang, dedup) "
                    "rồi tải chi tiết. Mặc định = văn bản HÔM NAY; cron dùng --days-back "
                    "để quét cửa sổ trượt N ngày gần nhất (bắt cả VB đăng trễ).")
    # Cấu hình khoảng ngày (dd/MM/yyyy). Có From-To = cào lại quá khứ (1 lần).
    # Bỏ trống, hoặc 'today' = cào list của NGÀY HÔM ĐÓ.
    ap.add_argument("--from", dest="from_", default=None,
                    help="Ngày bắt đầu dd/MM/yyyy (hoặc 'today'). Bỏ trống = hôm nay.")
    ap.add_argument("--to", dest="to", default=None,
                    help="Ngày kết thúc dd/MM/yyyy (hoặc 'today'). Bỏ trống = hôm nay.")
    ap.add_argument("--collect-only", action="store_true", help="Chỉ thu thập link vào hàng đợi.")
    ap.add_argument("--download-only", action="store_true", help="Chỉ tải các link pending trong hàng đợi.")
    ap.add_argument("--queue-file", type=Path, default=None,
                    help="File queue riêng (mặc định: ./queue.json). Hữu ích khi backfill độc lập.")
    ap.add_argument("--out-dir", type=Path, default=None,
                    help="Thư mục lưu file tải riêng (mặc định: ./downloads).")
    ap.add_argument("--preflight", action="store_true",
                    help="Chỉ kiểm tra profile + đăng nhập; không đọc queue, collect hay download.")
    ap.add_argument("--max-pages", type=int, default=500, help="Giới hạn số trang listing (an toàn).")
    ap.add_argument("--start-page", type=int, default=1,
                    help="Bắt đầu collect từ trang N (mặc định 1), dùng để resume range đã checkpoint.")
    ap.add_argument("--days-back", type=int, default=0,
                    help="Quét CỬA SỔ TRƯỢT N ngày gần nhất (hôm nay-(N-1) .. hôm nay) khi KHÔNG "
                         "truyền --from — bắt cả văn bản đăng trễ. 0 = chỉ hôm nay (mặc định). "
                         "Cron nên dùng --days-back 15. --from tường minh luôn thắng cờ này.")

    ap.add_argument("--mode", choices=["auto", "assist"], default="auto",
                    help="auto: tự mở tab 'Tải về' & bấm nút tải; assist: bạn tự bấm.")
    ap.add_argument("--download-selector", default=None, help="CSS selector nút tải (mode=auto).")
    ap.add_argument("--headless", action="store_true",
                    help="Chạy ẩn (KHÔNG khuyến nghị: Cloudflare chặn headless; dùng xvfb-run thay thế).")
    ap.add_argument("--ci", action="store_true",
                    help="Chế độ tự động (cron): KHÔNG chờ người; gặp bí thì ghi lỗi & thoát mã != 0.")
    ap.add_argument("--timeout", type=int, default=50000, help="ms chờ 1 file (mặc định 50s).")
    ap.add_argument("--max-downloads", type=int, default=0,
                    help="Chỉ xử lý tối đa N link từ queue (0 = không giới hạn). Dùng để test an toàn.")
    ap.add_argument("--min-delay", type=float, default=4.0, help="nghỉ tối thiểu giữa request (s).")
    ap.add_argument("--max-delay", type=float, default=9.0, help="nghỉ tối đa giữa request (s).")

    args = ap.parse_args()
    configure_site()
    if args.days_back < 0:
        ap.error("--days-back phải lớn hơn hoặc bằng 0")
    if args.min_delay < 0 or args.max_delay < 0:
        ap.error("--min-delay/--max-delay không được âm")
    if args.min_delay > args.max_delay:
        ap.error("--min-delay không được lớn hơn --max-delay")
    if args.max_downloads < 0:
        ap.error("--max-downloads phải lớn hơn hoặc bằng 0")
    if args.start_page < 1:
        ap.error("--start-page phải lớn hơn hoặc bằng 1")

    # Không để một lần backfill vô tình dùng queue/downloads mặc định.  Với
    # --collect-only, OUT_DIR không được tạo; nó chỉ xuất hiện lúc thực sự lưu file.
    global QUEUE_FILE, OUT_DIR
    if args.queue_file is not None:
        QUEUE_FILE = args.queue_file.expanduser().resolve()
        if QUEUE_FILE.exists() and QUEUE_FILE.is_dir():
            ap.error(f"--queue-file phải là file, không phải thư mục: {QUEUE_FILE}")
    if args.out_dir is not None:
        OUT_DIR = args.out_dir.expanduser().resolve()
        if OUT_DIR.exists() and not OUT_DIR.is_dir():
            ap.error(f"--out-dir phải là thư mục: {OUT_DIR}")

    # Preflight là bước operator chủ động chạy và được phép chờ thao tác challenge.
    global CI_MODE
    CI_MODE = False if args.preflight else (args.ci or not sys.stdin.isatty())

    # Bọc toàn bộ: BỊ CHẶN -> mã 3; crash -> mã 2; có vấn đề -> mã 1; sạch -> mã 0.
    # Lớp ngoài (run.sh/cron) dựa vào mã thoát + dòng 'RESULT:' để quyết định gọi AI.
    try:
        if args.preflight:
            cmd_preflight()
        else:
            cmd_run(args)
    except AccessBlockedError as e:
        # Mã 3 = "site chặn, không phải lỗi code" -> run.sh chỉ báo cáo, KHÔNG gọi AI
        # đi sửa code (chẳng có gì để sửa) và KHÔNG chạy lại ngay.
        print(f"\n[✗] {e}")
        print("\nRESULT: BLOCKED | problems=1")
        sys.exit(3)
    except Exception:
        traceback.print_exc()
        print("\nRESULT: FAIL (exception) | problems=1")
        sys.exit(2)

    if RUN_STOP_REASON == "soft_cloudflare_streak":
        print(f"\nRESULT: SOFT_BLOCKED | problems={len(PROBLEMS)}")
        sys.exit(4)

    if PROBLEMS:
        print(f"\nRESULT: FAIL | problems={len(PROBLEMS)}")
        for p in PROBLEMS:
            print(f"  - {p}")
        sys.exit(1)

    print("\nRESULT: OK | problems=0")
    sys.exit(0)


if __name__ == "__main__":
    main()
