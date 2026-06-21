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
from database import db, parse_sql_row_count
from constants import (
    BTN_WORK_START_DAY, BTN_WORK_START_NIGHT, BTN_WORK_END, WORK_BUTTONS,
    SPECIAL_BUTTONS, ACTIVITY_MAP, AdminStates, start_time,
)
from constants import active_back_processing
from bot_manager import bot_manager
from keyboards import get_main_keyboard, get_admin_keyboard, is_admin, calculate_work_fine, build_inline_back_keyboard
from i18n import get_lang_mode
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

logger = logging.getLogger("GroupCheckInBot")

async def get_user_lock(chat_id: int, uid: int):
    """获取用户锁的便捷函数"""
    return await user_lock_manager.get_lock(chat_id, uid)


async def auto_end_current_activity(
    chat_id: int, uid: int, user_data: dict, now: datetime, message: types.Message
):
    """自动结束当前活动 - 增强班次检查"""
    try:
        act = user_data["current_activity"]
        start_time_dt = datetime.fromisoformat(user_data["activity_start_time"])
        activity_shift = user_data.get("shift", "day")  # 活动的原始班次

        # ===== 获取当前操作的班次 =====
        # 从消息中获取当前操作的班次（需要在 process_work_checkin 中设置）
        current_operation_shift = getattr(message, "_current_shift", None)

        # 如果无法从消息获取，尝试从班次状态表获取用户当前活跃的班次
        if not current_operation_shift:
            active_shift = await db.get_user_active_shift(chat_id, uid)
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

        # 获取班次信息用于日期判定
        shift_info = await db.determine_shift_for_time(
            chat_id=chat_id,
            current_time=now,
            checkin_type="work_end",
            active_shift=activity_shift,
            active_record_date=start_time_dt.date(),
        )

        forced_date = None
        if shift_info:
            forced_date = shift_info.get("record_date")
            logger.info(
                f"📅 自动结束活动 - 班次判定: {shift_info.get('shift_detail')}, "
                f"记录日期: {forced_date}"
            )
        else:
            forced_date = now.date()

        # 完成活动
        await db.complete_user_activity(
            chat_id=chat_id,
            user_id=uid,
            activity=act,
            elapsed_time=elapsed,
            fine_amount=0,
            is_overtime=False,
            shift=activity_shift,  # 使用活动的原始班次
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
            SELECT 1 FROM work_records
            WHERE chat_id = $1
              AND user_id = $2
              AND checkin_type = 'work_end'
              AND shift = $3
              AND record_date = $4
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

    user_current_shift = await db.get_user_current_shift(chat_id, uid)

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

    shift_state = await db.get_user_shift_state(chat_id, uid, check_shift)

    if not shift_state:
        shift_text = "白班" if check_shift == "day" else "夜班"
        return (
            False,
            f"❌ 您当前没有进行中的{shift_text}班次，请先打{shift_text}上班卡！",
        )

    shift_start_time = shift_state["shift_start_time"]
    if isinstance(shift_start_time, str):
        try:
            shift_start_time = datetime.fromisoformat(
                shift_start_time.replace("Z", "+00:00")
            )
        except:
            shift_start_time = datetime.strptime(
                shift_start_time, "%Y-%m-%d %H:%M:%S.%f%z"
            )

    if now - shift_start_time > timedelta(hours=16):
        await db.clear_user_shift_state(chat_id, uid, check_shift)
        shift_text = "白班" if check_shift == "day" else "夜班"
        return False, f"❌ 您的{shift_text}班次已过期（超过16小时），请重新上班打卡！"

    shift_info = await db.determine_shift_for_time(
        chat_id=chat_id,
        current_time=now,
        checkin_type="activity",
        active_shift=check_shift,
    )

    if shift_info is None or shift_info.get("shift_detail") is None:
        shift_config = await db.get_shift_config(chat_id)
        day_start = shift_config.get("day_start", "09:00")
        day_end = shift_config.get("day_end", "21:00")

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
            SELECT 1 FROM work_records 
            WHERE chat_id = $1 
              AND user_id = $2 
              AND checkin_type = 'work_end'
              AND shift = $3
              AND record_date = $4
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

            checkin_message_id = await db.get_user_checkin_message_id(chat_id, uid)
            if checkin_message_id:
                try:
                    return await current_bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        parse_mode="HTML",
                        reply_markup=kb,
                        reply_to_message_id=checkin_message_id,
                    )
                except Exception as e:
                    logger.warning(f"⚠️ 引用发送失败，重试一次: {e}")
                    await asyncio.sleep(1)
                    try:
                        return await current_bot.send_message(
                            chat_id=chat_id,
                            text=text,
                            parse_mode="HTML",
                            reply_markup=kb,
                            reply_to_message_id=checkin_message_id,
                        )
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
            user_lock = await user_lock_manager.get_lock(chat_id, uid)
            async with user_lock:
                user_data = await db.get_user_cached(chat_id, uid)
                if not user_data or user_data["current_activity"] != act:
                    break

                start_time = datetime.fromisoformat(user_data["activity_start_time"])
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

                if overtime_seconds >= 120 * 60 and not force_back_sent:
                    force_back_sent = True
                    fine_amount = await calculate_fine(act, 120)
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
                    break_data = {"should_break": False}

                if 0 < remaining <= 60 and not one_minute_warning_sent:
                    msg = (
                        f"⏳ <b>即将超时警告</b>\n"
                        f"👤 {MessageFormatter.format_user_link(uid, nickname)} \n"
                        f"📊 班次： <code>{shift_text}</code> \n"
                        f"🕓 本次 {MessageFormatter.format_copyable_text(act)} 还有 <code>1</code> 分钟！\n"
                        f"💡 请及时回座，避免超时罚款"
                    )
                    await send_group_message(msg, build_quick_back_kb())
                    one_minute_warning_sent = True

                if remaining <= 0:
                    overtime_minutes = int(-remaining // 60)
                    msg = None

                    if overtime_minutes == 0 and not timeout_immediate_sent:
                        timeout_immediate_sent = True
                        msg = (
                            f"⚠️ <b>超时警告</b>\n"
                            f"👤 {MessageFormatter.format_user_link(uid, nickname)} \n"
                            f"📊 班次： <code>{shift_text}</code> \n"
                            f"🕓 本次 {MessageFormatter.format_copyable_text(act)} 已超时\n"
                            f"🏃‍♂️ 请立即回座，避免产生更多罚款！"
                        )
                        last_reminder_minute = 0

                    elif overtime_minutes == 5 and not timeout_5min_sent:
                        timeout_5min_sent = True
                        msg = (
                            f"🔔 <b>超时警告</b> \n"
                            f"👤 {MessageFormatter.format_user_link(uid, nickname)} \n"
                            f"📊 班次： <code>{shift_text}</code> \n"
                            f"🕓 本次 {MessageFormatter.format_copyable_text(act)} 已超时 <code>{overtime_minutes}</code> 分钟！\n"
                            f"😤 罚款正在累积，请立即回座！"
                        )
                        last_reminder_minute = 5

                    elif (
                        overtime_minutes >= 10
                        and overtime_minutes % 10 == 0
                        and overtime_minutes != last_reminder_minute
                    ):
                        last_reminder_minute = overtime_minutes
                        msg = (
                            f"🚨 <b>超时警告</b>\n"
                            f"👤 {MessageFormatter.format_user_link(uid, nickname)} \n"
                            f"📊 班次： <code>{shift_text}</code> \n"
                            f"🕓 本次 {MessageFormatter.format_copyable_text(act)} 已超时 <code>{overtime_minutes}</code> 分钟！\n"
                            f"💢 请立刻回座，避免产生更多罚款！"
                        )

                    if msg:
                        await send_group_message(msg, build_quick_back_kb())

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


async def start_activity(message: types.Message, act: str):
    """开始活动打卡"""
    chat_id = message.chat.id
    uid = message.from_user.id
    flow_start = time.time()

    watchdog = Watchdog(timeout=30, name=f"start_activity_{chat_id}_{uid}")

    async def _start_activity_impl():
        user_lock = await user_lock_manager.get_lock(chat_id, uid)
        async with user_lock:
            watchdog.feed()

            is_admin_user = await is_admin(uid)

            async def reply(text, **kwargs):
                kwargs.setdefault("reply_to_message_id", message.message_id)
                kwargs.setdefault(
                    "reply_markup",
                    await get_main_keyboard(chat_id=chat_id, show_admin=is_admin_user),
                )
                return await message.answer(text, **kwargs)

            await db.init_group(chat_id)
            business_date = await reset_daily_data_if_needed(chat_id, uid)
            await db.init_user(
                chat_id, uid, message.from_user.full_name, business_date=business_date
            )

            if not await db.activity_exists(act):
                await reply(f"❌ 活动 '{act}' 不存在")
                return

            user_data = await db.get_user_cached(chat_id, uid)
            if user_data and user_data.get("current_activity"):
                await reply(
                    Config.MESSAGES["has_activity"].format(
                        user_data["current_activity"]
                    )
                )
                return

            name = message.from_user.full_name
            now = db.get_beijing_time()

            user_shift_state = await db.get_user_active_shift(chat_id, uid)
            if not user_shift_state:
                await reply("❌ 您当前没有进行中的班次，请先打上班卡！")
                return

            shift_start_time = user_shift_state["shift_start_time"]
            if isinstance(shift_start_time, str):
                try:
                    shift_start_time = datetime.fromisoformat(
                        shift_start_time.replace("Z", "+00:00")
                    )
                except:
                    shift_start_time = datetime.strptime(
                        shift_start_time, "%Y-%m-%d %H:%M:%S.%f%z"
                    )

            if now - shift_start_time > timedelta(hours=16):
                await db.clear_user_shift_state(chat_id, uid, user_shift_state["shift"])
                await reply("❌ 您的班次已过期（超过16小时），请重新上班打卡！")
                return

            watchdog.feed()

            shift_info = await db.determine_shift_for_time(
                chat_id=chat_id,
                current_time=now,
                checkin_type="activity",
                active_shift=user_shift_state["shift"],
                active_record_date=user_shift_state["record_date"],
            )

            if shift_info:
                current_shift = shift_info["shift"]
                record_date = shift_info["record_date"]
                shift_detail = shift_info.get("shift_detail")
            else:
                current_shift = user_shift_state["shift"]
                record_date = user_shift_state["record_date"]
                shift_detail = user_shift_state.get("shift_detail", current_shift)
                logger.warning(f"⚠️ determine_shift_for_time 返回空，使用班次状态兜底")
            shift_text = "白班" if current_shift == "day" else "夜班"

            logger.info(
                f"🔄 [开始活动] 使用状态模型: {shift_text}, "
                f"详情={shift_detail}, 记录日期={record_date}"
            )

            can_perform, reason = await _check_work_end_blocks_activity(
                chat_id, uid, current_shift, record_date
            )
            if not can_perform:
                await reply(reason)
                return

            user_limit_task = asyncio.create_task(db.get_activity_user_limit(act))
            count_task = asyncio.create_task(
                check_activity_limit_by_shift(
                    chat_id,
                    uid,
                    act,
                    current_shift,
                    query_date=record_date,
                    skip_init=True,
                )
            )

            user_limit = await user_limit_task
            if user_limit > 0:
                current_users = await db.get_current_activity_users(chat_id, act)
                if current_users >= user_limit:
                    await reply(
                        f"❌ 活动 '<code>{act}</code>' 人数已满！\n\n"
                        f"📊 限制人数：<code>{user_limit}</code> 人\n"
                        f"• 当前进行：<code>{current_users}</code> 人\n"
                        f"• 剩余名额：<code>0</code> 人",
                        parse_mode="HTML",
                    )
                    return

            watchdog.feed()

            can_start, current_count, max_times = await count_task
            if not can_start:
                from handover_manager import handover_manager

                limit_msg = (
                    f"❌ {shift_text}的 '<code>{act}</code>' 次数已达上限\n\n"
                    f"📊 当前次数：<code>{current_count}</code> / <code>{max_times}</code>"
                )
                period = await handover_manager.determine_current_period(chat_id, now)
                if period.get("is_handover"):
                    effective_cycle = await handover_manager.get_user_effective_cycle(
                        chat_id, uid, period
                    )
                    if effective_cycle == 1 and period.get("next_reset_time"):
                        reset_str = period["next_reset_time"].strftime("%H:%M")
                        limit_msg += (
                            f"\n\n🔄 换班日：活动次数将在 <code>{reset_str}</code> 重置"
                        )
                await reply(limit_msg, parse_mode="HTML")
                return

            await db.update_user_activity(
                chat_id, uid, act, str(now), name, current_shift
            )

            time_limit = await db.get_activity_time_limit(act)
            await timer_manager.start_timer(
                chat_id, uid, act, time_limit, shift=current_shift
            )

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

            sent_message = await message.answer(
                activity_message,
                reply_to_message_id=message.message_id,
                reply_markup=inline_back_kb,
                parse_mode="HTML",
            )

            await db.update_user_checkin_message(chat_id, uid, sent_message.message_id)
            await db.update_pending_reply_message(chat_id, uid, sent_message.message_id)

            logger.info(
                f"📝 用户 {uid} 开始活动 {act}（{shift_text}），消息ID: {sent_message.message_id}, "
                f"记录日期: {record_date}, 班次详情: {shift_detail}, "
                f"耗时: {time.time() - flow_start:.2f}s"
            )

            if act == "吃饭":
                try:
                    notification_text = (
                        f"🍽️ <b>吃饭通知</b> <code>{shift_text}</code>\n"
                        f" {MessageFormatter.format_user_link(uid, name)} 去吃饭了\n"
                        f"⏰ 时间：<code>{now.strftime('%H:%M:%S')}</code>\n"
                    )
                    asyncio.create_task(
                        notification_service.send_notification(
                            chat_id, notification_text
                        )
                    )
                    logger.info(f"📣 已触发用户 {uid}（{shift_text}）的 {act} 推送")
                except Exception as e:
                    logger.error(f"❌ {act} 推送失败: {e}")

    try:
        return await watchdog.run(_start_activity_impl())
    except asyncio.CancelledError:
        logger.error(f"⏰ 开始活动操作超时: {chat_id}-{uid}")
        try:
            await message.answer("⏰ 开始活动操作超时，请重试")
        except Exception:
            pass
        return
    except Exception as e:
        logger.error(f"❌ 开始活动异常: {chat_id}-{uid}-{act}: {e}", exc_info=True)
        try:
            await message.answer(
                "⚠️ 打卡处理失败，请稍后重试。",
                reply_to_message_id=message.message_id,
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
        user_lock = await user_lock_manager.get_lock(chat_id, uid)
        async with user_lock:
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
        if isinstance(lock_time, (int, float)) and time.time() - lock_time > 30:
            logger.warning(
                f"⚠️ [回座] 强制释放过期锁: {key} (持有时间: {time.time()-lock_time:.1f}秒)"
            )
            active_back_processing.pop(key, None)
        else:
            await message.answer(
                "⚠️ 您的回座请求正在处理中，请稍候。",
                reply_to_message_id=message.message_id,
            )
            return

    active_back_processing[key] = time.time()
    back_sent = False

    try:
        now = db.get_beijing_time()

        await db.init_group(chat_id)
        business_date = await reset_daily_data_if_needed(chat_id, uid)
        await db.init_user(chat_id, uid, business_date=business_date)

        user_data = await db.get_user_cached(chat_id, uid)
        logger.debug(f"🔍 用户数据: {user_data}")

        if not user_data or not user_data.get("current_activity"):
            await message.answer(
                Config.MESSAGES["no_activity"],
                reply_markup=await get_main_keyboard(
                    chat_id=chat_id, show_admin=await is_admin(uid)
                ),
                reply_to_message_id=message.message_id,
            )
            return

        act = user_data["current_activity"]
        activity_start_time_str = user_data["activity_start_time"]
        nickname = user_data.get("nickname", "未知用户")

        original_shift = user_data.get("shift", "day")

        checkin_message_id = user_data.get("checkin_message_id")
        if not checkin_message_id:
            checkin_message_id = await db.get_user_checkin_message_id(chat_id, uid)
        logger.info(f"📝 回座: 用户 {uid}，原打卡消息ID: {checkin_message_id}")

        if not checkin_message_id:
            logger.warning(f"⚠️ 用户 {uid} 没有找到打卡消息ID")

        start_time_dt = None
        try:
            if activity_start_time_str:
                clean_str = str(activity_start_time_str).strip()
                if clean_str.endswith("Z"):
                    clean_str = clean_str.replace("Z", "+00:00")
                try:
                    start_time_dt = datetime.fromisoformat(clean_str)
                    if start_time_dt.tzinfo is None:
                        start_time_dt = beijing_tz.localize(start_time_dt)
                except ValueError:
                    formats = [
                        "%Y-%m-%d %H:%M:%S.%f",
                        "%Y-%m-%d %H:%M:%S",
                        "%Y-%m-%d %H:%M",
                        "%m/%d %H:%M:%S",
                        "%m/%d %H:%M",
                    ]
                    for fmt in formats:
                        try:
                            start_time_dt = datetime.strptime(clean_str, fmt)
                            if fmt.startswith("%m/%d"):
                                start_time_dt = start_time_dt.replace(year=now.year)
                            break
                        except ValueError:
                            continue
                    if start_time_dt and start_time_dt.tzinfo is None:
                        start_time_dt = beijing_tz.localize(start_time_dt)
        except Exception as e:
            logger.error(f"解析开始时间失败: {activity_start_time_str}, 错误: {e}")

        if not start_time_dt:
            logger.warning("时间解析失败，使用当前时间作为备用")
            start_time_dt = now

        user_shift_state = await db.get_user_active_shift(chat_id, uid)

        if user_shift_state:
            final_shift = user_shift_state["shift"]
            record_date = user_shift_state["record_date"]
            shift_start_time = user_shift_state["shift_start_time"]

            if isinstance(shift_start_time, str):
                try:
                    shift_start_time = datetime.fromisoformat(
                        shift_start_time.replace("Z", "+00:00")
                    )
                except:
                    shift_start_time = datetime.strptime(
                        shift_start_time, "%Y-%m-%d %H:%M:%S.%f%z"
                    )

            logger.info(
                f"📝 回座使用班次状态: {final_shift}, "
                f"记录日期={record_date}, 班次开始时间={shift_start_time.strftime('%Y-%m-%d %H:%M:%S')}"
            )

            shift_config = await db.get_shift_config(chat_id)
            day_end_str = shift_config.get("day_end", "21:00")
            day_end_hour, day_end_min = map(int, day_end_str.split(":"))

            if final_shift == "day":
                forced_date = record_date
                shift_detail = "day"
            else:
                day_end_dt = shift_start_time.replace(
                    hour=day_end_hour, minute=day_end_min, second=0, microsecond=0
                )

                if shift_start_time >= day_end_dt:
                    forced_date = record_date
                    shift_detail = "night_tonight"
                else:
                    forced_date = record_date
                    shift_detail = "night_last"

            logger.info(f"📝 班次详情: {shift_detail}, 强制日期={forced_date}")

        else:
            logger.warning(f"⚠️ 用户 {uid} 没有活跃班次状态，使用原始逻辑")

            if shift:
                final_shift = shift
                logger.info(f"📝 使用传入班次: {final_shift}")
            else:
                final_shift = original_shift
                logger.info(f"📝 使用用户原始班次: {final_shift}")

            shift_info = await db.determine_shift_for_time(
                chat_id=chat_id,
                current_time=start_time_dt,
                checkin_type="activity",
                active_shift=final_shift,
            )

            if shift_info:
                determined_shift = shift_info["shift"]
                shift_detail = shift_info["shift_detail"]
                forced_date = shift_info["record_date"]

                if determined_shift != final_shift:
                    logger.info(
                        f"📝 班次修正: 原班次={final_shift}, 判定班次={determined_shift}"
                    )
                    final_shift = determined_shift
            else:
                logger.warning("⚠️ determine_shift_for_time 返回 None，使用保底逻辑")
                shift_config = await db.get_shift_config(chat_id)

                if final_shift == "night":
                    day_start_str = shift_config.get("day_start", "09:00")
                    day_start_hour, day_start_min = map(int, day_start_str.split(":"))
                    day_start_dt = start_time_dt.replace(
                        hour=day_start_hour,
                        minute=day_start_min,
                        second=0,
                        microsecond=0,
                    )

                    if start_time_dt >= day_start_dt:
                        forced_date = start_time_dt.date()
                        shift_detail = "night_tonight"
                    else:
                        forced_date = start_time_dt.date() - timedelta(days=1)
                        shift_detail = "night_last"
                else:
                    forced_date = start_time_dt.date()
                    shift_detail = "day"

        shift_text_map = {
            "day": "白班",
            "night": "夜班",
            "night_last": "昨晚夜班",
            "night_tonight": "今晚夜班",
        }
        shift_text = shift_text_map.get(shift_detail, "白班")

        logger.info(
            f"📅 最终判定: 班次={final_shift}, 归属={shift_detail}, "
            f"强制日期={forced_date}"
        )

        elapsed = int((now - start_time_dt).total_seconds())

        from handover_manager import handover_manager

        record_result = await handover_manager.record_activity(
            chat_id, uid, act, elapsed, now
        )

        business_date = record_result["business_date"]

        time_limit_task = asyncio.create_task(db.get_activity_time_limit(act))
        time_limit_minutes = await time_limit_task
        time_limit_seconds = time_limit_minutes * 60

        is_overtime = elapsed > time_limit_seconds
        overtime_seconds = max(0, int(elapsed - time_limit_seconds))
        overtime_minutes = overtime_seconds / 60

        fine_amount = 0
        if is_overtime and overtime_seconds > 0:
            fine_amount = await calculate_fine(act, overtime_minutes)

        elapsed_time_str = MessageFormatter.format_time(int(elapsed))
        time_str = now.strftime("%m/%d %H:%M:%S")
        activity_start_time_for_notification = activity_start_time_str

        logger.info(f"📝 完成活动 - 班次: {final_shift}, 强制日期: {forced_date}")
        await db.complete_user_activity(
            chat_id,
            uid,
            act,
            int(elapsed),
            fine_amount,
            is_overtime,
            final_shift,
            forced_date=business_date,
        )

        await timer_manager.cancel_timer(
            chat_id=chat_id, uid=uid, preserve_message=True
        )

        # 获取用户总数据
        user_data_task = asyncio.create_task(db.get_user_cached(chat_id, uid))

        # 获取今天的统计数据（使用强制归档的日期）
        async with db.pool.acquire() as conn:
            # 查询今天的所有活动记录（从 user_activities 表）
            today_activities_rows = await conn.fetch(
                """
                SELECT activity_name, activity_count, accumulated_time
                FROM user_activities
                WHERE chat_id = $1 AND user_id = $2 AND activity_date = $3
                """,
                chat_id,
                uid,
                forced_date,  # 使用强制归档的日期
            )

            # 查询今天的累计时间和次数
            today_stats_row = await conn.fetchrow(
                """
                SELECT 
                    COALESCE(SUM(accumulated_time), 0) as total_time,
                    COALESCE(SUM(activity_count), 0) as total_count
                FROM user_activities
                WHERE chat_id = $1 
                  AND user_id = $2 
                  AND activity_date = $3
                """,
                chat_id,
                uid,
                forced_date,
            )

        # 等待用户总数据
        user_data = await user_data_task

        # 构建今天的活动计数
        today_activities = {}
        for row in today_activities_rows:
            act_name = row["activity_name"]
            today_activities[act_name] = {
                "count": row["activity_count"],
                "time": row["accumulated_time"],
            }

        # 获取今天的总时间和次数
        today_total_time = today_stats_row["total_time"] if today_stats_row else 0
        today_total_count = today_stats_row["total_count"] if today_stats_row else 0

        # 用于显示的活动计数（今天的数据）
        activity_counts = {
            act: info.get("count", 0) for act, info in today_activities.items()
        }

        back_message = MessageFormatter.format_back_message(
            user_id=uid,
            user_name=user_data.get("nickname", nickname),
            activity=act,
            time_str=time_str,
            elapsed_time=elapsed_time_str,
            total_activity_time=MessageFormatter.format_time(
                int(today_activities.get(act, {}).get("time", 0))
            ),
            total_time=MessageFormatter.format_time(
                int(today_total_time)
            ),  # 今天的累计时间
            activity_counts=activity_counts,
            total_count=int(today_total_count),  # 今天的活动次数
            is_overtime=is_overtime,
            overtime_seconds=overtime_seconds,
            fine_amount=fine_amount,
        )

        reply_target_id = (
            user_trigger_message.message_id
            if user_trigger_message
            else message.message_id
        )

        back_msg = await message.answer(
            back_message,
            reply_to_message_id=reply_target_id,
            reply_markup=await get_main_keyboard(
                chat_id, await is_admin(uid)
            ),
            parse_mode="HTML",
        )
        back_sent = True

        await db.clear_user_checkin_message(chat_id, uid)
        await db.update_pending_reply_message(chat_id, uid, back_msg.message_id)

        if is_overtime and fine_amount > 0:
            group_data = await db.get_group_cached(chat_id)
            if group_data.get("channel_id"):
                notification_user_data = user_data.copy() if user_data else {}
                notification_user_data["activity_start_time"] = (
                    activity_start_time_for_notification
                )
                notification_user_data["nickname"] = nickname
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
                except Exception:
                    pass

                eat_end_notification_text = (
                    f"🍽️ <b>吃饭结束通知</b>\n"
                    f"{MessageFormatter.format_user_link(uid, user_data.get('nickname', '用户'))} 吃饭回来了\n"
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
                f"业务日期:{business_date} | 强制日期:{forced_date} | "
                f"超时:{is_overtime} | 罚款:{fine_amount}"
            )
        except Exception as log_err:
            logger.warning(f"回座完成日志记录失败: {log_err}")

    except Exception as e:
        logger.error(f"回座处理异常: {e}")
        logger.error(traceback.format_exc())
        if not back_sent:
            await message.answer(
                "❌ 回座失败，请稍后重试。", reply_to_message_id=message.message_id
            )
        else:
            logger.error("回座主消息已发送，跳过后续失败提示")

    finally:
        # 先保存key状态
        had_lock = key in active_back_processing

        # 清理消息ID（可能和定时器冲突，但日志重要）
        try:
            current_message_id = await db.get_user_checkin_message_id(chat_id, uid)
            if current_message_id:
                # 快速检查用户是否还有活动
                final_user_data = await db.get_user_cached(chat_id, uid)
                if not final_user_data or not final_user_data.get("current_activity"):
                    await db.clear_user_checkin_message(chat_id, uid)
                    logger.info(f"🧹 finally 清理用户 {uid} 的打卡消息ID")
                else:
                    logger.debug(f"用户 {uid} 活动仍在进行，保留消息ID")
        except Exception as e:
            logger.warning(f"⚠️ finally 清理失败: {e}")

        # 释放处理锁
        if had_lock:
            active_back_processing.pop(key, None)
            logger.info(f"✅ [回座锁释放] key={key}")

        duration = round(time.time() - start_time, 2)
        logger.info(f"✅ [回座结束] key={key}，总耗时 {duration}s")


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

