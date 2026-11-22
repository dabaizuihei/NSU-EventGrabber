"""主窗口从托盘恢复时的回归测试。

macOS 的 pywebview `window.show()` 只是把窗口操作排进 Cocoa 主循环；
如果紧接着在主线程同步调用 `evaluate_js`，主线程会等一个排在自己后面的
JS 回调，表现就是点「显示主界面」后整窗卡死。
"""
from __future__ import annotations

import threading
import unittest
from unittest import mock

from gui.app import Controller


class FakeWindow:
    def __init__(self) -> None:
        self.shown = False
        self.evaluated = False

    def show(self) -> None:
        self.shown = True

    def evaluate_js(self, _script: str) -> None:
        self.evaluated = True


class TrayRestoreTest(unittest.TestCase):
    def make_controller(self) -> Controller:
        controller = object.__new__(Controller)
        controller._window = FakeWindow()
        controller._window_hidden = True
        controller._logs = []
        controller._lock = threading.Lock()
        controller._refresh_gui = mock.Mock()
        return controller

    def test_show_window_schedules_refresh_off_the_tray_callback(self) -> None:
        controller = self.make_controller()
        with mock.patch.object(Controller, "_activate_mac_app"), mock.patch(
            "gui.app.threading.Timer"
        ) as timer:
            Controller.show_window(controller)

        self.assertTrue(controller._window.shown)
        self.assertFalse(controller._window_hidden)
        timer.assert_called_once()
        self.assertIs(timer.call_args.args[1], controller._refresh_gui)

    def test_refresh_on_main_thread_is_deferred(self) -> None:
        controller = self.make_controller()
        controller._window_hidden = False
        with mock.patch("gui.app.sys.platform", "darwin"), mock.patch(
            "gui.app.threading.Timer"
        ) as timer:
            Controller._refresh_gui(controller, focus=True)

        self.assertFalse(controller._window.evaluated)
        timer.assert_called_once()


if __name__ == "__main__":
    unittest.main()
