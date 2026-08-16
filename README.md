# 91Fetch

[English](README_EN.md) | 简体中文

[![License: MIT](https://img.shields.io/badge/License-MIT-f5c542.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776ab.svg)](https://www.python.org/)
[![Docker Ready](https://img.shields.io/badge/Docker-Ready-2496ed.svg)](Dockerfile)
[![Tests: 26 passed](https://img.shields.io/badge/Tests-26%20passed-2ea44f.svg)](tests)
[![Version: 1.0.0](https://img.shields.io/badge/Version-1.0.0-e96b43.svg)](https://github.com/ChaosTechDev/91Fetch)

## 软件简介

91Fetch 是专用于浏览和批量下载 **91porn 网站**视频的开源工具，提供中文网页管理界面。它可以读取 91porn 的最新发布、当前最热、加精推荐、排行榜、高清视频、原创视频等分类，也可以读取指定作者主页；采集视频标题、封面、作者、时长、播放量和 viewkey 后，用户可以勾选或全选下载。

下载前会解析真实的 `.m3u8` 或 `.mp4` 媒体地址，并通过 yt-dlp 实现 HLS 分片并发、失败重试和断点续传。下载队列、进度和完成记录会持久保存，重新启动软件后仍可继续管理。

项目主页：[https://github.com/ChaosTechDev/91Fetch](https://github.com/ChaosTechDev/91Fetch)

## 功能

- 分类、作者主页和自定义列表 URL 浏览
- 最新、热门、加精、排行、高清等分类分页与页码跳转
- 标题、封面、作者、时长和播放量展示
- 单选、全选和批量加入下载，不打断继续浏览
- `.m3u8` / `.mp4` 解析，HLS 分片并发、重试和断点续传
- 下载管理按全部、未完成、已完成筛选，支持选择、重试和删除任务
- 下载队列与完成状态持久化，重启后保留
- 下载文件有效性检查和 viewkey 去重
- 日间/夜间主题、页面过渡动画和局部进度刷新
- 下载目录、归档方式、并发数、账号密码集中设置

## Windows 一键启动

需要 Windows 和 Python 3.11 或更高版本。双击 `启动.cmd` 即可：首次运行会自动创建 `.venv`、安装依赖、生成 `site.json` 并打开网页。

默认数据保存在 `downloads/`，视频保存在 `downloads/videos/`。这些运行数据不会被 Git 提交。

如需手动启动：

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
Copy-Item site.example.json site.json
.\.venv\Scripts\python.exe -m viewkey_batch.web
```

默认访问地址为 `http://127.0.0.1:8765`。端口被占用时，本地启动会自动选择后续可用端口。

## 站点配置

将 `site.example.json` 复制为 `site.json` 后，可调整入口、分类地址、请求间隔、抖动、重试和退避策略。程序默认限制请求频率，遇到 `429` / `5xx` 会退避，识别到验证页面时会停止当前任务。

下载受会话保护的内容时，可按命令行帮助传入 Cookie 文件。媒体下载前会重新解析视频页，以刷新可能过期的 CDN 地址。

## 命令行

```powershell
91fetch crawl --category latest --max-pages 10
91fetch crawl --author AUTHOR_ID
91fetch download --workers 2 --fragments 4
91fetch run --category featured --workers 2 --fragments 4
```

网页界面是默认使用方式；命令行适合脚本化的一次性任务。

## Docker

Docker 配置使用通用的本地 `./data` 目录，不包含任何 NAS 私有路径或默认口令：

```bash
docker compose up -d --build
docker compose logs -f 91fetch
```

打开 `http://127.0.0.1:8765`。需要账号登录时，在 Compose 环境变量中设置：

```yaml
VIEWKEY_AUTH_ENABLED: "1"
VIEWKEY_ADMIN_USER: "admin"
VIEWKEY_ADMIN_PASSWORD: "请设置强密码"
```

## 开源协议

本项目采用 [MIT License](LICENSE)。你可以使用、复制、修改、合并、发布、分发、再许可和销售软件副本，但必须在软件副本或主要部分中保留原版权声明和许可声明。软件按现状提供，不附带任何明示或默示担保。

使用者应自行确认下载内容的保存权限，并遵守目标站点条款及所在地规定。
