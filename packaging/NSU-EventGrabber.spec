# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：Windows 单文件，macOS / Linux 目录包。

在项目根目录执行：
    pyinstaller --noconfirm --clean packaging/NSU-EventGrabber.spec
"""
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

ROOT = Path.cwd()
IS_WINDOWS = sys.platform.startswith("win")
ICON_DIR = ROOT / "packaging"

# pywebview 的平台后端、WebView2 DLL、JS 资源都由它自己的 hook 收集。
webview_datas, webview_binaries, webview_hidden = collect_all("webview")
pystray_datas, pystray_binaries, pystray_hidden = collect_all("pystray")

datas = list(webview_datas) + list(pystray_datas)
datas.append((str(ROOT / "gui" / "web"), "gui/web"))
binaries = list(webview_binaries) + list(pystray_binaries)
hiddenimports = list(webview_hidden) + list(pystray_hidden)

if sys.platform.startswith("linux"):
    import sysconfig

    hiddenimports += [
        "gi",
        "gi.repository.Gtk",
        "gi.repository.WebKit2",
    ]
    libdir = sysconfig.get_config_var("LIBDIR")
    libname = sysconfig.get_config_var("LDLIBRARY")
    if libdir and libname:
        libpath = Path(libdir) / libname
        if libpath.exists():
            binaries.append((str(libpath), "."))

a = Analysis(
    [str(ROOT / "gui_launcher.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6"],
    noarchive=False,
)
pyz = PYZ(a.pure)

if IS_WINDOWS:
    # Windows 绿色版：单个 exe，首次启动会释放依赖到临时目录。
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="NSU-EventGrabber",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=str(ICON_DIR / "NSU-EventGrabber.ico"),
        version=str(ICON_DIR / "version_info.txt"),
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="NSU-EventGrabber",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name="NSU-EventGrabber",
    )

    if sys.platform == "darwin":
        app = BUNDLE(
            coll,
            name="NSU-EventGrabber.app",
            icon=str(ICON_DIR / "NSU-EventGrabber.icns"),
            bundle_identifier="cn.nsu.eventgrabber",
            info_plist={
                "CFBundleDisplayName": "NSU-EventGrabber",
                "CFBundleName": "NSU-EventGrabber",
                "CFBundleShortVersionString": "v1.0.2",
                "CFBundleVersion": "v1.0.2",
                "NSHighResolutionCapable": True,
            },
        )
