# Đóng gói TOÀN BỘ môi trường chạy (Python, Camoufox, Xvfb, cron) vào image
# để không phải cài gì trực tiếp lên máy host/server thật. Build 1 lần, chạy nhiều lần.
#
# Không sửa crawler.py/run.sh/notify.py trong image này — chỉ đóng gói, giữ nguyên logic gốc.

FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=Asia/Ho_Chi_Minh \
    PYTHONUNBUFFERED=1

# ----- Thư viện hệ thống cho Camoufox, Xvfb và OCR CAPTCHA -----
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl \
        xvfb xauth x11-utils cron tzdata \
        tesseract-ocr libgtk-3-0 libx11-xcb1 libasound2 \
        fonts-liberation fonts-noto-cjk \
    # xauth: xvfb-run cần để tạo file X authority, thiếu -> lỗi ngay "xauth command not found".
    # x11-utils (xdpyinfo): xvfb-run dùng lệnh này để tự kiểm tra Xvfb đã sẵn sàng chưa -> dùng
    # --no-install-recommends nên KHÔNG tự kéo theo (x11-utils chỉ là "recommends" của xvfb),
    # thiếu là xvfb-run TREO VÔ THỜI HẠN (không lỗi, không log gì) chờ mãi không thấy sẵn sàng.
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && tesseract --version \
    && rm -rf /var/lib/apt/lists/*

# ----- User không phải root (browser sandbox không nên chạy dưới root) -----
# cron daemon (PID 1) vẫn cần root để khởi động, nhưng JOB thật sự (run.sh) chạy
# dưới user này — xem .docker/entrypoint.sh + .docker/crontab.
ARG APP_UID=1000
ARG APP_GID=1000
RUN groupadd -g "$APP_GID" crawler \
    && useradd -m -u "$APP_UID" -g "$APP_GID" -s /bin/bash crawler

# ----- Claude Code CLI (cho tính năng "AI tự chẩn đoán/sửa khi cron lỗi" của run.sh) -----
# Cài bản BINARY NATIVE (không cần Node/npm) dưới CHÍNH user "crawler" — installer luôn
# đặt vào $HOME/.local, mà run.sh đã có sẵn "$HOME/.local/bin" trong PATH; thêm symlink
# /usr/local/bin/claude để gọi tay `claude ...` qua `docker compose run` cũng thấy.
# Image CHỈ đóng gói CÔNG CỤ — KHÔNG kèm credential nào. Bật/tắt (AI_ENABLED) và cấp
# quyền (API key hoặc đăng nhập tài khoản) là việc của NGƯỜI VẬN HÀNH lúc chạy, làm
# ngoài image: xem RUNBOOK mục B.4. Không có credential thì `claude` chỉ đơn giản báo
# lỗi xác thực, run.sh tự lùi về báo cáo Telegram mẫu fix cứng.
ARG CLAUDE_VERSION=stable
RUN su crawler -s /bin/bash -c \
        "export HOME=/home/crawler && curl -fsSL https://claude.ai/install.sh | bash -s -- '$CLAUDE_VERSION'" \
    && ln -s /home/crawler/.local/bin/claude /usr/local/bin/claude \
    && su crawler -s /bin/bash -c "export HOME=/home/crawler && claude --version"

# CLAUDE_CONFIG_DIR: gom TOÀN BỘ trạng thái của claude (credential, config, trạng thái
# "tin tưởng thư mục") vào đúng 1 thư mục -> chỉ cần mount 1 volume là giữ được qua các
# lần dựng lại container (xem docker-compose.yml: ../.claude-docker).
# DISABLE_AUTOUPDATER: khoá tự cập nhật -> phiên bản claude do IMAGE quyết định (chạy lại
# `docker compose up -d --build` để nâng cấp), không tự đổi ngầm giữa các lần cron chạy.
ENV CLAUDE_CONFIG_DIR=/home/crawler/.claude \
    DISABLE_AUTOUPDATER=1

WORKDIR /app

# ----- Cài Python deps vào venv riêng trong image (cache theo layer) -----
# (context build = thư mục gốc repo, xem docker-compose.yml -> build.context: "..";
# requirements.txt/crontab/entrypoint.sh nằm trong .docker/ cùng Dockerfile này)
COPY .docker/requirements.txt requirements.txt
# Browser cache phải thuộc HOME của user chạy thật; fetch dưới root sẽ cài nhầm
# vào /root/.cache và user crawler không nhìn thấy khi cron khởi động.
RUN python3 -m venv /app/.venv \
    && /app/.venv/bin/pip install --no-cache-dir --upgrade pip \
    && /app/.venv/bin/pip install --no-cache-dir -r requirements.txt \
    && su crawler -s /bin/bash -c \
        "export HOME=/home/crawler && /app/.venv/bin/python -m camoufox fetch"

# Kênh official hiện không xuất bản binary Linux và lệnh fetch của Camoufox 0.5.4
# vẫn thoát 0 khi không tìm thấy release. Pin mirror stable cùng SHA256 để build
# không phụ thuộc GitHub Releases API/rate limit và không thể xanh giả khi thiếu browser.
RUN su crawler -s /bin/bash -c 'export HOME=/home/crawler && /app/.venv/bin/python -' <<'PY'
from camoufox.__main__ import CamoufoxUpdate
from camoufox.addons import DefaultAddons, maybe_download_addons
from camoufox.pkgman import AvailableVersion, RepoConfig, Version

browser = AvailableVersion(
    version=Version(build="beta.25", version="152.0.4"),
    url=(
        "https://github.com/JWriter20/camoufox/releases/download/"
        "v152.0.4-20260712/camoufox-152.0.4-beta.25-lin.x86_64.zip"
    ),
    is_prerelease=False,
    asset_id=475026358,
    asset_size=663767219,
    asset_updated_at="2026-07-13T03:32:59Z",
    sha256="a7ddd3792a16885de2cf1651e6278d0efdf5e0fbffcfbb25417a51c4d4470348",
    asset_created_at="2026-07-13T03:30:21Z",
)
repo = RepoConfig.find_by_name("JWriter20")
if repo is None:
    raise RuntimeError("Camoufox repository JWriter20 is not configured")
CamoufoxUpdate(repo_config=repo, selected_version=browser).update()
maybe_download_addons(list(DefaultAddons))
PY
RUN su crawler -s /bin/bash -c \
        "export HOME=/home/crawler && /app/.venv/bin/python -m camoufox version | grep -q 'Installed.*Yes'"

# ----- Mã nguồn (không copy .env/downloads/logs/queue.json — đó là
# dữ liệu/bí mật, luôn mount từ ngoài vào lúc chạy, xem docker-compose.yml) -----
COPY crawler.py run.sh notify.py .env.example ./
RUN chmod +x run.sh

COPY .docker/crontab /etc/cron.d/crawler
RUN chmod 0644 /etc/cron.d/crawler

COPY .docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Thư mục dữ liệu tồn tại sẵn trong image (sẽ bị mount đè khi có volume, nhưng
# cần tồn tại + đúng quyền cho lần chạy đầu khi chưa mount gì).
RUN mkdir -p /app/.camoufox_profile /app/downloads /app/logs /home/crawler/.claude \
    && chown -R crawler:crawler /app /home/crawler

ENTRYPOINT ["/entrypoint.sh"]
CMD ["cron"]
