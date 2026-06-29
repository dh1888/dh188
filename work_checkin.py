import asyncio
import logging
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
from shift_window_helpers import (
    format_grace_window_hm,
    grace_from_config,
    build_work_start_window_error,
    build_work_end_window_error,
)
from database import db, parse_sql_row_count, normalize_db_timestamp
from constants import (
    BTN_WORK_START_DAY, BTN_WORK_START_NIGHT, BTN_WORK_END, WORK_BUTTONS,
    SPECIAL_BUTTONS, ACTIVITY_MAP, AdminStates,
)
from constants import active_back_processing
from keyboards import get_main_keyboard, get_admin_keyboard, is_admin, calculate_work_fine
from performance import (
    global_cache, track_performance, with_retry, message_deduplicate,
    rate_limit, user_rate_limit,
)
from utils import (
    MessageFormatter, user_lock_manager, timer_manager, notification_service,
    calculate_fine, get_beijing_time,
)
from fault_tolerance import Watchdog
from handover_manager import handover_manager
from reset_service import reset_daily_data_if_needed
from bot_manager import bot_manager
from activity_service import auto_end_current_activity
from i18n import work_button_label

logger = logging.getLogger("GroupCheckInBot")


def _resolve_forced_night_shift_detail(now: datetime, shift_config: dict) -> str:
    """
    关闭打卡窗口时区分今晚/昨晚夜班：
    - >= day_end 或 day_start~day_end：今晚夜班（含提前打卡）
    - < day_start：凌晨仍属昨晚夜班
    """
    day_end = shift_config.get("day_end", Config.DEFAULT_DUAL_DAY_END)
    day_start = shift_config.get("day_start", "09:00")
    day_end_h, day_end_m = map(int, day_end.split(":"))
    day_start_h, day_start_m = map(int, day_start.split(":"))
    current = now.time()
    if current >= dt_time(day_end_h, day_end_m):
        return "night_tonight"
    if current >= dt_time(day_start_h, day_start_m):
        return "night_tonight"
    return "night_last"


def _resolve_work_start_expected(
    now: datetime,
    shift_config: dict,
    shift_detail: str,
    record_date: date,
    work_hours: dict,
) -> tuple[str, date]:
    """上班期望时刻：夜班按实际开班日历日，避免 record_date 与显示日期错位。"""
    if shift_detail in ("night_last", "night_tonight"):
        expected_time = shift_config.get("day_end", Config.DEFAULT_DUAL_DAY_END)
        if shift_detail == "night_tonight":
            expected_date = now.date()
        else:
            expected_date = now.date() - timedelta(days=1)
    else:
        expected_time = work_hours["work_start"]
        expected_date = record_date
    return expected_time, expected_date


# ========== 上下班打卡功能 ==========
async def _format_work_window_failure(
    chat_id: int,
    uid: int,
    now: datetime,
    shift_config: dict,
    forced_shift: str,
    current_time: str,
) -> str:
    """生成上下班窗口失败时的详细提示"""
    shift_label = "白班" if forced_shift == "day" else "夜班"
    day_start = shift_config.get("day_start", Config.DEFAULT_DUAL_DAY_START)
    day_end = shift_config.get("day_end", Config.DEFAULT_DUAL_DAY_END)
    grace_before, grace_after, _, _ = grace_from_config(shift_config)

    day_work_start_start, day_work_start_end = format_grace_window_hm(
        now, day_start, grace_before, grace_after
    )
    night_work_start_start, night_work_start_end = format_grace_window_hm(
        now, day_end, grace_before, grace_after
    )

    period = await handover_manager.determine_current_period(chat_id, now)
    ho_cfg = await handover_manager.get_handover_config(chat_id)
    night_start = ho_cfg.get("night_start_time", "21:00")
    ho_day_start = ho_cfg.get("day_start_time", "09:00")
    ho_switch = handover_manager.get_handover_day_start_time(ho_cfg)
    ho_switch_decimal = handover_manager._handover_day_start_decimal(ho_cfg)

    handover_extra = ""
    if period.get("is_handover"):
        threshold = period.get(
            "reset_threshold_hours",
            handover_manager._get_reset_threshold_hours(ho_cfg),
        )
        if period.get("period_type") == "handover_day":
            handover_extra = (
                f"\n\n🔄 <b>换班日提示</b>\n"
                f"• 当前为换班白班时段（<code>{ho_switch}</code> 起至次日 <code>{ho_day_start}</code>）\n"
                f"• 请确认点击了正确的班次按钮"
            )
        elif period.get("period_type") == "handover_night":
            handover_extra = (
                f"\n\n🔄 <b>换班日提示</b>\n"
                f"• 当前为换班夜班时段（前一日 <code>{night_start}</code> 至换班日 <code>{ho_switch}</code>）\n"
                f"• 请确认点击了正确的班次按钮"
            )
        else:
            handover_extra = (
                f"\n\n🔄 <b>换班日提示</b>（{period.get('total_hours', 18):.0f}小时制）\n"
                f"• 活动次数每 <code>{threshold:.0f}</code> 小时重置一次\n"
                f"• 当前第 <code>{period.get('cycle', 1)}</code> 段"
            )

    btn_day = work_button_label("work_start_day")
    btn_night = work_button_label("work_start_night")

    suggested = "💡 请确认点击了正确的班次按钮"
    decimal = now.hour + now.minute / 60
    if period.get("period_type") == "handover_day":
        if decimal < ho_switch_decimal and forced_shift == "day":
            suggested = (
                f"💡 换班白班 <code>{ho_switch}</code> 起，当前请使用 <b>{btn_night}</b>"
            )
        elif decimal >= ho_switch_decimal and forced_shift == "night":
            suggested = (
                f"💡 换班日 <code>{ho_switch}</code> 后白班接班，请使用 <b>{btn_day}</b>"
            )
    elif period.get("period_type") == "handover_night":
        if forced_shift == "day" and decimal >= ho_switch_decimal:
            suggested = (
                f"💡 换班日 <code>{ho_switch}</code> 前仍为换班夜班，请使用 <b>{btn_night}</b>"
            )
    elif (
        day_work_start_start <= current_time <= day_work_start_end
        and forced_shift == "night"
    ):
        suggested = f"💡 当前在白班窗口，请使用 <b>{btn_day}</b>"
    elif forced_shift == "day" and (
        night_work_start_start <= current_time or current_time <= day_work_start_start
    ):
        suggested = f"💡 当前在夜班窗口，请使用 <b>{btn_night}</b>"

    relay = await handover_manager.get_handover_day_relay_handover_date(chat_id, now)
    if relay and forced_shift == "night" and now.hour < int(day_start.split(":")[0]):
        suggested = (
            f"💡 清晨 <code>{current_time}</code> 不能打夜班卡；"
            f"换班接班请 <b>{btn_day}</b>（约 <code>07:00</code> 起）"
        )

    return (
        f"❌ 当前时间不在<b>{shift_label}上班</b>打卡窗口内\n\n"
        f"📊 <b>允许的上班时间：</b>\n"
        f"• 白班上班：<code>{day_work_start_start} ~ {day_work_start_end}</code>\n"
        f"• 夜班上班：<code>{night_work_start_start} ~ {night_work_start_end}</code>（次日凌晨）\n\n"
        f"⏰ 当前时间：<code>{current_time}</code>\n"
        f"{suggested or '💡 请确认点击了正确的班次按钮'}"
        f"{handover_extra}"
    )


