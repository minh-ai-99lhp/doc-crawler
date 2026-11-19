# Runbook vận hành crawler

Tự động tải văn bản (Word/PDF) từ cổng văn bản nguồn (`SITE_BASE_URL`) theo **ngày ban hành**.

Chạy 2 pha:
1. **Thu thập** — duyệt trang tìm kiếm theo khoảng ngày, qua tất cả các trang (phân trang), gom link chi tiết vào hàng đợi `queue.json` (tự chống trùng).
2. **Tải chi tiết** — mở từng văn bản, tự đăng nhập, tự qua Cloudflare, bấm "Tải về", lưu file vào `downloads/`. Đi **chậm như người thật**.

---

## 🔀 Chọn cách chạy

| | **A. Chạy thường** | **B. Server (Docker)** |
|---|---|---|
| Dùng khi | Máy cá nhân / dev / WSL của bạn | Server thật, muốn tự động 24/7 |
| Cài gì lên máy | Python, Camoufox, xvfb | Chỉ cần **Docker** |
| Lệnh quan trọng nhất | `.venv/bin/python crawler.py` (WSLg) | `docker compose -f .docker/docker-compose.yml up -d --build` |
| Xem chi tiết | Phần A ↓ (bên dưới) | Phần B ↓ (bên dưới) |

---

# Phần A — Chạy thường (máy cá nhân/dev)

## A.1 Yêu cầu & cài đặt

- **Python 3.10+**, WSL/Linux.
- **Camoufox** — browser Firefox đã vá, tạo fingerprint Windows đồng bộ từ header tới
  navigator/font/WebGL. Đây là backend duy nhất của crawler.
- Chạy được **giao diện**: WSLg (Windows 11) hoặc `xvfb` — bắt buộc, vì Cloudflare chặn
  chế độ ẩn. Cần thêm Tesseract để đọc CAPTCHA ảnh site.

Cài từ đầu (máy mới, chưa có `.venv`) — gói gọn 1 lệnh:

```bash
sudo apt install xvfb tesseract-ocr libgtk-3-0 libx11-xcb1 libasound2
python3 -m venv .venv && .venv/bin/pip install camoufox==0.5.4 pillow pytesseract && .venv/bin/python -m camoufox fetch && cp -n .env.example .env
```

`.venv/`, `.camoufox_profile/`, `.env` **không** lên git → sang máy mới phải
chạy lại lệnh trên.

Kiểm tra chạy được:

```bash
xvfb-run -a .venv/bin/python crawler.py --help
```
> `.camoufox_profile/` giữ session và fingerprint Camoufox cố định.

## A.2 Cấu hình (`.env`)

Mọi cấu hình bí mật gom vào **một file `.env`** (tài khoản site + Telegram). Copy từ mẫu:

```bash
cp .env.example .env      # rồi mở .env điền giá trị thật
```
```dotenv
SITE_BASE_URL=https://example.com
SITE_USER=tên_đăng_nhập_hoặc_email
SITE_PASS=mật_khẩu
# (phần Telegram xem mục Chung > Thông báo Telegram)
```

`.env` đã được `.gitignore`. Cả `crawler.py` và `run.sh` đều tự đọc file này.

> Vẫn hỗ trợ **`auth.json`** cũ như phương án dự phòng: nếu `.env` không có
> `SITE_USER`/`SITE_PASS`, script sẽ đọc `auth.json`.

> ⚠️ Tài khoản chỉ cho **1 phiên đăng nhập cùng lúc**. Khi script đăng nhập, phiên
> Phiên trình duyệt đang login tài khoản này ở nơi khác sẽ bị đăng xuất (script tự bấm
> "Đồng ý").
> Nên **không dùng tài khoản này trên trình duyệt khác** trong lúc script chạy.

## A.3 Các cách chạy

> **Bắt buộc chạy có giao diện** (Cloudflare chặn chế độ ẩn). Trên server/WSL không màn
> hình, thêm `xvfb-run -a` phía trước để chạy trên màn hình ảo.

| Mục đích | Lệnh |
|---|---|
| ⭐ Cào văn bản **HÔM NAY** (mặc định) | `xvfb-run -a .venv/bin/python crawler.py` |
| Cào lại **quá khứ** theo khoảng ngày | `xvfb-run -a .venv/bin/python crawler.py --from 22/07/2026 --to 23/07/2026` |
| Chỉ **thu thập link** (không tải) | `.venv/bin/python crawler.py --collect-only` |
| Chỉ **tải** các link đang chờ | `.venv/bin/python crawler.py --download-only` |
| Xem tất cả tuỳ chọn | `.venv/bin/python crawler.py --help` |

