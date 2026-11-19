# Tech stack và các cơ chế xử lý

Tài liệu này mô tả kiến trúc kỹ thuật hiện tại của crawler văn bản
(cổng nguồn cấu hình qua `SITE_BASE_URL`), các thành phần vận hành và lý do tồn tại của những cơ chế quan trọng.
Hướng dẫn cài đặt và lệnh chạy nằm trong [`RUNBOOK.md`](RUNBOOK.md).

## 1. Mục tiêu

Crawler lấy văn bản site theo ngày ban hành, sau đó tải file tài liệu và metadata.
Luồng được thiết kế để chạy lặp lại an toàn: không tải trùng, có thể tiếp tục sau
lỗi, không cố retry dồn dập khi site hoặc Cloudflare đang chặn.

```text
Site search/listing
       │
       ▼
Thu thập URL ──► queue.json (dedup + trạng thái)
       │                       │
       └───────────────────────▼
                        Mở từng văn bản
                               │
                  đăng nhập / Cloudflare / CAPTCHA
                               │
                               ▼
                    tải Word → DOC → PDF fallback
                               │
                               ▼
             downloads/YYYY-MM-DD/ + file metadata .json
```

## 2. Tech stack

| Lớp | Công nghệ | Vai trò |
|---|---|---|
| Ngôn ngữ | Python 3.10+ (Docker dùng Python 3.12) | Logic crawler, queue, OCR, metadata |
| Browser | Camoufox 0.5.4 | Browser duy nhất; mở persistent profile, fingerprint Windows đồng bộ |
| Browser protocol | Playwright API đi kèm Camoufox | Điều khiển page, frame, download và timeout; không dùng Patchright |
| Hiển thị Linux | Xvfb / WSLg | Cung cấp giao diện headed cho browser khi server không có desktop |
| OCR CAPTCHA | Tesseract + Pillow + pytesseract | Đọc CAPTCHA ảnh của site |
| Đóng gói | Docker Compose, Debian Bookworm | Chạy nền trên server với môi trường tái lập được |
| Lập lịch | cron | Docker chạy 23:59 giờ Asia/Ho_Chi_Minh, quét cửa sổ 15 ngày |
| Thông báo | `notify.py`, Telegram Bot API | Gửi kết quả/lỗi từ `last-summary.json` |
| Hỗ trợ vận hành | Claude Code CLI (tùy chọn) | Đọc log và hỗ trợ chẩn đoán khi `run.sh` thất bại |
| Dữ liệu trạng thái | JSON trên filesystem | Queue, profile browser, log, summary và metadata tải về |

> Camoufox dùng Playwright như lớp API điều khiển của chính nó. Vì vậy có import
> `playwright.sync_api` trong mã, nhưng repo không có backend hay dependency Patchright.

## 3. Kiến trúc mã nguồn

| File/thư mục | Trách nhiệm |
|---|---|
| [`crawler.py`](crawler.py) | Toàn bộ crawl: browser, challenge, login, OCR CAPTCHA, queue, download, metadata và exit code |
| [`run.sh`](run.sh) | Wrapper vận hành: chọn display/Xvfb, tạo log, xử lý mã thoát, báo Telegram và gọi AI tùy cấu hình |
| [`notify.py`](notify.py) | Đọc summary và gửi Telegram |
| [`queue.json`](queue.json) | Hàng đợi URL với trạng thái `pending`, `failed`, `pdf_only`, `done` |
| [`downloads/`](downloads/) | File tải theo ngày ban hành và metadata `.json` đi kèm |
| [`logs/`](logs/) | Log từng run, `latest.log`, `last-summary.json`, `last-status.txt` |
| [`.camoufox_profile/`](.camoufox_profile/) | Cookie/session và fingerprint Camoufox cố định; không đưa lên git |
| [`.docker/`](.docker/) | Dockerfile, Compose, entrypoint và cron trong container |

## 4. Luồng chạy chi tiết

1. `run.sh` nạp `.env`, tạo file log và chạy `crawler.py --ci`. Nếu không có display thật,
   script dùng `xvfb-run`.
2. Crawler mở một Camoufox persistent context với profile tại
   `.camoufox_profile/browser` và fingerprint đã lưu trong `identity.json`.
