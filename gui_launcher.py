#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GUI 启动入口；登录子进程也由这里启动，不暴露命令行参数。"""
from __future__ import annotations

import os


def main() -> int:
    payload = os.environ.pop("NSU_LOGIN_PAYLOAD", "")
    result = os.environ.pop("NSU_LOGIN_RESULT", "")
    if payload and result:
        from login_webview import run_login_child

        return run_login_child(payload, result)

    from gui.app import run_gui

    run_gui()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
