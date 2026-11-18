"""单一职责：附件下载抽象。

- ``Fetcher`` 协议：``fetch(url) -> FetchResult``，把网络/文件差异挡在 attach 阶段之外；
- ``OfflineFetcher``：默认实现，只回放注册的内存字节，未注册 URL 报瞬时错误，
  保证整条流水线离线可测、绝不真的联网；
- ``HttpFetcher``：基于 httpx 的线上实现（运行环境缺依赖时延迟报错）。

下载结果一律先落到调用方提供的独立暂存目录（每个附件一个空目录），
返回字节与实际文件名，不做任何执行动作。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol


class FetchError(Exception):
    """下载失败基类。subtype: transient / missing。"""

    def __init__(self, message: str, *, subtype: str = "transient",
                 http_status: int | None = None):
        super().__init__(message)
        self.subtype = subtype
        self.http_status = http_status


@dataclass
class FetchResult:
    url: str
    content: bytes
    filename: str
    http_status: int = 200


class Fetcher(Protocol):
    def fetch(self, url: str) -> FetchResult: ...


class OfflineFetcher:
    """离线回放器：URL -> (字节, 文件名) 注册表；可注入失败脚本模拟重试场景。"""

    def __init__(self, pages: dict | None = None,
                 fail_script: Callable[[str, int], Exception | None] | None = None):
        self._pages: dict[str, tuple[bytes, str]] = dict(pages or {})
        # 每个 URL 已尝试次数，供 fail_script 实现“前两次失败、第三次成功”
        self.tries: dict[str, int] = {}
        self._fail_script = fail_script

    def register(self, url: str, content: bytes, filename: str | None = None) -> None:
        if filename is None:
            from .attach_blob import name_from_url
            filename = name_from_url(url)
        self._pages[url] = (content, filename)

    def fetch(self, url: str) -> FetchResult:
        n = self.tries.get(url, 0) + 1
        self.tries[url] = n
        if self._fail_script is not None:
            err = self._fail_script(url, n)
            if err is not None:
                raise err
        if url not in self._pages:
            raise FetchError(f"offline registry has no URL: {url}",
                             subtype="transient")
        content, filename = self._pages[url]
        return FetchResult(url=url, content=content, filename=filename)


class HttpFetcher:
    """线上 httpx 下载器。仅在显式选用时 import，离线测试不触发依赖。"""

    def __init__(self, timeout: float = 30.0):
        self._timeout = timeout

    def fetch(self, url: str) -> FetchResult:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - 环境缺依赖时的清晰报错
            raise FetchError(f"httpx unavailable: {exc}") from exc
        try:
            resp = httpx.get(url, timeout=self._timeout, follow_redirects=True)
        except httpx.HTTPError as exc:
            raise FetchError(f"transport error for {url}: {exc}",
                             subtype="transient") from exc
        if resp.status_code in (404, 410):
            raise FetchError(f"{resp.status_code} for {url}", subtype="missing",
                             http_status=resp.status_code)
        if resp.status_code >= 400:
            raise FetchError(f"http {resp.status_code} for {url}",
                             subtype="transient", http_status=resp.status_code)
        filename = ""
        disp = resp.headers.get("content-disposition", "")
        if "filename=" in disp:
            filename = disp.split("filename=", 1)[1].strip('"; ')
        if not filename:
            from .attach_blob import name_from_url
            filename = name_from_url(str(resp.url))
        return FetchResult(url=url, content=resp.content, filename=filename,
                           http_status=resp.status_code)
