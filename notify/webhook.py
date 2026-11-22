"""通用 Webhook 通知：Server酱 / PushPlus / Bark / Telegram / 企业微信 / 自定义 JSON。

给 GitHub Actions、无桌面服务器这类没有系统通知的场景用。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from netutil import ssl_context
from version import APP_VERSION

from .base import BaseProvider

USER_AGENT = f"NSU-EventGrabber/{APP_VERSION}"


class WebhookProvider(BaseProvider):
    name = "webhook"

    def __init__(self, url: str, kind: str = "auto") -> None:
        self.url = url.strip()
        self.kind = self._detect(kind.lower())
        self.detail = self.kind

    def _detect(self, kind: str) -> str:
        if kind and kind != "auto":
            return kind
        url = self.url.lower()
        if "sctapi.ftqq.com" in url or "sc.ftqq.com" in url:
            return "serverchan"
        if "pushplus" in url:
            return "pushplus"
        if "api.day.app" in url:
            return "bark"
        if "api.telegram.org" in url:
            return "telegram"
        if "qyapi.weixin.qq.com" in url:
            return "wecom"
        return "json"

    def available(self) -> bool:
        return bool(self.url)

    def _post(self, url: str, payload: dict | None, *, form: dict | None = None) -> bool:
        headers = {"User-Agent": USER_AGENT}
        if form is not None:
            data = urllib.parse.urlencode(form).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15, context=ssl_context()) as resp:
                return 200 <= resp.status < 300
        except (urllib.error.URLError, OSError):
            return False

    def _get(self, url: str) -> bool:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=15, context=ssl_context()) as resp:
                return 200 <= resp.status < 300
        except (urllib.error.URLError, OSError):
            return False

    def send(self, title: str, content: str) -> bool:
        text = content.replace("**", "")
        try:
            if self.kind == "serverchan":
                return self._post(self.url, None, form={"title": title, "desp": text})
            if self.kind == "pushplus":
                token = urllib.parse.parse_qs(urllib.parse.urlparse(self.url).query).get("token", [""])[0]
                if not token:
                    token = self.url.rstrip("/").split("/")[-1]
                base = "https://www.pushplus.plus/send"
                return self._post(base, {"token": token, "title": title, "content": text})
            if self.kind == "bark":
                base = self.url.rstrip("/")
                return self._get(
                    f"{base}/{urllib.parse.quote(title, safe='')}/"
                    f"{urllib.parse.quote(text[:500], safe='')}"
                )
            if self.kind == "telegram":
                parsed = urllib.parse.urlparse(self.url)
                query = urllib.parse.parse_qs(parsed.query)
                chat_id = query.get("chat_id", [""])[0]
                body = {"text": f"{title}\n{text}"}
                if chat_id:
                    body["chat_id"] = chat_id
                return self._post(urllib.parse.urlunparse(parsed._replace(query="")), body)
            if self.kind == "wecom":
                return self._post(
                    self.url, {"msgtype": "text", "text": {"content": f"{title}\n{text}"}}
                )
            return self._post(self.url, {"title": title, "content": text})
        except (urllib.error.URLError, OSError, ValueError):
            return False
