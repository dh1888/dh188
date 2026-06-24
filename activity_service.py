import asyncio
import logging
import os
import time
import traceback
from datetime import datetime, timedelta, date
from datetime import time as dt_time
from typing import Dict, Optional, List
from contextlib import suppress

from aiogram import types
from aiogram.filters import Command
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from functools import wraps

from config import Config, beijing_tz
from database import db, parse_sql_row_count, normalize_db_timestamp
from constants import (
    BTN_WORK_START_DAY, BTN_WORK_START_NIGHT, BTN_WORK_END, WORK_BUTTONS,
    SPECIAL_BUTTONS, ACTIVITY_MAP, AdminStates, start_time,
)
from constants import active_back_processing
from bot_manager import bot_manager
from keyboards import get_main_keyboard, get_admin_keyboard, is_admin, calculate_work_fine, build_inline_back_keyboard
from i18n import get_lang_mode
from message_chain import (
    SCOPE_ACTIVITY,
    SCOPE_WORK,
    answer_user_message,
    complete_message_context,
    get_root_message_id,
    record_bot_outgoing,
    resolve_context_reply_target,
)
from performance import (
    global_cache, track_performance, with_retry, message_deduplicate,
    rate_limit, user_rate_limit,
)
from utils import (
    MessageFormatter, timer_manager, notification_service,
    calculate_fine, get_beijing_time,
)
from fault_tolerance import Watchdog
from handover_manager import handover_manager
from reset_service import reset_daily_data_if_needed
from shift_window_helpers import grace_from_config, format_grace_window_hm

logger = logging.getLogger("GroupCheckInBot")

async def auto_end_current_activity(
    chat_id: int, uid: int, user_data: dict, now: datetime, message: types.Message
):
    """自动结束当前活动 - 增强班次检查"""
    try:
        act = user_data["current_activity"]
        start_time_dt = datetime.fromisoformat(user_data["activity_start_time"])
        activity_shift = user_data.get("shift", "day")
        activity_record_date = user_data.get("activity_record_date")

        # ===== 获取当前操作的班次 =====
        # 从消息中获取当前操作的班次（需要在 process_work_checkin 中设置）
        current_operation_shift = getattr(message, "_current_shift", None)

        # 如果无法从消息获取，尝试从班次状态表获取用户当前活跃的班次
        if not current_operation_shift:
            active_shift = await db.get_user_activity_shift(chat_id, uid)
            if active_shift:
                current_operation_shift = active_shift.get("shift")

        logger.info(
            f"🔍 自动结束活动检查: "
            f"活动班次={activity_shift}, "
            f"当前操作班次={current_operation_shift}"
        )

        # ===== 关键检查：只有相同班次才能结束 =====
        if current_operation_shift and current_operation_shift != activity_shift:
            logger.info(
                f"⏭️ 跳过结束不同班次活动: "
                f"活动班次={activity_shift}, "
                f"操作班次={current_operation_shift}"
            )
            return
        # ===== 检查结束 =====

        elapsed = int((now - start_time_dt).total_seconds())

        if activity_record_date is None:
            activity_record_date = await db.resolve_shift_record_date_at_time(
                chat_id, uid, activity_shift, start_time_dt
            )
        if activity_record_date is None:
            activity_record_date = start_time_dt.date()
        if isinstance(activity_record_date, str):
            activity_record_date = date.fromisoformat(activity_record_date[:10])

        shift_info = await db.determine_shift_for_time(
            chat_id=chat_id,
            current_time=start_time_dt,
            checkin_type="activity",
            active_shift=activity_shift,
            active_record_date=activity_record_date,
        )

        forced_date = activity_record_date
        if shift_info and shift_info.get("record_date"):
            forced_date = shift_info["record_date"]

        # 完成活动
        await db.complete_user_activity(
            chat_id=chat_id,
            user_id=uid,
            activity=act,
            elapsed_time=elapsed,
            fine_amount=0,
            is_overtime=False,
            shift=activity_shift,
            forced_date=forced_date,
        )

        # 清理定时器 - 使用活动的原始班次
        await timer_manager.cancel_timer(
            chat_id=chat_id,
            uid=uid,
            shift=activity_shift,  # ✅ 修复：使用 activity_shift 而不是未定义的 shift
            preserve_message=False,
        )

        logger.info(
            f"✅ 自动结束活动: {chat_id}-{uid} - {act} "
            f"(班次: {activity_shift}, 日期: {forced_date})"
        )

    except Exception as e:
        logger.error(f"❌ 自动结束活动失败 {chat_id}-{uid}: {e}")
        logger.exception(e)

async def handle_expired_activity(
    chat_id: int, user_id: int, activity: str, start_time: datetime
):
    """智能恢复活动"""
    try:
        now = db.get_beijing_time()
        elapsed = int((now - start_time).total_seconds())
        nickname = "用户"

        user_data = await db.get_user_cached(chat_id, user_id)
        if user_data:
            nickname = user_data.get("nickname", str(user_id))

        forced_date = start_time.date()

        shift = user_data.get("shift", None)
        if not shift:
            shift_info = await db.determine_shift_for_time(
                chat_id=chat_id,
                current_time=start_time,
                checkin_type="work_start",
            )
            if shift_info:
                shift = shift_info.get("shift", "day")
        shift = shift or "day"

        logger.info(
            f"🔄 恢复过期活动 - 活动开始时间: {start_time.strftime('%m/%d %H:%M:%S')}, "
            f"归档日期: {forced_date}, 班次: {shift}"
        )

        time_limit = await db.get_activity_time_limit(activity)
        time_limit_seconds = time_limit * 60
        is_overtime = elapsed > time_limit_seconds
        overtime_seconds = max(0, elapsed - time_limit_seconds)
        overtime_minutes = overtime_seconds / 60

        fine_amount = 0
        if is_overtime and overtime_seconds > 0:
            fine_amount = await calculate_fine(activity, overtime_minutes)

        await db.complete_user_activity(
            chat_id=chat_id,
            user_id=user_id,
            activity=activity,
            elapsed_time=elapsed,
            fine_amount=fine_amount,
            is_overtime=is_overtime,
            shift=shift,
            forced_date=forced_date,
        )

        date_desc = f"（归到{forced_date}）"
        timeout_msg = (
            f"🔄 <b>系统恢复通知</b>{date_desc}\n"
            f"👤 用户：{MessageFormatter.format_user_link(user_id, nickname)}\n"
            f"📝 检测到未结束的活动：<code>{activity}</code>\n"
            f"⏰ 活动开始时间：<code>{start_time.strftime('%m/%d %H:%M:%S')}</code>\n"
            f"⏱️ 活动总时长：<code>{MessageFormatter.format_time(int(elapsed))}</code>\n"
            f"⚠️ 由于服务重启，您的活动已自动结束"
        )

        if fine_amount > 0:
            timeout_msg += f"\n💰 超时罚款金额：<code>{fine_amount}</code> 泰铢"

        await bot_manager.bot.send_message(chat_id, timeout_msg, parse_mode="HTML")

        logger.info(
            f"已处理过期活动: {chat_id}-{user_id} - {activity} "
            f"(开始时间: {start_time.strftime('%m/%d %H:%M:%S')}, "
            f"归到: {forced_date}, 班次: {shift})"
        )

    except Exception as e:
        logger.error(f"处理过期活动失败 {chat_id}-{user_id}: {e}")


