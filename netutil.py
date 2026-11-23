"""网络请求共用的 TLS 配置。

打包后的 Python 不一定能找到系统根证书。macOS/Linux 上显式使用系统 CA
文件，Windows 继续使用 Python 自带的系统证书库。老校园平台常用旧版 TLS
协议或 SHA-1 证书链，而新发行版的系统 OpenSSL 默认 SECLEVEL=2（min TLS
1.2、拒绝 SHA-1 签名链）会直接拒掉握手，表现为 "unsupported protocol"
这类 TLS 错误；因此非 Windows 上主动放宽到 SECLEVEL=1，证书校验保持不变。
"""
from __future__ import annotations

import os
import ssl
import sys

_CA_CANDIDATES = (
    "/etc/ssl/cert.pem",                 # macOS / LibreSSL
    "/private/etc/ssl/cert.pem",         # macOS 的 /etc 镜像
    "/etc/ssl/certs/ca-certificates.crt",# Debian/Ubuntu
    "/etc/pki/tls/certs/ca-bundle.crt",  # RHEL/Fedora
)

_context: ssl.SSLContext | None = None


def _relax_for_legacy_servers(context: ssl.SSLContext) -> ssl.SSLContext:
    """放宽协议/套件限制以兼容老校园平台；证书校验不动。

    系统 OpenSSL 编译时可能禁用了老版本协议（赋值 minimum_version 会抛错），
    逐档尝试，取系统实际支持的最低档。
    """
    try:
        context.set_ciphers("DEFAULT@SECLEVEL=1")
    except ssl.SSLError:
        pass
    for version in (ssl.TLSVersion.TLSv1, ssl.TLSVersion.TLSv1_1):
        try:
            context.minimum_version = version
            break
        except (ValueError, ssl.SSLError):
            continue
    return context


def ssl_context() -> ssl.SSLContext:
    """返回一个带系统 CA 的 SSLContext。"""
    global _context
    if _context is not None:
        return _context
    if sys.platform.startswith("win"):
        _context = ssl.create_default_context()
        return _context
    for path in _CA_CANDIDATES:
        if os.path.isfile(path) and os.access(path, os.R_OK):
            _context = _relax_for_legacy_servers(ssl.create_default_context(cafile=path))
            return _context
    _context = _relax_for_legacy_servers(ssl.create_default_context())
    return _context