**Quy ước ngày** (`--from` / `--to`, định dạng `dd/MM/yyyy`):
- Có khoảng ngày → cào lại quá khứ, chạy 1 lần.
- Bỏ trống, hoặc `--from today --to today` → cào văn bản của **ngày hôm đó**.

**Tuỳ chọn khác:**
- `--max-pages N` — giới hạn số trang listing (mặc định 500).
- `--min-delay` / `--max-delay` — khoảng nghỉ ngẫu nhiên giữa các request (mặc định 4–9s).
- `--mode assist` — tự bạn bấm nút tải thay vì để script bấm.

## A.4 Định dạng tải

Mặc định ưu tiên **Word (.docx)**, không có thì fallback sang **.doc**, rồi **PDF gốc**.
Đổi thứ tự trong biến `DOWNLOAD_LABELS` ở đầu `crawler.py`.

## A.5 Cron tự động (host, không Docker) + tự gọi AI khi lỗi

Không gọi `crawler.py` trực tiếp từ cron, mà qua **`run.sh`** — lớp bọc:
- Chạy crawler ở chế độ `--ci` (tự động, **không chờ người**).
- Ghi **log riêng cho mỗi lần chạy** ở `logs/run-YYYYmmdd-HHMMSS.log` (+ `logs/latest.log`).
- Quyết định **thành/bại** qua exit code (`0` = OK; `1` = có vấn đề; `2` = crash) và dòng
  `RESULT: ...` cuối log; ghi tóm tắt vào `logs/last-status.txt`.
- **Khi LỖI** → tự gọi Claude để đọc log, suy luận, kiểm tra và sửa.

```bash
# Chạy tay qua wrapper:
./run.sh                                   # cào hôm nay
./run.sh --days-back 15                     # cào cửa sổ trượt 15 ngày gần nhất (như cron)
./run.sh --from 22/07/2026 --to 22/07/2026 # cào 1 ngày
AI_ON_FAIL=0 ./run.sh                       # tắt gọi AI (chỉ log) khi test

# Cron 23:59 mỗi ngày (máy ở giờ Asia/Ho_Chi_Minh nên 59 23 = 23:59 VN):
sudo service cron start
crontab -e
```
```cron
# --days-back 15: quét 15 ngày gần nhất để bắt cả văn bản đăng trễ (site upload trễ);
# queue.json dedup nên văn bản đã tải không tải lại.
59 23 * * * /path/to/doc-crawler/run.sh --days-back 15 >> /path/to/doc-crawler/logs/cron.log 2>&1
```

> ⚠️ Cron của WSL **chỉ chạy khi WSL đang mở**. Muốn chắc chắn hơn, dùng **Windows Task
> Scheduler** gọi `wsl.exe` (tự bật WSL), hoặc chuyển sang **Phần B (Docker)** bên dưới —
> container tự chạy nền, không phụ thuộc WSL có đang mở hay không. `run.sh` tự nhận diện
> thư mục nên chạy được từ bất kỳ đường dẫn nào; Claude CLI phải có trong PATH để dùng
> tính năng tự sửa.

# Phần B — Chạy trên SERVER (Docker)

> 🚀 **Khuyến nghị dùng cách này cho server thật** — không cần cài Python/Camoufox/xvfb lên
> máy host, mọi thứ nằm gọn trong container. Chỉ cần cài **Docker** là xong.

Đóng gói **toàn bộ môi trường chạy** (Python, Camoufox, Xvfb, cron) vào image. Toàn
bộ file liên quan nằm trong `.docker/` (`Dockerfile`, `docker-compose.yml`, `entrypoint.sh`,
`crontab`, `requirements.txt`).

**Khác với Phần A:** lịch chạy tự động nằm **trong container** (cron của image, xem
`.docker/crontab`, mặc định 23:59 giờ VN — không cần crontab ở host, không phụ thuộc WSL có
mở hay không). Bước **AI tự chẩn đoán/sửa lỗi khi cron thất bại** (A.5) vẫn dùng được:
image có sẵn `claude` — xem **B.4**. Image chỉ chứa **công cụ**, **không** chứa
credential; cấp quyền làm ngoài image.

## B.1 Chuẩn bị + chạy

Mọi lệnh dưới đây chạy ở **gốc repo**. File compose nằm trong `.docker/` nên nhớ `-f`:

