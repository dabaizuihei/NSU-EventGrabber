"""配置加载层。

优先级：`config.env` < `settings.json`（GUI 写入） < 环境变量。
环境变量只用于少量运行参数；日常设置都在 GUI 内完成。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_BASE_URL = "https://study.nsu.edu.cn"
DEFAULT_STATE_FILE = "state.json"
DEFAULT_AUTH_FILE = "credentials.json"
DEFAULT_SETTINGS_FILE = "settings.json"
DEFAULT_TENANT_ID = ""
DEFAULT_TENANT_NAME = ""
DEFAULT_BLOCKLIST = r"仅限.*报名|工作人员|参赛人员"

KNOWN_KEYS = {
    "NSU_BASE_URL",
    "NSU_TENANT_ID",
    "NSU_TENANT_NAME",
    "NSU_USERNAME",
    "NOTIFY_PROVIDERS",
    "WEBHOOK_URL",
    "WEBHOOK_KIND",
    "SCAN_INTERVAL_SECONDS",
    "REQUEST_TIMEOUT",
    "AUTO_REGISTER",
    "REGISTER_BLOCKLIST",
    "KEYWORD_INCLUDE",
    "KEYWORD_EXCLUDE",
    "UPCOMING_DAYS",
    "PAGE_SIZE",
    "AUTO_RELOGIN",
    "AUTO_MONITOR",
    "HIDE_PAST_REGISTERED",
    "CLOSE_TO_TRAY",
    "AUTO_CHECK_UPDATE",
}

# GUI 可写的设置项（写入 settings.json）
SETTINGS_KEYS = {
    "SCAN_INTERVAL_SECONDS",
    "AUTO_REGISTER",
    "REGISTER_BLOCKLIST",
    "KEYWORD_INCLUDE",
    "KEYWORD_EXCLUDE",
    "UPCOMING_DAYS",
    "NOTIFY_PROVIDERS",
    "WEBHOOK_URL",
    "WEBHOOK_KIND",
    "AUTO_RELOGIN",
    "AUTO_MONITOR",
    "HIDE_PAST_REGISTERED",
    "CLOSE_TO_TRAY",
    "AUTO_CHECK_UPDATE",
}


def parse_env_file(path: Path) -> dict[str, str]:
    """解析 .env 风格配置：支持注释、空行，值内允许 '='。"""
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            result[key] = value
    return result


def _truthy(raw: str, default: bool) -> bool:
    raw = (raw or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on", "是")


def resolve_data_dir(config_path: Path | None, explicit: str | None = None) -> Path:
    """决定数据目录：显式参数 > 配置文件所在目录 > 当前目录 > 用户目录。"""
    if explicit:
        return Path(explicit).expanduser().resolve()
    if config_path is not None and config_path.exists():
        return config_path.parent.resolve()
    if Path("config.env").exists():
        return Path.cwd().resolve()
    return (Path.home() / ".nsu-event-grabber").resolve()


@dataclass
class Config:
    base_url: str = DEFAULT_BASE_URL
    tenant_id: str = DEFAULT_TENANT_ID
    tenant_name: str = DEFAULT_TENANT_NAME
    username: str = ""

    notify_providers: tuple[str, ...] = ("desktop",)
    webhook_url: str = ""
    webhook_kind: str = "auto"

    data_dir: Path = field(default_factory=Path.cwd)
    state_file: Path = field(default_factory=lambda: Path(DEFAULT_STATE_FILE))
    auth_file: Path = field(default_factory=lambda: Path(DEFAULT_AUTH_FILE))
    settings_file: Path = field(default_factory=lambda: Path(DEFAULT_SETTINGS_FILE))

    scan_interval: int = 300
    request_timeout: int = 20
    auto_register: bool = True
    blocklist_pattern: str = DEFAULT_BLOCKLIST
    keyword_include: str = ""
    keyword_exclude: str = ""
    upcoming_days: int = 3
    page_size: int = 50

    auto_relogin: bool = True
    auto_monitor: bool = True
    hide_past_registered: bool = True
    close_to_tray: bool = True
    auto_check_update: bool = True

    block_re: re.Pattern = field(init=False, repr=False)

    def __post_init__(self) -> None:
        raw = (self.blocklist_pattern or "").strip()
        if not raw:
            # 用户把“屏蔽词”全部删掉时表示不屏蔽，不能变成一个匹配所有标题的空正则。
            self.block_re = re.compile(r"(?!)")
            self.blocklist_pattern = ""
            return
        try:
            self.block_re = re.compile(raw)
        except re.error:
            self.block_re = re.compile(DEFAULT_BLOCKLIST)

    # ---- GUI 用 -----------------------------------------------------------
    def public_dict(self) -> dict:
        return {
            "base_url": self.base_url,
            "tenant_id": self.tenant_id,
            "username": self.username,
            "scan_interval": self.scan_interval,
            "auto_register": self.auto_register,
            "blocklist": self.blocklist_pattern,
            "keyword_include": self.keyword_include,
            "keyword_exclude": self.keyword_exclude,
            "upcoming_days": self.upcoming_days,
            "notify_providers": list(self.notify_providers),
            "webhook_url": self.webhook_url,
            "webhook_kind": self.webhook_kind,
            "auto_relogin": self.auto_relogin,
            "auto_monitor": self.auto_monitor,
            "hide_past_registered": self.hide_past_registered,
            "close_to_tray": self.close_to_tray,
            "auto_check_update": self.auto_check_update,
        }


def load_config(config_path: str | Path | None = None, data_dir: str | None = None) -> Config:
    explicit_data = Path(data_dir).expanduser() if data_dir else None
    if config_path:
        cfg_path = Path(config_path).expanduser()
    elif explicit_data and (explicit_data / "config.env").exists():
        cfg_path = explicit_data / "config.env"
    else:
        cfg_path = Path("config.env")
    file_env = parse_env_file(cfg_path)

    data = resolve_data_dir(cfg_path if cfg_path.exists() else None, data_dir)
    data.mkdir(parents=True, exist_ok=True)
    # 固定数据目录里的 config.env 也可以作为“默认值”来源：
    # GUI 的 settings.json 优先，重置设置后回落到这里。
    if not file_env and config_path is None:
        data_cfg = data / "config.env"
        if data_cfg.exists():
            file_env = parse_env_file(data_cfg)

    merged: dict[str, str] = dict(file_env)
    settings_path = data / DEFAULT_SETTINGS_FILE
    if settings_path.exists():
        try:
            overrides = json.loads(settings_path.read_text(encoding="utf-8"))
            for key, value in (overrides or {}).items():
                if key in SETTINGS_KEYS and value is not None:
                    merged[key] = str(value)
        except (json.JSONDecodeError, OSError):
            pass
    merged.update({k: v for k, v in os.environ.items() if k in KNOWN_KEYS})

    def get(key: str, default: str = "") -> str:
        return str(merged.get(key, default) or "").strip()

    raw_providers = get("NOTIFY_PROVIDERS") or "desktop"
    providers = tuple(
        p.strip().lower() for p in raw_providers.replace(";", ",").split(",") if p.strip()
    ) or ("desktop",)

    def as_int(key: str, default: int) -> int:
        try:
            return int(get(key, str(default)) or default)
        except ValueError:
            return default

    return Config(
        base_url=(get("NSU_BASE_URL", DEFAULT_BASE_URL) or DEFAULT_BASE_URL).rstrip("/"),
        tenant_id=(get("NSU_TENANT_ID") or "").strip(),
        tenant_name=(get("NSU_TENANT_NAME") or "").strip(),
        username=get("NSU_USERNAME"),
        notify_providers=providers,
        webhook_url=get("WEBHOOK_URL"),
        webhook_kind=get("WEBHOOK_KIND", "auto").lower() or "auto",
        data_dir=data,
        state_file=data / DEFAULT_STATE_FILE,
        auth_file=data / DEFAULT_AUTH_FILE,
        settings_file=settings_path,
        scan_interval=max(30, as_int("SCAN_INTERVAL_SECONDS", 300)),
        request_timeout=max(5, as_int("REQUEST_TIMEOUT", 20)),
        auto_register=_truthy(get("AUTO_REGISTER"), True),
        # 显式保存空字符串表示“没有屏蔽词”；只有从未设置过才使用默认规则。
        blocklist_pattern=(
            get("REGISTER_BLOCKLIST")
            if "REGISTER_BLOCKLIST" in merged
            else DEFAULT_BLOCKLIST
        ),
        keyword_include=get("KEYWORD_INCLUDE"),
        keyword_exclude=get("KEYWORD_EXCLUDE"),
        upcoming_days=max(0, as_int("UPCOMING_DAYS", 3)),
        page_size=max(1, as_int("PAGE_SIZE", 50)),
        auto_relogin=_truthy(get("AUTO_RELOGIN"), True),
        auto_monitor=_truthy(get("AUTO_MONITOR"), True),
        hide_past_registered=_truthy(get("HIDE_PAST_REGISTERED"), True),
        close_to_tray=_truthy(get("CLOSE_TO_TRAY"), True),
        auto_check_update=_truthy(get("AUTO_CHECK_UPDATE"), True),
    )


def _validate_regex(value: str, label: str) -> None:
    if not value:
        return
    try:
        re.compile(value)
    except re.error as exc:
        raise ValueError(f"{label}正则无效：{exc}") from exc


# settings.json 的键 → (Config 属性, 值转换函数)。写入与内存应用共用一份映射。
_SETTING_CASTERS = {
    "SCAN_INTERVAL_SECONDS": lambda v: max(30, int(v)),
    "AUTO_REGISTER": bool,
    "REGISTER_BLOCKLIST": lambda v: str(v or ""),
    "KEYWORD_INCLUDE": str,
    "KEYWORD_EXCLUDE": str,
    "UPCOMING_DAYS": lambda v: max(0, int(v)),
    "NOTIFY_PROVIDERS": lambda v: (
        tuple(p for p in v.split(",") if p.strip())
        if isinstance(v, str) else tuple(v)
    ),
    "WEBHOOK_URL": str,
    "WEBHOOK_KIND": str,
    "AUTO_RELOGIN": bool,
    "AUTO_MONITOR": bool,
    "HIDE_PAST_REGISTERED": bool,
    "CLOSE_TO_TRAY": bool,
    "AUTO_CHECK_UPDATE": bool,
}

_SETTING_ATTRS = {
    "SCAN_INTERVAL_SECONDS": "scan_interval",
    "AUTO_REGISTER": "auto_register",
    "REGISTER_BLOCKLIST": "blocklist_pattern",
    "KEYWORD_INCLUDE": "keyword_include",
    "KEYWORD_EXCLUDE": "keyword_exclude",
    "UPCOMING_DAYS": "upcoming_days",
    "NOTIFY_PROVIDERS": "notify_providers",
    "WEBHOOK_URL": "webhook_url",
    "WEBHOOK_KIND": "webhook_kind",
    "AUTO_RELOGIN": "auto_relogin",
    "AUTO_MONITOR": "auto_monitor",
    "HIDE_PAST_REGISTERED": "hide_past_registered",
    "CLOSE_TO_TRAY": "close_to_tray",
    "AUTO_CHECK_UPDATE": "auto_check_update",
}


def save_settings(cfg: Config, patch: dict) -> Config:
    """把 GUI 修改写入 settings.json，并返回新的 Config。

    先校验正则再写文件，避免把非法规则写进去导致下一轮扫描直接失败。
    """
    for key, label in (
        ("REGISTER_BLOCKLIST", "屏蔽词"),
        ("KEYWORD_INCLUDE", "白名单"),
        ("KEYWORD_EXCLUDE", "黑名单"),
    ):
        if key in patch:
            _validate_regex(str(patch[key] or ""), label)
    current: dict[str, str] = {}
    if cfg.settings_file.exists():
        try:
            current = dict(json.loads(cfg.settings_file.read_text(encoding="utf-8")) or {})
        except (json.JSONDecodeError, OSError):
            current = {}

    for key in _SETTING_ATTRS:
        if key not in patch:
            continue
        value = patch[key]
        if isinstance(value, bool):
            value = "true" if value else "false"
        elif isinstance(value, (list, tuple)):
            value = ",".join(str(v) for v in value)
        current[key] = str(value)

    cfg.settings_file.write_text(
        json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    for key, attr in _SETTING_ATTRS.items():
        if key not in patch:
            continue
        setattr(cfg, attr, _SETTING_CASTERS[key](patch[key]))
        if key == "REGISTER_BLOCKLIST":
            # 屏蔽词变更要同步重编译正则，否则要等下一轮重建 Config 才生效。
            value = cfg.blocklist_pattern
            cfg.block_re = re.compile(value) if value else re.compile(r"(?!)")
    return cfg
