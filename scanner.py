"""扫描、过滤与自动报名业务。"""
from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from config import Config

log = logging.getLogger("nsu.scan")

STATUS_NOT_REGISTERED = 4
STATUS_APPROVING = 0
STATUS_REGISTERED = 1

TIME_KEYS_START = ("startTime", "activityStartTime", "beginTime", "activeStartTime", "activeBeginTime")
TIME_KEYS_END = ("endTime", "activityEndTime", "finishTime", "activeEndTime", "activeFinishTime")
REG_START_KEYS = ("registerStartTime", "signUpStartTime", "regStartTime")
REG_END_KEYS = ("registerEndTime", "signUpEndTime", "regEndTime")
QUOTA_KEYS = ("registerAndLimitPeople", "registerLimitPeople", "peopleLimit")


@dataclass
class ScanResult:
    """一轮扫描的完整产出。"""

    records: list[dict[str, Any]] = field(default_factory=list)
    new_activities: list[dict[str, Any]] = field(default_factory=list)
    reg_results: list[tuple[str, str]] = field(default_factory=list)
    releases: list[str] = field(default_factory=list)
    upcoming: list[str] = field(default_factory=list)
    blocked_ids: set[str] = field(default_factory=set)
    full_ids: set[str] = field(default_factory=set)
    baseline: bool = False
    changed: bool = False
    title: str = ""
    content: str = ""
    summary: str = ""
    error: str = ""
    # 本轮通知里是否包含失败项；应用内推送据此标红。
    notify_error: bool = False


# ---------------------------------------------------------------- 字段解析
def first(record: dict, keys, default=None):
    for key in keys:
        value = record.get(key)
        if value not in (None, ""):
            return value
    return default


def parse_dt(value: Any) -> Optional[dt.datetime]:
    if isinstance(value, dt.datetime):
        return value
    if isinstance(value, (int, float)):
        try:
            ts = float(value)
            if ts > 1e12:
                ts /= 1000.0
            return dt.datetime.fromtimestamp(ts)
        except (ValueError, OSError):
            return None
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return dt.datetime.fromisoformat(text)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def fmt_dt(value: Any) -> str:
    if isinstance(value, dt.datetime):
        return value.strftime("%m-%d %H:%M")
    parsed = parse_dt(value)
    return parsed.strftime("%m-%d %H:%M") if parsed else "时间未知"


def is_full(record: dict) -> bool:
    raw = first(record, QUOTA_KEYS)
    if not isinstance(raw, str) or "/" not in raw:
        return False
    left, _, right = raw.partition("/")
    try:
        return int(left) >= int(right) > 0
    except ValueError:
        return False


def quota_text(record: dict) -> str:
    raw = first(record, QUOTA_KEYS)
    return str(raw) if isinstance(raw, str) and raw else "名额未知"


def remaining_text(record: dict) -> str:
    """把「已报名/上限」换算成剩余名额。"""
    raw = first(record, QUOTA_KEYS[:2])
    if isinstance(raw, str) and "/" in raw:
        left, _, right = raw.partition("/")
        try:
            return str(max(0, int(right) - int(left)))
        except ValueError:
            pass
    limit = first(record, ("limitPeople", "peopleLimit"))
    try:
        if limit is not None and int(limit) <= 0:
            return "不限"
    except (TypeError, ValueError):
        pass
    return "名额未知"


def matches_blocklist(title: str, pattern: re.Pattern[str]) -> bool:
    return bool(pattern.search(title or ""))


def passes_keywords(title: str, include: str, exclude: str) -> bool:
    """按设置判断标题是否通过关键词过滤。

    黑名单优先：命中 exclude 直接失败；include 非空时还必须命中至少一个
    白名单表达式。规则本身支持正则，用户输入的字面词会被当作正则处理。
    """
    try:
        if exclude and re.search(exclude, title):
            return False
        if include and not re.search(include, title):
            return False
        return True
    except re.error:
        # 旧配置可能残留非法正则；单条规则出错不应让整轮扫描崩溃。
        log.warning("关键词无效，忽略过滤：include=%r exclude=%r", include, exclude)
        return True