```bash
# 1) Chuẩn bị BẮT BUỘC, chỉ làm 1 lần trước lần `up` đầu tiên
cp .env.example .env   # rồi mở .env điền SITE_USER/SITE_PASS + Telegram (xem A.2)
touch queue.json       # ⚠️ BẮT BUỘC — xem cảnh báo ngay dưới
mkdir -p .claude-docker downloads logs .camoufox_profile

# 2) Build + chạy nền
docker compose -f .docker/docker-compose.yml up -d --build

# 3) Xem container còn sống + lịch cron
docker compose -f .docker/docker-compose.yml logs -f
```

> ⚠️ **Đừng quên `touch queue.json` trước lần `up` đầu tiên.** Nếu file chưa tồn tại, Docker
> daemon sẽ tự bind-mount `./queue.json` thành **1 THƯ MỤC RỖNG** thay vì file — xảy ra lúc
> TẠO container, trước cả khi `entrypoint.sh` kịp chạy, nên không tự sửa được từ trong
> container (chỉ báo lỗi rõ ràng rồi dừng). Cách gỡ: xem "Sự cố thường gặp" (B.3).

> 💡 Ngại gõ `-f` mỗi lần thì `cd .docker` rồi dùng `docker compose up -d --build` như bình
> thường — mọi đường dẫn trong file compose đều tính từ `.docker/` nên chạy ở đó vẫn đúng.
> (Chỉ nhớ bước chuẩn bị ở trên vẫn phải làm tại **gốc repo**.)

## B.2 Chạy tay 1 lệnh bất kỳ (không đợi cron)

```bash
docker compose -f .docker/docker-compose.yml run --rm crawler ./run.sh --from 22/07/2026 --to 22/07/2026
docker compose -f .docker/docker-compose.yml run --rm crawler ./run.sh --collect-only
# hoặc gọi crawler.py trực tiếp (không qua run.sh, không có log/AI/Telegram):
docker compose -f .docker/docker-compose.yml run --rm crawler .venv/bin/python crawler.py --help
```
> Các lệnh trên đều tự chạy dưới user thường `crawler` trong container (không phải root)
> — xem `.docker/entrypoint.sh`. Browser chạy dưới user thường, không cần thêm quyền gì.

**Dữ liệu & log** (`.env`, `queue.json`, `downloads/`, `logs/`, `.camoufox_profile/`) được
**mount thẳng** từ thư mục dự án trên host vào container (xem `.docker/docker-compose.yml`)
— dữ liệu sống ngoài container, xoá/build lại container không mất gì:

```bash
tail -f logs/latest.log
```

## B.3 Sự cố thường gặp

- **`.camoufox_profile/` rỗng lần đầu** (máy/server mới) → có thể phải lấy `cf_clearance` bằng cách bấm
  checkbox TAY 1 lần **từ chính IP của server** (cookie gắn với IP, copy profile từ
  máy khác sang **không dùng được**). Server không màn hình: Xvfb + `x11vnc` để bấm 1 phát.
  Xem mục "Chống bot của site (Cloudflare)" ở phần Chung để hiểu đầy đủ.
- **Log báo `RESULT: BLOCKED` (exit code 3)**: đây là Cloudflare **chặn cứng**, KHÔNG phải
  lỗi code — đừng sửa gì, đừng chạy lại ngay. Nghỉ 30–60 phút; lần cron sau tự thử lại.
  Telegram sẽ có tin riêng "⛔ BỊ CHẶN" kèm Ray ID. Nếu lặp lại nhiều ngày liền thì kiểm tra
  backend: container phải dùng Camoufox (xem mục Chống bot).
- **`./queue.json` bị lỡ tạo thành THƯ MỤC** (quên `touch queue.json` trước lần `up` đầu):
  container sẽ dừng ngay với thông báo rõ ràng từ `entrypoint.sh`. Gỡ: dừng container, xoá
  thư mục rỗng đó trên host rồi tạo lại file trước khi chạy lại:
  ```bash
  docker compose -f .docker/docker-compose.yml down
  rmdir queue.json   # báo lỗi nếu KHÔNG rỗng -> có gì đó bất thường, tự kiểm tra lại tay
  touch queue.json
  docker compose -f .docker/docker-compose.yml up -d --build
  ```
