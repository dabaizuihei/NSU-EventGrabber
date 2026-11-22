"""内嵌系统 WebView 的登录窗口（pywebview）。

流程：
1. 打开平台登录页；
2. 注入脚本自动填入学号/密码并点击「登录」；
3. 若平台弹滑块验证码，由用户在窗口里拖一次；
4. 页面脚本轮询 localStorage，拿到 ACCESS_TOKEN / REFRESH_TOKEN 后通过
   js_api 回报；Python 侧先确认 token 有效，再关闭窗口并写回凭据。

窗口复用系统 WebView：Windows(WebView2) / macOS(WKWebView) / Linux(WebKitGTK)。

线程约束（Windows 上尤其重要）：
- WebView2 的事件回调和异步结果都依赖 UI 线程的消息泵。pywebview 的
  ``run_js`` / ``destroy`` 会同步 marshal 到 UI 线程，高频调用（旧实现每
  0.5 秒轮询一次 localStorage）或在 UI 线程上等待脚本结果，都会让窗口
  看起来“点击后无响应”。因此这里：
  * 不再从 Python 侧轮询页面，token 采集全部放在页面内完成；
  * 只在页面加载完成后注入有限次数脚本，且同一时刻只允许一次注入；
  * 拿到 token 后由独立线程关窗，不在 js_api 回调线程里直接销毁窗口；
  * Windows/Linux 不再跨线程调用 ``window.show()``（窗口创建时已显示）。
- 登录窗口使用独立子进程和全新的 WebView 缓存目录，退出登录时一并清理，
  避免上一次会话的 Cookie / localStorage 影响下一次登录。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from auth import AuthManager, CredentialStore
from config import Config

log = logging.getLogger("nsu.login")

_child_lock = threading.Lock()
_active_child: Optional[subprocess.Popen] = None


# ---------------------------------------------------------------- 子进程管理
def _set_active_child(proc: Optional[subprocess.Popen]) -> None:
    global _active_child
    with _child_lock:
        _active_child = proc


def terminate_login_child(wait: float = 5.0) -> None:
    """结束独立登录进程，避免留下孤立窗口或占用 WebView 缓存的进程。"""
    global _active_child
    with _child_lock:
        proc = _active_child
        _active_child = None
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
    except Exception:  # noqa: BLE001
        pass
    try:
        proc.wait(timeout=wait)
        return
    except Exception:  # noqa: BLE001
        pass
    try:
        proc.kill()
    except Exception:  # noqa: BLE001
        pass
    try:
        proc.wait(timeout=2)
    except Exception:  # noqa: BLE001
        pass


def purge_login_cache(data_dir: Path, retries: int = 4) -> None:
    """删除登录窗口专用的 WebView 缓存。

    Windows 上 WebView2 的子进程会短暂占用缓存目录，rmtree 可能失败；
    这里做有限重试，同时清掉旧版本和并发进程可能留下的其它目录。
    """
    base = Path(data_dir)
    targets = [base / "webview-login"]
    try:
        targets.extend(base.glob("webview-login-*"))
    except OSError:
        pass
    for target in targets:
        for attempt in range(retries):
            shutil.rmtree(target, ignore_errors=True)
            if not target.exists():
                break
            time.sleep(0.25 * (attempt + 1))


def _fresh_cache_dir(base: Path) -> Path:
    """准备一个全新的缓存目录；如果旧目录被锁无法删除，就换一个唯一目录。"""
    purge_login_cache(base.parent)
    try:
        base.mkdir(parents=True, exist_ok=True)
        return base
    except OSError:
        unique = base.with_name(f"{base.name}-{os.getpid()}-{int(time.time())}")
        unique.mkdir(parents=True, exist_ok=True)
        return unique


# ---------------------------------------------------------------- JS 桥接
class _Bridge:
    """暴露给页面 JS 的回调对象。"""

    def __init__(self, validator: Optional[Callable[[str, str], bool]] = None) -> None:
        self._access_token = ""
        self._refresh_token = ""
        # 注意：必须是下划线开头的私有属性。pywebview 会递归 introspect js_api
        # 对象的所有公开属性；一旦暴露 pywebview Window，它会顺着 window.native
        # 链在后台线程访问 WebView2 COM，真实站点加载期间跨套间调用会永久阻塞，
        # 导致 UI 消息泵停摆（窗口“点击无响应”）。_window 同样用于关窗。
        self._window = None
        self._token_event = threading.Event()
        self._closed_event = threading.Event()
        self._script_ready = threading.Event()
        self._validator = validator
        self._lock = threading.Lock()
        self._inflight: set[str] = set()
        self._rejected: set[str] = set()

    def report_status(self, status: str = "") -> bool:
        text = str(status or "").strip()
        if text == "script-ready":
            self._script_ready.set()
        log.info("登录页状态：%s", text[:200])
        return True

    def report_token(self, access: str, refresh: str = "") -> bool:
        """页面拿到 token 后调用。

        返回 False 表示 token 不可用，页面脚本会清除 localStorage 并要求重新登录；
        只有校验通过才会关窗，避免“窗口闪一下就关闭，最后提示 token 校验失败”。
        """
        access = str(access or "").strip()
        refresh = str(refresh or "").strip()
        if not access:
            return False
        with self._lock:
            if self._token_event.is_set():
                return True
            if access in self._rejected or access in self._inflight:
                return False
            self._inflight.add(access)
        try:
            accepted = self._validate(access, refresh)
        finally:
            with self._lock:
                self._inflight.discard(access)
        if not accepted:
            with self._lock:
                self._rejected.add(access)
            log.warning("旧Token无效，已忽略")
            return False
        with self._lock:
            if self._token_event.is_set():
                return True
            self._access_token = access
            self._refresh_token = refresh
            self._token_event.set()
        log.info("已获取Token，关闭登录窗口")
        _request_close(self._window)
        return True

    def _validate(self, access: str, refresh: str) -> bool:
        if self._validator is None:
            return True
        try:
            return bool(self._validator(access, refresh))
        except Exception:  # noqa: BLE001 - 校验异常交给父进程再校验
            log.exception("Token校验异常，交由主进程复核")
            return True


def _request_close(window) -> None:
    """关窗放到独立线程，避免阻塞 js_api 回调线程或页面脚本。"""
    if window is None:
        return

    def worker() -> None:
        time.sleep(0.05)
        _safe_destroy(window)

    threading.Thread(target=worker, daemon=True).start()


def _safe_destroy(window) -> None:
    try:
        window.destroy()
    except Exception:  # noqa: BLE001 - 窗口已关闭或 GUI 已退出
        pass


# ---------------------------------------------------------------- 页面脚本
_JS_TEMPLATE = r"""
(function () {
  if (window.__nsuGrabberLogin) { return; }
  window.__nsuGrabberLogin = true;

  var USERNAME = __USERNAME__;
  var PASSWORD = __PASSWORD__;
  var ZOOM = __ZOOM__;
  var submitted = false;
  var reported = {};
  var rejected = {};
  var ticks = 0;
  var announced = false;

  if (ZOOM && ZOOM < 1) {
    try { document.documentElement.style.zoom = String(ZOOM); } catch (e) {}
  }

  function apiRef() {
    return (window.pywebview && window.pywebview.api) ? window.pywebview.api : null;
  }

  function announce() {
    if (announced) { return; }
    var api = apiRef();
    if (!api || !api.report_status) { return; }
    announced = true;
    try { api.report_status('script-ready'); } catch (e) {}
  }

  function status(text) {
    var api = apiRef();
    if (!api || !api.report_status) { return; }
    try { api.report_status(String(text)); } catch (e) {}
  }

  function readToken(key) {
    var raw = null;
    try { raw = localStorage.getItem(key); } catch (e) { return ''; }
    if (!raw) { return ''; }
    var value = raw;
    try {
      var obj = JSON.parse(raw);
      if (obj && typeof obj === 'object' && obj.v) { value = obj.v; }
    } catch (e) {}
    if (typeof value === 'string') {
      try {
        var parsed = JSON.parse(value);
        if (typeof parsed === 'string') { value = parsed; }
      } catch (e) {}
      value = value.replace(/^"+|"+$/g, '');
    }
    return String(value || '');
  }

  function rememberedReject(token) {
    try { return sessionStorage.getItem('__nsuLoginRejected') === token; } catch (e) { return false; }
  }

  function rememberReject(token) {
    try { sessionStorage.setItem('__nsuLoginRejected', token); } catch (e) {}
  }

  function clearTokens() {
    try {
      localStorage.removeItem('ACCESS_TOKEN');
      localStorage.removeItem('REFRESH_TOKEN');
    } catch (e) {}
    reported = {};
  }

  function rejectToken(token) {
    rejected[token] = true;
    rememberReject(token);
    // 校验旧 token 期间用户可能已经重新登录并写入了新 token，
    // 此时只丢弃旧值，绝不能把刚拿到的新 token 一并清掉。
    var current = readToken('ACCESS_TOKEN');
    if (current && current !== token) { return; }
    clearTokens();
    status('token-rejected');
    // 旧 token 可能让页面直接停在首页；回到登录页等用户重新登录，
    // 同时避免“闪一下就关闭、提示 token 校验失败”的循环。
    try {
      var path = String(location.pathname || '');
      if (!/\/login(\/|$)/.test(path)) {
        location.href = '/login';
      }
    } catch (e) {}
  }

  function report() {
    var access = readToken('ACCESS_TOKEN');
    if (!access) { return; }
    if (reported[access] || rejected[access] || rememberedReject(access)) { return; }
    var api = apiRef();
    if (!api || !api.report_token) { return; }
    var refresh = readToken('REFRESH_TOKEN');
    reported[access] = true;
    var result;
    try {
      result = api.report_token(access, refresh || '');
    } catch (e) {
      reported = {};
      return;
    }
    if (result && typeof result.then === 'function') {
      result.then(function (ok) {
        if (ok === false) { rejectToken(access); }
      }).catch(function () { reported = {}; });
    } else if (result === false) {
      rejectToken(access);
    }
  }

  function visible(el) {
    var rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }

  function setValue(el, value) {
    var desc = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');
    desc.set.call(el, value);
    ['input', 'change', 'blur'].forEach(function (name) {
      el.dispatchEvent(new Event(name, { bubbles: true }));
    });
  }

  function isTenantInput(input) {
    var text = (input.placeholder || '') + (input.name || '') + (input.id || '');
    return /学校|租户|tenant|school/i.test(text);
  }

  function tryFill() {
    if (submitted || !USERNAME || !PASSWORD) { return; }
    var inputs = Array.prototype.slice.call(document.querySelectorAll('input')).filter(visible);
    var pwdIndex = -1;
    for (var i = 0; i < inputs.length; i += 1) {
      if (inputs[i].type === 'password') { pwdIndex = i; break; }
    }
    if (pwdIndex < 0) { return; }

    // 登录页有「学校名称 / 用户名 / 密码」三个输入框，用户名紧挨在密码框前面。
    var userInput = null;
    for (var j = pwdIndex - 1; j >= 0; j -= 1) {
      var candidate = inputs[j];
      if (candidate.type === 'password' || isTenantInput(candidate)) { continue; }
      if (candidate.type === 'text' || candidate.type === '') { userInput = candidate; break; }
    }
    if (!userInput) {
      userInput = inputs.filter(function (input) {
        return input.type !== 'password' && !isTenantInput(input);
      })[0];
    }
    if (!userInput) { return; }

    if (userInput.value !== USERNAME) { setValue(userInput, USERNAME); }
    if (inputs[pwdIndex].value !== PASSWORD) { setValue(inputs[pwdIndex], PASSWORD); }
    if (userInput.value !== USERNAME || inputs[pwdIndex].value !== PASSWORD) { return; }
    setTimeout(function () {
      var buttons = Array.prototype.slice.call(document.querySelectorAll('button'));
      var loginButton = buttons.filter(function (b) {
        return visible(b) && /登\s*录/.test((b.innerText || '').replace(/\s/g, ''));
      })[0];
      if (loginButton) {
        submitted = true;
        loginButton.click();
      }
    }, 150);
  }

  function focusFirstInput() {
    var active = document.activeElement;
    if (active && (active.tagName === 'INPUT' || active.tagName === 'TEXTAREA')) { return; }
    var inputs = Array.prototype.slice.call(document.querySelectorAll('input')).filter(visible);
    for (var i = 0; i < inputs.length; i += 1) {
      if (inputs[i].type !== 'password') { inputs[i].focus(); return; }
    }
  }

  setTimeout(focusFirstInput, 80);
  setTimeout(focusFirstInput, 900);

  var timer = setInterval(function () {
    ticks += 1;
    announce();
    try { report(); tryFill(); } catch (e) {}
    if (ticks > 1200) { clearInterval(timer); }
  }, 500);

  announce();
  try { tryFill(); report(); } catch (e) {}
})();
"""


def build_inject_script(username: str, password: str, zoom: float = 1.0) -> str:
    script = _JS_TEMPLATE.replace(
        "__USERNAME__", json.dumps(username or "", ensure_ascii=False)
    )
    script = script.replace(
        "__PASSWORD__", json.dumps(password or "", ensure_ascii=False)
    )
    script = script.replace("__ZOOM__", repr(float(zoom)))
    return script


# ---------------------------------------------------------------- 登录窗口
def _token_ok(auth: AuthManager, access: str, refresh: str = "") -> bool:
    """只校验 access token，不刷新；避免把旧会话通过 refresh token 续回来。"""
    return auth.probe_token(access) != "unauthorized"


def _login_inprocess(
    cfg: Config,
    auth: AuthManager,
    *,
    username: str = "",
    password: str = "",
    timeout: int = 300,
    start_loop: bool = False,
    epoch: Optional[int] = None,
) -> tuple[bool, str]:
    """打开登录窗口，返回 (是否成功, 提示语)。

    start_loop 只在独立登录子进程（或调试入口）中使用，因为 pywebview 的 GUI
    循环必须由主线程启动。
    """
    try:
        import webview  # type: ignore
    except ImportError:
        return False, "未安装pywebview，无法打开登录窗口"

    if sys.platform.startswith("win"):
        renderer = str(getattr(webview, "renderer", ""))
        if renderer == "mshtml":
            return False, "需要WebView2 Runtime，或改用账号密码登录"

    epoch = auth.epoch if epoch is None else epoch
    username = username or cfg.username
    bridge = _Bridge(validator=lambda access, refresh: _token_ok(auth, access, refresh))
    try:
        screen = webview.screens[0]
        login_width = min(1200, max(820, int(screen.width) - 120))
        login_height = min(900, max(700, int(screen.height) - 120))
    except Exception:
        login_width, login_height = 1200, 900
    page_zoom = min(1.0, login_width / 1200.0)
    try:
        window = webview.create_window(
            "NSU-EventGrabber · 登录",
            url=f"{cfg.base_url}/login",
            js_api=bridge,
            width=login_width,
            height=login_height,
            resizable=True,
            focus=True,
            min_size=(820, 700),
        )
    except Exception as exc:  # noqa: BLE001 - 无图形环境时给出可读提示
        return False, f"无法创建登录窗口：{exc}"

    bridge._window = window
    log.info("登录窗口已创建")
    script = build_inject_script(username, password, page_zoom)
    done = threading.Event()
    load_ok = threading.Event()
    load_failure: dict[str, str] = {}
    inject_lock = threading.Lock()
    deadline = time.time() + timeout

    def inject(reason: str) -> None:
        """同一时刻只允许一次 run_js，避免把 UI 线程的脚本队列堆满。"""
        if bridge._token_event.is_set() or bridge._closed_event.is_set():
            return
        if not inject_lock.acquire(blocking=False):
            return
        try:
            window.run_js(script)
            log.info("登录脚本已注入：%s", reason)
        except Exception as exc:  # noqa: BLE001 - 页面尚未就绪或已关闭
            log.debug("登录脚本注入失败：%s，%s", reason, exc)
        finally:
            inject_lock.release()

    def delayed_inject(reason: str, delay: float) -> None:
        time.sleep(delay)
        inject(reason)

    def on_loaded():
        load_ok.set()
        log.info("登录页已加载")
        threading.Thread(
            target=lambda: delayed_inject("loaded", 0.15), daemon=True
        ).start()

    try:
        window.events.loaded += on_loaded
    except Exception:  # noqa: BLE001 - 旧版本 pywebview 没有 loaded 事件
        log.debug("无loaded事件，改用兜底注入")

    def on_closed():
        log.info("登录窗口已关闭")
        bridge._closed_event.set()

    def on_closing():
        # 关闭动画或页面卸载可能比 closed 事件更早；先释放等待中的登录调用，
        # 避免主界面一直停在“登录中”的转圈状态。
        bridge._closed_event.set()

    try:
        window.events.closed += on_closed
        window.events.closing += on_closing
    except Exception:  # noqa: BLE001
        log.debug("无closed/closing事件")

    def fallback_injector():
        """部分环境下 loaded 事件不触发时的兜底：少量、递增间隔地注入。"""
        for delay in (2.0, 6.0, 12.0, 20.0):
            if load_ok.is_set() or bridge._token_event.is_set() or bridge._closed_event.is_set():
                return
            if bridge._script_ready.is_set():
                return
            time.sleep(delay)
            if bridge._script_ready.is_set() or bridge._token_event.is_set():
                return
            inject("fallback")

    threading.Thread(target=fallback_injector, daemon=True).start()

    def activate_on_macos():
        """macOS 上窗口创建后主动置前；Windows 交给 pywebview 自己处理。"""
        time.sleep(0.4)
        if done.is_set() or time.time() >= deadline:
            return
        try:
            window.show()
        except Exception as exc:  # noqa: BLE001 - 窗口还没完全初始化时忽略
            log.debug("登录窗口显示失败：%s", exc)
            return
        try:
            from AppKit import NSApplication  # type: ignore

            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        except Exception:  # noqa: BLE001
            pass

    def load_timeout():
        if load_ok.is_set() or bridge._script_ready.is_set() or bridge._token_event.is_set():
            return
        load_failure["message"] = "登录页加载超时，检查WebView2 Runtime或改用账号密码"
        log.warning(load_failure["message"])
        bridge._closed_event.set()
        _safe_destroy(window)

    load_timer = threading.Timer(45, load_timeout)
    load_timer.daemon = True
    load_timer.start()

    def watchdog():
        bridge._closed_event.set()
        _safe_destroy(window)

    watchdog_timer = threading.Timer(timeout, watchdog)
    watchdog_timer.daemon = True
    watchdog_timer.start()

    try:
        if start_loop:
            login_cache = _fresh_cache_dir(Path(cfg.data_dir) / "webview-login")
            webview.start(
                debug=bool(os.environ.get("NSU_GUI_DEBUG")),
                private_mode=False,
                storage_path=str(login_cache),
                func=activate_on_macos if sys.platform == "darwin" else None,
            )
        else:
            if sys.platform == "darwin":
                threading.Thread(target=activate_on_macos, daemon=True).start()
            while (
                time.time() < deadline
                and not bridge._token_event.is_set()
                and not bridge._closed_event.is_set()
            ):
                time.sleep(0.15)
    except Exception as exc:  # noqa: BLE001
        return False, f"登录窗口启动失败：{exc}"
    finally:
        done.set()
        load_timer.cancel()
        watchdog_timer.cancel()
        _safe_destroy(window)

    access = bridge._access_token
    refresh = bridge._refresh_token
    if not access:
        return False, load_failure.get("message", "登录窗口已关闭，未取得Token")

    return _commit_login(auth, cfg, username, access, refresh, epoch=epoch, validated=True)


def _commit_login(
    auth: AuthManager,
    cfg: Config,
    username: str,
    access: str,
    refresh: str,
    *,
    epoch: Optional[int] = None,
    validated: bool = False,
) -> tuple[bool, str]:
    """把登录窗口拿到的 token 写回主进程的凭据存储。

    validated=True 表示 token 已经在登录子进程里校验过；此时先落盘再取资料，
    避免父进程重复探活拖住“登录中”状态，也保证重启后登录态不丢。
    """
    access = str(access or "").strip()
    refresh = str(refresh or "").strip()
    epoch = auth.epoch if epoch is None else epoch
    if not access:
        return False, "登录窗口未返回Token"
    if not auth.is_current(epoch):
        return False, "登录已退出，取消本次登录"

    # 只校验、不刷新：旧会话的 refresh token 也可能还有效，一旦刷新就会把
    # 刚刚退出登录的账号重新写回本地。登录窗口路径已经在子进程校验过，父进程
    # 不再重复请求，否则网络慢时会一直卡在“登录中”，凭据也迟迟不落盘。
    if not validated:
        if auth.probe_token(access) == "unauthorized":
            return False, "Token校验失败，请重新登录"
        if not auth.is_current(epoch):
            return False, "登录已退出，取消本次登录"

    with auth.session_lock:
        if not auth.is_current(epoch):
            return False, "登录已退出，取消本次登录"
        creds = auth.store.credentials
        creds.base_url = cfg.base_url
        creds.tenant_id = cfg.tenant_id
        creds.tenant_name = cfg.tenant_name
        creds.username = username or cfg.username
        creds.access_token = access
        creds.refresh_token = refresh
        auth.store.save()

    if validated:
        # 资料请求是锦上添花，不能阻塞登录成功和扫描启动；凭据已经落盘，
        # 主界面会由凭据监视线程立即刷新。
        threading.Thread(
            target=_fetch_profile_safely, args=(auth, epoch), daemon=True
        ).start()
        return True, "登录成功"

    auth.fetch_profile(epoch)
    return True, "登录成功"


def _fetch_profile_safely(auth: AuthManager, epoch: int) -> None:
    try:
        auth.fetch_profile(epoch)
    except Exception:  # noqa: BLE001 - 资料失败不影响登录态
        log.warning("资料获取失败", exc_info=True)


def login_with_webview(
    cfg: Config,
    auth: AuthManager,
    *,
    username: str = "",
    password: str = "",
    timeout: int = 300,
    start_loop: bool = False,
    epoch: Optional[int] = None,
) -> tuple[bool, str]:
    """打开独立登录进程；主 GUI/主进程不直接创建第二个 WebView。"""
    epoch = auth.epoch if epoch is None else epoch
    if start_loop:
        return _login_inprocess(
            cfg, auth, username=username, password=password,
            timeout=timeout, start_loop=True, epoch=epoch,
        )
    return _login_with_subprocess(
        cfg, auth, username=username, password=password, timeout=timeout, epoch=epoch
    )


def _read_tail(workdir: Path) -> str:
    tails = []
    for name in ("login-child.log", "child.log"):
        path = workdir / name
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if text.strip():
            tails.append(f"[{name}] {text[-800:]}")
    return "\n".join(tails)


def _login_with_subprocess(
    cfg: Config,
    auth: AuthManager,
    *,
    username: str,
    password: str,
    timeout: int,
    epoch: Optional[int] = None,
) -> tuple[bool, str]:
    epoch = auth.epoch if epoch is None else epoch
    workdir = Path(tempfile.mkdtemp(prefix="nsu-login-"))
    payload_path = workdir / "payload.json"
    result_path = workdir / "result.json"
    log_path = workdir / "child.log"
    payload = {
        "base_url": cfg.base_url,
        "tenant_id": cfg.tenant_id,
        "tenant_name": cfg.tenant_name,
        "username": username or cfg.username,
        "password": password,
        "timeout": timeout,
        "workdir": str(workdir),
        "parent_pid": os.getpid(),
    }
    payload_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(payload_path, 0o600)
    except OSError:
        pass

    frozen = bool(getattr(sys, "frozen", False))
    if frozen:
        cmd = [sys.executable]
    else:
        launcher = Path(__file__).resolve().with_name("gui_launcher.py")
        cmd = [sys.executable, str(launcher)]
    child_env = os.environ.copy()
    child_env["NSU_LOGIN_PAYLOAD"] = str(payload_path)
    child_env["NSU_LOGIN_RESULT"] = str(result_path)

    kwargs = {}
    if sys.platform.startswith("win"):
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    log_file = open(log_path, "w", encoding="utf-8")
    proc = None
    try:
        try:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=log_file, stderr=subprocess.STDOUT,
                cwd=str(Path(__file__).resolve().parent), env=child_env, **kwargs
            )
        except Exception as exc:  # noqa: BLE001
            return False, f"登录窗口启动失败：{exc}"
        _set_active_child(proc)

        deadline = time.time() + timeout + 20
        while time.time() < deadline:
            if not auth.is_current(epoch):
                return False, "登录已退出，取消本次登录"
            if result_path.exists():
                try:
                    result = json.loads(result_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    result = None
                if result is not None:
                    if result.get("ok"):
                        return _commit_login(
                            auth, cfg, result.get("username") or username,
                            str(result.get("access_token") or ""),
                            str(result.get("refresh_token") or ""),
                            epoch=epoch,
                            validated=True,
                        )
                    return False, str(result.get("message") or "登录失败")
            if proc.poll() is not None:
                tail = _read_tail(workdir)
                return False, ("登录窗口异常退出" + (f"：{tail}" if tail else ""))
            time.sleep(0.2)
        return False, "登录窗口超时，未取得Token"
    finally:
        terminate_login_child()
        try:
            log_file.close()
        except OSError:
            pass
        if proc is not None and _active_child is proc:
            _set_active_child(None)
        for attempt in range(4):
            shutil.rmtree(workdir, ignore_errors=True)
            if not workdir.exists():
                break
            time.sleep(0.25 * (attempt + 1))


# ---------------------------------------------------------------- 子进程入口
def _setup_child_logging(workdir: Path) -> None:
    """窗口化打包后 sys.stderr 为 None，日志必须落文件，否则排查不到问题。"""
    handlers: list[logging.Handler] = []
    try:
        handlers.append(logging.FileHandler(workdir / "login-child.log", encoding="utf-8"))
    except OSError:
        pass
    if getattr(sys, "stderr", None) is not None:
        handlers.append(logging.StreamHandler(sys.stderr))
    if handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
            handlers=handlers,
            force=True,
        )


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform.startswith("win"):
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            kernel32.CloseHandle(handle)
            return True
        except Exception:  # noqa: BLE001
            return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True


def _watch_parent(pid: int) -> None:
    if pid <= 0:
        return
    while True:
        time.sleep(2.0)
        if not _pid_alive(pid):
            log.warning("主进程退出，关闭登录窗口")
            os._exit(0)


def run_login_child(payload_path: str, result_path: str) -> int:
    """独立登录进程入口：只负责显示登录窗、拿 token、写结果文件。"""
    workdir = Path(payload_path).resolve().parent
    _setup_child_logging(workdir)
    result_written = threading.Event()
    result = {"ok": False, "message": "登录窗口未返回结果"}

    def write_result(data: dict) -> None:
        if result_written.is_set():
            return
        result_written.set()
        try:
            Path(result_path).write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            log.exception("登录结果写入失败")

    try:
        payload = json.loads(Path(payload_path).read_text(encoding="utf-8"))
        timeout = int(payload.get("timeout") or 300)
        threading.Thread(
            target=_watch_parent,
            args=(int(payload.get("parent_pid") or 0),),
            daemon=True,
        ).start()
        # WebView2 在部分 Windows 环境下拆除会卡住；到点强制写结果并退出。
        threading.Thread(
            target=_hard_stop, args=(timeout + 45, write_result), daemon=True
        ).start()

        cfg = Config(
            base_url=payload["base_url"],
            tenant_id=payload.get("tenant_id") or "",
            tenant_name=payload.get("tenant_name") or "",
            username=payload.get("username") or "",
            data_dir=workdir,
            auth_file=workdir / "credentials.json",
            state_file=workdir / "state.json",
            settings_file=workdir / "settings.json",
        )
        store = CredentialStore(workdir / "credentials.json")
        auth = AuthManager(cfg, store)
        ok, message = _login_inprocess(
            cfg, auth, username=payload.get("username") or "",
            password=payload.get("password") or "",
            timeout=timeout, start_loop=True, epoch=auth.epoch,
        )
        result = {
            "ok": bool(ok),
            "message": message,
            "username": auth.username,
            "access_token": auth.access_token,
            "refresh_token": auth.refresh_token,
        }
    except Exception as exc:  # noqa: BLE001
        log.exception("登录子进程异常")
        result = {"ok": False, "message": f"登录窗口异常：{exc}"}
    finally:
        write_result(result)

    try:
        if sys.stdout:
            sys.stdout.flush()
        if sys.stderr:
            sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass
    # 直接退出：不让 WebView2 的拆除流程拖住进程，父进程已经拿到结果文件。
    os._exit(0 if result.get("ok") else 1)


def _hard_stop(delay: float, write_result: Callable[[dict], None]) -> None:
    time.sleep(max(30.0, delay))
    write_result({"ok": False, "message": "登录窗口超时，已自动关闭"})
    os._exit(1)
