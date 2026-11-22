"""活动广场 API 客户端。所有接口路径集中在这里，平台改版只改这一个文件。"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from auth import API_ACTIVITY_LIST, API_CANCEL, API_REGISTER, API_SQUARE, USER_AGENT
from config import Config
from netutil import ssl_context

log = logging.getLogger("nsu.client")


def _record_id(record: dict) -> str:
    """活动记录 ID：广场/列表接口用 infoId，个别响应退化为 id。"""
    return str(record.get("infoId") or record.get("id") or "")


class ApiError(Exception):
    """平台业务错误（HTTP 200 但 code 非 0/200），携带业务码与提示语。"""

    def __init__(self, code, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


class NsuClient:
    def __init__(self, cfg: Config, auth) -> None:
        self.cfg = cfg
        self.auth = auth

    # ---- 底层请求 ----------------------------------------------------------
    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        json_body: Any = None,
        auth: bool = True,
        _retry: bool = True,
    ):
        if auth and self.auth.access_token:
            self.auth.ensure_tenant()
        url = self.cfg.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Referer": self.cfg.base_url + "/",
            "tenant-id": self.cfg.tenant_id,
        }
        body = None
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if auth and self.auth.access_token:
            headers["Authorization"] = "Bearer " + self.auth.access_token

        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(
                req, timeout=self.cfg.request_timeout, context=ssl_context()
            ) as resp:
                status = resp.status
                raw = resp.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as exc:
            status = exc.code
            raw = exc.read().decode("utf-8", "ignore") if exc.fp else ""
        except urllib.error.URLError as exc:
            raise ApiError(None, f"网络错误: {exc.reason}") from exc

        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            raise ApiError(None, "响应不是 JSON，平台可能改版或需要登录") from exc

        code = payload.get("code")
        if code == 401 or status == 401:
            if _retry and auth and self.auth.refresh():
                return self._request(
                    method, path, params=params, json_body=json_body, auth=auth, _retry=False
                )
            raise ApiError(401, "登录态失效")
        if code not in (0, 200):
            raise ApiError(code, str(payload.get("msg") or "未知业务错误"))
        return payload.get("data")

    # ---- 业务接口 ----------------------------------------------------------
    def ensure_login(self) -> bool:
        """委托给认证层：探活 + 必要时 refresh。"""
        return self.auth.ensure_valid()

    def fetch_all_activities(self) -> list[dict[str, Any]]:
        collected: list[dict[str, Any]] = []
        current = 1
        while True:
            data = self._request(
                "GET", API_SQUARE, params={"current": current, "size": self.cfg.page_size}
            )
            records = (data or {}).get("records") or []
            collected.extend(records)
            total = int((data or {}).get("total") or 0)
            if len(collected) >= total or not records:
                break
            current += 1

        # 活动广场只给报名时间；活动起止时间要从活动库列表补全。
        try:
            times = self.fetch_activity_times({_record_id(r) for r in collected})
            for record in collected:
                detail = times.get(_record_id(record))
                if not detail:
                    continue
                for key in (
                    "activityStartTime", "activityEndTime",
                    "signInStartTime", "signInEndTime",
                    "signOutStartTime", "signOutEndTime",
                ):
                    if record.get(key) in (None, "") and detail.get(key) not in (None, ""):
                        record[key] = detail[key]
        except ApiError as exc:
            log.warning("活动时间补全失败：%s", exc.message)
        return collected

    def fetch_activity_times(self, wanted_ids: set[str]) -> dict[str, dict[str, Any]]:
        """从活动库分页列表里取活动起止时间，只保留广场里出现的活动。"""
        result: dict[str, dict[str, Any]] = {}
        current = 1
        pages = 1
        while current <= pages and wanted_ids:
            data = self._request(
                "GET", API_ACTIVITY_LIST,
                params={"current": current, "size": 500},
            ) or {}
            pages = int(data.get("pages") or 1)
            for item in data.get("records") or []:
                key = _record_id(item)
                if key in wanted_ids:
                    result[key] = item
            if len(result) >= len(wanted_ids):
                break
            current += 1
        return result

    def register(self, info_id: str) -> tuple[bool, str]:
        try:
            self._request("GET", API_REGISTER, params={"infoId": info_id})
            return True, "报名成功"
        except ApiError as exc:
            return False, exc.message

    def cancel(self, register_id: str) -> tuple[bool, str]:
        """取消报名。平台要求的是报名记录 ID（regisgerId），不是活动 infoId。"""
        try:
            self._request("GET", API_CANCEL, params={"registerId": register_id})
            return True, "已取消报名"
        except ApiError as exc:
            return False, exc.message