async def recover_expired_activities():
    """恢复服务重启前的过期活动"""
    try:
        logger.info("🔄 检查并恢复过期活动...")
        all_groups = await db.get_all_groups()
        recovered_count = 0

        for chat_id in all_groups:
            try:
                group_members = await db.get_group_members(chat_id)
                for user_data in group_members:
                    if user_data.get("current_activity") and user_data.get(
                        "activity_start_time"
                    ):
                        activity = user_data["current_activity"]
                        start_time = datetime.fromisoformat(
                            user_data["activity_start_time"]
                        )
                        user_id = user_data["user_id"]

                        await handle_expired_activity(
                            chat_id, user_id, activity, start_time
                        )
                        recovered_count += 1

            except Exception as e:
                logger.error(f"恢复群组 {chat_id} 活动失败: {e}")

        if recovered_count > 0:
            logger.info(f"✅ 已恢复 {recovered_count} 个过期活动")
        else:
            logger.info("✅ 没有需要恢复的过期活动")

        return recovered_count

    except Exception as e:
        logger.error(f"恢复过期活动失败: {e}")
        return 0


async def _check_work_end_blocks_activity(
    chat_id: int, uid: int, shift: str, record_date: date
) -> tuple[bool, str]:
    """仅检查是否已下班（活动路径其余校验由调用方完成）"""
    if not await db.has_work_hours_enabled(chat_id):
        return True, ""

    db._ensure_pool_initialized()
    async with db.pool.acquire() as conn:
        has_work_end = await conn.fetchval(
            """
            SELECT 1 FROM work_records we
            WHERE we.chat_id = $1
              AND we.user_id = $2
              AND we.checkin_type = 'work_end'
              AND we.shift = $3
              AND we.record_date = $4
              AND EXISTS (
                  SELECT 1 FROM work_records ws
                  WHERE ws.chat_id = we.chat_id
                    AND ws.user_id = we.user_id
                    AND ws.shift = we.shift
                    AND ws.record_date = we.record_date
                    AND ws.checkin_type = 'work_start'
                    AND ws.created_at < we.created_at
              )
            LIMIT 1
            """,
            chat_id,
            uid,
            shift,
            record_date,
        )

    if has_work_end:
        shift_text = "白班" if shift == "day" else "夜班"
        return False, f"❌ 您本{shift_text}已下班，无法进行活动！"
    return True, ""


def _work_hours_enabled_from_snapshot(snapshot: dict) -> bool:
    if snapshot.get("dual_mode") and snapshot.get("dual_day_start"):
        return True
    ws = snapshot.get("work_start_time") or Config.DEFAULT_WORK_HOURS["work_start"]
    we = snapshot.get("work_end_time") or Config.DEFAULT_WORK_HOURS["work_end"]
    return (
        ws != Config.DEFAULT_WORK_HOURS["work_start"]
        or we != Config.DEFAULT_WORK_HOURS["work_end"]
    )


def _resolve_shift_from_active_state(
    shift_state: dict, now: datetime, shift_config: dict
) -> tuple[str, date, str]:
    """根据班次状态本地解析班次（避免 determine_shift_for_time 额外往返）"""
    shift = shift_state["shift"]
    record_date = shift_state["record_date"]
    if isinstance(record_date, str):
        record_date = date.fromisoformat(record_date[:10])

    if shift == "day":
        return shift, record_date, "day"

    day_end_str = shift_config.get("day_end", Config.DEFAULT_DUAL_DAY_END)
    day_end_time = datetime.strptime(day_end_str, "%H:%M").time()
    night_start = datetime.combine(record_date, day_end_time).replace(tzinfo=now.tzinfo)
    night_end = night_start + timedelta(days=1)
    if night_start <= now < night_end:
        shift_detail = "night_tonight"
    else:
        shift_detail = "night_last"
    return shift, record_date, shift_detail


def _shift_config_from_snapshot(snapshot: dict) -> dict:
    ws = snapshot.get("work_start_time") or Config.DEFAULT_DUAL_DAY_START
    we = snapshot.get("work_end_time") or Config.DEFAULT_WORK_HOURS["work_end"]
    if snapshot.get("dual_mode"):
        ws = snapshot.get("dual_day_start") or ws
        we = snapshot.get("dual_day_end") or Config.DEFAULT_DUAL_DAY_END
    return {
        "dual_mode": bool(snapshot.get("dual_mode", True)),
        "day_start": ws,
        "day_end": we,
    }


def _notification_user_data(
    snapshot: dict, nickname: str, activity_start_time
) -> dict:
    """构建推送通知用的用户数据字典。"""
    return {
        "nickname": nickname,
        "activity_start_time": activity_start_time,
        "current_activity": snapshot.get("current_activity"),
        "shift": snapshot.get("user_shift") or snapshot.get("active_shift"),
    }


def _is_shift_expired(shift_start_time, now: datetime) -> bool:
    """已废弃：请用 db.is_shift_open_too_long（需 chat_id/shift）。保留供同步路径兜底。"""
    parsed = normalize_db_timestamp(shift_start_time, now)
    if not parsed:
        return False
    return (now - parsed).total_seconds() > Config.SHIFT_STATE_MAX_HOURS * 3600


async def _is_shift_expired_for_user(
    chat_id: int,
    shift: str,
    shift_start_time,
    now: datetime,
) -> tuple[bool, float, float]:
    return await db.is_shift_open_too_long(
        chat_id, shift, shift_start_time, now
    )


async def _shift_expired_user_message(
    chat_id: int,
    shift: str,
    now: datetime,
    closed_count: int = 0,
    elapsed_h: float = 0.0,
    max_h: float = 0.0,
) -> str:
    """班次超时提示：说明原因并引导重新上班。"""
    from handover_manager import handover_manager
    from i18n import work_button_label

    shift_config = await db.get_shift_config(chat_id)
    day_start = shift_config.get("day_start", Config.DEFAULT_DUAL_DAY_START)
    day_end = shift_config.get("day_end", Config.DEFAULT_DUAL_DAY_END)
    grace_before, grace_after, _, _ = grace_from_config(shift_config)
    _, night_we = format_grace_window_hm(now, day_end, grace_before, grace_after)
    shift_text = "白班" if shift == "day" else "夜班"

    period = await handover_manager.determine_current_period(chat_id, now)
    mode = (
        f"换班日（{Config.HANDOVER_NIGHT_HOURS if shift == 'night' else Config.HANDOVER_DAY_HOURS}h）"
        if period.get("is_handover")
        else f"普通班（{Config.NORMAL_NIGHT_HOURS if shift == 'night' else Config.NORMAL_DAY_HOURS}h）"
    )

    lines = [
        f"❌ 您的{shift_text}班次开放过久（{mode}，上限约 <code>{max_h:.0f}</code> 小时）",
    ]
    if elapsed_h > 0:
        lines.append(f"⏱️ 自上班起已 <code>{elapsed_h:.1f}</code> 小时")
    if closed_count:
        lines.append(
            f"✅ 已自动结束 <code>{closed_count}</code> 条超时未下班记录，请重新上班打卡"
        )
    else:
        lines.append("💡 请先打下班卡或重新打上班卡后再进行活动")

    relay = await handover_manager.get_handover_day_relay_handover_date(chat_id, now)
    btn_day = work_button_label("work_start_day")
    btn_night = work_button_label("work_start_night")
    if relay and now.hour < int(day_start.split(":")[0]):
        lines.append(
            f"\n🔄 <b>换班接班</b>：清晨请使用 <b>{btn_day}</b>（约 <code>07:00</code> 起），"
            f"不要用 <b>{btn_night}</b>"
        )
    else:
        lines.append(
            f"\n⏰ 当前 <code>{now.strftime('%H:%M')}</code> · "
            f"夜班上班窗口约至次日凌晨 <code>{night_we}</code>"
        )
        lines.append(f"💡 请确认点击正确的上班按钮（{btn_day} / {btn_night}）")

    return "\n".join(lines)