async def _resolve_forced_work_start_shift(
    chat_id: int,
    now: datetime,
    shift_config: dict,
    forced_shift: str,
) -> Optional[dict]:
    """根据用户点击的按钮，解析指定班次的上班打卡信息"""
    if shift_config.get("window_disabled"):
        if forced_shift == "day":
            shift_detail = "day"
        elif forced_shift == "night":
            shift_detail = _resolve_forced_night_shift_detail(now, shift_config)
        else:
            return None

        record_date = await db.get_business_date(
            chat_id=chat_id,
            current_dt=now,
            shift=forced_shift,
            checkin_type="work_start",
            shift_detail=shift_detail,
        )

        return {
            "shift": forced_shift,
            "shift_detail": shift_detail,
            "record_date": record_date,
            "business_date": record_date,
            "in_window": True,
        }

    window_info = db.calculate_shift_window(
        shift_config=shift_config,
        checkin_type="work_start",
        now=now,
    )

    if forced_shift == "day":
        day_window = window_info.get("day_window", {}).get("work_start", {})
        if not (
            day_window.get("start")
            and day_window.get("end")
            and day_window["start"] <= now <= day_window["end"]
        ):
            return None
        shift_detail = "day"
    elif forced_shift == "night":
        last_night = window_info.get("night_window", {}).get("last_night", {}).get(
            "work_start", {}
        )
        tonight = window_info.get("night_window", {}).get("tonight", {}).get(
            "work_start", {}
        )
        if (
            last_night.get("start")
            and last_night.get("end")
            and last_night["start"] <= now <= last_night["end"]
        ):
            shift_detail = "night_last"
        elif (
            tonight.get("start")
            and tonight.get("end")
            and tonight["start"] <= now <= tonight["end"]
        ):
            shift_detail = "night_tonight"
        else:
            return None
    else:
        return None

    record_date = await db.get_business_date(
        chat_id=chat_id,
        current_dt=now,
        shift=forced_shift,
        checkin_type="work_start",
        shift_detail=shift_detail,
    )

    return {
        "shift": forced_shift,
        "shift_detail": shift_detail,
        "record_date": record_date,
        "business_date": record_date,
        "in_window": True,
    }


