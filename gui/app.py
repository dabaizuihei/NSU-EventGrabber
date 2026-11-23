"""pywebview 图形界面：系统 WebView 渲染本地单页，无内嵌浏览器、无 npm 构建。"""
from __future__ import annotations

import collections
import copy
import hashlib
import importlib.util
import logging
import os
import shutil
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from auth import AuthManager, CredentialStore
from client import NsuClient
from config import Config, load_config, save_settings
from login_webview import login_with_webview, purge_login_cache, terminate_login_child
from notify import Notifier
from scanner import activity_title, activity_view, scan_once
from single_instance import SingleInstance
from state import State
from tray import TrayController
from updater import describe_failure, fetch_latest_release, is_newer
from version import APP_VERSION

log = logging.getLogger("nsu.gui")


@dataclass
class Runtime:
    """图形界面运行时上下文。"""

    cfg: Config
    store: CredentialStore
    auth: AuthManager
    client: NsuClient
    state: State
    notifier: Notifier


def build_runtime(cfg: Config) -> Runtime:
    store = CredentialStore(cfg.auth_file)
    auth = AuthManager(cfg, store)
    state = State(cfg.state_file)
    client = NsuClient(cfg, auth)
    notifier = Notifier(cfg)
    return Runtime(cfg=cfg, store=store, auth=auth, client=client, state=state, notifier=notifier)


class _LogHandler(logging.Handler):
    def __init__(self, controller: "Controller") -> None:
        super().__init__()
        self.controller = controller

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:  # noqa: BLE001
            return
        self.controller.push_log(record.levelname, message, record.created)


