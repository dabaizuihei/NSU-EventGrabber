"""网络请求共用的 TLS 配置。

打包后的 Python 不一定能找到系统根证书。macOS/Linux 上显式使用系统 CA
文件，Windows 继续使用 Python 自带的系统证书库。
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
            _context = ssl.create_default_context(cafile=path)
            return _context
    _context = ssl.create_default_context()
    return _context