- **Gọi tay `xvfb-run` trực tiếp qua `docker compose run` bị TREO vô thời hạn** (không lỗi,
  không log, không exit): do `xvfb-run` vô tình trở thành **PID 1** của container khi gọi
  kiểu này — cơ chế tín hiệu `SIGUSR1` mà nó dùng để biết Xvfb đã sẵn sàng không hoạt động
  bình thường ở PID 1 (đặc thù Linux namespace, không phải bug riêng của image này). Luôn
  chạy qua `./run.sh` (mục B.2) — `run.sh` có nhiều lệnh trước khi gọi `xvfb-run` nên không
  dính vấn đề này. Nếu thật sự cần gọi `xvfb-run` trần để debug, bọc thêm 1 lệnh no-op để
  tránh bị tối ưu thành PID 1:
  ```bash
  docker compose -f .docker/docker-compose.yml run --rm crawler \
      bash -c "xvfb-run -a .venv/bin/python crawler.py --collect-only; true"
  ```
- **Đổi giờ cron**: sửa `.docker/crontab` rồi build lại (`up -d --build`).
  (Bật/tắt AI thì **không** cần build lại — chỉ sửa `.env`, xem B.4.)

## B.4 AI tự chẩn đoán/sửa khi cron lỗi (trong container)

Image cài sẵn **Claude Code CLI** dưới user `crawler`, nhưng không kèm credential.

```bash
# Bật AI trong .env
AI_ENABLED=1

# Đăng nhập Claude và kiểm tra
docker compose -f .docker/docker-compose.yml run --rm crawler claude
docker compose -f .docker/docker-compose.yml run --rm crawler \
  claude -p "trả lời đúng 1 từ: OK"
```

- Credential nằm ở `./.claude-docker/` trên host, đã gitignore và được giữ lại khi dựng
  lại container.
- Muốn **tắt AI**: `AI_ENABLED=0` trong `.env` — vẫn log đầy đủ + Telegram báo theo **mẫu
  fix cứng** dựng từ `logs/last-summary.json`. Bật AI mà credential hỏng/hết quota thì
  `run.sh` **tự lùi về** mẫu fix cứng, không im lặng bỏ qua báo cáo.
- **Code AI sửa được đi thẳng vào repo trên host**: `crawler.py`, `run.sh`, `notify.py` được
  mount ghi được vào container (xem `.docker/docker-compose.yml`) → sáng hôm sau cứ `git
  diff`/`git checkout` để duyệt hoặc bỏ thay đổi của AI. (Bỏ 3 dòng mount đó nếu muốn
  container chạy cố định đúng bản code đã build vào image.)
- **Nâng cấp AI CLI**: chạy lại `up -d --build`. Muốn ghim phiên bản cụ thể:
  ```bash
  docker compose -f .docker/docker-compose.yml build \
    --build-arg CLAUDE_VERSION=2.1.220
  ```

---

## Downloader hai lane + ExpressVPN rotation

`run-rotate-dual.sh` chạy hai tài khoản đồng thời trên hai queue riêng. Hai lane chạy
hai queue đầu trong `SITE_ROTATION_QUEUE_ORDER`, xong thì tự nhận queue kế tiếp; hai lane dùng chung location/IP
ExpressVPN và đổi định kỳ mỗi 90 phút. Soft challenge chỉ pause/retry đúng lane;
hard block hoặc cả hai lane soft-block mới được đổi sớm sau dwell tối thiểu. Profile, staging
và log của từng lane/user được tách dưới `.runtime/download-dual-rotation/`.

```bash
# Cấu hình các biến SITE_ROTATION_* và EXPRESSVPN_BIN trong .env
# Khởi chạy supervisor:
./run-rotate-dual.sh
```

Không chạy downloader/collector thủ công khác trên cùng các queue trong lúc supervisor
dual đang hoạt động.

# Chung (áp dụng cả 2 cách)

## Cấu trúc file