class Controller:
    def __init__(self, config_path: Optional[str] = None, data_dir: Optional[str] = None) -> None:
        self.config_path = config_path
        self.cfg: Config = load_config(config_path, data_dir)
        self.runtime = build_runtime(self.cfg)
        self._lock = threading.RLock()
        self._logs: collections.deque = collections.deque(maxlen=400)
        self._activities: list[dict] = []
        self._records: list[dict] = []
        self._running = False
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # 监控循环的代号：停止后立刻再启动时，旧循环可能还在收尾，不能让它
        # 继续循环，也不能让它把新循环的 _running 改成 False。
        self._monitor_token = 0
        self._scan_lock = threading.Lock()
        self._last_scan_at = 0.0
        self._last_summary = ""
        self._notifications: list[dict] = []
        self._notify_seq = 0
        self._auth_notified = False
        self._login_open = False
        self._was_logged_in = self.runtime.auth.logged_in
        self._cred_mtime = self._credential_mtime()
        self._window = None
        self._window_hidden = False
        self._quitting = False
        self.tray: Optional[TrayController] = None
        self.web_url = ""
        self._update = {
            "checking": False,
            "available": False,
            "current": APP_VERSION,
            "latest": "",
            "url": "",
            "checked_at": 0.0,
            "error": "",
        }
        self._last_reload_at = 0.0
        self.logout_flag = Path(self.cfg.data_dir) / "logged-out.flag"
        self._explicit_logout = self.logout_flag.exists()
        self._auto_login_checked = False
        self._ui_sync_stop = threading.Event()
        self._ui_sync_thread: Optional[threading.Thread] = None

        handler = _LogHandler(self)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logging.getLogger("nsu").addHandler(handler)
        logging.getLogger("nsu").setLevel(logging.INFO)

        self.push_log("INFO", f"数据目录：{self.cfg.data_dir}")

    # ---- 基础 ------------------------------------------------------------
    def attach_window(self, window) -> None:
        self._window = window
        self._start_ui_sync()

    def setup_tray(self) -> None:
        """在主窗口创建后启动系统托盘；托盘不可用时安静回退。"""
        tray = TrayController(self)
        if tray.start():
            self.tray = tray
        else:
            self.tray = None

    def should_hide_on_close(self) -> bool:
        return bool(
            self.cfg.close_to_tray
            and not self._quitting
            and self.tray is not None
            and self.tray.available
        )

    def hide_to_tray(self) -> bool:
        """关闭主窗口时隐藏到托盘，监控与通知继续运行。"""
        if not self.should_hide_on_close():
            return False
        window = self._window
        if window is None:
            return False
        try:
            window.hide()
            self._window_hidden = True
            return True
        except Exception:
            log.exception("托盘隐藏失败")
            return False

    @staticmethod
    def _activate_mac_app() -> None:
        """把进程带回前台；只做一次 AppKit 主循环调度，不阻塞托盘回调。"""
        if sys.platform != "darwin":
            return
        try:
            from AppKit import NSApplication
            from PyObjCTools import AppHelper

            def activate() -> None:
                app = NSApplication.sharedApplication()
                app.unhide_(None)
                app.activateIgnoringOtherApps_(True)

            AppHelper.callAfter(activate)
        except Exception:
            log.debug("激活应用失败", exc_info=True)

    def show_window(self) -> None:
        window = self._window
        if window is None:
            return
        try:
            self._activate_mac_app()
            # 某些平台从最小化状态调用 show() 不会还原窗口，能 restore 就先还原。
            if hasattr(window, "restore"):
                try:
                    window.restore()
                except Exception:
                    log.debug("窗口还原失败", exc_info=True)
            window.show()
            self._window_hidden = False
            # macOS 的 window.show() 只是把 makeKeyAndOrderFront_ 排进主循环。
            # 如果在托盘回调里紧接着同步执行 evaluate_js，主线程会等 JS 回调，
            # 而 JS 回调又排在这个主循环后面，最终整窗卡死。
            timer = threading.Timer(0.25, self._refresh_gui)
            timer.daemon = True
            timer.start()
        except Exception:
            log.exception("托盘恢复失败")

    def _reload_gui(self) -> None:
        """登录状态变化后只推送状态，绝不在后台线程 load_url 重载整页。

        pywebview 在 macOS/Windows 上从非 GUI 线程调用 load_url 时，可能让
        整个 WebView 进入加载态并阻塞事件处理，表现为主窗口完全点不动。
        前端已经有启动重试和轮询，直接触发一次状态拉取即可。
        """
        window = self._window
        if window is None or not self.web_url:
            return
        now = time.time()
        if now - self._last_reload_at < 0.8:
            return
        self._last_reload_at = now
        threading.Thread(target=self._refresh_gui, daemon=True).start()

    def _start_ui_sync(self) -> None:
        """Python 侧心跳，绕开 WebView 在失焦/后台时暂停 JS 定时器的问题。"""
        if self._ui_sync_thread and self._ui_sync_thread.is_alive():
            return

        def worker() -> None:
            first = True
            while not self._ui_sync_stop.is_set():
                if self._ui_sync_stop.wait(1.5 if first else 4.0):
                    return
                first = False
                if self._window is not None:
                    self._refresh_gui()

        self._ui_sync_thread = threading.Thread(target=worker, daemon=True)
        self._ui_sync_thread.start()

    def _refresh_gui(self, focus: bool = False) -> None:
        """让前端立即拉一次状态。

        这里只执行 JS，不在后台线程调用 show/restore/on_top/load_url；
        这些原生窗口操作在 macOS 上可能阻塞主线程，造成登录后整窗无响应。
        """
        window = self._window
        if window is None or self._window_hidden:
            return
        # Cocoa 的 evaluateJavaScript 会同步等待主循环回调。如果当前已经是
        # 主线程，直接调用就会自己等自己，所以统一挪到后台线程。
        if sys.platform == "darwin" and threading.current_thread() is threading.main_thread():
            timer = threading.Timer(0.0, self._refresh_gui, args=(focus,))
            timer.daemon = True
            timer.start()
            return
        try:
            window.evaluate_js(
                "(window.__nsuBoot && window.__nsuBoot());"
                " (window.__nsuRefresh && window.__nsuRefresh())"
            )
        except Exception:
            log.warning("界面刷新失败", exc_info=True)

    def push_log(self, level: str, message: str, created: float | None = None) -> None:
        with self._lock:
            self._logs.append(
                {"time": created or time.time(), "level": level, "message": message}
            )

    def _credential_mtime(self) -> float:
        try:
            return self.runtime.cfg.auth_file.stat().st_mtime
        except OSError:
            return 0.0

    def _mark_logged_in(self) -> None:
        """登录成功后清除“主动退出”标记，允许下次启动恢复登录态。"""
        self._explicit_logout = False
        try:
            self.logout_flag.unlink(missing_ok=True)
        except OSError:
            pass

    def _mark_logged_out(self) -> None:
        """主动退出登录：持久化标记，下次启动不要自动弹登录窗口。"""
        self._explicit_logout = True
        try:
            self.logout_flag.parent.mkdir(parents=True, exist_ok=True)
            self.logout_flag.write_text("1", encoding="utf-8")
        except OSError:
            log.warning("退出标记写入失败", exc_info=True)

    def _credential_signature(self) -> tuple[str, str, str, str]:
        c = self.runtime.store.credentials
        return (c.access_token, c.refresh_token, c.username, c.tenant_id)

    def _reload_credentials_if_changed(self) -> None:
        mtime = self._credential_mtime()
        if not mtime or mtime == self._cred_mtime:
            return
        self._cred_mtime = mtime
        before = self._credential_signature()
        self.runtime.store.load()
        after = self._credential_signature()
        if after == before:
            # fetch_profile() 会写回凭据文件并改变 mtime。只比较文件时间会在
            # 下一次轮询再次触发，形成每秒一次的“检测到新凭据”循环。
            return
        was_logged_in = self._was_logged_in
        self._was_logged_in = self.runtime.auth.logged_in
        epoch = self.runtime.auth.epoch

        def apply_change() -> None:
            if not self.runtime.auth.is_current(epoch):
                return
            if self.runtime.auth.logged_in:
                self.runtime.auth.fetch_profile(epoch)
                self.push_log("INFO", "新凭据已载入")
                if not was_logged_in:
                    self._auth_notified = False
                    self.scan_now()
            else:
                self.push_log("INFO", "凭据已清除")

        # 不在 JS API 的 get_state 轮询线程里做网络请求和文件写入，避免界面卡住。
        threading.Thread(target=apply_change, daemon=True).start()

    def _push_notification(self, title: str, content: str) -> None:
        with self._lock:
            self._notify_seq += 1
            self._notifications.append(
                {"id": self._notify_seq, "title": title, "content": content}
            )
            self._notifications = self._notifications[-50:]

    # ---- 状态 ------------------------------------------------------------
    def maybe_check_update(self) -> None:
        """启动后按设置自动检查一次；手动检查仍可随时触发。"""
        if self.cfg.auto_check_update:
            self.check_update()

    def open_external(self, url: str = "") -> dict:
        """在系统默认浏览器中打开更新页，避免主界面被导航走。"""
        parsed = urlparse(str(url or "").strip())
        if parsed.scheme.lower() not in {"http", "https"}:
            return {"ok": False, "message": "链接格式不正确"}
        try:
            if not webbrowser.open(parsed.geturl()):
                return {"ok": False, "message": "无法打开浏览器"}
        except Exception as exc:  # noqa: BLE001 - 打开失败不应影响主界面
            log.warning("打开外部链接失败", exc_info=True)
            return {"ok": False, "message": str(exc)}
        return {"ok": True, "message": ""}

    def check_update(self, force: bool = False) -> dict:
        if not force and not self.cfg.auto_check_update:
            return {"ok": False, "message": "自动更新检查已关闭"}
        with self._lock:
            if self._update["checking"]:
                return {"ok": True, "message": "正在检查更新…"}
            checked_at = float(self._update.get("checked_at") or 0)
            if not force and checked_at and time.time() - checked_at < 6 * 3600:
                return {"ok": True, "message": "最近 6 小时内已检查过更新"}
            self._update["checking"] = True
            self._update["error"] = ""
        threading.Thread(target=self._check_update_worker, daemon=True).start()
        return {"ok": True, "message": "正在检查更新…"}

    def _check_update_worker(self) -> None:
        try:
            release = fetch_latest_release(
                cache_path=Path(self.cfg.data_dir) / "update-cache.json"
            )
            available = is_newer(release.version, APP_VERSION)
            checked_at = time.time()
            with self._lock:
                self._update.update(
                    {
                        "checking": False,
                        "available": available,
                        "current": APP_VERSION,
                        "latest": release.version,
                        "url": release.url,
                        "checked_at": checked_at,
                        "error": "",
                    }
                )
            if available:
                self.push_log(
                    "INFO",
                    "发现新版本，可在「设置 → 关于」查看 Release 页面",
                )
                self._push_notification(
                    "[NSU-EventGrabber] 发现新版本",
                    "有新版本可用，可在「设置 → 关于」查看 Release 页面。",
                )
        except Exception as exc:
            with self._lock:
                self._update.update(
                    {
                        "checking": False,
                        "available": False,
                        "latest": "",
                        "url": "",
                        "checked_at": time.time(),
                        "error": describe_failure(exc),
                    }
                )
            self.push_log("WARNING", f"更新检查失败：{describe_failure(exc)}")
        finally:
            self._refresh_gui()

    # ---- 状态 ------------------------------------------------------------
    def get_state(self, cursor: int | str = 0) -> dict:
        self._reload_credentials_if_changed()
        try:
            cursor = int(cursor)
        except (TypeError, ValueError):
            cursor = 0
        creds = self.runtime.store.credentials
        remaining = int(creds.expires_at - time.time()) if creds.expires_at else 0
        with self._lock:
            notifications = [n for n in self._notifications if n["id"] > cursor]
            notifications_cursor = self._notify_seq
            logs = list(self._logs)
            activities = list(self._activities)
            raw_count = len(self._records)
            update = dict(self._update)
        return {
            "auth": {
                "logged_in": self.runtime.auth.logged_in,
                "login_open": self._login_open,
                "has_refresh": bool(creds.refresh_token),
                "username": (creds.username or self.cfg.username) if self.runtime.auth.logged_in else "",
                "display_name": creds.display_name,
                "student_no": creds.student_no,
                "remaining": max(0, remaining),
                "expires_at": creds.expires_at,
            },
            "running": self._running,
            "scanning": self._scan_lock.locked(),
            "config": self.cfg.public_dict(),
            "activities": activities,
            "hidden_count": max(0, raw_count - len(activities)),
            "logs": logs,
            "notifications": notifications,
            "notifications_cursor": notifications_cursor,
            "last_scan_at": self._last_scan_at,
            "app_version": APP_VERSION,
            "tray_available": bool(self.tray and self.tray.available),
            "update": update,
            "initialized": self.runtime.state.initialized,
            "stats": {
                "total": len(activities),
                "unregistered": len([a for a in activities if a["status"] == 4 and not a["past"]]),
                "registered": len([
                    a for a in activities
                    if a["registered"] and (
                        not self.cfg.hide_past_registered
                        or a.get("auditing") or not a.get("past_activity")
                    )
                ]),
            },
        }

    # ---- 扫描 ------------------------------------------------------------
    def _do_scan(self) -> dict:
        if not self._scan_lock.acquire(blocking=False):
            return {"ok": False, "message": "上一轮扫描还在进行"}
        epoch = self.runtime.auth.epoch
        try:
            result = scan_once(
                self.cfg,
                self.runtime.client,
                self.runtime.notifier,
                self.runtime.state,
                notify_auth_failure=not self._auth_notified,
            )
            if not self.runtime.auth.is_current(epoch):
                # 扫描期间用户退出了登录：丢弃结果，更不能走“自动重新登录”，
                # 否则刚退出就会自己弹出登录窗口。
                self.push_log("INFO", "登录已退出，丢弃本轮数据")
                return {"ok": False, "message": "已退出登录"}
            with self._lock:
                # 成功但返回空列表时也要清掉旧数据；请求失败（result.error）则保留
                # 上一次结果，避免网络抖动把界面清空。
                if result.records or not result.error:
                    self._records = result.records
                    self._activities = (
                        activity_view(result.records, self.cfg) if result.records else []
                    )
                self._last_scan_at = time.time()
                self._last_summary = result.summary
            if result.error:
                self.push_log("ERROR", f"扫描失败：{result.error}")
                if result.error == "登录态失效":
                    self._auth_notified = True
                    self._push_notification(
                        "[NSU-EventGrabber] 登录态失效",
                        "请在界面上重新登录；登录后监控会自动恢复。",
                    )
                    if self.cfg.auto_relogin and not self._login_open and not self.logout_flag.exists():
                        self.push_log("INFO", "登录失效，打开登录窗口")
                        self.login()
            else:
                self._auth_notified = False
                for record in result.new_activities:
                    self.push_log("INFO", f"扫描到新活动：{activity_title(record)}")
            if result.title and result.changed:
                self._push_notification(result.title, result.content)
            self.runtime.state.save()
            self._refresh_gui()
            return {
                "ok": True,
                "changed": result.changed,
                "summary": result.summary,
                "error": result.error,
            }
        finally:
            self._scan_lock.release()

    def scan_now(self) -> dict:
        thread = threading.Thread(target=self._do_scan, daemon=True)
        thread.start()
        return {"ok": True, "message": "已开始扫描"}

    def _monitor_loop(self, token: int) -> None:
        while token == self._monitor_token and not self._stop_event.is_set():
            try:
                self._do_scan()
            except Exception as exc:  # noqa: BLE001 - 单轮异常不终止监控
                self.push_log("ERROR", f"扫描异常：{exc}")
            if self._stop_event.wait(self.cfg.scan_interval):
                break
        with self._lock:
            if token == self._monitor_token:
                self._running = False
                self.push_log("INFO", "监控停止")

    def start_monitor(self) -> dict:
        if self._running:
            return {"ok": True, "message": "监控已在运行"}
        # 先废除可能还在收尾的旧循环，再启动新循环。
        self._monitor_token += 1
        token = self._monitor_token
        self._stop_event.clear()
        self._running = True
        self._thread = threading.Thread(target=self._monitor_loop, args=(token,), daemon=True)
        self._thread.start()
        return {"ok": True, "message": "监控已启动"}

    def stop_monitor(self) -> dict:
        if not self._running:
            return {"ok": True, "message": "监控未运行"}
        # token 先失效，旧循环即使还在扫描，收尾后也不会继续跑下一轮。
        self._monitor_token += 1
        self._stop_event.set()
        self._running = False
        return {"ok": True, "message": "正在停止…"}

    # ---- 登录 ------------------------------------------------------------
    def maybe_auto_login(self) -> None:
        """启动后检查登录态：有效就保留，失效且非主动退出就自动弹登录。"""
        if self._auto_login_checked:
            return
        self._auto_login_checked = True
        # 启动登录窗只服从“上次是否主动退出登录”，不再受扫描设置里的
        # 自动重登开关影响。否则设置页异常把开关存成 false 后，登录态失效
        # 时反而不会弹窗，重启后就像账号被静默丢掉。
        if self.logout_flag.exists():
            log.info("上次已退出，跳过自动登录")
            return
        if self._login_open:
            return

        def worker() -> None:
            try:
                if self.runtime.auth.ensure_valid():
                    self._reload_credentials_if_changed()
                    self._refresh_gui()
                    self._maybe_start_auto_monitor()
                    return
                # 没有凭据，或 refresh token 已失效。主动退出标记可能在这期间产生。
                if self.logout_flag.exists():
                    return
                log.info("登录失效，打开登录窗口")
                self.login()
            except Exception:
                log.exception("登录检查失败，打开登录窗口")
                if not self.logout_flag.exists():
                    self.login()

        if not self.runtime.auth.access_token and not self.runtime.auth.refresh_token:
            log.info("无登录凭据，打开登录窗口")
            self.login()
            return
        threading.Thread(target=worker, daemon=True).start()

    def login(self) -> dict:
        if not self._gui_available():
            return {"ok": False, "message": "当前环境不支持图形登录，请先安装 pywebview"}
        if self._login_open:
            return {"ok": False, "message": "登录窗口已经打开了，请先完成登录"}

        # 记录本次登录对应的会话版本：期间如果用户点了退出，版本会变化，
        # 迟到的登录结果就不能再把账号写回来。
        epoch = self.runtime.auth.epoch
        self._login_open = True
        self.push_log("INFO", "登录窗口已打开")
        # 登录提交后会先写凭据、再做资料/探活请求。监听凭据文件变化，
        # 一旦写完就提前刷新主界面，不必等后台收尾线程全部结束。
        threading.Thread(
            target=self._login_refresh_watchdog,
            args=(epoch, self._credential_mtime()),
            daemon=True,
        ).start()
        # 登录窗口最多会开 300 秒。不要在这个 JS API 调用里同步等待整个登录流程：
        # pywebview 的桥接队列会被长调用占住，主界面的 get_state 轮询、扫描按钮
        # 和日志刷新都会排队，表现为登录后设置页/日志全空，直到下一次保存才恢复。
        threading.Thread(
            target=self._login_window_worker,
            args=(epoch,),
            daemon=True,
        ).start()
        return {"ok": True, "message": "登录窗口已打开，请完成登录"}

    def _maybe_start_auto_monitor(self) -> None:
        """登录态可用且开启“打开软件自动监控”时，启动持续监控。"""
        if (
            self.cfg.auto_monitor
            and self.runtime.auth.logged_in
            and not self._login_open
            and not self._running
        ):
            self.start_monitor()

    def _login_refresh_watchdog(self, epoch: int, baseline_mtime: float) -> None:
        """凭据一写入就刷新界面，避免登录收尾请求拖住“登录中”状态。"""
        deadline = time.time() + 320
        while self._login_open and time.time() < deadline:
            mtime = self._credential_mtime()
            if mtime and mtime != baseline_mtime:
                self._reload_credentials_if_changed()
                # 凭据已经落盘，立即解除“登录中”，不要等登录子进程完全收尾。
                if self.runtime.auth.logged_in:
                    self._login_open = False
                self.push_log("INFO", "凭据已保存，界面已刷新")
                self._reload_gui()
                return
            time.sleep(0.4)

    def _login_window_worker(self, epoch: int) -> None:
        try:
            ok, message = login_with_webview(
                self.cfg,
                self.runtime.auth,
                timeout=300,
                start_loop=False,
                epoch=epoch,
            )
        except Exception as exc:  # noqa: BLE001 - 不让窗口异常拖垮主界面
            log.exception("登录窗口异常")
            ok, message = False, f"登录窗口异常：{exc}"
        finally:
            self._login_open = False

        if ok and not self.runtime.auth.is_current(epoch):
            ok, message = False, "登录已退出，取消本次登录"
        if ok:
            self._was_logged_in = True
            self._mark_logged_in()
            self.push_log("INFO", "登录成功")
            self._reload_credentials_if_changed()
            # 只推送状态，不重载整页；重载会让 WebView 暂时无响应。
            self._reload_gui()
            if self.cfg.auto_monitor:
                self._maybe_start_auto_monitor()
            else:
                self.scan_now()
        else:
            self.push_log("WARNING", f"登录失败：{message}")
        # 登录子窗口关闭后，主窗口的 1.2 秒定时器可能还没恢复；主动推一次，
        # 再错峰补几次，避免窗口刚拿回焦点时 evaluate_js 还没生效。
        self._refresh_gui(focus=True)
        for delay in (0.6, 1.6, 3.0):
            timer = threading.Timer(delay, self._refresh_gui)
            timer.daemon = True
            timer.start()

    def logout(self) -> dict:
        self.stop_monitor()
        self._mark_logged_out()
        # 先让会话版本失效，再结束登录窗口：这样即使扫描、刷新、拉资料或
        # 登录提交此刻正好在网络上等待，返回后也会发现版本不一致而放弃写入，
        # 不会把旧 token 重新写回凭据文件。
        self.runtime.auth.reset_session()
        terminate_login_child()
        self.runtime.store.clear()
        # 清掉“记住的用户名”，否则退出后右上角仍会显示旧账号，
        # 手动登录窗口也会把旧账号填回去。
        self.cfg.username = ""
        self._was_logged_in = False
        self._auth_notified = False
        self._cred_mtime = self._credential_mtime()
        with self._lock:
            self._activities = []
            self._records = []
            self._last_summary = ""
            self._last_scan_at = 0.0
        try:
            self.runtime.state.seen_ids = []
            self.runtime.state.registered_ids = []
            self.runtime.state.ever_registered_ids = []
            self.runtime.state.quota = {}
            self.runtime.state.initialized = False
            self.runtime.state.path.unlink(missing_ok=True)
        except Exception:
            pass
        try:
            purge_login_cache(Path(self.cfg.data_dir))
        except Exception:
            pass
        self.push_log("INFO", "凭据与缓存已清除")
        return {"ok": True, "message": "已退出登录"}

    def _gui_available(self) -> bool:
        return importlib.util.find_spec("webview") is not None

    # ---- 其它操作 ------------------------------------------------------------
    def register(self, activity_id: str) -> dict:
        if not activity_id:
            return {"ok": False, "message": "活动 ID 为空"}

        def worker():
            ok, message = self.runtime.client.register(activity_id)
            self.push_log("INFO" if ok else "WARNING", f"手动报名：{activity_id}，{message}")
            self._do_scan()

        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True, "message": "已提交报名请求"}

    def cancel(self, activity_id: str) -> dict:
        """取消报名：按活动 ID 找到报名记录 ID，再调用平台取消接口。"""
        with self._lock:
            activity = next((a for a in self._activities if str(a.get("id")) == str(activity_id)), None)
        if not activity:
            return {"ok": False, "message": "活动不在当前列表中，请先扫描"}
        register_id = activity.get("register_id") or ""
        if not register_id:
            return {"ok": False, "message": "没有找到报名记录 ID，无法取消"}
        if not activity.get("can_cancel"):
            return {"ok": False, "message": "平台未开放这场活动的取消入口"}

        ok, message = self.runtime.client.cancel(register_id)
        self.push_log("INFO" if ok else "WARNING", f"取消报名：{activity_id}，{message}")
        if ok:
            threading.Thread(target=self._do_scan, daemon=True).start()
        return {"ok": ok, "message": message}

    def test_notify(self, providers=None, webhook_url=None) -> dict:
        """用设置页当前勾选的渠道发测试通知，不要求先保存。"""
        cfg = copy.copy(self.cfg)
        if providers is not None:
            cfg.notify_providers = tuple(
                str(p).strip().lower() for p in providers if str(p).strip()
            )
        if webhook_url is not None:
            cfg.webhook_url = str(webhook_url).strip()
        notifier = Notifier(cfg)
        channels = notifier.describe()
        if not channels:
            return {"ok": False, "message": "当前没有选择通知渠道"}
        ok = notifier.send(
            "[NSU-EventGrabber] 通知测试",
            "通知内容测试",
        )
        names = [c["name"] for c in channels]
        if not ok:
            self.push_log("ERROR", f"通知测试失败：{', '.join(names)}")
        return {"ok": ok, "message": "测试通知已发送" if ok else "测试通知发送失败，请查看日志"}

    def reset_settings(self) -> dict:
        """删除 GUI 设置文件，恢复 config.env / 代码默认设置。

        登录凭据、活动历史等运行数据不在这里，恢复默认设置不会退出登录。
        """
        try:
            self.cfg.settings_file.unlink(missing_ok=True)
        except OSError as exc:
            return {"ok": False, "message": f"恢复默认设置失败：{exc}"}

        old_auth_file = self.cfg.auth_file
        old_state_file = self.cfg.state_file
        data_dir = str(self.cfg.data_dir)
        new_cfg = load_config(self.config_path, data_dir=data_dir)
        # 恢复设置不应改变登录凭据和活动状态文件的位置。
        new_cfg.auth_file = old_auth_file
        new_cfg.state_file = old_state_file

        self.cfg = new_cfg
        self.runtime.cfg = new_cfg
        self.runtime.auth.cfg = new_cfg
        self.runtime.client.cfg = new_cfg
        self.runtime.notifier = Notifier(new_cfg)
        return {"ok": True, "message": "已恢复默认设置"}

    def save_settings(self, patch: dict | None = None) -> dict:
        if not isinstance(patch, dict):
            return {"ok": False, "message": "设置格式错误"}
        try:
            self.cfg = save_settings(self.cfg, patch)
        except (ValueError, TypeError) as exc:
            return {"ok": False, "message": f"设置保存失败：{exc}"}
        self.runtime.cfg = self.cfg
        self.runtime.notifier = Notifier(self.cfg)
        # 设置保存后立刻用新规则重算当前列表，不必等下一轮扫描。
        if self._records:
            with self._lock:
                self._activities = activity_view(self._records, self.cfg)
        return {"ok": True, "message": "设置已保存"}

    def clear_logs(self) -> dict:
        with self._lock:
            self._logs.clear()
        return {"ok": True}

    def request_quit(self) -> None:
        """托盘菜单里的真正退出：停止托盘、停止监控并销毁主窗口。"""
        self._quitting = True
        if self.tray is not None:
            self.tray.stop()
        self.close()
        window = self._window
        if window is not None:
            try:
                window.destroy()
            except Exception:
                log.exception("窗口销毁失败")

    def close(self) -> None:
        self._quitting = True
        self._ui_sync_stop.set()
        self.stop_monitor()
        if self.tray is not None:
            self.tray.stop()
        terminate_login_child()


