"""版本检查。

优先读取 GitHub Pages 上的公开 version.json，避免匿名访问 GitHub API 的
速率限制；清单不可用时再回退到 GitHub Release API，并尽量用 ETag 缓存。
只负责查询和比较版本，不下载、不替换正在运行的程序。
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from netutil import ssl_context
from version import APP_VERSION, LATEST_RELEASE_API, RELEASES_URL, UPDATE_MANIFEST_URL

_VERSION_RE = re.compile(r"v?(\d+(?:\.\d+){1,2})", re.IGNORECASE)


@dataclass
class ReleaseInfo:
    version: str
    tag: str
    url: str
    name: str
    published_at: str = ""


def parse_version(value: str) -> tuple[int, ...]:
    """把 v1.0 / v1.0.2 / NSU-EventGrabber v1.0.2 归一成可比较的元组。"""
    match = _VERSION_RE.search(str(value or ""))
    if not match:
        return ()
    parts = [int(part) for part in match.group(1).split(".")]
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def extract_version(*values: str) -> str:
    for value in values:
        match = _VERSION_RE.search(str(value or ""))
        if match:
            parts = [int(part) for part in match.group(1).split(".")]
            while len(parts) < 3:
                parts.append(0)
            return ".".join(str(part) for part in parts[:3])
    return ""


def is_newer(latest: str, current: str = APP_VERSION) -> bool:
    latest_tuple = parse_version(latest)
    current_tuple = parse_version(current)
    return bool(latest_tuple and current_tuple and latest_tuple > current_tuple)


def _read_cache(cache_path: Optional[Path]) -> tuple[str, Optional[ReleaseInfo]]:
    if cache_path is None or not cache_path.exists():
        return "", None
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        release = payload.get("release") if isinstance(payload, dict) else None
        if not isinstance(release, dict):
            return "", None
        return str(payload.get("etag") or ""), ReleaseInfo(**release)
    except (OSError, ValueError, TypeError):
        return "", None


def _write_cache(cache_path: Optional[Path], etag: str, release: ReleaseInfo) -> None:
    if cache_path is None:
        return
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps({"etag": etag, "release": asdict(release)}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass


def _manifest_url() -> str:
    return (os.environ.get("NSU_UPDATE_MANIFEST_URL") or UPDATE_MANIFEST_URL).strip()


def _fetch_manifest(timeout: int) -> ReleaseInfo:
    """读取公开的 version.json；不经过 GitHub API，因此不受匿名限流影响。"""
    url = _manifest_url()
    if not url:
        raise RuntimeError("没有配置更新清单地址")
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Cache-Control": "no-cache",
            "User-Agent": f"NSU-EventGrabber/{APP_VERSION}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=ssl_context()) as response:
            raw = response.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise RuntimeError("更新清单尚未发布") from exc
        raise RuntimeError(f"更新清单返回 HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"无法连接更新清单：{exc.reason}") from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("更新清单不是有效 JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("更新清单格式不正确")

    version = extract_version(
        str(payload.get("version") or ""),
        str(payload.get("name") or ""),
        str(payload.get("tag") or ""),
    )
    if not version:
        raise RuntimeError("无法从更新清单识别版本号")

    return ReleaseInfo(
        version=version,
        tag=str(payload.get("tag") or ""),
        url=str(payload.get("url") or RELEASES_URL),
        name=str(payload.get("name") or ""),
        published_at=str(payload.get("published_at") or ""),
    )


def _fetch_api(timeout: int, cache_path: Optional[Path]) -> ReleaseInfo:
    cached_etag, cached_release = _read_cache(cache_path)
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"NSU-EventGrabber/{APP_VERSION}",
    }
    if cached_etag:
        headers["If-None-Match"] = cached_etag
    # 私有仓库的 Release 对匿名 API 不可见。允许开发者通过环境变量提供
    # 只读 token，不把 token 写进 GUI 设置或仓库。
    token = (os.environ.get("NSU_GITHUB_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(LATEST_RELEASE_API, headers=headers)
    etag = cached_etag
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=ssl_context()) as response:
            raw = response.read().decode("utf-8", "ignore")
            etag = response.headers.get("ETag") or etag
    except urllib.error.HTTPError as exc:
        if exc.code == 304 and cached_release is not None:
            return cached_release
        if exc.code == 403:
            raise RuntimeError("GitHub API 请求受限（可能触发速率限制）") from exc
        if exc.code == 404:
            raise RuntimeError("GitHub Release 接口返回 404（仓库可能为私有，或没有 Release）") from exc
        raise RuntimeError(f"GitHub API 返回 HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"无法连接 GitHub：{exc.reason}") from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("GitHub Release 返回的不是 JSON") from exc

    tag = str(payload.get("tag_name") or "")
    name = str(payload.get("name") or "")
    version = extract_version(name, tag)
    if not version:
        raise RuntimeError("无法从 Release 标题或 tag 中识别版本号")

    release = ReleaseInfo(
        version=version,
        tag=tag,
        url=str(payload.get("html_url") or RELEASES_URL),
        name=name,
        published_at=str(payload.get("published_at") or ""),
    )
    _write_cache(cache_path, etag, release)
    return release


def fetch_latest_release(timeout: int = 10, cache_path: Optional[Path] = None) -> ReleaseInfo:
    """先查公开清单，失败再查 GitHub API。

    Release 标题优先于 tag：发布流程把真正的版本号写在标题里
    （例如 ``NSU-EventGrabber v1.0.2``）。
    """
    manifest_error: Optional[Exception] = None
    try:
        return _fetch_manifest(timeout)
    except Exception as exc:  # noqa: BLE001 - 清单失败时继续走 API
        manifest_error = exc

    try:
        return _fetch_api(timeout, cache_path)
    except Exception as exc:  # noqa: BLE001 - 保留两个通道的错误信息
        if manifest_error is not None:
            raise RuntimeError(f"{exc}；更新清单也不可用：{manifest_error}") from exc
        raise


def describe_failure(exc: Exception) -> str:
    text = str(exc or "").strip()
    return text[:200] if text else exc.__class__.__name__
