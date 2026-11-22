"""通知抽象层：一个事件可以同时投递多个渠道。"""
from __future__ import annotations

import logging
import sys

from config import Config

log = logging.getLogger("nsu.notify")


class BaseProvider:
    name = "base"

    def available(self) -> bool:
        return True

    def send(self, title: str, content: str) -> bool:
        raise NotImplementedError


class DesktopProvider(BaseProvider):
    """按当前平台自动选择系统通知后端。"""

    name = "desktop"

    def __init__(self) -> None:
        self._impl = None
        self.detail = ""

    def _load(self) -> BaseProvider | None:
        if self._impl is not None:
            return self._impl
        platform = sys.platform
        if platform.startswith("win"):
            from .desktop import WindowsToastProvider
            self._impl = WindowsToastProvider()
        elif platform == "darwin":
            from .desktop import MacProvider
            self._impl = MacProvider()
        elif platform.startswith("linux"):
            from .desktop import LinuxProvider
            self._impl = LinuxProvider()
        else:
            self._impl = None
        if self._impl is not None:
            self.detail = getattr(self._impl, "detail", "")
        return self._impl

    def available(self) -> bool:
        impl = self._load()
        return bool(impl and impl.available())

    def send(self, title: str, content: str) -> bool:
        impl = self._load()
        if not impl:
            return False
        return impl.send(title, content)


class Notifier:
    """多渠道路由。任何单渠道失败都不影响扫描主流程。"""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.providers: list[BaseProvider] = []
        for raw_name in cfg.notify_providers:
            name = (raw_name or "").strip().lower()
            provider = self._build(name)
            if provider is not None:
                self.providers.append(provider)

    def _build(self, name: str) -> BaseProvider | None:
        if name in ("", "none", "off"):
            return None
        if name in ("desktop", "system", "auto"):
            return DesktopProvider()
        if name in ("webhook", "push"):
            from .webhook import WebhookProvider
            if not self.cfg.webhook_url:
                log.warning("Webhook缺少地址，已跳过")
                return None
            return WebhookProvider(self.cfg.webhook_url, self.cfg.webhook_kind)
        log.warning("未知通知渠道：%s（可选 desktop/webhook）", name)
        return None

    def describe(self) -> list[dict]:
        rows: list[dict] = []
        for p in self.providers:
            try:
                available = bool(p.available())
                detail = getattr(p, "detail", "")
            except Exception as exc:  # noqa: BLE001 - 单个渠道异常不能阻断状态读取
                log.warning("通知状态读取失败：%s，%s", p.name, exc)
                available = False
                detail = str(exc)[:120]
            rows.append({"name": p.name, "available": available, "detail": detail})
        return rows

    def send(self, title: str, content: str) -> bool:
        if not self.providers:
            log.warning("无可用通知渠道")
            return False
        success = False
        for provider in self.providers:
            try:
                ok = provider.send(title, content)
                if not ok:
                    log.warning("通知失败：%s", provider.name)
                success = success or ok
            except Exception as exc:  # noqa: BLE001 - 通知失败不应阻断主流程
                log.error("通知异常：%s，%s", provider.name, exc)
        return success
