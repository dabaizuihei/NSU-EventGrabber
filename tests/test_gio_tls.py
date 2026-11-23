"""GIO TLS 模块兜底选择的测试。"""
import tempfile
import unittest
from pathlib import Path

from gui.app import _gio_module_has_tls, _pick_gio_extra_modules


class GioTlsFallbackTest(unittest.TestCase):
    def test_host_module_wins(self) -> None:
        """宿主机已有 TLS 模块时不动环境变量，避免重复注册同一类型。"""
        with tempfile.TemporaryDirectory() as host, tempfile.TemporaryDirectory() as bundled:
            Path(host, "libgiognutls.so").touch()
            Path(bundled, "libgiognutls.so").touch()
            self.assertIsNone(_pick_gio_extra_modules([Path(host)], Path(bundled)))

    def test_falls_back_to_bundled(self) -> None:
        """宿主机只有代理模块、没有 TLS 模块时，使用包内副本。"""
        with tempfile.TemporaryDirectory() as host, tempfile.TemporaryDirectory() as bundled:
            Path(host, "libgioenvironmentproxy.so").touch()
            Path(bundled, "libgiognutls.so").touch()
            self.assertEqual(_pick_gio_extra_modules([Path(host)], Path(bundled)), Path(bundled))

    def test_no_tls_anywhere(self) -> None:
        with tempfile.TemporaryDirectory() as host, tempfile.TemporaryDirectory() as bundled:
            self.assertIsNone(_pick_gio_extra_modules([Path(host)], Path(bundled)))

    def test_missing_bundled_dir(self) -> None:
        with tempfile.TemporaryDirectory() as host:
            self.assertIsNone(_pick_gio_extra_modules([Path(host)], Path(host) / "nope"))
            self.assertFalse(_gio_module_has_tls(Path(host) / "nope"))


if __name__ == "__main__":
    unittest.main()