| File/Thư mục | Vai trò | Lên git? |
|---|---|---|
| `crawler.py` | Toàn bộ mã nguồn | ✅ |
| `run.sh` | Wrapper cho cron (log + tự gọi AI khi lỗi) | ✅ |
| `run-backfill.sh` | Backfill nhiều ngày, mỗi ngày một container | ✅ |
| `dual_rotation.py`, `run-rotate-dual.sh`, `switch_vpn.sh` | Downloader hai lane + xoay VPN | ✅ |
| `.env.example` | Mẫu cấu hình (tài khoản + Telegram) | ✅ |
| `README.md`, `RUNBOOK.md`, `TECHSTACK.md` | Giới thiệu, vận hành, kiến trúc | ✅ |
| `.docker/` | Đóng gói Docker (Phần B) — tuỳ chọn | ✅ |
| `.env` | Cấu hình thật (tài khoản, token Telegram) | ❌ (gitignore) |
| `auth.json` | Tài khoản thật (dự phòng, nếu không dùng .env) | ❌ (gitignore) |
| `queue.json` | Hàng đợi + trạng thái (`pending`/`done`/`failed`) | ❌ |
| `downloads/YYYY-MM-DD/` | File tải, gom theo **Ngày ban hành**; mỗi file kèm `.json` metadata | ❌ (trừ `downloads/README.md`) |
| `logs/` | Log mỗi lần chạy + `last-status.txt` | ❌ |
| `.camoufox_profile/` | Session + fingerprint cố định của Camoufox | ❌ |
| `.claude-docker/` | Credential/config của `claude` CLI trong container (B.4) | ❌ (gitignore) |
| `.runtime/` | State runtime, logs, profile của các tiến trình xoay vòng download | ❌ |

## ⚠️ Chống bot của site (Cloudflare) — ĐỌC TRƯỚC KHI DỰNG SERVER

Cập nhật **12/08/2026**. Site siết chống bot trong khoảng 30/07 → 12/08/2026; các mẹo cũ
không còn đủ. Ba việc phải biết:

**1. WAF chặn thẳng mọi User-Agent desktop Linux.** Đo bằng curl, cùng IP, cùng URL, chỉ
khác chuỗi UA — tái lập 3/3 lần:

| User-Agent gửi đi | Cloudflare trả về |
|---|---|
| `Mozilla/5.0 (Windows NT 10.0; Win64; x64)…` | 403 + `cf-mitigated: challenge` → challenge bình thường, giải được |
| `Mozilla/5.0 (X11; Linux x86_64)…` | trang **"Sorry, you have been blocked"** → chặn thẳng |

Không phải IP (curl từ đúng IP đó với UA Windows vẫn chỉ bị challenge), không phải cookie
(profile trắng tinh vẫn dính), không phải fingerprint JS (chặn ngay request đầu, trước khi
có JS chạy). Rule chỉ khớp **chuỗi UA** — gửi UA Windows kèm client-hint `sec-ch-ua-platform:
"Linux"` vẫn chỉ bị challenge.

→ `crawler.py` chỉ dùng **Camoufox**. Nó tạo fingerprint Firefox/Windows đồng bộ ở cấp
browser và lưu cố định trong `.camoufox_profile/`; không chỉ thay một chuỗi User-Agent.

**2. Bị chặn cứng ≠ bị challenge.** Trang chặn cứng (`Attention Required! | Cloudflare` /
"Sorry, you have been blocked") **không có checkbox nào để bấm**, mọi retry chỉ làm thời gian
bị chặn dài thêm. `crawler.py` phân biệt 2 loại: gặp chặn cứng → thoát trong ~1 giây kèm
Ray ID, **không reload lần nào**, **exit code 3** (`RESULT: BLOCKED`). `run.sh` thấy mã 3 thì
chỉ báo Telegram và **không gọi AI** đi sửa code — vì không có gì để sửa, chỉ cần nghỉ
30–60 phút rồi để lần chạy sau tự thử lại (hàng đợi giữ nguyên, không mất dữ liệu).

**3. Challenge hiện là loại `interactive` — cần NGƯỜI bấm 1 lần.** Cloudflare trả
`cType: 'interactive'`; lần tạo profile mới có thể cần người bấm checkbox một lần. Script
chạy tay sẽ chờ tối đa 3 phút ngay trên browser, không tự click/reload dồn dập và không cần
bấm Enter ở terminal. Sau khi qua, cookie được giữ trong `.camoufox_profile/`.

Thủ tục lấy `cf_clearance` lần đầu (làm 1 lần cho mỗi máy/server):

```bash
# Chạy CÓ giao diện thật (KHÔNG dùng xvfb-run), tự bấm checkbox trên cửa sổ Camoufox:
.venv/bin/python crawler.py --collect-only     # trên máy có màn hình / WSLg
```

Lưu ý quan trọng: **`cf_clearance` gắn với IP đã giải challenge** → không copy `.camoufox_profile/`
từ máy khác sang server được. Server không có màn hình thì phải bấm qua VNC/X11 forwarding
(Xvfb + `x11vnc`) đúng 1 lần, sau đó chạy ẩn như bình thường.