def _needs_daily_reset(last_updated_raw, business_date: date) -> bool:
    if not last_updated_raw:
        return True
    if isinstance(last_updated_raw, datetime):
        last_date = last_updated_raw.date()
    elif isinstance(last_updated_raw, date):
        last_date = last_updated_raw
    elif isinstance(last_updated_raw, str):
        try:
            last_date = datetime.fromisoformat(
                last_updated_raw.replace("Z", "+00:00")
            ).date()
        except Exception:
            return True
    else:
        return True
    return last_date < business_date


async def _effective_activity_count(
    chat_id: int,
    uid: int,
    raw_count: int,
    record_date: date,
    now: datetime,
    period: Optional[dict] = None,
) -> int:
    """应用换班周期重置后的活动次数"""
    from handover_manager import handover_manager

    if period is None:
        period = await handover_manager.determine_current_period(chat_id, now)

    if not period.get("is_handover"):
        return raw_count

    if period.get("cycle", 1) >= 2:
        return 0

    cache_key = f"eff_cnt:{chat_id}:{uid}:{record_date}:{raw_count}:{period.get('cycle')}"
    cached = await global_cache.get(cache_key)
    if cached is not None:
        return cached

    effective_cycle = await handover_manager.get_user_effective_cycle(
        chat_id, uid, period
    )
    result = 0 if effective_cycle >= 2 else raw_count
    await global_cache.set(cache_key, result, Config.EFF_ACTIVITY_COUNT_CACHE_TTL)
    return result


async def check_activity_limit_by_shift(
    chat_id: int,
    user_id: int,
    activity: str,
    shift: str | None = None,
    query_date: Optional[date] = None,
    skip_init: bool = False,
    period: Optional[dict] = None,
) -> tuple[bool, int, int]:
    """检查活动次数是否达到上限（支持换班重置）"""
    if not skip_init:
        await db.init_group(chat_id)
        await db.init_user(chat_id, user_id)

    now = db.get_beijing_time()

    from handover_manager import handover_manager

    # 使用换班管理器获取当前周期的计数
    current_count = await handover_manager.get_activity_count(
        chat_id,
        user_id,
        activity,
        shift,
        query_date=query_date,
        current_time=now,
        period=period,
    )

    max_times = await db.get_activity_max_times(activity)

    logger.debug(
        f"📊 [次数检查] 用户{user_id} {activity} 当前{current_count}/{max_times}"
    )
    return current_count < max_times, current_count, max_times


async def has_active_activity(chat_id: int, uid: int) -> tuple[bool, Optional[str]]:
    """检查用户是否有活动正在进行"""
    await db.init_group(chat_id)
    await db.init_user(chat_id, uid)
    user_data = await db.get_user_cached(chat_id, uid)
    return user_data["current_activity"] is not None, user_data["current_activity"]


async def can_perform_activities(
    chat_id: int,
    uid: int,
    current_shift: str = None,
    record_date: Optional[date] = None,
) -> tuple[bool, str]:
    """检查用户是否可以执行活动"""

    logger.info(f"🔍 [活动检查] 用户={uid}, 班次={current_shift}")

    if not await db.has_work_hours_enabled(chat_id):
        return True, ""

    now = db.get_beijing_time()

    user_current_shift = await db.get_user_activity_shift(chat_id, uid)

    check_shift = current_shift

    if not check_shift:
        if user_current_shift:
            check_shift = user_current_shift["shift"]
            logger.info(f"📌 使用用户当前活跃班次: {check_shift}")
        else:
            user_data = await db.get_user_cached(chat_id, uid)
            if user_data and user_data.get("shift"):
                check_shift = user_data["shift"]
                logger.info(f"📌 使用用户数据班次: {check_shift}")
            else:
                check_shift = "day"
                logger.info(f"📌 使用默认班次: {check_shift}")

    open_shift = await db.get_user_current_shift(chat_id, uid)
    if not open_shift:
        check_shift = current_shift or "day"
        shift_text = "白班" if check_shift == "day" else "夜班"
        return (
            False,
            f"❌ 您当前没有进行中的{shift_text}班次，请先打{shift_text}上班卡！",
        )

    check_shift = open_shift["shift"]

    expired, elapsed_h, max_h = await _is_shift_expired_for_user(
        chat_id, check_shift, open_shift["shift_start_time"], now
    )
    if expired:
        closed = await db.auto_close_expired_open_work_shifts(chat_id, uid)
        shift_text = "白班" if check_shift == "day" else "夜班"
        msg = await _shift_expired_user_message(
            chat_id, check_shift, now, closed, elapsed_h, max_h
        )
        return False, msg

    shift_info = await db.determine_shift_for_time(
        chat_id=chat_id,
        current_time=now,
        checkin_type="activity",
        active_shift=check_shift,
    )

    if shift_info is None or shift_info.get("shift_detail") is None:
        shift_config = await db.get_shift_config(chat_id)
        day_start = shift_config.get("day_start", Config.DEFAULT_DUAL_DAY_START)
        day_end = shift_config.get("day_end", Config.DEFAULT_DUAL_DAY_END)

        shift_text = "白班" if check_shift == "day" else "夜班"
        window_text = (
            f"<code>{day_start} ~ {day_end}</code>"
            if check_shift == "day"
            else f"<code>{day_end} ~ 次日 {day_start}</code>"
        )

        return False, (
            f"❌ 当前时间不在{shift_text}活动允许的时间窗口内\n"
            f"📊 {shift_text}活动时间：{window_text}\n\n"
            f"⏰ 当前时间：<code>{now.strftime('%H:%M')}</code>\n"
            f"💡 请等待{shift_text}的正常活动时间"
        )

    async with db.pool.acquire() as conn:
        has_work_end = await conn.fetchval(
            """
            SELECT 1 FROM work_records we
            WHERE we.chat_id = $1 
              AND we.user_id = $2 
              AND we.checkin_type = 'work_end'
              AND we.shift = $3
              AND we.record_date = $4
              AND EXISTS (
                  SELECT 1 FROM work_records ws
                  WHERE ws.chat_id = we.chat_id
                    AND ws.user_id = we.user_id
                    AND ws.shift = we.shift
                    AND ws.record_date = we.record_date
                    AND ws.checkin_type = 'work_start'
                    AND ws.created_at < we.created_at
              )
            LIMIT 1
            """,
            chat_id,
            uid,
            check_shift,
            shift_state["record_date"],
        )

        if has_work_end:
            shift_text = "白班" if check_shift == "day" else "夜班"
            return False, f"❌ 您本{shift_text}已下班，无法进行活动！"

    shift_text = "白班" if check_shift == "day" else "夜班"
    logger.info(f"✅ [活动检查] 用户={uid} 允许执行活动（班次：{shift_text}）")
    return True, ""


