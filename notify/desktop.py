"""各平台的原生桌面通知实现（按需导入，避免无谓依赖）。

- macOS：osascript（系统自带）
- Linux：notify-send（libnotify），降级 zenity
- Windows：PowerShell + WinRT Toast，零第三方依赖
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from shutil import which

from .base import BaseProvider


class MacProvider(BaseProvider):
    name = "mac"

    def __init__(self) -> None:
        self.detail = "osascript"
        self._osascript = which("osascript")

    def available(self) -> bool:
        return sys.platform == "darwin" and bool(self._osascript)

    @staticmethod
    def _escape(text: str) -> str:
        return text.replace("\\", "\\\\").replace('"', '\\"').replace("\r", "").replace("\n", "\\n")

    def send(self, title: str, content: str) -> bool:
        if not self._osascript:
            return False
        body = content.replace("**", "").strip()
        if len(body) > 800:
            body = body[:800] + "…"
        script = (
            f'display notification "{self._escape(body)}" '
            f'with title "{self._escape(title)}" sound name "Glass"'
        )
        try:
            completed = subprocess.run(
                [self._osascript, "-e", script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
            )
            return completed.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False


class LinuxProvider(BaseProvider):
    name = "linux"

    def __init__(self) -> None:
        self._notify_send = which("notify-send")
        self._zenity = which("zenity")
        if self._notify_send:
            self.detail = "notify-send"
        elif self._zenity:
            self.detail = "zenity"
        else:
            self.detail = "未找到 notify-send/zenity"

    def available(self) -> bool:
        return sys.platform.startswith("linux") and bool(self._notify_send or self._zenity)

    def send(self, title: str, content: str) -> bool:
        body = content.replace("**", "").strip()
        if len(body) > 800:
            body = body[:800] + "…"
        if self._notify_send:
            try:
                completed = subprocess.run(
                    [
                        self._notify_send,
                        "-a", "NSU-EventGrabber",
                        "-u", "normal",
                        title,
                        body,
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                )
                if completed.returncode == 0:
                    return True
            except (OSError, subprocess.SubprocessError):
                pass
        if self._zenity:
            try:
                completed = subprocess.run(
                    ["zenity", "--notification", f"--title={title}", f"--text={body}"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                )
                return completed.returncode == 0
            except (OSError, subprocess.SubprocessError):
                return False
        return False


# 用 PowerShell 自己的 AppID，无需注册也能正常弹出
DEFAULT_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"

TOAST_SCRIPT = r"""
param([string]$Title, [string]$Body, [string]$AppId)
$null = [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$texts = $template.GetElementsByTagName('text')
$null = $texts.Item(0).AppendChild($template.CreateTextNode($Title))
$null = $texts.Item(1).AppendChild($template.CreateTextNode($Body))
$toast = [Windows.UI.Notifications.ToastNotification]::new($template)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($AppId).Show($toast)
"""


class WindowsToastProvider(BaseProvider):
    name = "win"

    def __init__(self) -> None:
        self.detail = "PowerShell Toast"
        self._powershell = which("powershell") or which("pwsh")

    def available(self) -> bool:
        return sys.platform.startswith("win") and bool(self._powershell)

    def send(self, title: str, content: str) -> bool:
        if not self._powershell:
            return False
        body = content.replace("**", "").strip()
        if len(body) > 800:
            body = body[:800] + "…"
        script_path = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", suffix=".ps1", delete=False, encoding="utf-8-sig"
            ) as handle:
                handle.write(TOAST_SCRIPT)
                script_path = handle.name
            extra = {}
            if sys.platform.startswith("win"):
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = subprocess.SW_HIDE
                extra["startupinfo"] = startupinfo
                extra["creationflags"] = subprocess.CREATE_NO_WINDOW
            completed = subprocess.run(
                [
                    self._powershell,
                    "-NoProfile",
                    "-NonInteractive",
                    "-WindowStyle", "Hidden",
                    "-ExecutionPolicy", "Bypass",
                    "-File", script_path,
                    title,
                    body,
                    os.environ.get("NSU_TOAST_APP_ID", DEFAULT_APP_ID),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
                check=False,
                **extra,
            )
            return completed.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False
        finally:
            if script_path:
                try:
                    Path(script_path).unlink(missing_ok=True)
                except OSError:
                    pass