3. Crawler vào trang tìm kiếm site, nhận diện chặn cứng, Cloudflare challenge hoặc CAPTCHA
   ảnh trước khi tiếp tục.
4. Pha thu thập duyệt phân trang, chuẩn hóa URL và merge vào `queue.json` để chống trùng.
5. Pha tải lần lượt xử lý URL `pending`/`failed` và URL PDF đến hạn kiểm tra lại. Thứ tự
   ưu tiên là Word (`.docx`), DOC rồi PDF gốc.
6. Mỗi download thành công được lưu vào `downloads/<ngày-ban-hành>/`; metadata trang và
   lược đồ quan hệ văn bản được lưu trong file `.json` cạnh tài liệu.
7. Crawler ghi `last-summary.json`; `run.sh` dựa vào exit code và summary để báo kết quả.

## 5. Các vấn đề đã giải quyết

### 5.1 Browser và fingerprint

**Vấn đề:** UA Linux hoặc các tín hiệu browser không đồng bộ dễ bị WAF đưa vào challenge
hoặc block.

**Giải pháp:** chỉ dùng Camoufox với cấu hình fingerprint Windows đồng bộ ở cấp browser.
Fingerprint được sinh một lần và lưu bền cùng profile; khi Camoufox đổi phiên bản, cấu hình
được sinh lại để version browser và UA không lệch nhau. Không dùng Chrome, Patchright,
flag giả User-Agent hay profile browser thứ hai.

### 5.2 Cloudflare challenge và block cứng

**Vấn đề:** challenge tương tác có checkbox; trong khi trang block cứng không có checkbox
để xử lý. Retry/reload liên tục làm tình trạng nặng hơn.

**Giải pháp:**

- Phân biệt `AccessChallengeError` với `AccessBlockedError` dựa trên title, URL, nội dung và
  Ray ID.
- Khi gặp challenge, crawler dùng logic Camoufox hiện có để chờ/giải challenge trong đúng
  browser context. Cookie `cf_clearance` được giữ trong `.camoufox_profile/`.
- Khi block cứng, crawler dừng sớm với exit code `3`, không tự retry, không gọi AI sửa code;
  URL chưa xử lý vẫn còn trong queue cho lượt sau.
- Challenge ở một link tải được retry trọn luồng một lần; nhiều link liên tiếp gặp challenge
  sẽ dừng pha tải để tránh tạo thêm request bất thường.

### 5.3 CAPTCHA ảnh của site

**Vấn đề:** trang CAPTCHA có thể render chậm; nếu OCR hoặc submit trước khi DOM sẵn sàng,
script dễ kết luận sai và refresh khi request vẫn đang xử lý.

**Giải pháp:** crawler chờ ảnh và ô nhập xuất hiện, OCR tối đa năm lần, rồi chờ trang chuyển
sau submit. CAPTCHA ảnh có bộ đếm lỗi liên tiếp riêng, không bị cộng nhầm vào ngưỡng
Cloudflare.

### 5.4 Mất phiên hoặc bị tài khoản khác đăng nhập

**Vấn đề:** Site chỉ cho một phiên tài khoản. Nếu bị đá ra giữa run, các văn bản phía sau đều
hiện “chưa đăng nhập” và trước đây có thể bị đánh dấu fail hàng loạt.

**Giải pháp:**

- Khi tab tải báo chưa đăng nhập, crawler trước hết mở lại chính URL với query cache-bust để
  loại trừ HTML anonymous còn trong cache.
- Sau đó kiểm tra phiên trên một số văn bản đã tải thành công. Nếu phiên thực sự mất, crawler
  mở lại login, có thể logout cookie cũ, rồi thử lại chính văn bản đang dở.
- Số lần đăng nhập lại bị giới hạn trong một run. Vượt ngưỡng thì dừng pha tải và giữ nguyên
  queue, tránh hai nơi dùng cùng tài khoản đá nhau vô hạn.

### 5.5 Bắt download ổn định

**Vấn đề:** nút tải có thể điều hướng sang Cloudflare thay vì phát download; hoặc download
xuất hiện trong event loop sau khi click.