class Api:
    """暴露给 JS 的方法集合。"""

    def __init__(self, controller: Controller) -> None:
        self.ctrl = controller

    def get_state(self, cursor=0):
        return self.ctrl.get_state(cursor)

    def start(self):
        return self.ctrl.start_monitor()

    def stop(self):
        return self.ctrl.stop_monitor()

    def scan(self):
        return self.ctrl.scan_now()

    def login(self):
        return self.ctrl.login()

    def logout(self):
        return self.ctrl.logout()

    def register(self, activity_id=""):
        return self.ctrl.register(activity_id)

    def cancel(self, activity_id=""):
        return self.ctrl.cancel(activity_id)

    def test_notify(self, providers=None, webhook_url=None):
        return self.ctrl.test_notify(providers, webhook_url)

    def save_settings(self, patch=None):
        return self.ctrl.save_settings(patch)

    def reset_settings(self):
        return self.ctrl.reset_settings()

    def clear_logs(self):
        return self.ctrl.clear_logs()

    def open_url(self, url=""):
        return self.ctrl.open_external(url)

    def check_update(self, force=True):
        return self.ctrl.check_update(force=bool(force))


_GIO_TLS_MODULE_NAMES = ("libgiognutls.so", "libgioopenssl.so", "libgiotls.so")