async def _send_work_checkin_reply_chain(
    message: types.Message,
    chat_id: int,
    uid: int,
    result_msg: str,
    keyboard,
):
    """发送上下班打卡结果，引用用户本次操作消息形成闭环"""
    try:
        sent_message = await message.answer(
            result_msg,
            reply_markup=keyboard,
            reply_to_message_id=message.message_id,
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning(
            f"引用回复下班/上班结果失败，降级为普通回复: chat={chat_id} uid={uid} err={e}"
        )
        sent_message = await message.answer(
            result_msg,
            reply_markup=keyboard,
            parse_mode="HTML",
        )

    await db.update_user_checkin_message(chat_id, uid, sent_message.message_id)
    await db.update_pending_reply_message(chat_id, uid, sent_message.message_id)

    return sent_message


async def process_work_checkin(
    message: types.Message, checkin_type: str, forced_shift: str = None
):
    """智能化上下班打卡系统（带看门狗保护）"""

    chat_id = message.chat.id
    uid = message.from_user.id
    name = message.from_user.full_name

    # ===== 新增：创建看门狗，45秒超时 =====
    watchdog = Watchdog(timeout=45, name=f"work_checkin_{chat_id}_{uid}_{checkin_type}")

    async def _process_work_checkin_impl():
        # 原有函数体，保持完全不变
        if not await db.has_work_hours_enabled(chat_id):
            await message.answer(
                "❌ 本群组尚未启用上下班打卡功能\n\n"
                "👑 请联系管理员使用命令：\n"
                "<code>/setdualmode on 09:00 21:00</code>\n"
                "或 <code>/setworktime 09:00 18:00</code>\n"
                "设置后即可使用",
                reply_markup=await get_main_keyboard(chat_id, await is_admin(uid)),
                reply_to_message_id=message.message_id,
                parse_mode="HTML",
            )
            logger.info(f"❌ 群组 {chat_id} 未启用上下班功能，用户 {uid} 尝试打卡")
            return

        now = db.get_beijing_time()
        current_time = now.strftime("%H:%M")
        trace_id = f"{chat_id}-{uid}-{int(time.time())}"

        action_text = "上班" if checkin_type == "work_start" else "下班"
        status_type = "迟到" if checkin_type == "work_start" else "早退"

        logger.info(f"🟢[{trace_id}] 开始处理{action_text}打卡请求：{name}({uid})")

        # 喂狗：开始处理
        watchdog.feed()

        work_hours_task = asyncio.create_task(db.get_group_work_time(chat_id))
        shift_config_task = asyncio.create_task(db.get_shift_config(chat_id))
        is_admin_task = asyncio.create_task(is_admin(uid))

        try:
            await db.init_group(chat_id)
            business_date = await reset_daily_data_if_needed(chat_id, uid)
            await db.init_user(chat_id, uid, name, business_date=business_date)
            user_data = await db.get_user_cached(chat_id, uid)
        except Exception as e:
            logger.error(f"[{trace_id}] ❌ 初始化用户/群组失败: {e}")
            logger.error(traceback.format_exc())
            await message.answer(
                "⚠️ 数据初始化失败，请稍后再试。",
                reply_to_message_id=message.message_id,
                reply_markup=await get_main_keyboard(chat_id, await is_admin_task),
            )
            return

        work_hours = await work_hours_task
        shift_config = await shift_config_task
        is_admin_user = await is_admin_task

        # 喂狗：获取配置后
        watchdog.feed()

        active_shift = None
        active_record_date = None
        if checkin_type == "work_end":
            user_shift_state = await db.get_user_pending_work_end_shift(chat_id, uid)
            if user_shift_state:
                active_shift = user_shift_state.get("shift")
                active_record_date = user_shift_state.get("record_date")
                logger.info(
                    f"[{trace_id}] 📋 待下班班次(FIFO): "
                    f"{active_shift} / record_date={active_record_date}"
                )
            else:
                shift_status, shift_info = await db.resolve_shift_for_activity(
                    chat_id, uid
                )
                if shift_status == "ended" and shift_info:
                    shift_text = (
                        "白班" if shift_info.get("shift") == "day" else "夜班"
                    )
                    await message.answer(
                        f"❌ 您本{shift_text}已下班，无需重复打卡！\n"
                        f"💡 若仍需上班，请先打对应班次上班卡",
                        reply_to_message_id=message.message_id,
                        reply_markup=await get_main_keyboard(
                            chat_id, await is_admin_task
                        ),
                    )
                    logger.warning(
                        f"[{trace_id}] ⚠️ 下班打卡但班次已结束 "
                        f"{shift_info.get('shift')}/{shift_info.get('record_date')}"
                    )
                    return
                await message.answer(
                    "❌ 没有待下班的班次记录，请先打上班卡！",
                    reply_to_message_id=message.message_id,
                    reply_markup=await get_main_keyboard(
                        chat_id, await is_admin_task
                    ),
                )
                logger.warning(f"[{trace_id}] ⚠️ 下班打卡但无待下班班次")
                return

        if checkin_type == "work_start" and forced_shift:
            shift_info = await _resolve_forced_work_start_shift(
                chat_id, now, shift_config, forced_shift
            )
            if shift_info is None:
                fail_msg = await _format_work_window_failure(
                    chat_id,
                    uid,
                    now,
                    shift_config,
                    forced_shift,
                    current_time,
                )
                await message.answer(
                    fail_msg,
                    reply_to_message_id=message.message_id,
                    reply_markup=await get_main_keyboard(
                        chat_id, await is_admin_task
                    ),
                    parse_mode="HTML",
                )
                return
        else:
            shift_info = await db.determine_shift_for_time(
                chat_id=chat_id,
                current_time=now,
                checkin_type=checkin_type,
                active_shift=active_shift,
                active_record_date=active_record_date,
            )

        if shift_info is None or shift_info.get("shift_detail") is None:
            if checkin_type == "work_start":
                fail_text = build_work_start_window_error(
                    shift_config, now, current_time, action_text
                )
                await message.answer(
                    fail_text,
                    reply_to_message_id=message.message_id,
                    reply_markup=await get_main_keyboard(
                        chat_id, await is_admin_task
                    ),
                    parse_mode="HTML",
                )
                return
            else:
                fail_text = build_work_end_window_error(
                    shift_config, now, current_time, action_text
                )
                await message.answer(
                    fail_text,
                    reply_to_message_id=message.message_id,
                    reply_markup=await get_main_keyboard(
                        chat_id, await is_admin_task
                    ),
                    parse_mode="HTML",
                )
                return

        shift = shift_info["shift"]
        shift_detail = shift_info["shift_detail"]
        record_date = shift_info["record_date"]
        business_date = shift_info.get("business_date", record_date)
        message._current_shift = shift

        shift_text_map = {
            "day": "白班",
            "night": "夜班",
            "night_last": "昨晚夜班",
            "night_tonight": "今晚夜班",
        }
        shift_text = shift_text_map.get(shift_detail, "白班")

        logger.info(
            f"[{trace_id}] ✅ 班次判定: {shift_text} | "
            f"shift={shift}, detail={shift_detail}, record_date={record_date}"
        )

        # 喂狗：班次判定后
        watchdog.feed()

        if checkin_type == "work_start":
            if shift_detail is None:
                await message.answer(
                    f"❌ 当前时间不在任何班次的{action_text}窗口内",
                    reply_to_message_id=message.message_id,
                    reply_markup=await get_main_keyboard(
                        chat_id, await is_admin_task
                    ),
                )
                return

            user_data = await db.get_user_cached(chat_id, uid)
            if user_data and user_data.get("current_activity"):
                current_shift = user_data.get("shift", "day")
                current_activity = user_data["current_activity"]
                activity_record_date = user_data.get("activity_record_date")

                if activity_record_date is None and user_data.get(
                    "activity_start_time"
                ):
                    try:
                        act_start = datetime.fromisoformat(
                            user_data["activity_start_time"].replace("Z", "+00:00")
                        )
                        activity_record_date = (
                            await db.resolve_shift_record_date_at_time(
                                chat_id, uid, current_shift, act_start
                            )
                        )
                    except Exception:
                        activity_record_date = None

                same_shift_type = current_shift == shift
                same_record_date = (
                    activity_record_date is not None
                    and activity_record_date == record_date
                )
                should_auto_end = not same_shift_type or not same_record_date

                if should_auto_end:
                    logger.info(
                        f"[{trace_id}] 🔄 班次切换检测: "
                        f"旧班次={current_shift}(活动:{current_activity}, "
                        f"日期={activity_record_date}), "
                        f"新班次={shift}(日期={record_date})，自动结束旧活动"
                    )

                    await message.answer(
                        f"🔄 <b>系统自动处理</b>\n"
                        f"检测到您有未结束的<code>{current_shift}</code>班次活动：<code>{current_activity}</code>\n"
                        f"由于您正在打<code>{shift}</code>班次上班卡，该活动已自动结束。",
                        parse_mode="HTML",
                    )

                    await auto_end_current_activity(
                        chat_id=chat_id,
                        uid=uid,
                        user_data=user_data,
                        now=now,
                        message=message,
                    )

                    user_data = await db.get_user_cached(chat_id, uid)

            existing_record = await _find_shift_work_record_on_date(
                chat_id, uid, "work_start", shift, record_date
            )
            if existing_record:
                existing_time = existing_record.get("checkin_time", "未知时间")
                existing_status = existing_record.get("status", "未知状态")
                existing_created = existing_record.get("created_at")
                created_dt = normalize_db_timestamp(existing_created, now)
                created_str = (
                    created_dt.strftime("%m/%d %H:%M")
                    if created_dt
                    else "未知"
                )

                await message.answer(
                    f"🚫 您本班次已经打过{action_text}卡了！\n\n"
                    f"📊 <b>已有记录详情：</b>\n"
                    f"   • 打卡时间：<code>{existing_time}</code>\n"
                    f"   • 打卡状态：{existing_status}\n"
                    f"   • 班次类型：<code>{shift_text}</code>\n"
                    f"   • 记录时间：<code>{created_str}</code>\n\n"
                    f"💡 如需重新打卡，请联系管理员",
                    parse_mode="HTML",
                    reply_to_message_id=message.message_id,
                    reply_markup=await get_main_keyboard(
                        chat_id, await is_admin_task
                    ),
                )
                logger.info(f"[{trace_id}] ⚠️ 用户本班次重复{action_text}")
                return

            if await _is_shift_work_cycle_complete(chat_id, uid, shift, record_date):
                existing_end_record = await _find_shift_work_record_on_date(
                    chat_id, uid, "work_end", shift, record_date
                )
                existing_time = (
                    existing_end_record.get("checkin_time", "未知时间")
                    if existing_end_record
                    else "未知时间"
                )

                await message.answer(
                    f"🚫 您本班次已经在 <code>{existing_time}</code> 打过下班卡，无法再打{action_text}卡！\n\n"
                    f"💡 如需重新打卡，请联系管理员或等待下一班次",
                    parse_mode="HTML",
                    reply_to_message_id=message.message_id,
                    reply_markup=await get_main_keyboard(
                        chat_id, await is_admin_task
                    ),
                )
                logger.info(
                    f"[{trace_id}] 🔁 班次 {shift}/{record_date} 已完成上下班，拒绝重复上班"
                )
                return

            expected_time, expected_date = _resolve_work_start_expected(
                now, shift_config, shift_detail, record_date, work_hours
            )
            logger.info(
                f"[{trace_id}] 🌙 夜班上班: detail={shift_detail}, "
                f"期望={expected_date} {expected_time}, record_date={record_date}"
            )

            expected_hour, expected_minute = map(int, expected_time.split(":"))
            expected_dt = datetime.combine(
                expected_date, dt_time(expected_hour, expected_minute)
            ).replace(tzinfo=now.tzinfo)

            time_diff_seconds = int((now - expected_dt).total_seconds())
            time_diff_minutes = time_diff_seconds / 60

            fine_amount = 0
            status = "✅ 准时"
            is_late_early = False
            emoji_status = "👍"

            if time_diff_seconds > 0:
                fine_amount = await calculate_work_fine(
                    "work_start", time_diff_minutes
                )
                duration = MessageFormatter.format_duration(time_diff_seconds)
                status = f"🚨 迟到 {duration}"
                if fine_amount:
                    status += f"\n💰罚款金额: {fine_amount} 泰铢"
                is_late_early = True
                emoji_status = "😅"
            elif time_diff_seconds < 0:
                duration = MessageFormatter.format_duration(abs(time_diff_seconds))
                status = f"✅ 早到 {duration}"
                emoji_status = "👍"

            # ===== 上班打卡 - 使用新的 add_work_record 方法 =====
            db_write_success = False
            for attempt in range(3):
                try:
                    # ✅ 1. 使用新的 add_work_record 方法（会自动处理 work_records + daily_statistics + monthly_statistics）
                    await db.add_work_record(
                        chat_id=chat_id,
                        user_id=uid,
                        record_date=record_date,  # 使用班次判定得到的 record_date
                        checkin_type="work_start",
                        checkin_time=current_time,
                        status=status,
                        time_diff_minutes=time_diff_minutes,
                        fine_amount=fine_amount,
                        shift=shift,
                        shift_detail=shift_detail,
                    )

                    # ✅ 2. 仍然需要设置用户班次状态（这个单独处理）
                    wr_row = await _find_shift_work_record_on_date(
                        chat_id, uid, "work_start", shift, record_date
                    )
                    shift_start_time = (
                        normalize_db_timestamp(wr_row.get("created_at"), now)
                        if wr_row
                        else now
                    )
                    success = await db.set_user_shift_state(
                        chat_id=chat_id,
                        user_id=uid,
                        shift=shift,
                        record_date=record_date,
                        shift_start_time=shift_start_time,
                    )

                    if success:
                        shift_text_display = "白班" if shift == "day" else "夜班"
                        logger.info(
                            f"🏁 [{trace_id}] 用户班次状态设置成功: {shift_text_display}, 用户={uid}"
                        )

                    db_write_success = True
                    break

                except Exception as e:
                    logger.error(
                        f"[{trace_id}] ❌ 上班打卡失败 (尝试 {attempt + 1}/3): {e}"
                    )
                    if attempt == 2:  # 最后一次尝试失败
                        await message.answer("❌ 系统繁忙，请稍后重试")
                        return
                    await asyncio.sleep(1 * (2**attempt))  # 指数退避：1, 2, 4秒

            if not db_write_success:
                await message.answer(
                    f"❌ {action_text}打卡写入失败，请稍后重试或联系管理员",
                    reply_to_message_id=message.message_id,
                    reply_markup=await get_main_keyboard(
                        chat_id, await is_admin_task
                    ),
                )
                return

            await db.update_user_last_updated(chat_id, uid, record_date)

            result_msg = (
                f"{emoji_status} <b>{shift_text}{action_text}完成</b>\n"
                f"👤 用户：{MessageFormatter.format_user_link(uid, name)}\n"
                f"⏰ 打卡时间：<code>{current_time}</code>\n"
                f"📅 {action_text}时间：<code>{expected_dt.strftime('%m/%d %H:%M')}</code>\n"
                f"📊 状态：{status}"
            )

            handover_hint = await handover_manager.get_handover_status_hint(
                chat_id, uid, now
            )
            if handover_hint:
                result_msg += f"\n\n{handover_hint}"

            relay_handover = await handover_manager.get_handover_day_relay_handover_date(
                chat_id, now
            )
            if (
                relay_handover
                and shift == "day"
                and record_date > relay_handover
            ):
                pending = await db.get_user_pending_work_end_shift(chat_id, uid)
                if pending and pending.get("record_date") == relay_handover:
                    ho_switch = handover_manager.get_handover_day_start_time(
                        await handover_manager.get_handover_config(chat_id)
                    )
                    result_msg += (
                        f"\n\n🔄 <b>换班接班</b>\n"
                        f"• 已记录 <code>{record_date.strftime('%m/%d')}</code> 白班上班\n"
                        f"• 此后活动归属本班次\n"
                        f"• 下次「下班」将结束换班白班"
                        f"（<code>{relay_handover.strftime('%m/%d')} {ho_switch}</code> 起）"
                    )

            await _send_work_checkin_reply_chain(
                message,
                chat_id,
                uid,
                result_msg,
                await get_main_keyboard(chat_id, await is_admin_task),
            )

            await send_work_notification(
                chat_id=chat_id,
                user_id=uid,
                user_name=name,
                checkin_time=current_time,
                checkin_dt=now,
                expected_dt=expected_dt,
                action_text=action_text,
                status_type=status_type if is_late_early else "准时",
                fine_amount=fine_amount,
                trace_id=trace_id,
                shift=shift,
            )

            logger.info(f"✅[{trace_id}] {shift_text}{action_text}打卡流程完成")
            return

        elif checkin_type == "work_end":
            if shift_detail is None:
                await message.answer(
                    f"❌ 当前时间不在任何班次的{action_text}窗口内",
                    reply_to_message_id=message.message_id,
                    reply_markup=await get_main_keyboard(
                        chat_id, await is_admin_task
                    ),
                )
                return

            existing_record = await _find_shift_work_record(
                chat_id, uid, "work_end", shift, record_date
            )
            if existing_record:
                existing_time = existing_record.get("checkin_time", "未知时间")
                existing_status = existing_record.get("status", "未知状态")
                existing_created = existing_record.get("created_at")
                created_dt = normalize_db_timestamp(existing_created, now)
                created_str = (
                    created_dt.strftime("%m/%d %H:%M")
                    if created_dt
                    else "未知"
                )

                await message.answer(
                    f"🚫 您本班次已经打过{action_text}卡了！\n\n"
                    f"📊 <b>已有记录详情：</b>\n"
                    f"    • 打卡时间：<code>{existing_time}</code>\n"
                    f"    • 打卡状态：{existing_status}\n"
                    f"    • 班次类型：<code>{shift_text}</code>\n"
                    f"    • 记录时间：<code>{created_str}</code>",
                    parse_mode="HTML",
                    reply_to_message_id=message.message_id,
                    reply_markup=await get_main_keyboard(
                        chat_id, await is_admin_task
                    ),
                )
                logger.info(f"[{trace_id}] ⚠️ 用户本班次重复{action_text}")
                return

            has_work_start = await _check_shift_work_record(
                chat_id,
                uid,
                "work_start",
                shift,
                record_date,
            )

            if not has_work_start and shift == "night":
                yesterday = record_date - timedelta(days=1)
                has_work_start_yesterday = await _check_shift_work_record(
                    chat_id,
                    uid,
                    "work_start",
                    shift,
                    yesterday,
                )
                if has_work_start_yesterday:
                    record_date = yesterday
                    has_work_start = True
                    logger.info(
                        f"[{trace_id}] 🌙 检测到昨晚夜班上班记录，使用昨天日期: {yesterday}"
                    )

            if not has_work_start:
                shift_text_display = "白班" if shift == "day" else "夜班"
                await message.answer(
                    f"❌ 未找到 {record_date} 的上班记录，无法打{action_text}卡！\n"
                    f"💡 请先打{shift_text_display}上班卡",
                    reply_to_message_id=message.message_id,
                    reply_markup=await get_main_keyboard(
                        chat_id, await is_admin_task
                    ),
                )
                logger.warning(
                    f"[{trace_id}] ⚠️ 用户试图{action_text}打卡但未找到上班记录"
                )
                return

            if shift_detail in ["night_last", "night_tonight"] or shift == "night":
                expected_time = work_hours["work_start"]

                night_work_date = record_date

                if shift_detail == "night_tonight":
                    expected_date = night_work_date + timedelta(days=1)
                    logger.info(
                        f"[{trace_id}] 🌙 今晚夜班下班: "
                        f"上班日期={night_work_date}, 下班日期={expected_date}"
                    )
                else:
                    expected_date = night_work_date + timedelta(days=1)
                    logger.info(
                        f"[{trace_id}] 🌙 昨晚夜班下班: "
                        f"上班日期={night_work_date}, 下班日期={expected_date}"
                    )

                logger.info(
                    f"[{trace_id}] 🌙 夜班下班最终: "
                    f"期望时间={expected_time}, 期望日期={expected_date}"
                )
                final_record_date = record_date
            else:
                expected_time = work_hours["work_end"]
                expected_date = record_date
                final_record_date = record_date

            expected_hour, expected_minute = map(int, expected_time.split(":"))
            expected_dt = datetime.combine(
                expected_date, dt_time(expected_hour, expected_minute)
            ).replace(tzinfo=now.tzinfo)

            time_diff_seconds = int((now - expected_dt).total_seconds())
            time_diff_minutes = time_diff_seconds / 60

            logger.debug(
                f"📊 [{trace_id}] 时间差计算: now={now}, expected={expected_dt}, 差值={time_diff_seconds}秒"
            )

            fine_amount = 0
            status = "✅ 准时"
            is_late_early = False
            emoji_status = "👍"

            if time_diff_seconds < 0:
                fine_amount = await calculate_work_fine(
                    "work_end", abs(time_diff_minutes)
                )
                duration = MessageFormatter.format_duration(abs(time_diff_seconds))
                status = f"🚨 早退 {duration} \n"
                if fine_amount:
                    status += f"💰罚款金额 {fine_amount} 泰铢"
                is_late_early = True
                emoji_status = "🏃"
            elif time_diff_seconds > 0:
                duration = MessageFormatter.format_duration(time_diff_seconds)
                status = f"✅ 加班 {duration}"
                emoji_status = "⏰"

            activity_auto_ended = False
            current_activity = (
                user_data.get("current_activity") if user_data else None
            )
            current_activity_shift = user_data.get("shift") if user_data else None

            if current_activity:
                # ===== 检查活动班次与下班班次是否匹配 =====
                if current_activity_shift and current_activity_shift != shift:
                    logger.info(
                        f"[{trace_id}] ⏭️ 跳过结束不同班次活动: "
                        f"活动班次={current_activity_shift}, "
                        f"下班班次={shift}"
                    )
                    # 可以发送提醒，但不结束活动
                    await message.answer(
                        f"ℹ️ <b>提示</b>\n\n"
                        f"您当前有 <code>{'夜班' if current_activity_shift == 'night' else '白班'}</code> 活动 "
                        f"<code>{current_activity}</code> 正在进行中，\n"
                        f"但您正在打 <code>{'白班' if shift == 'day' else '夜班'}</code> 下班卡。\n\n"
                        f"该活动不会被自动结束，请在换班前手动结束。",
                        parse_mode="HTML",
                        reply_to_message_id=message.message_id,
                    )
                else:
                    # 只有班次匹配时才自动结束活动
                    with suppress(Exception):
                        await auto_end_current_activity(
                            chat_id, uid, user_data, now, message
                        )
                        activity_auto_ended = True
                        logger.info(
                            f"[{trace_id}] 🔄 已自动结束活动：{current_activity}"
                        )
                # ===== 结束检查 =====

            # ===== 下班打卡 - 使用新的 add_work_record 方法 =====
            db_write_success = False
            for attempt in range(3):
                try:
                    # ✅ 1. 使用新的 add_work_record 方法（自动处理 work_records + daily_statistics + monthly_statistics）
                    await db.add_work_record(
                        chat_id=chat_id,
                        user_id=uid,
                        record_date=final_record_date,
                        checkin_type="work_end",
                        checkin_time=current_time,
                        status=status,
                        time_diff_minutes=time_diff_minutes,
                        fine_amount=fine_amount,
                        shift=shift,
                        shift_detail=shift_detail,
                    )

                    # ✅ 2. 清除用户班次状态（这个单独处理）
                    success = await db.clear_user_shift_state(
                        chat_id=chat_id,
                        user_id=uid,
                        shift=shift,
                    )

                    shift_text_display = "白班" if shift == "day" else "夜班"

                    if success:
                        logger.info(
                            f"🏁 [{trace_id}] 用户班次状态清除成功: {shift_text_display}, 用户={uid}"
                        )

                        # ✅ 3. 检查是否还有其他人在这个班次
                        async with db.pool.acquire() as check_conn:
                            other_users = await check_conn.fetchval(
                                """
                                SELECT COUNT(*) FROM group_shift_state
                                WHERE chat_id = $1 AND shift = $2
                                """,
                                chat_id,
                                shift,
                            )

                            if other_users == 0:
                                # 定义发送通知的函数
                                async def send_end_notification():
                                    try:
                                        await message.answer(
                                            f"📢 <b>{shift_text_display}班次结束</b> 所有用户已完成下班打卡",
                                            parse_mode="HTML",
                                        )
                                    except Exception as e:
                                        logger.error(f"发送班次结束通知失败: {e}")

                                asyncio.create_task(send_end_notification())
                                logger.info(
                                    f"🏁 [{trace_id}] {shift_text_display}班次所有用户已下班"
                                )
                            else:
                                logger.info(
                                    f"ℹ️ [{trace_id}] 仍有 {other_users} 人在{shift_text_display}班次工作中"
                                )
                    else:
                        logger.warning(
                            f"⚠️ [{trace_id}] 用户班次状态清除失败: {shift_text_display}, 用户={uid}"
                        )

                    db_write_success = True
                    break

                except Exception as e:
                    logger.error(
                        f"[{trace_id}] ❌ 下班打卡失败 (尝试 {attempt + 1}/3): {e}"
                    )
                    if attempt == 2:  # 最后一次尝试失败
                        await message.answer("❌ 系统繁忙，请稍后重试")
                        return
                    await asyncio.sleep(1 * (2**attempt))  # 指数退避：1, 2, 4秒

            if not db_write_success:
                await message.answer(
                    f"❌ {action_text}打卡写入失败，请稍后重试或联系管理员",
                    reply_to_message_id=message.message_id,
                    reply_markup=await get_main_keyboard(
                        chat_id, await is_admin_task
                    ),
                )
                return

            await db.sync_user_shift_state_from_records(chat_id, uid)

            result_msg = (
                f"{emoji_status} <b>{shift_text}{action_text}完成</b>\n"
                f"👤 用户：{MessageFormatter.format_user_link(uid, name)}\n"
                f"⏰ 打卡时间：<code>{current_time}</code>\n"
                f"📅 {action_text}时间：<code>{expected_dt.strftime('%m/%d %H:%M')}</code>\n"
                f"📊 状态：{status}"
            )

            relay_handover = await handover_manager.get_handover_day_relay_handover_date(
                chat_id, now
            )
            if (
                relay_handover
                and shift == "day"
                and final_record_date == relay_handover
            ):
                ho_switch = handover_manager.get_handover_day_start_time(
                    await handover_manager.get_handover_config(chat_id)
                )
                remaining = await db.get_user_current_shift(chat_id, uid)
                remain_text = ""
                if remaining:
                    rd = remaining["record_date"]
                    remain_text = (
                        f"\n• 当前在岗：<code>{rd.strftime('%m/%d')}</code> "
                        f"{'白班' if remaining['shift'] == 'day' else '夜班'}"
                    )
                result_msg += (
                    f"\n\n🔄 <b>换班白班已结束</b>（"
                    f"<code>{relay_handover.strftime('%m/%d')} {ho_switch}</code> 起）"
                    f"{remain_text}\n"
                    f"• 之后按正常流程打卡即可"
                )

            if activity_auto_ended and current_activity:
                result_msg += f"\n\n🔄 检测到未结束活动 <code>{current_activity}</code>，已自动结束"

            await _send_work_checkin_reply_chain(
                message,
                chat_id,
                uid,
                result_msg,
                await get_main_keyboard(chat_id, await is_admin_task),
            )

            status_display = status_type if is_late_early else "准时"
            if time_diff_seconds > 0 and action_text == "下班":
                status_display = "加班"

            await send_work_notification(
                chat_id=chat_id,
                user_id=uid,
                user_name=name,
                checkin_time=current_time,
                checkin_dt=now,
                expected_dt=expected_dt,
                action_text=action_text,
                status_type=status_display,
                fine_amount=fine_amount,
                trace_id=trace_id,
                shift=shift,
            )

            logger.info(f"✅[{trace_id}] {shift_text}{action_text}打卡流程完成")
            return

    # ===== 使用看门狗运行 =====
    try:
        return await watchdog.run(_process_work_checkin_impl())
    except asyncio.CancelledError:
        logger.error(f"⏰ 上下班打卡操作超时: {chat_id}-{uid} ({checkin_type})")
        try:
            await message.answer("⏰ 打卡操作超时，请重试")
        except Exception:
            pass
        return
    except Exception as e:
        logger.error(
            f"❌ 上下班打卡异常: {chat_id}-{uid} ({checkin_type}): {e}",
            exc_info=True,
        )
        try:
            await message.answer(
                "⚠️ 打卡处理失败，请稍后重试。若持续失败请联系管理员。",
                reply_to_message_id=message.message_id,
            )
        except Exception:
            pass
        return


async def _find_shift_work_record_on_date(
    chat_id: int,
    user_id: int,
    checkin_type: str,
    shift: str,
    record_date: date,
) -> Optional[dict]:
    """仅查指定 record_date 的打卡记录（不含夜班跨日 fallback）。"""
    if not all([chat_id, user_id, checkin_type, shift, record_date]):
        return None
    try:
        async with db.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT checkin_time, status, created_at, record_date
                FROM work_records
                WHERE chat_id = $1
                  AND user_id = $2
                  AND checkin_type = $3
                  AND shift = $4
                  AND record_date = $5
                ORDER BY created_at DESC
                LIMIT 1
                """,
                chat_id,
                user_id,
                checkin_type,
                shift,
                record_date,
            )
        if row:
            return {
                "checkin_time": row["checkin_time"],
                "status": row["status"],
                "created_at": row["created_at"],
                "record_date": row["record_date"],
            }
        return None
    except Exception as e:
        logger.error(f"查找班次打卡记录失败: {e}")
        return None


async def _is_shift_work_cycle_complete(
    chat_id: int, user_id: int, shift: str, record_date: date
) -> bool:
    """同一 shift + record_date 是否已有完整上班+下班（才禁止再次上班）。"""
    start = await _find_shift_work_record_on_date(
        chat_id, user_id, "work_start", shift, record_date
    )
    end = await _find_shift_work_record_on_date(
        chat_id, user_id, "work_end", shift, record_date
    )
    return start is not None and end is not None


async def _find_shift_work_record(
    chat_id: int,
    user_id: int,
    checkin_type: str,
    shift: str,
    record_date: date,
) -> Optional[Dict]:
    """查找指定班次的打卡记录（单次连接，按日期优先级尝试）"""
    if not all([chat_id, user_id, checkin_type, shift, record_date]):
        return None

    dates_to_try = [record_date]
    if shift == "night":
        dates_to_try.append(record_date - timedelta(days=1))

    try:
        async with db.pool.acquire() as conn:
            for try_date in dates_to_try:
                row = await conn.fetchrow(
                    """
                    SELECT checkin_time, status, created_at, record_date
                    FROM work_records
                    WHERE chat_id = $1
                      AND user_id = $2
                      AND checkin_type = $3
                      AND shift = $4
                      AND record_date = $5
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    chat_id,
                    user_id,
                    checkin_type,
                    shift,
                    try_date,
                )
                if row:
                    return {
                        "checkin_time": row["checkin_time"],
                        "status": row["status"],
                        "created_at": row["created_at"],
                        "record_date": row["record_date"],
                    }
        return None
    except Exception as e:
        logger.error(f"查找班次打卡记录失败: {e}")
        return None