**Giải pháp:** đăng ký listener download trước click, chờ theo deadline và liên tục kiểm tra
challenge/trạng thái phiên. Nếu challenge xuất hiện sau click thì giải ở đúng trang và thử lại
một lần; nếu không còn phiên thì chuyển sang cơ chế đăng nhập lại.

### 5.6 Queue, resume và PDF fallback

**Vấn đề:** chạy theo ngày hoặc cron dễ có văn bản đăng muộn, lỗi tạm thời hay trang chỉ có PDF.

**Giải pháp:**

- Queue merge theo URL, không reset giữa các lần chạy.
- `pending` và `failed` được thử lại trong các run sau; `done` cũ được prune theo thời hạn.
- Với tài liệu chỉ có PDF, trạng thái `pdf_only` được giữ và kiểm tra lại sau số ngày cấu hình,
  để tải Word nếu site bổ sung sau này mà không tải PDF trùng lặp.
- Cron quét cửa sổ trượt 15 ngày để bắt văn bản được site đăng trễ.

### 5.7 Quan hệ “Lược đồ” của văn bản

**Vấn đề:** nội dung lược đồ tải qua AJAX, nhãn quan hệ site có nhiều biến thể và có thể đổi.

**Giải pháp:** crawler bấm đúng tab theo ID, chờ pane AJAX, chuẩn hóa quan hệ thành key
snake_case cố định và lưu doc ID liên quan. Nhãn mới không bị bỏ mất: chúng được lưu trong
`logs/unknown_luoc_do_labels.json` để bổ sung mapping sau.

## 6. Vận hành và an toàn

- Chạy thực tế nên dùng headed browser qua WSLg hoặc Xvfb; `--headless` không được khuyến nghị.
- Không copy `.camoufox_profile/` giữa máy/server: cookie Cloudflare có thể gắn với IP đã giải
  challenge.
- Không chạy nhiều crawler đồng thời với cùng tài khoản site.
- Không xóa `queue.json` khi có lỗi; đây là điểm resume của hệ thống.
- `RESULT: BLOCKED`/exit code `3` là tín hiệu cooldown vận hành, không phải tín hiệu tự sửa
  selector hay tự tăng retry.
- `.env`, `auth.json`, profile browser, queue, downloads và logs là dữ liệu vận hành/bí mật,
  không commit lên git.

## 7. Mã thoát

| Mã | Ý nghĩa | Hành động wrapper |
|---:|---|---|
| `0` | Run hoàn tất sạch | Ghi trạng thái OK, có thể gửi báo cáo |
| `1` | Có vấn đề nghiệp vụ/challenge/CAPTCHA chưa qua | Ghi log, báo cáo và có thể gọi AI tùy cấu hình |
| `2` | Crash không dự kiến | Ghi log, báo cáo và có thể gọi AI tùy cấu hình |
| `3` | Cloudflare block cứng | Chỉ báo cáo, không retry và không gọi AI |

## 8. Điểm cấu hình quan trọng

- `.env`: `SITE_BASE_URL`, `SITE_USER`, `SITE_PASS`, cấu hình Telegram và các cờ `AI_*`.
- Đầu [`crawler.py`](crawler.py): nhãn download ưu tiên, delay, timeout, ngưỡng challenge,
  CAPTCHA, relogin và thời hạn retry PDF.
- [`.docker/crontab`](.docker/crontab): lịch chạy container.
- `--from`/`--to`: cào lại khoảng ngày; `--days-back`: cửa sổ ngày trượt; `--collect-only`
  và `--download-only`: tách từng pha để vận hành/kiểm tra.

## 9. Giới hạn có chủ ý

- Challenge tương tác vẫn có thể cần người thao tác ở lần tạo profile mới hoặc khi Cloudflare
  thay đổi đánh giá. Crawler giữ context/profile để giảm nhu cầu này, không cố bypass chặn cứng.
- OCR CAPTCHA là best-effort; khi không đọc được, queue được giữ để tiếp tục ở run sau.
- AI trong `run.sh` là lớp hỗ trợ chẩn đoán, không phải thành phần bắt buộc để crawler chạy.
- Để image Docker thực sự chỉ còn Camoufox sau thay đổi dependency, cần rebuild image sau khi
  lượt chạy đang hoạt động kết thúc.
