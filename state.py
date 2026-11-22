"""本地状态文件（与原版字段完全兼容，旧 state.json 可直接复用）。"""
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger("nsu.state")

SCHEMA_VERSION = 3


class State:
    """记录已见活动、已报名历史、名额快照与基线状态。

    字段含义与原版保持一致：
    - seen_ids: 已见过的活动 ID（含基线存量）
    - registered_ids: 自动报名成功的活动 ID
    - ever_registered_ids: 历史上报过名的活动（防止"取消后被重新报名"）
    - quota: {id: "已报/上限"} 快照，用于识别名额释放
    - last_upcoming_date: 上次发送"即将参加"提醒的日期
    - pending_registration_ids: 已发现但还没到报名时间的活动，开放后补报
    - initialized: 是否已建立首次运行基线
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.seen_ids: list[str] = []
        self.registered_ids: list[str] = []
        self.ever_registered_ids: list[str] = []
        self.quota: dict[str, str] = {}
        self.last_upcoming_date = ""
        self.pending_registration_ids: list[str] = []
        self.initialized = False
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.seen_ids = list(data.get("seen_ids") or [])
            self.registered_ids = list(data.get("registered_ids") or [])
            self.ever_registered_ids = list(data.get("ever_registered_ids") or [])
            self.quota = dict(data.get("quota") or {})
            self.last_upcoming_date = data.get("last_upcoming_date") or ""
            self.pending_registration_ids = list(data.get("pending_registration_ids") or [])
            self.initialized = bool(data.get("initialized"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("状态读取失败，重新开始：%s", exc)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "seen_ids": self.seen_ids[-3000:],
            "registered_ids": self.registered_ids[-3000:],
            "ever_registered_ids": self.ever_registered_ids[-3000:],
            "quota": self.quota,
            "last_upcoming_date": self.last_upcoming_date,
            "pending_registration_ids": self.pending_registration_ids[-3000:],
            "initialized": self.initialized,
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def mark_seen(self, ids: list[str]) -> None:
        for i in ids:
            if i not in self.seen_ids:
                self.seen_ids.append(i)

    def mark_ever_registered(self, ids: list[str]) -> None:
        for i in ids:
            if i not in self.ever_registered_ids:
                self.ever_registered_ids.append(i)

    def mark_pending_registration(self, ids: list[str]) -> None:
        for i in ids:
            if i not in self.pending_registration_ids:
                self.pending_registration_ids.append(i)

    def clear_pending_registration(self, ids: list[str]) -> None:
        pending = set(ids)
        self.pending_registration_ids = [
            i for i in self.pending_registration_ids if i not in pending
        ]