def _gio_module_has_tls(directory: Path) -> bool:
    """判断目录里是否放着 GIO 的 TLS 后端模块。"""
    return any((directory / name).exists() for name in _GIO_TLS_MODULE_NAMES)


def _host_gio_module_dirs() -> list[Path]:
    """宿主机可能的 GIO 模块目录（含发行版的多架构路径）。"""
    patterns = (
        "usr/lib/*/gio/modules",
        "usr/lib64/gio/modules",
        "usr/local/lib/*/gio/modules",
        "usr/lib/gio/modules",
    )
    dirs: list[Path] = []
    for pattern in patterns:
        dirs.extend(path for path in Path("/").glob(pattern) if path.is_dir())
    return dirs


def _pick_gio_extra_modules(host_dirs: list[Path], bundled: Path) -> Optional[Path]:
    """宿主机没有任何 TLS 后端模块时，返回包内模块目录；否则返回 None。"""
    if any(_gio_module_has_tls(directory) for directory in host_dirs):
        return None
    if _gio_module_has_tls(bundled):
        return bundled
    return None


def _prepare_gio_modules() -> None:
    """宿主机缺 glib-networking 时，改用包内自带的 GIO TLS 模块。

    GIO 找不到 TLS 后端只会退回 dummy 后端，WebKit 加载 https 页面时报
    "TLS support is not available"（登录页因此打不开）。宿主机有模块时保持
    原样，避免同一类型被注册两次。
    """
    bundled = Path(getattr(sys, "_MEIPASS", "") or "/nonexistent") / "gio" / "modules"
    extra = _pick_gio_extra_modules(_host_gio_module_dirs(), bundled)
    if extra is None:
        return
    parts = [part for part in os.environ.get("GIO_EXTRA_MODULES", "").split(":") if part]
    if str(extra) not in parts:
        parts.append(str(extra))
        os.environ["GIO_EXTRA_MODULES"] = ":".join(parts)
        log.info("宿主机缺少 GIO TLS 模块，改用包内副本：%s", extra)


