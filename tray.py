"""系统托盘与后台常驻。

托盘依赖是可选的：没有 pystray / 没有桌面托盘时，程序退回普通关闭行为，
不会因为托盘初始化失败而影响主界面。
"""
from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path

from version import APP_VERSION

log = logging.getLogger("nsu.tray")


class TrayController:
    def __init__(self, controller) -> None:
        self.controller = controller
        self._icon = None
        self._thread = None
        self._available = False
        self._stopping = False

    @property
    def available(self) -> bool:
        return self._available

    def start(self) -> bool:
        if self._available:
            return True
        if sys.platform.startswith("linux") and not (
            os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
        ):
            log.warning("托盘不可用：无图形环境")
            return False
        try:
            import pystray
        except Exception as exc:  # noqa: BLE001 - 托盘缺失不能影响主程序
            log.warning("托盘不可用：%s", exc)
            return False

        try:
            kwargs = {}
            if sys.platform == "darwin":
                # 尽量复用 pywebview 所在的 NSApplication；找不到时 pystray
                # 会退回 sharedApplication，不影响主窗口继续运行。
                try:
                    from AppKit import NSApplication
                    kwargs["darwin_nsapplication"] = NSApplication.sharedApplication()
                except Exception:
                    pass
            icon = pystray.Icon(
                "NSU-EventGrabber",
                self._build_icon(),
                f"NSU-EventGrabber {APP_VERSION}",
                pystray.Menu(
                    pystray.MenuItem("显示主界面", self._show, default=True),
                    pystray.MenuItem("立即扫描", self._scan),
                    pystray.MenuItem("检查更新", self._check_update),
                    pystray.Menu.SEPARATOR,
                    pystray.MenuItem("退出", self._quit),
                ),
                **kwargs,
            )
            self._icon = icon
            if sys.platform == "darwin":
                # macOS 必须把托盘事件循环挂到 NSApplication 的主循环里；
                # pystray 的 run_detached 正是为此准备，不能像 Windows 一样丢到线程。
                if not hasattr(icon, "run_detached"):
                    raise RuntimeError("当前 pystray 不支持 macOS detached 模式")
                icon.run_detached()
            elif sys.platform.startswith("linux"):
                # Linux 的 appindicator 后端会在 run() 里自建 GLib 主循环。放到
                # 线程里跑会先抢占默认主上下文，随后 pywebview 的 g_application_run()
                # 报 "cannot acquire the default main context"，窗口直接起不来
                # （没装 appindicator 时退回 Xorg 后端，不用 GLib，所以不冲突）。
                # run_detached 只把后续操作以 idle 回调排进主窗口已有的 GTK 主循环。
                try:
                    icon.run_detached()
                except NotImplementedError:
                    self._thread = threading.Thread(
                        target=icon.run, name="nsu-tray", daemon=True
                    )
                    self._thread.start()
            else:
                self._thread = threading.Thread(
                    target=icon.run, name="nsu-tray", daemon=True
                )
                self._thread.start()
            self._available = True
            return True
        except Exception as exc:  # noqa: BLE001 - 托盘异常只影响关闭行为
            self._available = False
            self._icon = None
            hint = ""
            if sys.platform.startswith("linux"):
                hint = "；如需托盘可安装 gir1.2-ayatanaappindicator3-0.1"
            log.warning("托盘启动失败：%s%s", exc, hint)
            return False

    @staticmethod
    def _logo_file() -> Path | None:
        base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
        candidates = (
            base / "gui" / "web" / "NSU-EventGrabber.png",
            base / "packaging" / "NSU-EventGrabber.png",
            Path(__file__).resolve().parent / "gui" / "web" / "NSU-EventGrabber.png",
        )
        for path in candidates:
            if path.exists():
                return path
        return None

    @staticmethod
    def _build_icon():
        from PIL import Image, ImageDraw

        logo = TrayController._logo_file()
        if logo is not None:
            with Image.open(logo) as source:
                return source.convert("RGBA").resize((64, 64), Image.LANCZOS)

        # 资源缺失时的兜底图标，保持与主界面一致的蓝色。
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle(
            (4, 4, 59, 59), radius=14, fill=(7, 62, 149, 255), outline=(255, 255, 255, 255), width=2
        )
        draw.rectangle((20, 18, 44, 24), fill=(255, 255, 255, 255))
        draw.rectangle((20, 29, 40, 35), fill=(255, 255, 255, 255))
        draw.rectangle((20, 40, 44, 46), fill=(255, 255, 255, 255))
        draw.rectangle((20, 18, 26, 46), fill=(255, 255, 255, 255))
        return image

    def _show(self, icon=None, item=None) -> None:
        self.controller.show_window()

    def _scan(self, icon=None, item=None) -> None:
        self.controller.scan_now()

    def _check_update(self, icon=None, item=None) -> None:
        self.controller.check_update(force=True)

    def _quit(self, icon=None, item=None) -> None:
        self.controller.request_quit()

    def stop(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        self._available = False
        icon = self._icon
        if icon is not None:
            try:
                icon.stop()
            except Exception:  # noqa: BLE001 - 退出阶段不再处理托盘异常
                pass
        self._icon = None
        log.info("托盘已停止")
