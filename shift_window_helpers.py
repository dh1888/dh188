"""上下班宽容窗口文案与边界计算"""
from datetime import datetime, timedelta
from typing import Dict, Tuple

from config import Config


def grace_from_config(shift_config: Dict) -> Tuple[int, int, int, int]:
    """返回 (上班前, 上班后, 下班前, 下班后) 宽容分钟数。"""
    return (
        shift_config.get("grace_before", Config.DEFAULT_GRACE_BEFORE),
        shift_config.get("grace_after", Config.DEFAULT_GRACE_AFTER),
        shift_config.get("workend_grace_before", Config.DEFAULT_WORKEND_GRACE_BEFORE),
        shift_config.get("workend_grace_after", Config.DEFAULT_WORKEND_GRACE_AFTER),
    )


def format_grace_window_hm(
    now: datetime,
    time_hm: str,
    grace_before: int,
    grace_after: int,
) -> Tuple[str, str]:
    """给定锚点时刻与宽容分钟，返回 (窗口开始, 窗口结束) HH:MM。"""
    hour, minute = map(int, time_hm.split(":")[:2])
    anchor = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    start = (anchor - timedelta(minutes=grace_before)).strftime("%H:%M")
    end = (anchor + timedelta(minutes=grace_after)).strftime("%H:%M")
    return start, end


def build_work_start_window_error(
    shift_config: Dict,
    now: datetime,
    current_time: str,
    action_text: str = "上班",
) -> str:
    """生成「不在上班窗口内」提示文案。"""
    day_start = shift_config.get("day_start", Config.DEFAULT_DUAL_DAY_START)
    day_end = shift_config.get("day_end", Config.DEFAULT_DUAL_DAY_END)
    grace_before, grace_after, _, _ = grace_from_config(shift_config)

    day_ws, day_we = format_grace_window_hm(now, day_start, grace_before, grace_after)
    night_ws, night_we = format_grace_window_hm(now, day_end, grace_before, grace_after)

    return (
        f"❌ 当前时间不在{action_text}打卡窗口内\n\n"
        f"📊 <b>允许的上班时间：</b>\n"
        f"• 白班上班：<code>{day_ws} ~ {day_we}</code>\n"
        f"• 夜班上班：<code>{night_ws} ~ {night_we}</code>（次日凌晨）\n\n"
        f"⏰ 当前时间：<code>{current_time}</code>\n"
        f"💡 请等待对班时间窗口或联系管理员调整时间设置"
    )


def build_work_end_window_error(
    shift_config: Dict,
    now: datetime,
    current_time: str,
    action_text: str = "下班",
) -> str:
    """生成「不在下班窗口内」提示文案。"""
    day_start = shift_config.get("day_start", Config.DEFAULT_DUAL_DAY_START)
    day_end = shift_config.get("day_end", Config.DEFAULT_DUAL_DAY_END)
    _, _, we_before, we_after = grace_from_config(shift_config)

    day_ws, day_we = format_grace_window_hm(now, day_end, we_before, we_after)

    day_start_h, day_start_m = map(int, day_start.split(":")[:2])
    next_day_anchor = now.replace(
        hour=day_start_h, minute=day_start_m, second=0, microsecond=0
    ) + timedelta(days=1)
    night_ws = (
        next_day_anchor - timedelta(minutes=we_before)
    ).strftime("%H:%M")
    night_we = (
        next_day_anchor + timedelta(minutes=we_after)
    ).strftime("%H:%M")

    return (
        f"❌ 当前时间不在{action_text}打卡窗口内\n\n"
        f"📊 <b>允许的下班时间：</b>\n"
        f"• 白班下班：<code>{day_ws} ~ {day_we}</code>\n"
        f"• 夜班下班：<code>{night_ws} ~ {night_we}</code>（次日早上）\n\n"
        f"⏰ 当前时间：<code>{current_time}</code>\n"
        f"💡 请等待对班时间窗口或联系管理员调整时间设置"
    )