def activity_id(record: dict) -> str:
    return str(first(record, ("infoId", "id", "activityId"), ""))


def activity_title(record: dict) -> str:
    return str(first(record, ("activityName", "name", "title"), "未命名活动"))


def activity_status(record: dict):
    value = record.get("status")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def fmt_register_end(end_ms: Any) -> str:
    try:
        end = dt.datetime.fromtimestamp(int(end_ms) / 1000)
    except (TypeError, ValueError, OSError):
        return "时间未知"
    return end.strftime("%m-%d %H:%M")


def is_past_register(record: dict) -> bool:
    end_ms = record.get("registerEndTime")
    if not end_ms:
        return False
    try:
        end = dt.datetime.fromtimestamp(int(end_ms) / 1000)
    except (TypeError, ValueError, OSError):
        return False
    return dt.datetime.now() > end


# ---------------------------------------------------------------- 展示
def render_summary(records: list[dict], cfg: Config) -> str:
    upcoming = [r for r in records if not is_past_register(r)]
    if not upcoming:
        return "无报名窗口内活动"

    not_registered = 0
    registered = 0
    for r in upcoming:
        status = activity_status(r)
        if status == STATUS_REGISTERED:
            registered += 1
        elif status == STATUS_NOT_REGISTERED:
            not_registered += 1

    return f"活动 {len(upcoming)}/{len(records)}，未报名 {not_registered}，已报名 {registered}"


def activity_view(records: list[dict], cfg: Config) -> list[dict]:
    """给 GUI 用的结构化活动列表。

    活动广场接口只返回报名时间，不返回活动起止时间；有活动时间就用活动时间，
    没有则退回报名开始时间，保证「时间」列不再是一排“时间未知”。
    """
    result = []
    now = dt.datetime.now()
    for record in records:
        title = activity_title(record)
        # 主页展示与通知/自动报名共用同一套关键词规则：
        # 黑名单或未命中非空白名单的活动，不进主页列表。
        if not passes_keywords(title, cfg.keyword_include, cfg.keyword_exclude):
            continue
        status = activity_status(record)
        registered = status in (STATUS_APPROVING, STATUS_REGISTERED)
        start_raw = first(record, TIME_KEYS_START)
        start_dt = parse_dt(start_raw) if start_raw else None
        reg_start_dt = parse_dt(first(record, REG_START_KEYS))
        # 列表接口不一定给活动开始时间；活动时间缺失时显示“待公布”，
        # 不能拿报名开始时间冒充，但判断是否过期时仍可用报名时间兜底。
        past_dt = start_dt or reg_start_dt
        activity_start = fmt_dt(start_dt) if start_dt else "待公布"
        result.append(
            {
                "id": activity_id(record),
                "title": title,
                "status": status,
                "quota": quota_text(record),
                "remaining": remaining_text(record),
                "full": is_full(record),
                "blocked": matches_blocklist(title, cfg.block_re),
                "registered": registered,
                "auditing": status == STATUS_APPROVING,
                "past": is_past_register(record),
                "past_activity": bool(past_dt and past_dt < now),
                "start": activity_start,
                "activity_start": activity_start,
                "register_start": fmt_dt(reg_start_dt) if reg_start_dt else "待公布",
                "end": fmt_dt(first(record, TIME_KEYS_END)),
                "register_end": fmt_dt(first(record, REG_END_KEYS)),
                "register_id": str(first(record, ("regisgerId", "registerId"), "") or ""),
                "can_cancel": bool(first(record, ("regisgerId", "registerId"), "")),
            }
        )
    return result


