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

    # GIO 的插件模块（glib-networking 提供的 TLS 后端等）作为兜底随包携带：
    # 宿主机没装 glib-networking 时，WebKit 加载 https 页面会报
    # "TLS support is not available"，启动时按需通过 GIO_EXTRA_MODULES 启用。
    gio_modules = Path(libdir or "") / "gio" / "modules"
    if gio_modules.is_dir():
        for module in sorted(gio_modules.glob("*.so*")):
            binaries.append((str(module), "gio/modules"))

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

if sys.platform.startswith("linux"):
    import re

    # gi hook 会把构建机的整套 GTK/GLib 栈连同全部传递依赖打进包里。新系统上
    # pywebview 加载宿主 WebKit 4.1 时，进程里同时存在打包的旧库和系统的新库，
    # Gtk.Application 的 g_application_run 拿不到默认主上下文，窗口起不来。
    # 策略：凡是来自构建机系统目录的共享库一律不带（libpython 除外，
    # WebKitWebProcess 等辅助进程也一样），运行时全部使用宿主机同款库，
    # 等同源码运行。libmount/libblkid/libuuid/libstdc++/libgcc_s/libpcre2/
    # libz/libgnutls/libssl 这类传递依赖由此一并交给宿主机，避免新旧混载。
    gtk_lib_re = re.compile(
        r"^(?:lib)?(?:glib|gobject|gmodule|gthread|gio|girepository|gtk|gdk|"
        r"pango|atk|atspi|cairo|harfbuzz|graphite2|fribidi|epoxy|webkit|"
        r"javascriptcore|soup|ffi|json-glib|mount|blkid|uuid|stdc\+\+|gcc_s|"
        r"pcre2|z\.|gnutls|nettle|hogweed|tasn1|idn2?|unistring|ssl|crypto)"
    )
    # 版本化的系统共享库：libfoo.so / libfoo.so.1.2.3
    versioned_so_re = re.compile(r"^lib.+\.(?:so|so\.[0-9])")

    def _is_system_runtime(entry) -> bool:
        dest, src = str(entry[0]), str(entry[1])
        name = Path(dest).name
        if name.startswith("libpython"):
            # 解释器本体必须随包携带。
            return False
        if "gio/modules" in src or "gio/modules" in dest:
            # GIO 插件模块（TLS 后端）随包携带，宿主机缺 glib-networking 时兜底。
            return False
        if "webkit" in name or "webkit" in src:
            # WebKitWebProcess / WebKitNetworkProcess 等辅助进程必须用宿主机的。
            return True
        if src.startswith(("/lib/", "/usr/lib", "/lib64/", "/usr/local/lib")):
            if versioned_so_re.match(name) or gtk_lib_re.match(name):
                return True
        return bool(gtk_lib_re.match(name))

    a.binaries = [entry for entry in a.binaries if not _is_system_runtime(entry)]
    a.datas = [
        entry
        for entry in a.datas
        if not entry[0].endswith((".typelib", ".gir"))
        and "gi_typelibs" not in entry[0]
    ]

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
                "CFBundleShortVersionString": "v1.0.3",
                "CFBundleVersion": "v1.0.3",
                "NSHighResolutionCapable": True,
            },
        )
