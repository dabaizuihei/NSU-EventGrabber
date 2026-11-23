"""netutil TLS 放宽行为的回归测试。"""
import ssl
import unittest

import netutil


class RelaxLegacyServersTest(unittest.TestCase):
    def test_verification_untouched(self) -> None:
        """放宽 SECLEVEL 只扩协议/套件范围，证书校验必须保持开启。"""
        context = ssl.create_default_context()
        netutil._relax_for_legacy_servers(context)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_floor_lowered_when_supported(self) -> None:
        """系统 OpenSSL 支持老协议时，最低版本应放宽到 TLSv1（不支持则跳过）。"""
        context = ssl.create_default_context()
        netutil._relax_for_legacy_servers(context)
        self.assertLessEqual(context.minimum_version, ssl.TLSVersion.TLSv1_1)

    def test_ssl_context_cached_and_verified(self) -> None:
        """共享上下文必须缓存，且保持证书校验。"""
        context = netutil.ssl_context()
        self.assertIs(context, netutil.ssl_context())
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)


if __name__ == "__main__":
    unittest.main()