# ---------------------------------------------------------------- 主流程
def scan_once(cfg: Config, client, notifier, state, *, notify_auth_failure: bool = True) -> ScanResult:
    """执行一轮完整扫描：保活 → 拉取 → 基线 → 筛选 → 报名 → 推送。"""
    result = ScanResult()

    if not client.ensure_login():
        # 区分“从未登录”和“token 已失效”：
        # - 从未登录：不再输出“保活失败/无refresh token”这类多余日志，
        #   只告诉上层扫描失败是因为未登录，由界面决定是否弹登录窗。
        # - 已登录但 token 失效：保留原有日志和外部通知（邮件/IM）。
        if getattr(client.auth, "access_token", ""):
            log.error("登录保活失败：Token失效")
            result.error = "登录态失效"
            if notify_auth_failure:
                notifier.send(
                    "[NSU-EventGrabber] 登录态失效",
                    "access token 已失效，监控已暂停。\n"
                    "请打开客户端，点「登录」重新登录。",
                )
        else:
            result.error = "未登录"
        return result

    try:
        records = client.fetch_all_activities()
    except Exception as exc:  # noqa: BLE001 - 单轮失败不应中断守护进程
        log.error("活动列表拉取失败：%s", exc)
        result.error = "登录态失效" if getattr(exc, "code", None) == 401 else str(exc)
        return result

    result.records = records

    # ---- 首次运行：建立基线 ----
    if not state.initialized:
        if not records:
            return result
        registered_now = []
        for record in records:
            aid = activity_id(record)
            if aid:
                state.quota[aid] = quota_text(record)
                if activity_status(record) == STATUS_REGISTERED:
                    registered_now.append(aid)
        state.mark_seen([activity_id(r) for r in records if activity_id(r)])
        state.mark_ever_registered(registered_now)
        state.registered_ids = list(dict.fromkeys(state.registered_ids + registered_now))
        state.initialized = True
        state.save()

        result.baseline = True
        # 首次运行只建立基线：既不发系统/Webhook 通知，也不触发应用内通知。
        result.changed = False
        result.summary = render_summary(records, cfg)
        return result

    # ---- 常规轮次 ----
    now = dt.datetime.now()
    new_activities: list[dict[str, Any]] = []
    blocked_new_ids: set[str] = set()
    full_new_ids: set[str] = set()
    releases: list[str] = []
    reg_results: list[tuple[str, str]] = []
    upcoming: list[str] = []

    for record in records:
        aid = activity_id(record)
        if not aid:
            continue
        title = activity_title(record)
        status = activity_status(record)
        full = is_full(record)
        quota = quota_text(record)

        is_new = aid not in state.seen_ids

        prev_quota = state.quota.get(aid)
        quota_released = bool(
            prev_quota and is_full({"registerAndLimitPeople": prev_quota}) and not full
        )
        if quota_released:
            releases.append(f"{title}（{prev_quota} -> {quota}）")

        if status == STATUS_REGISTERED:
            start = parse_dt(first(record, TIME_KEYS_START))
            # 0 天表示关闭“即将参加”提醒，不能落到“start == now”这种偶然命中。
            if (
                cfg.upcoming_days > 0
                and start
                and now <= start <= now + dt.timedelta(days=cfg.upcoming_days)
            ):
                upcoming.append(f"{fmt_dt(start)} {title}")
            state.mark_ever_registered([aid])
            if aid not in state.registered_ids:
                state.registered_ids.append(aid)

        blocked = matches_blocklist(title, cfg.block_re)
        keyword_ok = passes_keywords(title, cfg.keyword_include, cfg.keyword_exclude)

        if is_new and keyword_ok:
            new_activities.append(record)
            if blocked:
                blocked_new_ids.add(aid)
            if full:
                full_new_ids.add(aid)

        reg_start = parse_dt(first(record, REG_START_KEYS))
        reg_end = parse_dt(first(record, REG_END_KEYS))
        reg_not_started = bool(reg_start and now < reg_start)
        reg_closed = bool(reg_end and now > reg_end)
        is_pending = aid in state.pending_registration_ids
        auto_eligible = (
            cfg.auto_register
            and keyword_ok
            and not blocked
            and status == STATUS_NOT_REGISTERED
            and aid not in state.ever_registered_ids
        )

        # 报名时间没到时先挂起，避免平台返回一次“报名失败”后就永久错过。
        # 对已经见过的活动也这样处理：只要报名尚未开放，就等开放后补报一次。
        # 报名已开放的存量活动不会追溯补报，仍只处理新发现/名额释放的活动。
        if not auto_eligible or reg_closed:
            state.clear_pending_registration([aid])
        elif reg_not_started:
            state.mark_pending_registration([aid])
        elif is_new or quota_released or is_pending:
            if full:
                # 开放后仍满额：不补报，等名额释放时再走 quota_released。
                state.clear_pending_registration([aid])
            else:
                ok, message = client.register(aid)
                state.clear_pending_registration([aid])
                if ok:
                    if aid not in state.registered_ids:
                        state.registered_ids.append(aid)
                    state.mark_ever_registered([aid])
                    reg_results.append((title, "已自动报名"))
                    log.info("自动报名成功：%s", title)
                else:
                    reg_results.append((title, f"报名失败：{message}"))
                    log.warning("自动报名失败：%s，%s", title, message)

        state.quota[aid] = quota
        if is_new:
            state.mark_seen([aid])

    alive_ids = {activity_id(r) for r in records}
    state.quota = {k: v for k, v in state.quota.items() if k in alive_ids}
    state.pending_registration_ids = [
        aid for aid in state.pending_registration_ids if aid in alive_ids
    ]

    today = now.strftime("%Y-%m-%d")
    need_upcoming_notice = bool(upcoming) and state.last_upcoming_date != today
    changed = bool(new_activities or releases or reg_results or need_upcoming_notice)

    result.new_activities = new_activities
    result.reg_results = reg_results
    result.releases = releases
    result.upcoming = upcoming
    result.blocked_ids = blocked_new_ids
    result.full_ids = full_new_ids
    result.changed = changed
    result.notify_error = any("报名失败" in message for _, message in reg_results)

    if not changed:
        state.save()
        return result

    result.summary = render_summary(records, cfg)
    result.title, result.content = build_message(
        new_activities,
        reg_results,
        releases,
        upcoming,
        blocked_new_ids,
        full_new_ids,
        cfg.upcoming_days,
    )
    notifier.send(result.title, result.content)
    if need_upcoming_notice:
        state.last_upcoming_date = today
    state.save()
    return result