def _check_linux_gtk() -> None:
    """Linux 启动前探测 GTK/WebKit 绑定；打包版不再内置 GTK 栈，全靠宿主机。

    缺 GI 包（typelib 不存在）才提示安装命令；typelib 在但底层 so 加载失败时
    原样透出 dlopen 错误，避免用户对着已经装好的包再白装一遍。
    """
    if not sys.platform.startswith("linux"):
        return
    _prepare_gio_modules()
    install_hint = (
        "缺少 GTK3 / WebKitGTK 运行环境，请安装：\n"
        "  sudo apt install gir1.2-gtk-3.0 gir1.2-webkit2-4.1\n"
        "旧发行版请把 gir1.2-webkit2-4.1 换成 gir1.2-webkit2-4.0"
    )
    try:
        import gi
    except Exception as exc:
        raise SystemExit(f"PyGObject(gi) 模块加载失败：{exc}\n{install_hint}")
    try:
        gi.require_version("Gtk", "3.0")
        try:
            gi.require_version("WebKit2", "4.1")
        except ValueError:
            gi.require_version("WebKit2", "4.0")
        from gi.repository import Gtk, WebKit2  # noqa: F401
    except ValueError as exc:
        raise SystemExit(f"{exc}\n{install_hint}")
    except Exception as exc:
        raise SystemExit(
            "GTK/WebKit 库加载失败；相关包已安装时通常是库版本冲突，"
            f"不要重复安装。原始错误：\n{exc}"
        )
    # AppImage / 桌面环境的任务栏图标取自 GTK 默认窗口图标。pywebview 不设置它，
    # 不显式指定时 Linux 会显示通用的可执行文件占位图标。这里统一用与
    # Windows/macOS 相同的 logo，并且对未集成 .desktop 的 AppImage 也有效。
    try:
        icon_path = Path(__file__).with_name("web") / "NSU-EventGrabber.png"
        if icon_path.exists():
            Gtk.Window.set_default_icon_from_file(str(icon_path))
    except Exception:  # noqa: BLE001 - 图标失败不影响启动
        log.debug("设置窗口图标失败", exc_info=True)
    try:
        from gi.repository import Gio, GObject

        backend = Gio.TlsBackend.get_default()
        backend_name = GObject.type_name(backend.__gtype__) if backend is not None else ""
        if not backend_name or "dummy" in backend_name.lower():
            log.warning(
                "当前系统没有可用的 GIO TLS 后端，登录页等 https 请求会失败"
                "（TLS support is not available）；可执行 sudo apt install glib-networking"
            )
    except Exception:  # noqa: BLE001 - 仅用于诊断，不影响启动
        log.debug("GIO TLS 后端检测失败", exc_info=True)


