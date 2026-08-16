# 91Fetch

English | [简体中文](README.md)

91Fetch is a web-based browser and batch downloader for viewkey videos. It reads category pages directly, displays titles, thumbnails, authors, durations, and view counts, and adds selected videos to a persistent download queue.

## Features

- Browse categories, author pages, and custom listing URLs
- Pagination and page jumps for latest, popular, featured, ranked, HD, and other categories
- Display titles, thumbnails, authors, durations, and view counts
- Select individual or all videos and keep browsing after adding downloads
- Resolve `.m3u8` and `.mp4` streams with concurrent HLS fragments, retries, and resume support
- Filter downloads by all, incomplete, or completed; select, retry, and remove tasks
- Persist the download queue and completion state across restarts
- Validate downloaded media and deduplicate by viewkey
- Light and dark themes, page transitions, and partial progress updates
- Central settings for the download directory, archive layout, concurrency, and credentials

## Windows Quick Start

Windows and Python 3.11 or newer are required. Double-click `启动.cmd`. On first run, it creates `.venv`, installs dependencies, generates `site.json`, starts the service, and opens the web interface.

Application data is stored in `downloads/` by default, with videos in `downloads/videos/`. Runtime data is excluded from Git.

To start manually:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
Copy-Item site.example.json site.json
.\.venv\Scripts\python.exe -m viewkey_batch.web
```

The default address is `http://127.0.0.1:8765`. Local startup selects the next available port if 8765 is occupied.

## Site Configuration

Copy `site.example.json` to `site.json`, then adjust the base URL, category URLs, request interval, jitter, retries, and backoff settings. Requests are rate-limited by default. The crawler backs off on `429` and `5xx` responses and stops the current task when it detects a challenge page.

For session-protected content, pass a cookie file using the relevant command-line option. Media pages are resolved again before each download to refresh expiring CDN URLs.

## Command Line

```powershell
91fetch crawl --category latest --max-pages 10
91fetch crawl --author AUTHOR_ID
91fetch download --workers 2 --fragments 4
91fetch run --category featured --workers 2 --fragments 4
```

The web interface is the default workflow. The CLI is intended for one-off scripted tasks.

## Docker

The Docker setup uses the generic local `./data` directory and contains no NAS-specific path or default password:

```bash
docker compose up -d --build
docker compose logs -f 91fetch
```

Open `http://127.0.0.1:8765`. To enable authentication, set these Compose environment variables:

```yaml
VIEWKEY_AUTH_ENABLED: "1"
VIEWKEY_ADMIN_USER: "admin"
VIEWKEY_ADMIN_PASSWORD: "set-a-strong-password"
```

## License

91Fetch is released under the [MIT License](LICENSE). You may use, copy, modify, merge, publish, distribute, sublicense, and sell copies of the software, provided that the copyright and license notices remain in copies or substantial portions of the software. The software is provided as-is without warranty.

Users are responsible for confirming that they may save downloaded content and for complying with the target site's terms and applicable local rules.