def build_message(
    new_activities,
    reg_results,
    releases,
    upcoming,
    blocked_new_ids=None,
    full_new_ids=None,
    upcoming_days: int = 3,
):
    blocked_new_ids = blocked_new_ids or set()
    full_new_ids = full_new_ids or set()
    parts: list[str] = []
    if new_activities:
        parts.append(f"**发现 {len(new_activities)} 个新活动**")
        for idx, record in enumerate(new_activities, 1):
            title = activity_title(record)
            aid = activity_id(record)
            start = first(record, TIME_KEYS_START)
            end = first(record, TIME_KEYS_END)
            reg_end = first(record, REG_END_KEYS)
            lines = [f"{idx}. **{title}**"]
            if start or end:
                lines.append(f"   时间 {fmt_dt(start)} ~ {fmt_dt(end)}")
            if reg_end:
                lines.append(f"   报名截止 {fmt_dt(reg_end)}")
            lines.append(f"   报名 {quota_text(record)}")
            if aid in blocked_new_ids:
                lines.append("   [屏蔽词] 命中屏蔽词，未自动报名（如需参加请手动报名）")
            if aid in full_new_ids:
                lines.append("   [已满额] 已满额，暂未报名（释放名额将自动提醒）")
            parts.append("\n".join(lines))

    if reg_results:
        parts.append("**报名结果**")
        for title, message in reg_results:
            icon = "[OK]" if message == "已自动报名" else "[FAIL]"
            parts.append(f"- {icon} {title}：{message}")

    if releases:
        parts.append("**名额释放提醒**")
        parts.extend(f"- {item}" for item in releases)

    if upcoming:
        parts.append(f"**即将参加（{upcoming_days} 天内）**")
        parts.extend(f"- {item}" for item in upcoming)

    if not parts:
        parts.append("（本轮无内容）")

    if new_activities:
        headline = f"[NSU-EventGrabber] 发现 {len(new_activities)} 个新活动"
    elif releases:
        headline = "[NSU-EventGrabber] 名额释放了"
    elif reg_results:
        headline = "[NSU-EventGrabber] 报名结果"
    else:
        headline = "[NSU-EventGrabber] 即将参加提醒"
    return headline, "\n\n".join(parts)
