import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from config import Config
from gui.app import Controller
from notify import Notifier


class SettingsDefaultsTest(unittest.TestCase):
    def test_auto_monitor_default_is_on(self) -> None:
        self.assertTrue(Config().auto_monitor)
        self.assertTrue(Config().close_to_tray)
        self.assertTrue(Config().auto_check_update)

    def test_data_dir_config_env_is_default_source(self) -> None:
        with TemporaryDirectory() as tmp:
            Path(tmp, "config.env").write_text(
                "SCAN_INTERVAL_SECONDS=120\nAUTO_MONITOR=true\nHIDE_PAST_REGISTERED=false\n",
                encoding="utf-8",
            )
            Path(tmp, "settings.json").write_text(
                json.dumps({"SCAN_INTERVAL_SECONDS": "45"}),
                encoding="utf-8",
            )
            controller = Controller(data_dir=tmp)
            self.assertEqual(controller.cfg.scan_interval, 45)
            self.assertTrue(controller.cfg.auto_monitor)

            controller.reset_settings()

            # settings.json 清掉后，data_dir/config.env 成为默认来源。
            self.assertEqual(controller.cfg.scan_interval, 120)
            self.assertTrue(controller.cfg.auto_monitor)
            self.assertFalse(controller.cfg.hide_past_registered)

    def test_reset_settings_restores_code_defaults(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Path(tmp) / "settings.json"
            settings.write_text(
                json.dumps(
                    {
                        "SCAN_INTERVAL_SECONDS": "45",
                        "AUTO_REGISTER": "false",
                        "AUTO_MONITOR": "false",
                    }
                ),
                encoding="utf-8",
            )
            controller = Controller(data_dir=tmp)
            self.assertEqual(controller.cfg.scan_interval, 45)
            self.assertFalse(controller.cfg.auto_register)
            self.assertFalse(controller.cfg.auto_monitor)
            self.assertTrue(controller.cfg.close_to_tray)
            self.assertTrue(controller.cfg.auto_check_update)

            result = controller.reset_settings()

            self.assertTrue(result["ok"])
            self.assertEqual(controller.cfg.scan_interval, 300)
            self.assertTrue(controller.cfg.auto_register)
            self.assertTrue(controller.cfg.auto_monitor)
            self.assertFalse(settings.exists())


class NotifyStatusTest(unittest.TestCase):
    def test_default_desktop_channel_describe_does_not_raise(self) -> None:
        notifier = Notifier(Config())
        status = notifier.describe()
        self.assertTrue(status)
        self.assertTrue(any(item["name"] == "desktop" for item in status))