**Hệ quả cho Phần B (Docker):** container dùng cùng backend Camoufox Linux và mount riêng
`.camoufox_profile/`. Lần đầu trên server vẫn có thể cần bấm tay từ chính IP server để lấy
`cf_clearance`.

## Ghi chú kỹ thuật

- **Cloudflare**: xem mục "Chống bot của site" ở trên — trang tìm kiếm thường tự qua; trang
  phân trang & trang chi tiết hay ra challenge tương tác. Chế độ chạy tay để người dùng bấm;
  chế độ CI chỉ click khi ảnh chụp xác nhận ô vuông ổn định qua 3 frame, không click spinner
  và không reload. Sau click đầu, chỉ được click lần hai khi đã thấy ít nhất 3 frame không có
  ô vuông rồi ô vuông xuất hiện ổn định trở lại. Watchdog mặc định 120 giây không có chuyển
  trạng thái có ý nghĩa. Một trang lẻ không qua thì **bỏ qua link đó rồi đi tiếp**
  (link giữ `failed`, lần chạy sau tự retry). Nếu **3 link liên tiếp** đều dính challenge thì
  dừng pha tải sớm (`CF_STREAK_ABORT` trong `crawler.py`) — site đang siết, cố nữa chỉ tổ
  tụt điểm; các link còn lại giữ nguyên cho lần chạy sau.
- **`?dll=true`**: script tự loại bỏ tham số này (nó ép trang hiện bản chưa-đăng-nhập).
- **State & resume**: nhờ `queue.json`, chạy lại sẽ bỏ qua link đã `done`, thử lại link
  `failed`, và không thêm trùng. `done` cũ hơn 30 ngày tự dọn (`prune_done()` trong
  `crawler.py`), `pending`/`failed` không bao giờ bị đụng tới.
- **Đăng nhập**: nhận diện qua link "Thoát"/`logout.aspx` và nhãn "Tài khoản :".
- **Gom theo ngày + metadata**: mỗi văn bản lưu ở `downloads/<Ngày ban hành>/` (yyyy-mm-dd),
  kèm 1 file `.json` cùng tên chứa: số hiệu, loại VB, nơi ban hành, người ký, ngày ban hành,
  ngày hiệu lực, tình trạng, url, doc_id, **`luoc_do`** (văn bản liên quan theo quan hệ — xem
  `downloads/README.md` để biết bảng key/nhãn)… → dễ đối chiếu đủ/thiếu theo ngày. Không đọc
  được ngày ban hành thì gom vào thư mục `khong-ro-ngay/`.

## Thông báo Telegram (trợ lý báo cáo)

Cấu hình trong `.env`:
```dotenv
TELEGRAM_ENABLED=1
TELEGRAM_BOT_TOKEN=<token từ @BotFather>
TELEGRAM_CHAT_ID=<id user/group, group là số âm>
TELEGRAM_ONLY_ON_FAIL=0   # 0 = báo cả khi thành công; 1 = chỉ báo khi lỗi
```

Mỗi lần chạy, `crawler.py` ghi **số liệu thật** ra `logs/last-summary.json` (thu thập found/new/duplicate, tải ok/failed, `downloads_by_date`, `queue`, `problems`, `result`). AI báo cáo **chỉ được thuật lại số liệu này**, không suy diễn — tránh báo cáo "ảo".

`notify.py` là "năng lực" gửi tin (chỉ dùng thư viện chuẩn):
```bash
.venv/bin/python notify.py "nội dung báo cáo"
```

Luồng trong `run.sh` (cron gọi):
- **Thất bại** → gọi Claude đọc log, chẩn đoán,
  **tự sửa** nếu được, rồi **báo cáo** tình hình cho nhóm qua `notify.py`.
- **Thành công** → (nếu `TELEGRAM_ONLY_ON_FAIL=0`) gọi AI tổng hợp số liệu lần cào
  (thu thập/tải bao nhiêu, theo ngày nào) rồi **báo cáo** cho nhóm.
- Cờ điều khiển: `AI_ENABLED` (mặc định 1), `AI_ON_FAIL` (1), `AI_ON_SUCCESS` (1).
  Thiếu Claude CLI, tắt AI hoặc Claude lỗi →
  `run.sh` gửi báo cáo theo **mẫu fix cứng** dựng từ `last-summary.json`. Nghĩa là dù AI
  hỏng thì Telegram vẫn luôn có tin — không bao giờ im lặng.
- **Docker (Phần B)**: Claude CLI có sẵn trong image; credential nằm ở
  `.claude-docker/` — xem **B.4**.
