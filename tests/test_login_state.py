"""登录状态、退出清理与登录窗口桥接的回归测试。

覆盖两个已知问题：
1. 登录窗口不能把上一次会话的旧 token 当成登录成功（窗口闪一下就关闭）；
2. 退出登录后，任何迟到的刷新/资料请求/登录提交都不能把账号写回来。

运行：
    python -m unittest discover -s tests -t . -v
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from auth import AuthManager, CredentialStore, Credentials
from config import Config
import login_webview


TENANT_ID = "123456789012345678"


def make_cfg(tmp: str) -> Config:
    data = Path(tmp)
    return Config(
        base_url="https://study.example.com",
        tenant_id=TENANT_ID,
        username="20230001",
        data_dir=data,
        auth_file=data / "credentials.json",
        state_file=data / "state.json",
        settings_file=data / "settings.json",
        request_timeout=5,
    )


def make_auth(tmp: str) -> tuple[Config, AuthManager, Path]:
    cfg = make_cfg(tmp)
    path = cfg.auth_file
    store = CredentialStore(path)
    auth = AuthManager(cfg, store)
    return cfg, auth, path


class CredentialStoreTest(unittest.TestCase):
    def test_clear_removes_credentials_and_temp_file(self) -> None:
        with TemporaryDirectory() as tmp:
            _, auth, path = make_auth(tmp)
            auth.store.credentials = Credentials(
                access_token="old-access", refresh_token="old-refresh", username="u"
            )
            auth.store.save()
            tmp_file = path.with_suffix(".tmp")
            tmp_file.write_text("{}", encoding="utf-8")
            self.assertTrue(path.exists())

            auth.store.clear()

            self.assertFalse(path.exists())
            self.assertFalse(tmp_file.exists())
            self.assertEqual(auth.access_token, "")
            self.assertEqual(auth.refresh_token, "")

    def test_empty_credentials_do_not_recreate_file_after_logout(self) -> None:
        """退出后残留的租户解析不能把“空凭据文件”写回来。"""
        with TemporaryDirectory() as tmp:
            _, auth, path = make_auth(tmp)
            auth.reset_session()
            auth.store.credentials.tenant_name = "测试学校"
            auth.store.credentials.tenant_id = TENANT_ID
            auth.store.save()
            self.assertFalse(path.exists())


class SessionEpochTest(unittest.TestCase):
    def test_inflight_refresh_is_discarded_after_logout(self) -> None:
        """刷新请求返回时用户已经退出登录，不能把新 token 写回本地。"""
        with TemporaryDirectory() as tmp:
            _, auth, path = make_auth(tmp)
            auth.store.credentials = Credentials(
                base_url=make_cfg(tmp).base_url,
                tenant_id=TENANT_ID,
                username="20230001",
                access_token="old-access",
                refresh_token="old-refresh",
            )
            auth.store.save()

            def fake_request(*args, **kwargs):
                # 模拟网络请求期间用户点击“退出登录”
                auth.reset_session()
                return 200, {
                    "code": 0,
                    "data": {"accessToken": "new-access", "refreshToken": "new-refresh"},
                }

            with mock.patch.object(auth, "_request", side_effect=fake_request):
                self.assertFalse(auth.refresh())

            self.assertEqual(auth.access_token, "")
            self.assertEqual(auth.refresh_token, "")
            self.assertFalse(path.exists())

    def test_inflight_profile_request_is_discarded_after_logout(self) -> None:
        with TemporaryDirectory() as tmp:
            _, auth, path = make_auth(tmp)
            auth.store.credentials = Credentials(
                base_url=make_cfg(tmp).base_url,
                tenant_id=TENANT_ID,
                access_token="old-access",
                username="20230001",
            )
            auth.store.save()
            epoch = auth.epoch

            def fake_request(*args, **kwargs):
                auth.reset_session()
                return 200, {
                    "code": 0,
                    "data": {"user": {"nickname": "旧账号", "number": "20230001"}},
                }

            with mock.patch.object(auth, "_request", side_effect=fake_request):
                auth.fetch_profile(epoch)

            self.assertFalse(path.exists())
            self.assertEqual(auth.store.credentials.display_name, "")

    def test_stale_epoch_cannot_commit_login(self) -> None:
        with TemporaryDirectory() as tmp:
            cfg, auth, path = make_auth(tmp)
            stale_epoch = auth.epoch
            auth.reset_session()

            ok, message = login_webview._commit_login(
                auth, cfg, "20230001", "old-access", "old-refresh", epoch=stale_epoch
            )

            self.assertFalse(ok)
            self.assertIn("取消", message)
            self.assertFalse(path.exists())


class CommitLoginTest(unittest.TestCase):
    def test_invalid_token_is_rejected_without_writing(self) -> None:
        with TemporaryDirectory() as tmp:
            cfg, auth, path = make_auth(tmp)
            with mock.patch.object(auth, "probe_token", return_value="unauthorized"):
                ok, message = login_webview._commit_login(
                    auth, cfg, "20230001", "stale-token", "stale-refresh", epoch=auth.epoch
                )
            self.assertFalse(ok)
            self.assertIn("校验失败", message)
            self.assertFalse(path.exists())
            self.assertEqual(auth.access_token, "")

    def test_valid_token_is_saved(self) -> None:
        with TemporaryDirectory() as tmp:
            cfg, auth, path = make_auth(tmp)
            with mock.patch.object(auth, "probe_token", return_value="ok"), mock.patch.object(
                auth, "fetch_profile"
            ) as profile:
                ok, message = login_webview._commit_login(
                    auth, cfg, "20230001", "good-token", "good-refresh", epoch=auth.epoch
                )
            self.assertTrue(ok, message)
            profile.assert_called_once()
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["access_token"], "good-token")
            self.assertEqual(data["refresh_token"], "good-refresh")
            self.assertEqual(auth.username, "20230001")


class BridgeTest(unittest.TestCase):
    class FakeWindow:
        def __init__(self) -> None:
            self.destroyed = threading.Event()

        def destroy(self) -> None:
            self.destroyed.set()

    def test_valid_token_closes_window(self) -> None:
        window = self.FakeWindow()
        bridge = login_webview._Bridge(validator=lambda access, refresh: True)
        bridge._window = window

        self.assertTrue(bridge.report_token("good-token", "good-refresh"))
        self.assertTrue(bridge._token_event.wait(2))
        self.assertTrue(window.destroyed.wait(2))
        self.assertEqual(bridge._access_token, "good-token")
        self.assertEqual(bridge._refresh_token, "good-refresh")

    def test_stale_token_is_rejected_and_window_stays_open(self) -> None:
        window = self.FakeWindow()
        bridge = login_webview._Bridge(validator=lambda access, refresh: False)
        bridge._window = window

        self.assertFalse(bridge.report_token("stale-token", ""))
        self.assertFalse(bridge._token_event.is_set())
        self.assertFalse(window.destroyed.wait(0.3))
        self.assertEqual(bridge._access_token, "")

    def test_validator_exception_does_not_block_login(self) -> None:
        window = self.FakeWindow()
        bridge = login_webview._Bridge(validator=lambda access, refresh: 1 / 0)
        bridge._window = window

        self.assertTrue(bridge.report_token("token", ""))
        self.assertTrue(window.destroyed.wait(2))


class InjectedScriptTest(unittest.TestCase):
    def test_credentials_are_escaped_and_placeholders_replaced(self) -> None:
        script = login_webview.build_inject_script('a"b\\c', "p'w\n", 0.85)
        self.assertNotIn("__USERNAME__", script)
        self.assertNotIn("__PASSWORD__", script)
        self.assertNotIn("__ZOOM__", script)
        self.assertIn('"a\\"b\\\\c"', script)
        self.assertIn("0.85", script)

    def test_script_reports_token_and_handles_rejection(self) -> None:
        script = login_webview.build_inject_script("u", "p")
        # 采集在页面内完成，Python 不再轮询 localStorage
        self.assertIn("report_token", script)
        self.assertIn("ACCESS_TOKEN", script)
        self.assertIn("token-rejected", script)
        self.assertIn("sessionStorage", script)


class TerminateChildTest(unittest.TestCase):
    def test_terminate_login_child_kills_running_process(self) -> None:
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        login_webview._set_active_child(proc)
        login_webview.terminate_login_child(wait=3)
        self.assertIsNotNone(proc.poll())
        self.assertIsNone(login_webview._active_child)


class ControllerLogoutTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        data = Path(self.tmp.name)
        (data / "config.env").write_text(
            "\n".join(
                [
                    "NSU_BASE_URL=https://study.example.com",
                    f"NSU_TENANT_ID={TENANT_ID}",
                    "NSU_USERNAME=20230001",
                ]
            ),
            encoding="utf-8",
        )
        from gui.app import Controller, _LogHandler

        self.Controller = Controller
        self.LogHandler = _LogHandler
        self.controller = Controller(None, data_dir=self.tmp.name)

    def tearDown(self) -> None:
        logger = logging.getLogger("nsu")
        for handler in list(logger.handlers):
            if isinstance(handler, self.LogHandler):
                logger.removeHandler(handler)
        self.tmp.cleanup()

    def test_logout_clears_credentials_and_login_cache(self) -> None:
        controller = self.controller
        data = Path(self.tmp.name)
        controller.runtime.store.credentials = Credentials(
            access_token="old-access", refresh_token="old-refresh", username="20230001"
        )
        controller.runtime.store.save()
        login_cache = data / "webview-login"
        login_cache.mkdir()
        (login_cache / "EBWebView").write_text("cache", encoding="utf-8")
        controller.runtime.state.path.write_text("{}", encoding="utf-8")
        controller._was_logged_in = True

        result = controller.logout()

        self.assertTrue(result["ok"])
        self.assertFalse((data / "credentials.json").exists())
        self.assertFalse((data / "credentials.tmp").exists())
        self.assertFalse(login_cache.exists())
        self.assertFalse(controller.runtime.auth.logged_in)
        self.assertEqual(controller.runtime.auth.access_token, "")
        self.assertEqual(controller.cfg.username, "")
        self.assertFalse(controller.runtime.state.path.exists())

    def test_logout_is_idempotent(self) -> None:
        self.controller.logout()
        result = self.controller.logout()
        self.assertTrue(result["ok"])
        self.assertFalse(self.controller.runtime.auth.logged_in)


if __name__ == "__main__":
    unittest.main()
