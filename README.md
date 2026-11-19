# Doc Crawler

Scheduled crawler that downloads documents (Word/PDF) and their metadata from a document portal by issue date. Built to run unattended every night behind Cloudflare.

## Features

- **Two-phase pipeline**: collect links (pagination, dedup, per-page checkpoint), then download each document. A JSON queue (`pending` / `failed` / `pdf_only` / `done`) makes runs resumable.
- **Camoufox browser** (patched Firefox via the Playwright API) with a persistent profile and a consistent Windows fingerprint.
- **Challenge handling**: Cloudflare Turnstile solver driven by screenshot frames with a stall watchdog; hard blocks are detected and stop the run (exit 3) instead of retrying. Image CAPTCHAs are solved with Tesseract OCR.
- **Session recovery**: detects lost or kicked sessions, re-logs in with a per-run limit, and retries the current document.
- **Dual-lane downloader**: two accounts on separate queues share one VPN exit and rotate location on a schedule; soft blocks pause only the affected lane.
- **Monitoring**: exit codes, a JSON run summary and Telegram reports. On failure, an AI agent (Claude Code CLI) reads the logs, diagnoses and tries a fix; a template report is the fallback.
- **Docker**: Camoufox, Xvfb and cron in one image, runs daily at 23:59 over a 15-day sliding window.

## Architecture

```
cron → run.sh → crawler.py: collect → queue.json → download → downloads/
                    │
                    ├─ logs/last-summary.json → notify.py → Telegram
                    └─ on failure → AI diagnose / fix → report

run-rotate-dual.sh → dual_rotation.py ─┬─ lane A: crawler.py --download-only (queue 1)
                                        ├─ lane B: crawler.py --download-only (queue 2)
                                        └─ switch_vpn.sh (rotate location, verify new IP)
```

## Tech stack

Python · Camoufox / Playwright · Tesseract · Bash · Docker · Xvfb · cron · Telegram Bot API

## Quick start

```bash
cp .env.example .env    # set SITE_BASE_URL, SITE_USER, SITE_PASS, TELEGRAM_*
```

**Local**

```bash
sudo apt install xvfb tesseract-ocr
python3 -m venv .venv && .venv/bin/pip install camoufox==0.5.4 pillow pytesseract
.venv/bin/python -m camoufox fetch
xvfb-run -a .venv/bin/python crawler.py                                   # today's documents
xvfb-run -a .venv/bin/python crawler.py --from 01/07/2026 --to 07/07/2026   # backfill
```

**Docker**

```bash
touch queue.json && mkdir -p downloads logs .camoufox_profile .claude-docker
docker compose -f .docker/docker-compose.yml up -d --build
```

## Output

```
downloads/YYYY-MM-DD/<document>.docx
downloads/YYYY-MM-DD/<document>.json    # metadata, schema in downloads/README.md
```

See [RUNBOOK.md](RUNBOOK.md) for operations and troubleshooting, and [TECHSTACK.md](TECHSTACK.md) for the design.