async def _check_shift_work_record(
    chat_id: int, user_id: int, checkin_type: str, shift: str, business_date: date
) -> bool:
    """检查指定班次的打卡记录"""
    return (
        await _find_shift_work_record(
            chat_id, user_id, checkin_type, shift, business_date
        )
        is not None
    )


async def _get_existing_work_record(
    chat_id: int, user_id: int, checkin_type: str, shift: str, business_date: date
) -> Optional[Dict]:
    """获取已存在的打卡记录详情"""
    return await _find_shift_work_record(
        chat_id, user_id, checkin_type, shift, business_date
    )


async def send_work_notification(
    chat_id: int,
    user_id: int,
    user_name: str,
    checkin_time: str,
    checkin_dt: datetime,
    expected_dt: datetime,
    action_text: str,
    status_type: str,
    fine_amount: int,
    trace_id: str,
    shift: str = None,
):

    try:
        group_data = await db.get_group_cached(chat_id)
        channel_id = group_data.get("channel_id") if group_data else None
        extra_work_group_id = await db.get_extra_work_group(chat_id)

        push_settings = await db.get_push_settings()
        enable_group_push = push_settings.get("enable_group_push", False)
        enable_channel_push = push_settings.get("enable_channel_push", True)

        chat_info = await bot_manager.bot.get_chat(chat_id)
        chat_title = getattr(chat_info, "title", str(chat_id))

        # 必须使用真实打卡时刻（含正确日期），不可把 HH:MM 拼到 expected_dt 的日期上，
        # 否则跨日打「昨晚夜班」会把今天凌晨算成昨天凌晨，出现「早到 17 小时」类错误。
        diff_seconds = int((checkin_dt - expected_dt).total_seconds())

        logger.debug(
            f"[{trace_id}] 📊 时间差计算:\n"
            f"   • 期望时间: {expected_dt.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"   • 打卡时间: {checkin_dt.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"   • 时间差: {diff_seconds}秒"
        )

        if action_text == "上班":
            if diff_seconds > 0:
                actual_status = "迟到"
                title = "⚠️ <b>上班迟到通知</b>"
                status_line = f"⏱️ 迟到 {MessageFormatter.format_duration(diff_seconds)}"
            elif diff_seconds < 0:
                actual_status = "早到"
                title = "✅ <b>上班通知</b>"
                status_line = (
                    f"⏱️ 早到 {MessageFormatter.format_duration(abs(diff_seconds))}"
                )
            else:
                actual_status = "准时"
                title = "✅ <b>上班通知</b>"
                status_line = "⏱️ 准时到达"
        else:
            if diff_seconds < 0:
                actual_status = "早退"
                title = "⚠️ <b>下班早退通知</b>"
                status_line = (
                    f"⏱️ 早退 {MessageFormatter.format_duration(abs(diff_seconds))}"
                )
            elif diff_seconds > 0:
                actual_status = "加班"
                title = "✅ <b>下班通知</b>"
                status_line = f"⏱️ 加班 {MessageFormatter.format_duration(diff_seconds)}"
            else:
                actual_status = "准时"
                title = "✅ <b>下班准时通知</b>"
                status_line = "⏱️ 准时下班"

        if shift is None:
            hour = expected_dt.hour
            shift = "day" if 6 <= hour < 18 else "night"

        shift_text = "白班" if shift == "day" else "夜班"

        channel_notif_text = (
            f"{title} <code>{shift_text}</code>\n"
            f"🏢 群组/班次：<code>{chat_title}</code> \n"
            f"{MessageFormatter.create_dashed_line()}\n"
            f"👤 用户：{MessageFormatter.format_user_link(user_id, user_name)}\n"
            f"⏰ 打卡时间：<code>{checkin_time}</code>\n"
            f"📅 {action_text}时间：<code>{expected_dt.strftime('%m/%d %H:%M')}</code>\n"
        )

        if action_text == "下班":
            try:
                business_date = await db.get_business_date(chat_id)

                shift_value = shift if shift else shift_text

                if shift_value in ["night", "夜班"]:
                    start_date = business_date - timedelta(days=1)
                    logger.info(
                        f"[{trace_id}] 🌙 夜班下班，查询日期范围: {start_date} 到 {business_date}"
                    )
                else:
                    start_date = business_date
                    logger.info(f"[{trace_id}] ☀️ 白班下班，查询日期: {business_date}")

                end_date = business_date

                work_records = await db.get_work_records_by_shift(
                    chat_id,
                    user_id,
                    shift_value,
                    start_date,
                    end_date,
                )

                work_start_time = None

                if work_records and work_records.get("work_start"):
                    work_start_time = work_records["work_start"][0]["checkin_time"]
                    logger.info(f"[{trace_id}] 📝 找到上班记录: {work_start_time}")

                if work_start_time:
                    start_dt = datetime.strptime(work_start_time, "%H:%M")
                    end_dt = datetime.strptime(checkin_time, "%H:%M")

                    if end_dt < start_dt:
                        end_dt += timedelta(days=1)
                        logger.info(f"[{trace_id}] 🔄 跨天工作: {start_dt} -> {end_dt}")

                    work_duration = int((end_dt - start_dt).total_seconds())
                    work_duration_str = MessageFormatter.format_duration(work_duration)

                    # 计算活动总时长（从 daily_statistics 表）
                    async with db.pool.acquire() as conn:
                        if shift_value in ["night", "夜班"]:
                            # 夜班：需要查询前一天的活动（根据您的数据）
                            query_date = business_date - timedelta(days=1)
                            logger.info(
                                f"[{trace_id}] 🌙 夜班活动查询日期: {query_date}"
                            )

                            activity_total = (
                                await conn.fetchval(
                                    """
                                    SELECT COALESCE(SUM(accumulated_time), 0)
                                    FROM user_activities 
                                    WHERE chat_id = $1 
                                      AND user_id = $2 
                                      AND activity_date = $3
                                      AND shift = 'night'
                                    """,
                                    chat_id,
                                    user_id,
                                    query_date,
                                )
                                or 0
                            )
                        else:
                            # 白班：查询当天
                            query_date = business_date
                            logger.info(
                                f"[{trace_id}] ☀️ 白班活动查询日期: {query_date}"
                            )

                            activity_total = (
                                await conn.fetchval(
                                    """
                                    SELECT COALESCE(SUM(accumulated_time), 0)
                                    FROM user_activities 
                                    WHERE chat_id = $1 
                                      AND user_id = $2 
                                      AND activity_date = $3
                                      AND shift = 'day'
                                    """,
                                    chat_id,
                                    user_id,
                                    query_date,
                                )
                                or 0
                            )

                        logger.info(
                            f"[{trace_id}] 📊 查询到的活动总时长: {activity_total}秒"
                        )

                    actual_work_duration = max(0, work_duration - activity_total)
                    actual_work_str = MessageFormatter.format_duration(
                        actual_work_duration
                    )
                    activity_total_str = MessageFormatter.format_duration(
                        activity_total
                    )

                    channel_notif_text += (
                        f"🕒 上班时间：<code>{work_start_time}</code>\n"
                    )
                    channel_notif_text += (
                        f"⏱️ 总工作时长：<code>{work_duration_str}</code>\n"
                    )
                    channel_notif_text += (
                        f"📊 活动总时长：<code>{activity_total_str}</code>\n"
                    )
                    channel_notif_text += (
                        f"💪 实际工作时间：<code>{actual_work_str}</code>\n"
                    )
                else:
                    logger.warning(f"[{trace_id}] ⚠️ 未找到上班记录")

            except Exception as e:
                logger.error(f"[{trace_id}] ❌ 计算工作时长失败: {e}")

        if fine_amount > 0:
            if action_text == "上班" and diff_seconds > 0:
                channel_notif_text += (
                    f"\n💰 <b>罚款信息</b>\n"
                    f"💸 罚款金额：<code>{fine_amount}</code> 泰铢\n"
                )
            elif action_text == "下班" and diff_seconds < 0:
                channel_notif_text += (
                    f"\n💰 <b>罚款信息</b>\n"
                    f"💸 罚款金额：<code>{fine_amount}</code> 泰铢\n"
                )

        channel_notif_text += f"{status_line}\n"

        extra_notif_text = f"<code>{shift_text}</code> {MessageFormatter.format_user_link(user_id, user_name)} {action_text} 了！\n"

        if fine_amount > 0:
            if action_text == "上班" and diff_seconds > 0:
                extra_notif_text += (
                    f"⚠️ 迟到 {MessageFormatter.format_duration(diff_seconds)}，"
                    f"💰罚款金额：<code>{fine_amount}</code> 泰铢"
                )
            elif action_text == "下班" and diff_seconds < 0:
                extra_notif_text += (
                    f"⚠️ 早退 {MessageFormatter.format_duration(abs(diff_seconds))}，\n"
                    f"💰罚款金额：<code>{fine_amount}</code> 泰铢"
                )

        logger.info(
            f"[{trace_id}] 📊 通知详情:\n"
            f"   • 用户: {user_name}({user_id})\n"
            f"   • 动作: {action_text}\n"
            f"   • 状态: {actual_status}\n"
            f"   • 打卡时间: {checkin_time}\n"
            f"   • 期望时间: {expected_dt.strftime('%m/%d %H:%M')}\n"
            f"   • 时间差: {diff_seconds}秒 ({MessageFormatter.format_duration(abs(diff_seconds))})\n"
            f"   • 罚款: {fine_amount}\n"
            f"   • 班次: {shift_text}"
        )

        async def safe_send(target_id: int, text: str, target_desc: str = ""):
            try:
                logger.info(f"[{trace_id}] 📤 尝试发送到 {target_desc} ID: {target_id}")

                try:
                    target_info = await bot_manager.bot.get_chat(target_id)
                    logger.info(
                        f"[{trace_id}] ℹ️ 目标群组信息: 标题='{target_info.title}', 类型={target_info.type}"
                    )
                except Exception as e:
                    logger.error(
                        f"[{trace_id}] ❌ 无法获取目标群组信息，机器人可能不在群组中: {e}"
                    )
                    return

                await bot_manager.bot.send_message(target_id, text, parse_mode="HTML")

                if target_desc:
                    logger.info(f"[{trace_id}] ✅ {target_desc}发送成功({target_id})")
                else:
                    logger.info(f"[{trace_id}] ✅ 发送成功({target_id})")

            except Exception as e:
                logger.error(f"[{trace_id}] ❌ 发送到 {target_desc} 失败: {e}")

                try:
                    logger.info(f"[{trace_id}] 🔄 尝试使用 bot_manager 重试...")
                    if bot_manager and hasattr(bot_manager, "send_message_with_retry"):
                        success = await bot_manager.send_message_with_retry(
                            target_id, text, parse_mode="HTML"
                        )
                        if success:
                            logger.info(
                                f"[{trace_id}] ✅ bot_manager {target_desc}发送成功({target_id})"
                            )
                            return
                except Exception as e2:
                    logger.error(f"[{trace_id}] ❌ bot_manager 重试也失败: {e2}")

        if channel_id and enable_channel_push:
            await safe_send(channel_id, channel_notif_text, "频道")
        elif channel_id:
            logger.info(f"[{trace_id}] ℹ️ 推送设置已禁用频道通知")

        if extra_work_group_id:
            logger.info(f"[{trace_id}] 📤 发送到额外群组: {extra_work_group_id}")
            await safe_send(extra_work_group_id, extra_notif_text, "额外上下班群组")
        else:
            logger.info(f"[{trace_id}] ℹ️ 没有配置额外群组")

    except Exception as e:
        logger.error(
            f"[{trace_id}] ❌ send_work_notification总异常: {e}", exc_info=True
        )

