from __future__ import annotations

from pathlib import Path
from urllib.parse import quote, urljoin
import logging

import typer
from rich.console import Console

from .crawler import Crawler, append_manifest
from .downloader import BatchDownloader
from .http import build_client, load_cookies
from .models import SiteConfig, VideoItem


app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console()


def setup(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format="%(levelname)s %(message)s")


def source_url(config: SiteConfig, category: str | None, author: str | None, url: str | None) -> str:
    supplied = sum(value is not None for value in (category, author, url))
    if supplied != 1:
        raise typer.BadParameter("--category、--author、--url 必须且只能提供一个")
    if url:
        return urljoin(config.base_url, url)
    if author:
        return urljoin(config.base_url, config.author_url.format(author=quote(author)))
    assert category
    if category not in config.category_urls:
        raise typer.BadParameter(f"未知分类 {category}，可用值: {', '.join(config.category_urls)}")
    return urljoin(config.base_url, config.category_urls[category])


def read_manifest(path: Path) -> list[VideoItem]:
    if not path.exists():
        return []
    unique: dict[str, VideoItem] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            item = VideoItem.from_json(line)
            unique[item.identity] = item
    return list(unique.values())


@app.command()
def crawl(
    config_path: Path = typer.Option(Path("site.json"), "--config", "-c"),
    category: str | None = typer.Option(None, "--category"),
    author: str | None = typer.Option(None, "--author"),
    url: str | None = typer.Option(None, "--url"),
    max_pages: int = typer.Option(0, min=0, help="0 表示抓到没有新内容为止"),
    manifest: Path = typer.Option(Path("downloads/manifest.jsonl")),
    cookies: Path | None = typer.Option(None, help="JSON 或 Netscape Cookie 文件"),
    resolve: bool = typer.Option(True, "--resolve/--no-resolve"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """遍历分类、作者或任意列表 URL，生成 JSONL 下载清单。"""
    setup(verbose)
    config = SiteConfig.load(config_path)
    start = source_url(config, category, author, url)
    existing = {item.identity for item in read_manifest(manifest)}
    added = 0
    with build_client(config, load_cookies(cookies)) as client:
        crawler = Crawler(client, config)
        for item in crawler.crawl(start, max_pages):
            if item.identity in existing:
                continue
            if resolve:
                item = crawler.resolve(item)
            append_manifest(manifest, item)
            existing.add(item.identity)
            added += 1
            console.print(f"[green]+[/green] {item.viewkey} {item.title}")
    console.print(f"新增 {added} 条，清单: {manifest}")


@app.command()
def download(
    manifest: Path = typer.Option(Path("downloads/manifest.jsonl")),
    output: Path = typer.Option(Path("downloads/videos")),
    config_path: Path = typer.Option(Path("site.json"), "--config", "-c"),
    workers: int = typer.Option(2, min=1, max=32, help="同时下载的视频数"),
    fragments: int = typer.Option(4, min=1, max=64, help="每个 HLS 的并发分片数"),
    rate_limit: int = typer.Option(0, min=0, help="单任务限速，字节/秒；0 为不限速"),
    cookies: Path | None = typer.Option(None, help="Netscape 格式 Cookie 文件"),
    refresh: bool = typer.Option(True, "--refresh/--no-refresh", help="下载前刷新临时视频地址"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """批量下载清单中的视频，自动使用 .part 文件续传。"""
    setup(verbose)
    items = read_manifest(manifest)
    if not items:
        raise typer.BadParameter(f"清单不存在或为空: {manifest}")
    if refresh:
        config = SiteConfig.load(config_path)
        with build_client(config, load_cookies(cookies)) as client:
            crawler = Crawler(client, config)
            items = [crawler.resolve(item) for item in items]
    results = BatchDownloader(output, workers, fragments, cookies, rate_limit).download(items)
    failed = [(item, error) for item, error in results if error]
    console.print(f"完成 {len(results) - len(failed)}/{len(results)}，失败 {len(failed)}")
    if failed:
        failure_path = manifest.with_name("failed.jsonl")
        failure_path.write_text("".join(item.to_json() + "\n" for item, _ in failed), encoding="utf-8")
        raise typer.Exit(1)


@app.command("run")
def run_all(
    config_path: Path = typer.Option(Path("site.json"), "--config", "-c"),
    category: str | None = typer.Option(None, "--category"),
    author: str | None = typer.Option(None, "--author"),
    url: str | None = typer.Option(None, "--url"),
    max_pages: int = typer.Option(0, min=0),
    output: Path = typer.Option(Path("downloads/videos")),
    workers: int = typer.Option(2, min=1, max=32),
    fragments: int = typer.Option(4, min=1, max=64),
    rate_limit: int = typer.Option(0, min=0, help="单任务限速，字节/秒；0 为不限速"),
    cookies_json: Path | None = typer.Option(None, help="抓取用 JSON Cookie"),
    cookies_txt: Path | None = typer.Option(None, help="下载用 Netscape Cookie"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """抓取并立即下载，不保留重复任务。"""
    setup(verbose)
    config = SiteConfig.load(config_path)
    start = source_url(config, category, author, url)
    manifest = Path("downloads/manifest.jsonl")
    existing = {item.identity: item for item in read_manifest(manifest)}
    items: list[VideoItem] = []
    with build_client(config, load_cookies(cookies_json)) as client:
        crawler = Crawler(client, config)
        for item in crawler.crawl(start, max_pages):
            resolved = crawler.resolve(item)
            items.append(resolved)
            if resolved.identity not in existing:
                append_manifest(manifest, resolved)
                existing[resolved.identity] = resolved
    results = BatchDownloader(output, workers, fragments, cookies_txt, rate_limit).download(items)
    failed = sum(error is not None for _, error in results)
    console.print(f"完成 {len(results) - failed}/{len(results)}，失败 {failed}")
    if failed:
        raise typer.Exit(1)


@app.command()
def menu() -> None:
    """交互式抓取和下载菜单。"""
    setup(False)
    config_path = Path("site.json")
    if not config_path.exists():
        raise typer.BadParameter("当前目录缺少 site.json，请从完整发布包启动")

    console.print("\n[bold]91Fetch[/bold]")
    console.print("1. 最新\n2. 当前最热\n3. 加精\n4. 指定作者\n5. 自定义列表 URL\n6. 下载已有清单\n0. 退出")
    choice = typer.prompt("请选择", default="1").strip()
    if choice == "0":
        return

    if choice == "6":
        download(
            manifest=Path("downloads/manifest.jsonl"),
            output=Path("downloads/videos"),
            config_path=config_path,
            workers=2,
            fragments=4,
            rate_limit=0,
            cookies=None,
            refresh=True,
            verbose=False,
        )
        return

    category = None
    author = None
    url = None
    choices = {"1": "latest", "2": "hot", "3": "featured"}
    if choice in choices:
        category = choices[choice]
    elif choice == "4":
        author = typer.prompt("作者 UID（作者主页 URL 中 UID= 后面的值）").strip()
    elif choice == "5":
        url = typer.prompt("列表 URL").strip()
    else:
        raise typer.BadParameter("菜单选项无效")

    max_pages = typer.prompt("抓取页数（0 表示直到没有新内容）", default=1, type=int)
    workers = typer.prompt("同时下载的视频数", default=2, type=int)
    fragments = typer.prompt("每个 HLS 的并发分片数", default=4, type=int)
    run_all(
        config_path=config_path,
        category=category,
        author=author,
        url=url,
        max_pages=max(0, max_pages),
        output=Path("downloads/videos"),
        workers=max(1, workers),
        fragments=max(1, fragments),
        rate_limit=0,
        cookies_json=None,
        cookies_txt=None,
        verbose=False,
    )


if __name__ == "__main__":
    app()
