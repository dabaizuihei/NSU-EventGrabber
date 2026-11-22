"""认证层：凭据存储、token 探活/刷新与登录态管理。"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from config import Config
from netutil import ssl_context

log = logging.getLogger("nsu.auth")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

API_SQUARE = "/web-api/activities/info/activitySquare"
API_ACTIVITY_LIST = "/web-api/activities/info/list"
API_REGISTER = "/web-api/activities/student/register/add"
API_CANCEL = "/web-api/activities/student/register/cancel"
API_REFRESH = "/web-api/system/auth/refresh-token"
API_PERMISSION = "/web-api/system/auth/get-permission-info"
API_TENANT_BY_WEBSITE = "/web-api/system/tenant/get-by-website"

# ---------------------------------------------------------------- 凭据
@dataclass
class Credentials:
    """落盘凭据（不含密码）。"""

    base_url: str = ""
    tenant_id: str = ""
    tenant_name: str = ""
    username: str = ""
    access_token: str = ""
    refresh_token: str = ""
    expires_at: float = 0.0
    display_name: str = ""
    student_no: str = ""
    updated_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "schema_version": 1,
            "base_url": self.base_url,
            "tenant_id": self.tenant_id,
            "tenant_name": self.tenant_name,
            "username": self.username,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "display_name": self.display_name,
            "student_no": self.student_no,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Credentials":
        return cls(
            base_url=data.get("base_url", ""),
            tenant_id=str(data.get("tenant_id", "") or ""),
            tenant_name=data.get("tenant_name", ""),
            username=data.get("username", ""),
            access_token=data.get("access_token", "") or "",
            refresh_token=data.get("refresh_token", "") or "",
            expires_at=float(data.get("expires_at") or 0),
            display_name=data.get("display_name", ""),
            student_no=data.get("student_no", ""),
            updated_at=float(data.get("updated_at") or 0),
        )


# ---------------------------------------------------------------- 凭据存储
class CredentialStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.credentials = Credentials()
        self.load()

    def load(self) -> Credentials:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.credentials = Credentials.from_dict(data)
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("凭据读取失败：%s", exc)
                self.credentials = Credentials()
        return self.credentials

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        c = self.credentials
        if (
            not self.path.exists()
            and not c.access_token
            and not c.refresh_token
            and not c.username
        ):
            # 退出登录后，后台的租户解析等流程仍可能带着“只有租户信息”的空凭据
            # 调用 save()。此时不要再把凭据文件创建出来，否则界面会误判为“已登录”。
            return
        c.updated_at = time.time()
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(c.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass  # Windows 上 chmod 语义有限，忽略

    def clear(self) -> None:
        """清空内存凭据并删除凭据文件（含 save() 留下的临时文件）。"""
        self.credentials = Credentials()
        for path in (self.path, self.path.with_suffix(".tmp")):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                # Windows 上文件可能被安全软件短暂占用，退化为写入空凭据，
                # 保证下次 load() 不会再读到旧 token。
                log.warning("凭据删除失败：%s，已写入空凭据", exc)
                try:
                    path.write_text(
                        json.dumps(Credentials().to_dict(), ensure_ascii=False),
                        encoding="utf-8",
                    )
                except OSError:
                    pass


# ---------------------------------------------------------------- 认证管理
class AuthManager:
    """负责「有没有可用的 access token」这一件事。"""

    def __init__(self, cfg: Config, store: CredentialStore) -> None:
        self.cfg = cfg
        self.store = store
        # 登录会话版本号：退出登录时自增。所有可能在网络上耗时、随后又要把
        # token 写回本地的操作（刷新、拉资料、解析租户、登录提交）都先记下
        # 版本号，写回前再比对一次；不一致说明期间已经退出登录，直接丢弃结果。
        # 这样退出后不会被“迟到的请求”把旧账号写回来。
        self._epoch = 0
        self.session_lock = threading.RLock()
        self._tenant_ready = False
        stored_tid = str(self.store.credentials.tenant_id or "")
        if stored_tid and (not cfg.tenant_id or cfg.tenant_id == "1"):
            cfg.tenant_id = stored_tid
        if not cfg.tenant_name and self.store.credentials.tenant_name:
            cfg.tenant_name = self.store.credentials.tenant_name

    # ---- 会话版本 ---------------------------------------------------------
    @property
    def epoch(self) -> int:
        return self._epoch

    def is_current(self, epoch: int) -> bool:
        return int(epoch) == self._epoch

    def reset_session(self) -> None:
        """退出登录：使所有在途请求失效，并清空本地凭据。"""
        with self.session_lock:
            self._epoch += 1
            self.store.clear()

    def _save_if_current(self, epoch: int) -> bool:
        with self.session_lock:
            if epoch != self._epoch:
                log.info("登录已结束，忽略凭据写入")
                return False
            self.store.save()
            return True

    # ---- 基础属性 ---------------------------------------------------------
    @property
    def access_token(self) -> str:
        return self.store.credentials.access_token

    @property
    def refresh_token(self) -> str:
        return self.store.credentials.refresh_token

    @property
    def username(self) -> str:
        return self.store.credentials.username or self.cfg.username

    @property
    def logged_in(self) -> bool:
        return bool(self.access_token)

    @property
    def expires_at(self) -> float:
        return self.store.credentials.expires_at

    # ---- HTTP -----------------------------------------------------------
    def _headers(
        self,
        with_auth: bool = False,
        content_type: str = "application/json",
        with_tenant: bool = True,
        bearer: Optional[str] = None,
    ) -> dict[str, str]:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Referer": self.cfg.base_url + "/",
        }
        if with_tenant and self.cfg.tenant_id:
            headers["tenant-id"] = self.cfg.tenant_id
        if content_type:
            headers["Content-Type"] = content_type
        token = bearer if bearer is not None else self.access_token
        if with_auth and token:
            headers["Authorization"] = "Bearer " + token
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        json_body: Any = None,
        auth: bool = False,
        timeout: Optional[int] = None,
        with_tenant: bool = True,
        bearer: Optional[str] = None,
    ) -> tuple[int, Any]:
        url = self.cfg.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        body = None
        content_type = "application/json"
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
        headers = self._headers(
            with_auth=auth,
            content_type=content_type,
            with_tenant=with_tenant,
            bearer=bearer,
        )
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(
                req, timeout=timeout or self.cfg.request_timeout, context=ssl_context()
            ) as resp:
                raw = resp.read().decode("utf-8", "ignore")
                status = resp.status
        except urllib.error.HTTPError as exc:
            status = exc.code
            raw = exc.read().decode("utf-8", "ignore") if exc.fp else ""
        except urllib.error.URLError as exc:
            raise ConnectionError(f"网络错误: {exc.reason}") from exc
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"raw": raw[:200]}
        return status, payload

    @staticmethod
    def _ok(payload: dict) -> bool:
        return payload.get("code") in (0, 200)

    # ---- token 生命周期 ----------------------------------------------------
    def _probe_square(self, token: str, *, strict_403: bool) -> str:
        """GET 活动广场做 token 探针，返回 ok / unauthorized / error。

        strict_403=False：403 一律视为 unauthorized（当前会话已不可用）。
        strict_403=True：403 可能只是租户头不匹配，不能据此断定 token 失效，
        返回 error 交给调用方再处理。
        """
        try:
            status, payload = self._request(
                "GET", API_SQUARE, params={"current": 1, "size": 1},
                auth=True, bearer=token,
            )
        except ConnectionError as exc:
            log.warning("Token校验网络异常：%s", exc)
            return "error"
        code = payload.get("code")
        if status == 401 or code == 401:
            return "unauthorized"
        if code == 403 or (strict_403 and status == 403):
            return "error" if strict_403 else "unauthorized"
        return "ok" if self._ok(payload) else "error"

    def _probe(self, token: Optional[str] = None) -> str:
        """返回 ok / unauthorized / error。token 为空时使用当前凭据。"""
        token = self.access_token if token is None else str(token or "")
        if not token:
            return "unauthorized"
        return self._probe_square(token, strict_403=False)

    def probe_token(self, access: str) -> str:
        """校验指定 access token，不读写本地凭据，也不会用 refresh token 续期。

        登录窗口和“登录提交”都用它确认 token 真的属于当前租户；过期 token 不应
        触发刷新，否则会把上一次会话的账号重新续期回来（退出登录不彻底）。
        """
        access = str(access or "").strip()
        if not access:
            return "unauthorized"
        self.ensure_tenant()
        return self._probe_square(access, strict_403=True)

    def ensure_tenant(self, epoch: Optional[int] = None) -> None:
        """确保拿到了当前站点真正的租户 ID。

        平台按域名返回的租户 ID 是一串雪花号（不是 1），登录 token 也绑定这个租户。
        租户头带错会直接 403「您无权访问该租户的数据」，所以这里显式解析一次。
        """
        if self._tenant_ready:
            return
        tid = str(self.cfg.tenant_id or "")
        if tid and tid != "1" and len(tid) >= 10:
            self._tenant_ready = True
            return
        self.resolve_tenant(epoch)

    def ensure_valid(self, epoch: Optional[int] = None) -> bool:
        """保证当前有一个可用 token：探活 → 必要时刷新。"""
        epoch = self._epoch if epoch is None else epoch
        self.ensure_tenant(epoch)
        if not self.is_current(epoch):
            return False
        probe = self._probe()
        if probe == "ok":
            return True
        if probe == "error":
            # 网络抖动不打断主流程（与原版一致）
            return bool(self.access_token)
        if self.refresh(epoch):
            if not self.is_current(epoch):
                return False
            return self._probe() == "ok"
        return False

    def apply_login_data(self, data: dict, epoch: Optional[int] = None) -> bool:
        epoch = self._epoch if epoch is None else epoch
        with self.session_lock:
            if not self.is_current(epoch):
                log.info("登录已结束，忽略登录数据")
                return False
            creds = self.store.credentials
            creds.base_url = self.cfg.base_url
            creds.tenant_id = self.cfg.tenant_id
            if data.get("accessToken"):
                creds.access_token = str(data["accessToken"])
            if data.get("refreshToken"):
                creds.refresh_token = str(data["refreshToken"])
            expires = data.get("expiresTime") or data.get("expiresTimeMs") or 0
            try:
                expires = float(expires)
            except (TypeError, ValueError):
                expires = 0.0
            if expires > 1e12:
                expires /= 1000.0
            if 0 < expires < 1e11:
                creds.expires_at = expires
            if data.get("tenantId"):
                creds.tenant_id = str(data["tenantId"])
                self.cfg.tenant_id = creds.tenant_id
            if self.cfg.username:
                creds.username = self.cfg.username
            self.store.save()
            return True

    def refresh(self, epoch: Optional[int] = None) -> bool:
        epoch = self._epoch if epoch is None else epoch
        self.ensure_tenant(epoch)
        if not self.is_current(epoch):
            return False
        token = self.refresh_token
        if not token:
            log.info("无refresh token，无法续期")
            return False
        try:
            status, payload = self._request(
                "POST", API_REFRESH, params={"refreshToken": token}, json_body=None
            )
        except ConnectionError as exc:
            log.warning("Token刷新网络异常：%s", exc)
            return False
        if status == 401 or not self._ok(payload):
            log.warning("refresh token失效：%s", payload.get("msg") or status)
            return False
        data = payload.get("data") or {}
        if not data.get("accessToken"):
            return False
        if not self.apply_login_data(data, epoch):
            return False
        log.info("Token已续期")
        return True

    # ---- 租户与账号信息 -----------------------------------------------------
    def resolve_tenant(self, epoch: Optional[int] = None) -> None:
        """尽量从服务端拿到租户名称，失败就用配置里的值。"""
        epoch = self._epoch if epoch is None else epoch
        host = urllib.parse.urlparse(self.cfg.base_url).hostname or ""
        if not host:
            return
        try:
            status, payload = self._request(
                "GET", API_TENANT_BY_WEBSITE, params={"website": host}, with_tenant=False
            )
        except ConnectionError:
            return
        data = payload.get("data") or {}
        if self._ok(payload) and data.get("name"):
            with self.session_lock:
                if not self.is_current(epoch):
                    return
                self.cfg.tenant_name = str(data["name"])
                if data.get("id"):
                    self.cfg.tenant_id = str(data["id"])
                self.store.credentials.tenant_name = self.cfg.tenant_name
                self.store.credentials.tenant_id = self.cfg.tenant_id
                self.store.save()
                self._tenant_ready = True

    def fetch_profile(self, epoch: Optional[int] = None) -> None:
        """登录后拉一次用户信息，用于界面显示姓名/学号。"""
        epoch = self._epoch if epoch is None else epoch
        token = self.access_token
        if not token:
            return
        try:
            status, payload = self._request("GET", API_PERMISSION, auth=True, bearer=token)
        except ConnectionError:
            return
        data = payload.get("data") or {}
        user = data.get("user") or {}
        with self.session_lock:
            if not self.is_current(epoch):
                return
            if user.get("nickname"):
                self.store.credentials.display_name = str(user["nickname"])
            if user.get("number"):
                self.store.credentials.student_no = str(user["number"])
                if not self.store.credentials.username:
                    self.store.credentials.username = str(user["number"])
            if self.store.credentials.display_name or self.store.credentials.student_no:
                self.store.save()
