# 91Fetch

English | [简体中文](README.md)

[![License: MIT](https://img.shields.io/badge/License-MIT-f5c542.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776ab.svg)](https://www.python.org/)
[![Version: 1.1.0](https://img.shields.io/badge/Version-1.1.0-e96b43.svg)](https://github.com/ChaosTechDev/91Fetch/releases)

## About

91Fetch is a local web application for browsing, collecting, and batch-downloading videos from the **91porn website**. It supports latest, currently popular, featured, daily ranking, monthly popular, most favorited, most discussed, HD, and original-video categories, along with author pages and custom listing URLs.

Listings show titles, thumbnails, authors, durations, view counts, and viewkeys. Select one video or the whole visible page and add it to the download queue without leaving the catalog. 91Fetch resolves actual `.m3u8` or `.mp4` media URLs and uses yt-dlp for concurrent HLS fragments, retries, and resume support.

Project homepage: [https://github.com/ChaosTechDev/91Fetch](https://github.com/ChaosTechDev/91Fetch)

## Features

- Browse nine 91porn categories, author pages, and custom listing URLs
- Navigate with previous/next controls or jump to a specific page
- Display titles, thumbnails, authors, durations, view counts, and viewkeys
- Select one or all videos and keep browsing after adding downloads
- Queue additional downloads while another batch is running
- Resolve `.m3u8` and `.mp4` streams with concurrent HLS fragments, retries, and resume support
- Filter downloads by all, incomplete, or completed; select, retry, and remove tasks
- Persist tasks, progress, and completion state; resume interrupted tasks after restart
- Scan the download directory, deduplicate by viewkey, and show downloaded status across categories
- Light and dark themes, list transitions, and partial progress updates
- Local settings for download directory, archive layout, concurrency, and UI refresh interval

## Windows Quick Start

Windows 10/11 and Python 3.11 or newer are required. Download and extract the Release archive, then double-click `启动.cmd`. On first run it creates `.venv`, installs dependencies, generates `site.json`, starts the local service, and opens the web interface.

Application data is stored in `downloads/` by default, with videos in `downloads/videos/`. The video directory and archive layout can be changed in Settings.

Manual startup:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
Copy-Item site.example.json site.json
.\.venv\Scripts\python.exe -m viewkey_batch.web
```

The default address is `http://127.0.0.1:8765`. The application selects the next available local port when 8765 is occupied.

## Site Configuration and Rate Limits

`启动.cmd` creates `site.json` from `site.example.json` on first run. Edit it to adjust category URLs, request interval, jitter, retries, and backoff settings.

Requests are rate-limited by default. The client backs off on `429` and `5xx` responses and stops the current task when it detects a challenge page. Category browsing establishes a site session and adds a cache-busting query value to prevent stale cross-category results. Media pages are resolved again before each download to refresh expiring CDN URLs.

## Command Line

```powershell
91fetch crawl --category latest --max-pages 10
91fetch crawl --author AUTHOR_ID
91fetch download --workers 2 --fragments 4
91fetch run --category featured --workers 2 --fragments 4
```

The web interface is the default workflow. The CLI is intended for one-off scripted tasks.

## License

91Fetch is released under the [MIT License](LICENSE). You may use, copy, modify, merge, publish, distribute, sublicense, and sell copies of the software, provided that the copyright and license notices remain in copies or substantial portions of the software. The software is provided as-is without warranty.

Users are responsible for confirming that they may save downloaded content and for complying with the target site's terms and applicable local rules.