async def activity_timer(
    chat_id: int,
    uid: int,
    act: str,
    limit: int,
    shift: str = "day",
    preserve_message: bool = False,
):
    try:
        max_wait = 30
        wait_interval = 1
        waited = 0

        while not bot_manager or not bot_manager.bot and waited < max_wait:
            if waited == 0:
                logger.info(f"⏳ 等待 bot 初始化... (chat={chat_id}, uid={uid})")
            await asyncio.sleep(wait_interval)
            waited += wait_interval

        if not bot_manager or not bot_manager.bot:
            logger.error(f"❌ bot 未能在 {max_wait} 秒内初始化，定时器终止")
            return

        if waited > 0:
            logger.info(f"✅ bot 已就绪，继续执行定时器 (等待 {waited}s)")

        shift_text = "白班" if shift == "day" else "夜班"
        logger.info(f"⏰ 定时器启动: {chat_id}-{uid} - {act}（{shift_text}）")

        one_minute_warning_sent = False
        timeout_immediate_sent = False
        timeout_5min_sent = False
        last_reminder_minute = 0
        force_back_sent = False

        _message_sent_cache = {}
        _cache_lock = asyncio.Lock()

        async def send_group_message(text: str, kb=None):
            msg_key = f"{chat_id}:{uid}:{text}"
            now = time.time()

            async with _cache_lock:
                expired_keys = [
                    k for k, t in _message_sent_cache.items() if now - t > 5
                ]
                for k in expired_keys:
                    _message_sent_cache.pop(k, None)

                if msg_key in _message_sent_cache:
                    logger.debug(f"⏱️ 相同消息5秒内已发送，跳过: {text[:30]}...")
                    return None

                _message_sent_cache[msg_key] = now

            current_bot = bot_manager.bot
            if not current_bot:
                logger.error("❌ bot_manager.bot 为 None，无法发送消息")
                return None

            reply_target = await resolve_context_reply_target(
                chat_id, uid, scope_id=SCOPE_ACTIVITY
            )
            if reply_target:
                try:
                    sent = await current_bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        parse_mode="HTML",
                        reply_markup=kb,
                        reply_to_message_id=reply_target,
                    )
                    await record_bot_outgoing(
                        chat_id,
                        sent.message_id,
                        checkin_message_id,
                        user_id=uid,
                        inherit_session_root=True,
                    )
                    return sent
                except Exception as e:
                    logger.warning(
                        f"⚠️ 引用 own={reply_target} 发送失败，重试一次: {e}"
                    )
                    await asyncio.sleep(1)
                    try:
                        sent = await current_bot.send_message(
                            chat_id=chat_id,
                            text=text,
                            parse_mode="HTML",
                            reply_markup=kb,
                            reply_to_message_id=reply_target,
                        )
                        await record_bot_outgoing(
                            chat_id,
                            sent.message_id,
                            checkin_message_id,
                            user_id=uid,
                            inherit_session_root=True,
                        )
                        return sent
                    except Exception as e2:
                        logger.warning(f"⚠️ 引用发送重试失败，降级普通发送: {e2}")

            try:
                return await current_bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=kb,
                )
            except Exception as e:
                logger.error(f"❌ 普通发送也失败: {e}")
                return None

        def build_quick_back_kb():
            return InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="👉 点击✅立即回座 👈",
                            callback_data=f"quick_back:{chat_id}:{uid}:{shift}",
                        )
                    ]
                ]
            )

        async def push_force_back_notification(nickname, elapsed, fine_amount):
            try:
                current_bot = bot_manager.bot
                if not current_bot:
                    logger.error(f"❌ bot_manager.bot 为 None，无法获取聊天信息")
                    return False

                chat_title = str(chat_id)
                try:
                    info = await current_bot.get_chat(chat_id)
                    chat_title = info.title or chat_title
                except Exception as e:
                    logger.debug(f"获取聊天信息失败: {e}")

                notification_text = (
                    f"🚨 <b>超时强制回座通知</b>\n"
                    f"🏢 群组：<code>{chat_title}</code>\n"
                    f"{MessageFormatter.create_dashed_line()}\n"
                    f"👤 用户：{MessageFormatter.format_user_link(uid, nickname)}\n"
                    f"📝 活动：<code>{act}</code>\n"
                    f"📊 班次：<code>{shift_text}</code>\n"
                    f"⏰ 自动回座时间：<code>{db.get_beijing_time().strftime('%m/%d %H:%M:%S')}</code>\n"
                    f"⏱️ 总活动时长：<code>{MessageFormatter.format_time(elapsed)}</code>\n"
                    f"⚠️ 系统自动回座原因：超时超过2小时\n"
                    f"💰 本次罚款金额：<code>{fine_amount}</code> 泰铢"
                )

                if not notification_service.bot and bot_manager.bot:
                    notification_service.bot = bot_manager.bot
                if not notification_service.bot_manager and bot_manager:
                    notification_service.bot_manager = bot_manager

                await notification_service.send_notification(
                    chat_id,
                    notification_text,
                    notification_type="channel",
                )
                logger.info(
                    f"✅ 强制回座通知推送成功: chat={chat_id}, uid={uid}（班次: {shift}）"
                )
                return True
            except Exception as e:
                logger.error(f"❌ 强制回座通知推送失败: {e}")
                return False

        while True:
            pending_msg = None
            pending_kb = None
            timer_should_stop = False

            break_data = {"should_break": False}

            user_data = await db.get_user_cached(chat_id, uid)
            if not user_data or user_data["current_activity"] != act:
                timer_should_stop = True
            else:
                start_time = datetime.fromisoformat(
                    user_data["activity_start_time"]
                )
                now = db.get_beijing_time()
                elapsed = int((now - start_time).total_seconds())

                try:
                    limit_int = int(limit)
                except (ValueError, TypeError):
                    logger.error(f"时间限制格式错误: {limit}，使用默认值30分钟")
                    limit_int = 30

                remaining = limit_int * 60 - elapsed
                nickname = user_data.get("nickname", str(uid))
                time_limit_seconds = limit_int * 60
                overtime_seconds = max(0, elapsed - time_limit_seconds)

                if overtime_seconds >= Config.FORCE_BACK_OVERTIME_MINUTES * 60 and not force_back_sent:
                    force_back_sent = True
                    fine_amount = await calculate_fine(
                        act, Config.FORCE_BACK_OVERTIME_MINUTES
                    )
                    logger.info(
                        f"⏰ [强制回座] 用户{uid} 活动{act} "
                        f"超时 {MessageFormatter.format_time(overtime_seconds)} "
                        f"(总时长: {MessageFormatter.format_time(elapsed)}, "
                        f"限制: {limit_int}分钟)"
                    )

                    await db.complete_user_activity(
                        chat_id=chat_id,
                        user_id=uid,
                        activity=act,
                        elapsed_time=elapsed,
                        fine_amount=fine_amount,
                        is_overtime=True,
                        shift=shift,
                    )

                    break_data = {
                        "should_break": True,
                        "fine_amount": fine_amount,
                        "elapsed": elapsed,
                        "nickname": nickname,
                    }
                else:
                    if 0 < remaining <= 60 and not one_minute_warning_sent:
                        pending_msg = (
                            f"⏳ <b>即将超时警告</b>\n"
                            f"👤 {MessageFormatter.format_user_link(uid, nickname)} \n"
                            f"📊 班次： <code>{shift_text}</code> \n"
                            f"🕓 本次 {MessageFormatter.format_copyable_text(act)} 还有 <code>1</code> 分钟！\n"
                            f"💡 请及时回座，避免超时罚款"
                        )
                        pending_kb = build_quick_back_kb()
                        one_minute_warning_sent = True

                    elif remaining <= 0:
                        overtime_minutes = int(-remaining // 60)

                        if overtime_minutes == 0 and not timeout_immediate_sent:
                            timeout_immediate_sent = True
                            pending_msg = (
                                f"⚠️ <b>超时警告</b>\n"
                                f"👤 {MessageFormatter.format_user_link(uid, nickname)} \n"
                                f"📊 班次： <code>{shift_text}</code> \n"
                                f"🕓 本次 {MessageFormatter.format_copyable_text(act)} 已超时\n"
                                f"🏃‍♂️ 请立即回座，避免产生更多罚款！"
                            )
                            pending_kb = build_quick_back_kb()
                            last_reminder_minute = 0

                        elif overtime_minutes == 5 and not timeout_5min_sent:
                            timeout_5min_sent = True
                            pending_msg = (
                                f"🔔 <b>超时警告</b> \n"
                                f"👤 {MessageFormatter.format_user_link(uid, nickname)} \n"
                                f"📊 班次： <code>{shift_text}</code> \n"
                                f"🕓 本次 {MessageFormatter.format_copyable_text(act)} 已超时 <code>{overtime_minutes}</code> 分钟！\n"
                                f"😤 罚款正在累积，请立即回座！"
                            )
                            pending_kb = build_quick_back_kb()
                            last_reminder_minute = 5

                        elif (
                            overtime_minutes >= 10
                            and overtime_minutes % 10 == 0
                            and overtime_minutes != last_reminder_minute
                        ):
                            last_reminder_minute = overtime_minutes
                            pending_msg = (
                                f"🚨 <b>超时警告</b>\n"
                                f"👤 {MessageFormatter.format_user_link(uid, nickname)} \n"
                                f"📊 班次： <code>{shift_text}</code> \n"
                                f"🕓 本次 {MessageFormatter.format_copyable_text(act)} 已超时 <code>{overtime_minutes}</code> 分钟！\n"
                                f"💢 请立刻回座，避免产生更多罚款！"
                            )
                            pending_kb = build_quick_back_kb()

            if timer_should_stop:
                break

            if pending_msg:
                await send_group_message(pending_msg, pending_kb)

            if break_data.get("should_break", False):
                msg = (
                    f"🛑 <b>自动安全回座</b>\n"
                    f"👤 用户：{MessageFormatter.format_user_link(uid, break_data['nickname'])}\n"
                    f"📝 活动：<code>{act}</code>\n"
                    f"📊 班次：<code>{shift_text}</code>\n"
                    f"⚠️ 超时超过2小时，系统已自动回座\n"
                    f"💰 本次罚款金额：<code>{break_data['fine_amount']}</code> 泰铢"
                )
                await send_group_message(msg)

                for attempt in range(3):
                    if await push_force_back_notification(
                        break_data["nickname"],
                        break_data["elapsed"],
                        break_data["fine_amount"],
                    ):
                        break
                    logger.warning(f"⚠️ 强制回座通知发送失败，重试 {attempt + 1}/3")
                    await asyncio.sleep(2)

                await db.clear_user_checkin_message(chat_id, uid)
                await timer_manager.cancel_timer(
                    chat_id=chat_id,
                    uid=uid,
                    shift=shift,
                    preserve_message=False,
                )
                break

            await asyncio.sleep(30)

    except asyncio.CancelledError:
        logger.info(f"定时器 {chat_id}-{uid} 被取消（班次: {shift}）")
        if preserve_message:
            logger.debug(f"⏭️ 被取消的定时器保留消息ID")
            return
        raise

    except Exception as e:
        logger.error(f"定时器错误（班次: {shift}）: {e}")

    finally:
        try:
            if preserve_message:
                logger.debug(f"⏭️ 定时器跳过清理消息ID (preserve_message=True)")
                return

            current_user_data = await db.get_user_cached(chat_id, uid)

            if not current_user_data or not current_user_data.get("current_activity"):
                current_message_id = await db.get_user_checkin_message_id(chat_id, uid)
                if current_message_id:
                    await db.clear_user_checkin_message(chat_id, uid)
                    logger.debug(
                        f"🧹 定时器清理消息ID: {current_message_id} (用户无活动)"
                    )

            elif current_user_data.get("current_activity") != act:
                logger.debug(
                    f"⏭️ 定时器跳过清理: 用户已有新活动 {current_user_data['current_activity']}"
                )

            else:
                logger.warning(f"⚠️ 定时器异常退出但活动仍存在: {act}")
                await db.clear_user_checkin_message(chat_id, uid)

        except Exception as e:
            logger.error(f"❌ 定时器清理异常: {e}")


def _parse_activity_start_time(value, now: datetime) -> datetime:
    if not value:
        return now
    clean_str = str(value).strip()
    if clean_str.endswith("Z"):
        clean_str = clean_str.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(clean_str)
        if dt.tzinfo is None:
            dt = beijing_tz.localize(dt)
        return dt
    except ValueError:
        pass
    for fmt in (
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%m/%d %H:%M:%S",
        "%m/%d %H:%M",
    ):
        try:
            dt = datetime.strptime(clean_str, fmt)
            if fmt.startswith("%m/%d"):
                dt = dt.replace(year=now.year)
            if dt.tzinfo is None:
                dt = beijing_tz.localize(dt)
            return dt
        except ValueError:
            continue
    return now


def _resolve_back_forced_date(
    snapshot: dict, start_time_dt: datetime
) -> tuple[str, date, str]:
    """回座归属：优先使用活动开始时的班次（users.shift）"""
    final_shift = (
        snapshot.get("user_shift")
        or snapshot.get("active_shift")
        or "day"
    )
    record_date = snapshot.get("active_record_date") or start_time_dt.date()
    if isinstance(record_date, str):
        record_date = date.fromisoformat(record_date[:10])

    shift_start_time = _parse_activity_start_time(
        snapshot.get("shift_start_time"), start_time_dt
    )
    day_end_str = snapshot.get("dual_day_end") or snapshot.get("work_end_time") or Config.DEFAULT_DUAL_DAY_END
    day_end_hour, day_end_min = map(int, day_end_str.split(":")[:2])

    if final_shift == "day":
        return final_shift, record_date, "day"

    day_end_dt = shift_start_time.replace(
        hour=day_end_hour, minute=day_end_min, second=0, microsecond=0
    )
    shift_detail = "night_tonight" if shift_start_time >= day_end_dt else "night_last"
    return final_shift, record_date, shift_detail


async def _resolve_back_reply_target(
    chat_id: int,
    user_id: int,
    user_trigger_message: Optional[types.Message],
) -> Optional[int]:
    """回座引用 activity scope 的 root（仅本用户 context graph）。"""
    if user_trigger_message:
        ctx = await db.get_context_for_message(
            chat_id,
            user_trigger_message.message_id,
            user_id=user_id,
        )
        if ctx and int(ctx["user_id"]) == user_id:
            return int(ctx["root_message_id"])

    return await resolve_context_reply_target(
        chat_id,
        user_id,
        scope_id=SCOPE_ACTIVITY,
        prefer_root=True,
    )


async def _resolve_back_shift_context(
    chat_id: int,
    uid: int,
    snapshot: dict,
    start_time_dt: datetime,
) -> tuple[str, date, str]:
    """回座时按活动开始时刻锁定的班次与 record_date"""
    final_shift = (
        snapshot.get("user_shift")
        or snapshot.get("active_shift")
        or "day"
    )
    record_date = snapshot.get("activity_record_date")
    if record_date is None:
        record_date = await db.resolve_shift_record_date_at_time(
            chat_id, uid, final_shift, start_time_dt
        )
    if record_date is None:
        record_date = snapshot.get("active_record_date") or start_time_dt.date()
    if isinstance(record_date, str):
        record_date = date.fromisoformat(record_date[:10])

    shift_config = _shift_config_from_snapshot(snapshot)
    if final_shift == "day":
        return final_shift, record_date, "day"

    day_end_str = shift_config.get("day_end", Config.DEFAULT_DUAL_DAY_END)
    day_end_time = datetime.strptime(day_end_str, "%H:%M").time()
    night_start = datetime.combine(record_date, day_end_time).replace(
        tzinfo=start_time_dt.tzinfo
    )
    shift_detail = (
        "night_tonight" if start_time_dt >= night_start else "night_last"
    )
    return final_shift, record_date, shift_detail


async def _persist_start_activity(
    message: types.Message,
    chat_id: int,
    uid: int,
    act: str,
    name: str,
    start_time_str: str,
    current_shift: str,
    record_date: date,
    time_limit: int,
    sent_message_id: int,
):
    from handover_manager import handover_manager

    try:
        started, duplicate = await db.try_start_user_activity(
            chat_id, uid, act, start_time_str, name, current_shift, record_date
        )
        if not started:
            dup = duplicate or "未知活动"
            await answer_user_message(
                message,
                Config.MESSAGES["has_activity"].format(dup),
                user_id=uid,
            )
            return
        handover_manager.invalidate_activity_count_cache(chat_id, uid)
        await db.update_user_message_ids(chat_id, uid, sent_message_id)
        await timer_manager.start_timer(
            chat_id, uid, act, time_limit, shift=current_shift
        )
    except Exception as e:
        logger.error(f"❌ 后台写入活动开始失败: {chat_id}-{uid}-{act}: {e}")


async def _persist_back_completion(
    chat_id: int,
    uid: int,
    act: str,
    elapsed: int,
    fine_amount: int,
    is_overtime: bool,
    final_shift: str,
    forced_date: date,
    time_limit_minutes: int,
    now: datetime,
):
    from handover_manager import handover_manager

    try:
        await handover_manager.record_activity(chat_id, uid, act, elapsed, now)
        await db.complete_user_activity(
            chat_id,
            uid,
            act,
            elapsed,
            fine_amount,
            is_overtime,
            final_shift,
            forced_date=forced_date,
            time_limit_minutes=time_limit_minutes,
        )
        await timer_manager.cancel_timer(
            chat_id=chat_id, uid=uid, preserve_message=True
        )
        handover_manager.invalidate_activity_count_cache(chat_id, uid)
    except Exception as e:
        logger.error(f"❌ 后台回座落库失败: {chat_id}-{uid}-{act}: {e}", exc_info=True)


async def start_activity(
    message: types.Message, act: str, activity_limits: Optional[dict] = None
):
    """开始活动打卡"""
    chat_id = message.chat.id
    uid = message.from_user.id
    flow_start = time.time()

    watchdog = Watchdog(timeout=30, name=f"start_activity_{chat_id}_{uid}")

    async def _start_activity_impl():
        watchdog.feed()
        name = message.from_user.full_name
        now = db.get_beijing_time()

        from handover_manager import handover_manager

        init_date = now.date()
        if activity_limits is not None:
            limits = activity_limits
            period, snapshot = await asyncio.gather(
                handover_manager.determine_current_period(chat_id, now),
                db.fetch_activity_start_snapshot(
                    chat_id, uid, name, init_date, act
                ),
            )
        else:
            period, limits, snapshot = await asyncio.gather(
                handover_manager.determine_current_period(chat_id, now),
                db.get_activity_limits_cached(),
                db.fetch_activity_start_snapshot(
                    chat_id, uid, name, init_date, act
                ),
            )

        business_date = period["business_date"]

        if act not in limits:
            await answer_user_message(
                message, f"❌ 活动 '{act}' 不存在", user_id=uid
            )
            return

        act_cfg = limits[act]
        max_times = act_cfg.get("max_times", 0)
        time_limit = act_cfg.get("time_limit", 0)

        if snapshot and _needs_daily_reset(snapshot.get("last_updated"), business_date):
            if snapshot.get("current_activity"):
                await reset_daily_data_if_needed(
                    chat_id, uid, business_date=business_date
                )
                snapshot = await db.fetch_activity_start_snapshot(
                    chat_id, uid, name, business_date, act
                )
            else:
                asyncio.create_task(
                    reset_daily_data_if_needed(
                        chat_id, uid, business_date=business_date
                    )
                )

        if not snapshot or not snapshot.get("active_shift"):
            await answer_user_message(
                message,
                "❌ 您当前没有进行中的班次，请先打上班卡！",
                user_id=uid,
            )
            return

        if snapshot.get("current_activity"):
            await answer_user_message(
                message,
                Config.MESSAGES["has_activity"].format(
                    snapshot["current_activity"]
                ),
                user_id=uid,
            )
            return

        shift_state = {
            "shift": snapshot["active_shift"],
            "record_date": snapshot["active_record_date"],
            "shift_start_time": snapshot["shift_start_time"],
        }

        expired, elapsed_h, max_h = await _is_shift_expired_for_user(
            chat_id,
            shift_state["shift"],
            shift_state["shift_start_time"],
            now,
        )

        if expired:
            closed = await db.auto_close_expired_open_work_shifts(chat_id, uid)
            expired_msg = await _shift_expired_user_message(
                chat_id,
                shift_state["shift"],
                now,
                closed,
                elapsed_h,
                max_h,
            )
            logger.warning(
                f"⏰ [活动拒绝] 用户{uid} 班次={shift_state['shift']} "
                f"上班={normalize_db_timestamp(shift_state['shift_start_time'], now)} "
                f"已持续{elapsed_h:.1f}h 上限{max_h:.0f}h"
            )
            await answer_user_message(
                message, expired_msg, user_id=uid, parse_mode="HTML"
            )
            return

        watchdog.feed()

        shift_config = _shift_config_from_snapshot(snapshot)
        current_shift, record_date, shift_detail = _resolve_shift_from_active_state(
            shift_state, now, shift_config
        )
        shift_text = "白班" if current_shift == "day" else "夜班"

        logger.info(
            f"🔄 [开始活动] 使用状态模型: {shift_text}, "
            f"详情={shift_detail}, 记录日期={record_date}"
        )

        if _work_hours_enabled_from_snapshot(snapshot) and snapshot.get(
            "has_work_end"
        ):
            await answer_user_message(
                message,
                f"❌ 您本{shift_text}已下班，无法进行活动！",
                user_id=uid,
            )
            return

        raw_count = int(snapshot.get("activity_count") or 0)
        current_count = await _effective_activity_count(
            chat_id, uid, raw_count, record_date, now, period=period
        )

        if current_count >= max_times:
            limit_msg = (
                f"❌ {shift_text}的 '<code>{act}</code>' 次数已达上限\n\n"
                f"📊 当前次数：<code>{current_count}</code> / <code>{max_times}</code>"
            )
            if period.get("is_handover"):
                effective_cycle = await handover_manager.get_user_effective_cycle(
                    chat_id, uid, period
                )
                if effective_cycle == 1 and period.get("next_reset_time"):
                    reset_str = period["next_reset_time"].strftime("%H:%M")
                    limit_msg += (
                        f"\n\n🔄 换班日：活动次数将在 <code>{reset_str}</code> 重置"
                    )
            await answer_user_message(
                message, limit_msg, user_id=uid, parse_mode="HTML"
            )
            return

        user_limit = int(snapshot.get("user_limit") or 0)
        if user_limit > 0:
            current_users = int(snapshot.get("current_activity_users") or 0)
            if current_users >= user_limit:
                await answer_user_message(
                    message,
                    f"❌ 活动 '<code>{act}</code>' 人数已满！\n\n"
                    f"📊 限制人数：<code>{user_limit}</code> 人\n"
                    f"• 当前进行：<code>{current_users}</code> 人\n"
                    f"• 剩余名额：<code>0</code> 人",
                    user_id=uid,
                    parse_mode="HTML",
                )
                return

        watchdog.feed()

        start_time_str = now.isoformat()
        activity_message = MessageFormatter.format_activity_message(
            user_id=uid,
            user_name=name,
            activity=act,
            time_str=now.strftime("%m/%d %H:%M:%S"),
            count=current_count + 1,
            max_times=max_times,
            time_limit=time_limit,
            shift=current_shift,
        )

        inline_back_kb = build_inline_back_keyboard(
            chat_id, uid, current_shift, get_lang_mode(chat_id)
        )

        reply_to_id = await resolve_context_reply_target(
            chat_id, uid, scope_id=SCOPE_ACTIVITY
        )
        send_kwargs = dict(
            reply_markup=inline_back_kb,
            parse_mode="HTML",
        )
        if reply_to_id is not None:
            send_kwargs["reply_to_message_id"] = reply_to_id
        sent_message = await message.answer(activity_message, **send_kwargs)
        root_id = await record_bot_outgoing(
            chat_id,
            sent_message.message_id,
            message.message_id,
            user_id=uid,
            new_thread=True,
            context_type="activity",
            activity_name=act,
        )

        await db.update_user_message_ids(chat_id, uid, sent_message.message_id)

        asyncio.create_task(
            _persist_start_activity(
                message,
                chat_id,
                uid,
                act,
                name,
                start_time_str,
                current_shift,
                record_date,
                time_limit,
                sent_message.message_id,
            )
        )

        logger.info(
            f"📝 用户 {uid} 开始活动 {act}（{shift_text}），消息ID: {sent_message.message_id}, "
            f"root: {root_id}, scope: {SCOPE_ACTIVITY}, 记录日期: {record_date}, "
            f"班次详情: {shift_detail}, 耗时: {time.time() - flow_start:.2f}s"
        )

        if act == "吃饭":
            try:
                notification_text = (
                    f"🍽️ <b>吃饭通知</b> <code>{shift_text}</code>\n"
                    f" {MessageFormatter.format_user_link(uid, name)} 去吃饭了\n"
                    f"⏰ 时间：<code>{now.strftime('%H:%M:%S')}</code>\n"
                )
                asyncio.create_task(
                    notification_service.send_notification(chat_id, notification_text)
                )
                logger.info(f"📣 已触发用户 {uid}（{shift_text}）的 {act} 推送")
            except Exception as e:
                logger.error(f"❌ {act} 推送失败: {e}")

    try:
        return await watchdog.run(_start_activity_impl())
    except asyncio.CancelledError:
        logger.error(f"⏰ 开始活动操作超时: {chat_id}-{uid}")
        try:
            await answer_user_message(
                message,
                "⏰ 开始活动操作超时，请重试",
                user_id=uid,
            )
        except Exception:
            pass
        return
    except Exception as e:
        logger.error(f"❌ 开始活动异常: {chat_id}-{uid}-{act}: {e}", exc_info=True)
        try:
            await answer_user_message(
                message,
                "⚠️ 打卡处理失败，请稍后重试。",
                user_id=uid,
            )
        except Exception:
            pass
        return


# ========== 回座功能 ==========
async def process_back(message: types.Message):
    """回座打卡（添加看门狗保护）"""
    watchdog = Watchdog(
        timeout=60, name=f"process_back_{message.chat.id}_{message.from_user.id}"
    )

    async def _process():
        chat_id = message.chat.id
        uid = message.from_user.id
        await _process_back_locked(message, chat_id, uid)

    try:
        return await watchdog.run(_process())
    except asyncio.CancelledError:
        logger.error(f"❌ 回座操作超时: {message.chat.id}-{message.from_user.id}")
        await message.answer("⏰ 操作超时，请重试")
        return


async def _process_back_locked(
    message: types.Message,
    chat_id: int,
    uid: int,
    shift: str = None,
    user_trigger_message: types.Message = None,
):
    """线程安全的回座逻辑"""
    start_time = time.time()
    key = f"{chat_id}:{uid}"

    if key in active_back_processing:
        lock_time = active_back_processing.get(key)
        if isinstance(lock_time, (int, float)) and time.time() - lock_time > Config.BACK_PROCESSING_LOCK_SEC:
            logger.warning(
                f"⚠️ [回座] 强制释放过期锁: {key} (持有时间: {time.time()-lock_time:.1f}秒)"
            )
            active_back_processing.pop(key, None)
        else:
            await answer_user_message(
                message,
                "⚠️ 您的回座请求正在处理中，请稍候。",
                user_id=uid,
            )
            return

    active_back_processing[key] = time.time()
    back_sent = False

    try:
        now = db.get_beijing_time()
        show_admin = uid in Config.ADMINS

        snapshot, limits_cfg, keyboard = await asyncio.gather(
            db.fetch_back_finish_snapshot(chat_id, uid),
            db.get_activity_limits_cached(),
            get_main_keyboard(chat_id=chat_id, show_admin=show_admin),
        )

        if not snapshot or not snapshot.get("current_activity"):
            asyncio.create_task(reset_daily_data_if_needed(chat_id, uid))
            await answer_user_message(
                message,
                Config.MESSAGES["no_activity"],
                user_id=uid,
                reply_markup=keyboard,
            )
            return

        act = snapshot["current_activity"]
        nickname = snapshot.get("nickname") or "未知用户"
        start_time_dt = _parse_activity_start_time(
            snapshot.get("activity_start_time"), now
        )

        final_shift, forced_date, shift_detail = await _resolve_back_shift_context(
            chat_id, uid, snapshot, start_time_dt
        )
        elapsed = max(0, int((now - start_time_dt).total_seconds()))
        time_limit_minutes = limits_cfg.get(act, {}).get("time_limit", 0)
        time_limit_seconds = time_limit_minutes * 60
        is_overtime = elapsed > time_limit_seconds
        overtime_seconds = max(0, int(elapsed - time_limit_seconds))
        overtime_minutes = overtime_seconds / 60
        fine_amount = 0
        if is_overtime and overtime_seconds > 0:
            fine_amount = await calculate_fine(act, overtime_minutes)

        projected_act_count = int(snapshot.get("act_count") or 0) + 1
        projected_act_time = int(snapshot.get("act_time") or 0) + elapsed
        projected_daily_time = int(snapshot.get("daily_time") or 0) + elapsed
        projected_daily_count = int(snapshot.get("daily_count") or 0) + 1
        elapsed_time_str = MessageFormatter.format_time(elapsed)
        time_str = now.strftime("%m/%d %H:%M:%S")
        activity_start_time_for_notification = snapshot.get("activity_start_time")

        logger.info(
            f"📅 [回座快路径] 班次={final_shift}, 归属={shift_detail}, "
            f"强制日期={forced_date}, 耗时={elapsed}s"
        )

        back_message = MessageFormatter.format_back_message(
            user_id=uid,
            user_name=nickname,
            activity=act,
            time_str=time_str,
            elapsed_time=elapsed_time_str,
            total_activity_time=MessageFormatter.format_time(projected_act_time),
            total_time=MessageFormatter.format_time(projected_daily_time),
            activity_counts={act: projected_act_count},
            total_count=projected_daily_count,
            is_overtime=is_overtime,
            overtime_seconds=overtime_seconds,
            fine_amount=fine_amount,
        )

        reply_target_id = await _resolve_back_reply_target(
            chat_id, uid, user_trigger_message
        )

        try:
            back_msg = await message.answer(
                back_message,
                reply_to_message_id=reply_target_id,
                reply_markup=keyboard,
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning(
                f"⚠️ [回座] 引用 root={reply_target_id} 失败，降级发送: {e}"
            )
            back_msg = await message.answer(
                back_message,
                reply_markup=keyboard,
                parse_mode="HTML",
            )
        back_sent = True

        parent_for_chain = (
            user_trigger_message.message_id
            if user_trigger_message
            else message.message_id
        )
        completed_ctx = await complete_message_context(
            chat_id, uid, back_msg.message_id, context_type="activity"
        )
        await record_bot_outgoing(
            chat_id,
            back_msg.message_id,
            parent_for_chain,
            user_id=uid,
            inherit_session_root=True,
            context_id=(
                int(completed_ctx["id"]) if completed_ctx else None
            ),
            scope_id=SCOPE_ACTIVITY,
        )
        await db.update_user_message_ids(chat_id, uid, back_msg.message_id)

        asyncio.create_task(reset_daily_data_if_needed(chat_id, uid))
        asyncio.create_task(
            _persist_back_completion(
                chat_id,
                uid,
                act,
                elapsed,
                fine_amount,
                is_overtime,
                final_shift,
                forced_date,
                time_limit_minutes,
                now,
            )
        )

        if is_overtime and fine_amount > 0:
            group_data = await db.get_group_cached(chat_id)
            if group_data.get("channel_id"):
                notification_user_data = _notification_user_data(
                    snapshot, nickname, activity_start_time_for_notification
                )
                asyncio.create_task(
                    send_overtime_notification_async(
                        chat_id=chat_id,
                        uid=uid,
                        user_data=notification_user_data,
                        act=act,
                        fine_amount=fine_amount,
                        now=now,
                        elapsed_time=int(elapsed),
                        time_limit_minutes=time_limit_minutes,
                    )
                )

        if act == "吃饭":
            try:
                chat_title = str(chat_id)
                try:
                    chat_info = await message.bot.get_chat(chat_id)
                    chat_title = chat_info.title or chat_title
                except Exception as chat_err:
                    logger.debug(f"获取群标题失败: {chat_err}")

                eat_end_notification_text = (
                    f"🍽️ <b>吃饭结束通知</b>\n"
                    f"{MessageFormatter.format_user_link(uid, nickname)} 吃饭回来了\n"
                    f"⏱️ 吃饭耗时：<code>{elapsed_time_str}</code>\n"
                )

                asyncio.create_task(
                    notification_service.send_notification(
                        chat_id, eat_end_notification_text
                    )
                )
                logger.info(f"🍽️ 已触发用户 {uid} 的吃饭回座推送")

            except Exception as e:
                logger.error(f"❌ 吃饭回座推送失败: {e}")

        try:
            logger.info(
                f"📊 [回座完成] 用户{uid} | 活动:{act} | "
                f"班次:{final_shift} | 归属:{shift_detail} | "
                f"强制日期:{forced_date} | "
                f"超时:{is_overtime} | 罚款:{fine_amount} | "
                f"耗时:{round(time.time() - start_time, 2)}s"
            )
        except Exception as log_err:
            logger.warning(f"回座完成日志记录失败: {log_err}")

    except Exception as e:
        logger.error(f"回座处理异常: {e}")
        logger.error(traceback.format_exc())
        if not back_sent:
            await answer_user_message(
                message,
                "❌ 回座失败，请稍后重试。",
                user_id=uid,
            )
        else:
            logger.error("回座主消息已发送，跳过后续失败提示")

    finally:
        # 先保存key状态
        had_lock = key in active_back_processing

        # 释放处理锁（落库在后台进行，不再阻塞）
        if had_lock:
            active_back_processing.pop(key, None)

        duration = round(time.time() - start_time, 2)
        logger.info(f"✅ [回座结束] key={key}，响应耗时 {duration}s")


async def send_overtime_notification_async(
    chat_id: int,
    uid: int,
    user_data: dict,
    act: str,
    fine_amount: int,
    now: datetime,
    elapsed_time: int = None,
    time_limit_minutes: int = None,
):
    """异步发送超时通知到频道"""
    try:
        group_data = await db.get_group_cached(chat_id)
        channel_id = group_data.get("channel_id")
        if not channel_id:
            logger.debug(f"⏱️ 群组 {chat_id} 未绑定频道，跳过推送")
            return

        chat_title = str(chat_id)
        try:
            chat_info = await bot_manager.bot.get_chat(chat_id)
            chat_title = chat_info.title or chat_title
        except Exception:
            pass

        nickname = user_data.get("nickname", "未知用户")

        if elapsed_time is not None and time_limit_minutes is not None:
            time_limit_seconds = time_limit_minutes * 60
            if elapsed_time > time_limit_seconds:
                overtime_seconds = elapsed_time - time_limit_seconds
                overtime_str = MessageFormatter.format_time(overtime_seconds)
            else:
                overtime_str = "未超时"
        else:
            activity_start_time = user_data.get("activity_start_time")
            if activity_start_time:
                try:
                    start_time = datetime.fromisoformat(activity_start_time)
                    time_limit = await db.get_activity_time_limit(act)
                    time_limit_seconds = time_limit * 60
                    total_elapsed = int((now - start_time).total_seconds())

                    if total_elapsed > time_limit_seconds:
                        overtime_seconds = total_elapsed - time_limit_seconds
                        overtime_str = MessageFormatter.format_time(overtime_seconds)
                except Exception as e:
                    logger.error(f"时间计算失败: {e}")

        notif_text = (
            f"🚨 <b>超时回座通知</b>\n"
            f"🏢 群组：<code>{chat_title}</code>\n"
            f"{MessageFormatter.create_dashed_line()}\n"
            f"👤 用户：{MessageFormatter.format_user_link(uid, nickname)}\n"
            f"📝 活动：<code>{act}</code>\n"
            f"⏰ 回座时间：<code>{now.strftime('%m/%d %H:%M:%S')}</code>\n"
            f"⏱️ 超时时长：<code>{overtime_str}</code>\n"
            f"💰 罚款金额：<code>{fine_amount}</code> 泰铢"
        )

        await notification_service.send_notification(chat_id, notif_text)
        logger.info(f"✅ 超时通知已推送到频道 {channel_id}: 用户{uid} - {act}")

    except Exception as e:
        logger.error(f"❌ 超时通知推送异常: {e}")


async def _notify_admins(text: str) -> None:
    """向所有管理员发送系统通知"""
    if not Config.ADMINS:
        logger.debug("未配置管理员，跳过系统通知")
        return

    sender = notification_service.bot_manager or bot_manager
    if not sender or not getattr(sender, "bot", None):
        logger.warning("Bot 未就绪，无法发送系统通知")
        return

    for admin_id in Config.ADMINS:
        try:
            if hasattr(sender, "send_message_with_retry"):
                await sender.send_message_with_retry(
                    admin_id, text, parse_mode="HTML"
                )
            else:
                await sender.bot.send_message(admin_id, text, parse_mode="HTML")
        except Exception as e:
            logger.error(f"发送系统通知给管理员 {admin_id} 失败: {e}")


async def send_startup_notification():
    """系统启动后通知管理员"""
    try:
        now = db.get_beijing_time()
        env_label = "Render" if os.environ.get("RENDER") else "本地"
        group_count = 0
        if db._initialized:
            try:
                group_count = len(await db.get_all_groups())
            except Exception:
                pass

        text = (
            f"🟢 <b>机器人已启动</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"🕒 启动时间：<code>{now.strftime('%Y-%m-%d %H:%M:%S')}</code>\n"
            f"🌐 运行环境：<code>{env_label}</code>\n"
            f"📊 已注册群组：<code>{group_count}</code> 个"
        )
        await _notify_admins(text)
        logger.info("✅ 启动通知已发送")
    except Exception as e:
        logger.error(f"发送启动通知失败: {e}")


async def send_shutdown_notification():
    """系统关闭前通知管理员"""
    try:
        now = db.get_beijing_time()
        uptime_seconds = int(time.time() - start_time)
        uptime_str = MessageFormatter.format_time(uptime_seconds)

        text = (
            f"🔴 <b>机器人正在关闭</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"🕒 关闭时间：<code>{now.strftime('%Y-%m-%d %H:%M:%S')}</code>\n"
            f"⏱️ 运行时长：<code>{uptime_str}</code>"
        )
        await _notify_admins(text)
        logger.info("✅ 关闭通知已发送")
    except Exception as e:
        logger.error(f"发送关闭通知失败: {e}")