def run_gui(config_path: Optional[str] = None, data_dir: Optional[str] = None) -> None:
    _check_linux_gtk()
    cfg = load_config(config_path, data_dir)
    instance = SingleInstance(cfg.data_dir)
    if not instance.acquire():
        # 已有实例：只写显示请求，不再创建第二个窗口。
        instance.request_show()
        return
    instance.start()
    controller = None
    try:
        import webview  # type: ignore

        controller = Controller(config_path=config_path, data_dir=data_dir)
        try:
            from logging.handlers import RotatingFileHandler

            log_path = Path(controller.cfg.data_dir) / "gui.log"
            file_handler = RotatingFileHandler(
                log_path, maxBytes=2 * 1024 * 1024, backupCount=2, encoding="utf-8"
            )
            file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
            logging.getLogger("nsu").addHandler(file_handler)
        except Exception:
            pass
        web_src = Path(__file__).with_name("web")
        # 用内容哈希生成一个全新的资源目录，绕开 WebView 对旧 app.js/style.css 的
        # 持久缓存。否则升级 app 后界面仍可能执行上一版前端逻辑。
        try:
            digest = hashlib.sha256()
            for name in ("index.html", "app.js", "style.css", "NSU-EventGrabber.png"):
                digest.update((web_src / name).read_bytes())
            web_version = digest.hexdigest()[:10]
            web_dir = Path(controller.cfg.data_dir) / f"web-{web_version}"
            if not all((web_dir / name).exists() for name in ("index.html", "app.js", "style.css", "NSU-EventGrabber.png")):
                shutil.rmtree(web_dir, ignore_errors=True)
                shutil.copytree(web_src, web_dir)
            html = web_dir / "index.html"
        except Exception:
            web_version = "unknown"
            html = web_src / "index.html"
        controller.web_url = str(html)
        window = webview.create_window(
            "NSU-EventGrabber",
            url=str(html),
            js_api=Api(controller),
            width=1060,
            height=760,
            x=120,
            y=70,
            min_size=(600, 620),
            background_color="#f6f7f9",
        )
        controller.attach_window(window)
        instance.set_show_callback(controller.show_window)
        controller.setup_tray()

        def on_main_closing():
            # 关闭按钮默认隐藏到托盘；托盘不可用或用户选择退出时才真正结束进程。
            if controller.should_hide_on_close():
                if controller.hide_to_tray():
                    return False
            # pywebview 的 JS API 线程不是 daemon；如果关闭时还有一次调用在排队，
            # Python 解释器会在 Py_FinalizeEx 中等待它，窗口就一直转圈。
            controller.close()
            def force_exit() -> None:
                log.warning("关闭超时，强制退出")
                os._exit(0)
            timer = threading.Timer(4.0, force_exit)
            timer.daemon = True
            timer.start()

        try:
            window.events.closing += on_main_closing
        except Exception:
            pass

        webview_cache = Path(controller.cfg.data_dir) / "webview"
        webview_cache.mkdir(parents=True, exist_ok=True)

        # 固定 WebView 的 UserData 目录，避免 pywebview 在 Windows 上使用
        # 会被回收的临时目录导致 WebView2 初始化卡住；同时保留登录 Cookie。
        def after_start():
            try:
                log.info("WebView：%s", getattr(webview, "renderer", "unknown"))
            except Exception:
                pass
            # 等主页面完成一次 boot 后再检查登录态，避免和首屏抢窗口。
            timer = threading.Timer(1.2, controller.maybe_auto_login)
            timer.daemon = True
            timer.start()
            # 自动更新检查放后台，不阻塞首屏和登录态恢复。
            update_timer = threading.Timer(4.0, controller.maybe_check_update)
            update_timer.daemon = True
            update_timer.start()

        webview.start(
            debug=bool(os.environ.get("NSU_GUI_DEBUG")),
            private_mode=False,
            storage_path=str(webview_cache),
            func=after_start,
        )
    finally:
        instance.close()
        if controller is not None:
            controller.close()
