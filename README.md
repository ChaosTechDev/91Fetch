# 91Fetch

[English](README_EN.md) | 简体中文

[![License: MIT](https://img.shields.io/badge/License-MIT-f5c542.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776ab.svg)](https://www.python.org/)
[![Version: 1.1.0](https://img.shields.io/badge/Version-1.1.0-e96b43.svg)](https://github.com/ChaosTechDev/91Fetch/releases)

## 软件简介

91Fetch 是专用于 **91porn 网站**的视频浏览、采集和批量下载工具，提供中文本地网页界面。软件可直接浏览最新发布、当前最热、加精推荐、每日排行、本月最热、最多收藏、最多讨论、高清视频和原创视频分类，也支持指定作者主页与自定义列表 URL。

列表会显示视频标题、封面、作者、时长、播放量和 viewkey。勾选单个视频或全选当前页即可加入下载队列，添加后仍可继续浏览和选择。下载前会解析真实 `.m3u8` 或 `.mp4` 地址，并通过 yt-dlp 实现 HLS 分片并发、失败重试和断点续传。

项目主页：[https://github.com/ChaosTechDev/91Fetch](https://github.com/ChaosTechDev/91Fetch)

## 主要功能

- 91porn 九类视频目录、作者主页与自定义 URL 浏览
- 分类分页、上一页、下一页和指定页码跳转
- 标题、封面、作者、时长、播放量和 viewkey 展示
- 单选、全选和批量加入下载，下载队列支持继续追加
- `.m3u8` / `.mp4` 解析，HLS 分片并发、重试和断点续传
- 下载管理按全部、未完成、已完成筛选，支持选择、重试和删除任务
- 下载任务、进度和完成状态持久化，重新启动后自动续排未完成任务
- 扫描下载目录识别本地文件，跨分类显示“已下载”并按 viewkey 去重
- 日间/夜间主题、列表过渡动画和局部进度刷新
- 设置中心统一管理下载目录、归档方式、并发数和界面刷新间隔

## Windows 一键启动

需要 Windows 10/11 和 Python 3.11 或更高版本。下载 Release 压缩包并解压后，双击 `启动.cmd`。首次运行会自动创建 `.venv`、安装依赖、生成 `site.json`、启动本地服务并打开网页。

默认数据保存在 `downloads/`，视频保存在 `downloads/videos/`。可在网页设置中心修改视频目录和归档方式。

手动启动：

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
Copy-Item site.example.json site.json
.\.venv\Scripts\python.exe -m viewkey_batch.web
```

默认地址为 `http://127.0.0.1:8765`；端口被占用时会自动选择后续可用端口。

## 站点配置与请求限制

`启动.cmd` 首次运行时会从 `site.example.json` 生成 `site.json`。可在其中调整分类入口、请求间隔、随机抖动、重试次数和退避策略。

程序默认限制请求频率，遇到 `429` / `5xx` 会自动退避；识别到验证页面时会停止当前任务。每次访问分类会先建立站点会话并添加防缓存参数，避免不同分类返回相同旧数据。媒体下载前会重新解析视频页，以刷新可能过期的 CDN 地址。

## 命令行

```powershell
91fetch crawl --category latest --max-pages 10
91fetch crawl --author AUTHOR_ID
91fetch download --workers 2 --fragments 4
91fetch run --category featured --workers 2 --fragments 4
```

网页界面是默认使用方式；命令行适合一次性脚本任务。

## 开源协议

本项目采用 [MIT License](LICENSE)。你可以使用、复制、修改、合并、发布、分发、再许可和销售软件副本，但必须在软件副本或主要部分中保留原版权声明和许可声明。软件按现状提供，不附带任何明示或默示担保。

使用者应自行确认下载内容的保存权限，并遵守目标站点条款及所在地规定。
