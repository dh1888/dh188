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
from aiogram.types import ForceReply, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from functools import wraps

from config import Config, beijing_tz
from database import db, parse_sql_row_count
from i18n import resolve_button, ACTIVITY_VI, resolve_activity_name
from constants import SPECIAL_BUTTONS, ACTIVITY_MAP, AdminStates, active_back_processing
from keyboards import get_main_keyboard, get_admin_keyboard, is_admin, calculate_work_fine
from decorators import admin_required
from performance import (
    global_cache, track_performance, with_retry, message_deduplicate,
    rate_limit, user_rate_limit,
)
from utils import (
    MessageFormatter, user_lock_manager, timer_manager, notification_service,
    calculate_fine, get_quote_id, get_beijing_time,
)
from fault_tolerance import Watchdog
from handover_manager import handover_manager
from i18n import ACTIVITY_VI

logger = logging.getLogger("GroupCheckInBot")

from work_checkin import process_work_checkin
from activity_service import (
    start_activity, process_back, check_activity_limit_by_shift,
    has_active_activity,
)
from activity_commands import is_activity_command, resolve_activity_command, extract_command
from reset_service import reset_daily_data_if_needed
from export_service import export_and_push_csv
# ========== 消息处理器 ==========
@rate_limit(rate=5, per=60)
@message_deduplicate
async def cmd_start(message: types.Message):
    """开始命令"""
    uid = message.from_user.id
    is_admin_user = await is_admin(uid)

    await message.answer(
        Config.MESSAGES["welcome"],
        reply_markup=await get_main_keyboard(message.chat.id, is_admin_user),
        reply_to_message_id=message.message_id,
    )


@rate_limit(rate=5, per=60)
async def cmd_menu(message: types.Message):
    """显示主菜单"""
    uid = message.from_user.id
    await message.answer(
        "📋 主菜单",
        reply_markup=await get_main_keyboard(
            chat_id=message.chat.id, show_admin=await is_admin(uid)
        ),
        reply_to_message_id=message.message_id,
    )


@rate_limit(rate=5, per=60)
async def cmd_help(message: types.Message):
    """帮助命令"""
    uid = message.from_user.id

    help_text = (
        "📋 打卡机器人使用帮助\n\n"
        "🟢 开始活动打卡：\n"
        "• 直接输入活动名称\n"
        "• 或使用命令：/ci 活动名\n"
        "• 或点击下方活动按钮\n\n"
        "🔴 结束活动回座：\n"
        "• 直接输入：回座\n"
        "• 或使用命令：/at\n\n"
        "🕒 上下班打卡（双班模式）：\n"
        "• ⚫ 夜班上班 - 夜班上班打卡\n"
        "• 🟢 白班上班 - 白班上班打卡\n"
        "• 🔴 下班 - 下班打卡\n"
        "• /workstart - 自动判定班次上班\n"
        "• /workend - 下班打卡\n\n"
        "🔄 换班设置：\n"
        "• /sethandoverday [日期] - 设置换班日期\n"
        "• /sethandoverday status - 查看换班配置\n\n"
        "📊 查看记录：\n"
        "• 点击 📊 我的记录 查看个人统计\n"
        "• 点击 🏆 排行榜 查看群内排名\n\n"
        "🔧 其他命令：\n"
        "• /start - 开始使用机器人\n"
        "• /menu - 显示主菜单\n"
        "• /help - 显示此帮助信息"
    )

    await message.answer(
        help_text,
        reply_markup=await get_main_keyboard(
            chat_id=message.chat.id, show_admin=await is_admin(uid)
        ),
        reply_to_message_id=message.message_id,
        parse_mode="HTML",
    )


@rate_limit(rate=10, per=60)
@track_performance("cmd_myinfo")
async def handle_myinfo_command(message: types.Message):
    """处理 /myinfo 命令"""
    chat_id = message.chat.id
    uid = message.from_user.id

    args = message.text.split()
    if len(args) == 2:
        await handle_myinfo_shift_command(message)
        return

    user_lock = await user_lock_manager.get_lock(chat_id, uid)
    async with user_lock:
        await show_history(message)


@rate_limit(rate=10, per=60)
@track_performance("cmd_myinfo_shift")
async def handle_myinfo_shift_command(message: types.Message):
    """处理 /myinfo <shift> 命令"""
    args = message.text.split()
    chat_id = message.chat.id
    uid = message.from_user.id

    if len(args) != 2:
        await message.answer(
            "❌ 用法：/myinfo <shift>\n" "💡 参数：day (白班) 或 night (夜班)",
            reply_to_message_id=message.message_id,
        )
        return

    shift = args[1].lower()
    if shift not in ["day", "night"]:
        await message.answer(
            "❌ 班次参数错误\n" "💡 请使用：day (白班) 或 night (夜班)",
            reply_to_message_id=message.message_id,
        )
        return

    shift_config = await db.get_shift_config(chat_id)
    if not shift_config.get("dual_mode", True):
        await message.answer(
            "❌ 当前群组未启用双班模式\n"
            "💡 请联系管理员使用 /setdualmode 命令开启双班模式",
            reply_to_message_id=message.message_id,
        )
        return

    user_lock = await user_lock_manager.get_lock(chat_id, uid)
    async with user_lock:
        await show_history(message, shift)


@rate_limit(rate=10, per=60)
@track_performance("cmd_myinfo_day")
async def handle_myinfo_day_command(message: types.Message):
    """处理 /myinfoday 命令"""
    chat_id = message.chat.id
    uid = message.from_user.id

    shift_config = await db.get_shift_config(chat_id)
    if not shift_config.get("dual_mode", True):
        await message.answer(
            "❌ 当前群组未启用双班模式\n"
            "💡 请联系管理员使用 /setdualmode 命令开启双班模式",
            reply_to_message_id=message.message_id,
        )
        return

    user_lock = await user_lock_manager.get_lock(chat_id, uid)
    async with user_lock:
        await show_history(message, "day")


@rate_limit(rate=10, per=60)
@track_performance("cmd_myinfo_night")
async def handle_myinfo_night_command(message: types.Message):
    """处理 /myinfonight 命令"""
    chat_id = message.chat.id
    uid = message.from_user.id

    shift_config = await db.get_shift_config(chat_id)
    if not shift_config.get("dual_mode", True):
        await message.answer(
            "❌ 当前群组未启用双班模式\n"
            "💡 请联系管理员使用 /setdualmode 命令开启双班模式",
            reply_to_message_id=message.message_id,
        )
        return

    user_lock = await user_lock_manager.get_lock(chat_id, uid)
    async with user_lock:
        await show_history(message, "night")


@user_rate_limit(rate=10, per=60)
@rate_limit(rate=30, per=60)
@track_performance("cmd_ranking")
async def handle_ranking_command(message: types.Message):
    """处理 /ranking 命令"""
    chat_id = message.chat.id
    uid = message.from_user.id

    args = message.text.split()
    if len(args) == 2:
        await handle_ranking_shift_command(message)
        return

    user_lock = await user_lock_manager.get_lock(chat_id, uid)
    async with user_lock:
        await show_rank(message)


@rate_limit(rate=10, per=60)
@track_performance("cmd_ranking_shift")
async def handle_ranking_shift_command(message: types.Message):
    """处理 /ranking <shift> 命令"""
    args = message.text.split()
    chat_id = message.chat.id
    uid = message.from_user.id

    if len(args) != 2:
        await message.answer(
            "❌ 用法：/ranking <shift>\n" "💡 参数：day (白班) 或 night (夜班)",
            reply_to_message_id=message.message_id,
        )
        return

    shift = args[1].lower()
    if shift not in ["day", "night"]:
        await message.answer(
            "❌ 班次参数错误\n" "💡 请使用：day (白班) 或 night (夜班)",
            reply_to_message_id=message.message_id,
        )
        return

    user_lock = await user_lock_manager.get_lock(chat_id, uid)
    async with user_lock:
        await show_rank(message, shift)


@rate_limit(rate=10, per=60)
@track_performance("cmd_ranking_day")
async def handle_ranking_day_command(message: types.Message):
    """处理 /rankingday 命令"""
    chat_id = message.chat.id
    uid = message.from_user.id

    user_lock = await user_lock_manager.get_lock(chat_id, uid)
    async with user_lock:
        await show_rank(message, "day")


@rate_limit(rate=10, per=60)
@track_performance("cmd_ranking_night")
async def handle_ranking_night_command(message: types.Message):
    """处理 /rankingnight 命令"""
    chat_id = message.chat.id
    uid = message.from_user.id

    user_lock = await user_lock_manager.get_lock(chat_id, uid)
    async with user_lock:
        await show_rank(message, "night")


@rate_limit(rate=10, per=60)
@message_deduplicate
@with_retry("cmd_ci", max_retries=2)
@track_performance("cmd_ci")
async def cmd_ci(message: types.Message):
    """指令打卡"""
    args = message.text.split(maxsplit=1)
    if len(args) != 2:
        await message.answer(
            "❌ 用法：/ci <活动名>",
            reply_markup=await get_main_keyboard(
                chat_id=message.chat.id, show_admin=await is_admin(message.from_user.id)
            ),
            reply_to_message_id=message.message_id,
        )
        return

    act = resolve_button(args[1].strip())

    activity_aliases = {
        "抽烟": "抽烟或休息",
        "休息": "抽烟或休息",
        "smoke": "抽烟或休息",
        "吸烟": "抽烟或休息",
    }
    for zh, vi in ACTIVITY_VI.items():
        activity_aliases[vi] = zh
    if act in activity_aliases:
        act = activity_aliases[act]

    if not await db.activity_exists(act):
        await message.answer(
            f"❌ 活动 '<code>{act}</code>' 不存在，请先使用 /addactivity 添加或检查拼写",
            reply_markup=await get_main_keyboard(
                chat_id=message.chat.id, show_admin=await is_admin(message.from_user.id)
            ),
            reply_to_message_id=message.message_id,
            parse_mode="HTML",
        )
        return

    await start_activity(message, act)


@rate_limit(rate=10, per=60)
@message_deduplicate
@with_retry("cmd_at", max_retries=2)
@track_performance("cmd_at")
async def cmd_at(message: types.Message):
    """指令回座"""
    await process_back(message)


@user_rate_limit(rate=3, per=60)
@message_deduplicate
@with_retry("work_start", max_retries=2)
@track_performance("work_start")
async def cmd_workstart(message: types.Message):
    """上班打卡"""
    await process_work_checkin(message, "work_start")


@user_rate_limit(rate=3, per=60)
@message_deduplicate
@with_retry("work_end", max_retries=2)
@track_performance("work_end")
async def cmd_workend(message: types.Message):
    """下班打卡"""
    await process_work_checkin(message, "work_end")


async def handle_back_command(message: types.Message):
    """处理回座命令"""
    await process_back(message)


@user_rate_limit(rate=5, per=60)
@rate_limit(rate=100, per=60)
@message_deduplicate
async def handle_work_buttons(message: types.Message):
    """处理双班上下班按钮"""
    chat_id = message.chat.id
    uid = message.from_user.id
    text = message.text.strip()

    try:
        if not await db.has_work_hours_enabled(chat_id):
            await message.answer(
                "❌ 本群组尚未启用上下班打卡功能\n\n"
                "👑 请联系管理员使用命令：\n"
                "<code>/setdualmode on 09:00 21:00</code>\n"
                "或 <code>/setworktime 09:00 18:00</code>",
                reply_markup=await get_main_keyboard(
                    chat_id=chat_id, show_admin=await is_admin(uid)
                ),
                reply_to_message_id=message.message_id,
                parse_mode="HTML",
            )
            return

        action = resolve_button(text)
        if action == "work_start_day":
            await process_work_checkin(message, "work_start", forced_shift="day")
        elif action == "work_start_night":
            await process_work_checkin(message, "work_start", forced_shift="night")
        elif action == "work_end":
            await process_work_checkin(message, "work_end")
    except Exception as e:
        logger.error(f"上下班按钮处理失败 {chat_id}-{uid}: {e}", exc_info=True)
        await message.answer(
            "⚠️ 打卡处理失败，请稍后重试。",
            reply_to_message_id=message.message_id,
        )


@admin_required
@rate_limit(rate=2, per=60)
@track_performance("handle_export_button")
async def handle_export_button(message: types.Message):
    """处理导出数据按钮"""
    chat_id = message.chat.id
    await message.answer(
        "⏳ 正在导出数据，请稍候...", reply_to_message_id=message.message_id
    )
    try:
        await export_and_push_csv(chat_id)
        await message.answer(
            "✅ 数据已导出并推送！", reply_to_message_id=message.message_id
        )
    except Exception as e:
        await message.answer(
            f"❌ 导出失败：{e}", reply_to_message_id=message.message_id
        )


@rate_limit(rate=10, per=60)
@track_performance("handle_my_record")
async def handle_my_record(message: types.Message):
    """处理我的记录按钮"""
    chat_id = message.chat.id
    uid = message.from_user.id

    user_lock = await user_lock_manager.get_lock(chat_id, uid)
    async with user_lock:
        await show_history(message)


@rate_limit(rate=10, per=60)
@track_performance("handle_rank")
async def handle_rank(message: types.Message):
    """处理排行榜按钮"""
    chat_id = message.chat.id
    uid = message.from_user.id

    user_lock = await user_lock_manager.get_lock(chat_id, uid)
    async with user_lock:
        await show_rank(message)


@rate_limit(rate=5, per=60)
async def handle_admin_panel_button(message: types.Message):
    """处理管理员面板按钮"""
    if not await is_admin(message.from_user.id):
        markup = await get_main_keyboard(chat_id=message.chat.id, show_admin=False)
        await message.answer(
            Config.MESSAGES["no_permission"],
            reply_markup=markup,
            reply_to_message_id=message.message_id,
            parse_mode=None,
        )
        return

    admin_text = (
        "👑 <b>管理员面板</b>\n"
        "━━━━━━━━━━━━━━━━\n\n"
        "📢 <b>频道与推送</b>\n"
        "├ <code>/setchannel [ID]</code>\n"
        "├ <code>/setgroup [ID]</code>\n"
        "├ <code>/addextraworkgroup [ID]</code> - 添加上下班额外推送群组\n"
        "├ <code>/clearextraworkgroup</code> - 清除额外推送群组\n"
        "├ <code>/setpush [目标] [开关]</code>\n"
        "├ <code>/showeverypush</code>\n"
        "│ 目标: ch|gr|ad\n"
        "│ 开关: on|off\n\n"
        "🎯 <b>活动管理</b>\n"
        "├ <code>/addactivity [名] [次] [分]</code>\n"
        "├ <code>/delactivity [名]</code>\n"
        "├ <code>/actnum [名] [人数]</code>\n"
        "└ <code>/actstatus</code>\n\n"
        "💰 <b>罚款管理</b>\n"
        "├ <code>/setfine [名] [段] [元]</code>\n"
        "├ <code>/setfines_all [段1] [元1] ...</code>\n"
        "├ <code>/setworkfine [类型] [分] [元]</code>\n"
        "└ <code>/finesstatus</code>\n"
        "  类型: start|end\n\n"
        "🔄 <b>重置设置</b>\n"
        "├ <code>/setresettime [时] [分]</code>\n"
        "├ <code>/resetuser [用户ID]</code>\n"
        "└ <code>/resettime</code>\n\n"
        "⏰ <b>上下班管理</b>\n"
        "├ <code>/setdualmode on 9:00 21:00</code>\n"
        "├ <code>/setworktime [上] [下]</code>\n"
        "├ <code>/setshiftgrace</code>\n"
        "├ <code>/setworkendgrace</code>\n"
        "├ <code>/worktime</code>\n"
        "├ <code>/checkdual</code>\n"
        "├ <code>/delwork</code>\n"
        "└ <code>/delwork_clear</code>\n\n"
        "🔄 <b>换班管理</b>\n"
        "├ <code>/handover</code> - 查看当前换班状态\n"
        "├ <code>/handoverconfig</code> - 查看换班配置\n"
        "├ <code>/sethandoverday [日期] [月份]</code> - 设置换班日期\n"
        "│  ├ <code>/sethandoverday status</code> - 查看当前设置\n"
        "│  ├ <code>/sethandoverday off</code> - 关闭换班功能\n"
        "│  ╰ 示例: 15(每月) | 31(月末) | 15 12(指定月)\n"
        "├ <code>/sethour [类型] [小时]</code> - 设置工作时长\n"
        "│  类型: handover_night|handover_day|normal_night|normal_day\n"
        "│  ╰ 示例: /sethour handover_night 18\n\n"
        "📊 <b>数据管理</b>\n"
        "├ <code>/export</code>\n"
        "├ <code>/exportmonthly [年] [月]</code>\n"
        "├ <code>/monthlyreport [年] [月]</code>\n"
        "├ <code>/cleanup_monthly [年] [月]</code>\n"
        "├ <code>/monthly_stats_status</code>\n"
        "├ <code>/cleanup_inactive [天]</code>\n"
        "└ <code>/fixmessages</code> - 修复消息引用\n\n"
        "💾 <b>数据显示</b>\n"
        "└ <code>/showsettings</code>\n\n"
        "🔧 <b>调试工具</b>\n"
        "├ <code>/testgroupaccess [群组ID]</code> - 测试群组访问\n"
        "└ <code>/checkperms</code> - 检查机器人权限\n\n"
        "━━━━━━━━━━━━━━━━\n"
        "<i>💡 提示：发送 /help [命令] 查看详情</i>"
    )

    await message.answer(
        admin_text,
        reply_markup=get_admin_keyboard(),
        reply_to_message_id=message.message_id,
        parse_mode="HTML",
    )


@rate_limit(rate=5, per=60)
async def handle_back_to_main_menu(message: types.Message):
    """处理返回主菜单按钮"""
    chat_id = message.chat.id
    uid = message.from_user.id

    logger.info(f"用户 {uid} 点击了返回主菜单按钮")

    await message.answer(
        "📋 主菜单",
        reply_markup=await get_main_keyboard(
            chat_id=chat_id, show_admin=await is_admin(uid)
        ),
        reply_to_message_id=message.message_id,
    )
    logger.info(f"已为用户 {uid} 返回主菜单")


@user_rate_limit(rate=10, per=60)
@rate_limit(rate=100, per=60)
async def handle_all_text_messages(message: types.Message):
    """统一处理活动按钮等文本消息"""
    text = message.text.strip()
    chat_id = message.chat.id
    uid = message.from_user.id

    # 已由其他 handler 处理的按钮，此处跳过
    action = resolve_button(text)
    if action in SPECIAL_BUTTONS.values() or action in (
        "work_start_day",
        "work_start_night",
        "work_end",
    ):
        return

    try:
        activity_limits = await db.get_activity_limits_cached()
        act = resolve_activity_name(text, activity_limits)
        if act:
            logger.info(f"活动按钮点击: {act} - 用户 {uid}")
            await start_activity(message, act)
            return
    except Exception as e:
        logger.error(f"处理活动按钮时出错: {e}", exc_info=True)
        await message.answer(
            "⚠️ 打卡处理失败，请稍后重试。",
            reply_to_message_id=message.message_id,
        )
        return

    logger.debug(f"忽略未识别的文本消息: {text!r} - 用户 {uid}")


@rate_limit(rate=10, per=60)
@message_deduplicate
@with_retry("fixed_activity", max_retries=2)
@track_performance("fixed_activity")
async def handle_activity_command(message: types.Message):
    """处理动态活动 / 命令"""
    cmd = extract_command(message.text or "")
    if not cmd:
        return

    act = resolve_activity_command(cmd)
    if not act:
        logger.debug(f"非活动命令: /{cmd}")
        return

    logger.info(f"✅ 活动命令: /{cmd} -> {act}")
    await start_activity(message, act)

async def show_history(message: types.Message, shift: str = None):
    """显示用户历史记录"""

    chat_id = message.chat.id
    uid = message.from_user.id

    await db.init_group(chat_id)
    await db.init_user(chat_id, uid)

    business_date = await db.get_business_date(chat_id)
    current_hour = db.get_beijing_time().hour
    current_minute = db.get_beijing_time().minute
    current_time_decimal = current_hour + current_minute / 60

    group_data = await db.get_group_cached(chat_id)
    reset_hour = group_data.get("reset_hour", Config.DAILY_RESET_HOUR)
    reset_minute = group_data.get("reset_minute", Config.DAILY_RESET_MINUTE)

    shift_config = await db.get_shift_config(chat_id)
    day_start_str = shift_config.get("day_start", "09:00")
    day_start_hour = int(day_start_str.split(":")[0])
    day_start_minute = int(day_start_str.split(":")[1])
    day_start_decimal = day_start_hour + day_start_minute / 60

    user_data = await db.get_user_cached(chat_id, uid)
    if not user_data:
        await message.answer(
            "暂无记录，请先进行打卡活动",
            reply_markup=await get_main_keyboard(
                chat_id=chat_id, show_admin=await is_admin(uid)
            ),
            reply_to_message_id=message.message_id,
        )
        return

    is_dual_mode = shift_config.get("dual_mode", True)

    first_line = (
        f"👤 用户：{MessageFormatter.format_user_link(uid, user_data['nickname'])}"
    )

    if shift:
        shift_text = "白班" if shift == "day" else "夜班"
        title = f"{first_line}\n📊 【{shift_text}】记录统计"
    elif is_dual_mode:
        title = f"{first_line}\n📊 当前周期记录（双班）"
    else:
        title = f"{first_line}\n📊 当前周期记录"

    text = (
        f"{title}\n"
        f"📅 统计周期：<code>{business_date.strftime('%Y-%m-%d')}</code>\n"
        f"⏰ 重置时间：{reset_hour:02d}:{reset_minute:02d}\n\n"
    )

    # ===== 获取换班周期信息 =====
    from handover_manager import handover_manager

    now = db.get_beijing_time()
    period = await handover_manager.determine_current_period(chat_id, now)
    is_handover = period["is_handover"]
    handover_type = period["period_type"]
    cycle_number = period["cycle"]  # 从 period 获取正确的 cycle
    cycle_start_time = None
    period_type = period["period_type"]

    if is_handover and shift:
        try:
            # 获取用户当前周期的累计时间
            cycle_data = await handover_manager.get_user_cycle(
                chat_id, uid, period["business_date"], period_type, cycle_number
            )

            if cycle_data and cycle_number == 2:
                cycle_start_time = cycle_data.get("cycle_start_time")
                # 可以记录周期累计时间用于显示
                cycle_total_minutes = cycle_data.get("total_work_seconds", 0) // 60
                logger.info(
                    f"🔄 [我的记录] 用户 {uid} 周期{cycle_number} 已累计 {cycle_total_minutes} 分钟"
                )
            else:
                logger.info(
                    f"🔄 [我的记录] 用户 {uid} 当前周期: {cycle_number}, 班次: {shift}"
                )

        except Exception as e:
            logger.error(f"获取换班周期信息失败: {e}")
    # ===== 获取换班周期信息结束 =====

    has_records = False

    work_records = await db.get_work_records_by_shift(chat_id, uid, shift)

    if work_records:
        text += "🕒 <b>上下班记录</b>\n"

        shift_work = {
            "day": {"work_start": [], "work_end": []},
            "night": {"work_start": [], "work_end": []},
        }

        for check_type, records in work_records.items():
            for r in records:
                s = r.get("shift", "day")
                shift_work[s][check_type].append(r)

        if shift:
            stats = shift_work.get(shift, {})
            for ct in ("work_start", "work_end"):
                if stats.get(ct):
                    type_text = "上班" if ct == "work_start" else "下班"
                    latest = stats[ct][0]
                    text += (
                        f"• {type_text}：<code>{len(stats[ct])}</code> 次\n"
                        f"  最近：{latest['checkin_time']}（{latest['status']}）\n"
                    )
        else:
            total_start = sum(len(shift_work[s]["work_start"]) for s in shift_work)
            total_end = sum(len(shift_work[s]["work_end"]) for s in shift_work)
            if total_start or total_end:
                text += (
                    f"• 上班：<code>{total_start}</code> 次\n"
                    f"• 下班：<code>{total_end}</code> 次\n"
                )

        text += "\n"
        has_records = True

    activity_limits = await db.get_activity_limits_cached()

    async with db.pool.acquire() as conn:
        if shift:
            if shift == "night":
                now = db.get_beijing_time()
                # 如果是凌晨（0-12点），查询前一天；如果是下午/晚上，查询当天
                if now.hour < 12:
                    query_date = business_date - timedelta(days=1)
                    logger.info(
                        f"🌙 [我的记录-夜班] 凌晨查询前一天: "
                        f"业务日期={business_date}, 查询日期={query_date}"
                    )
                else:
                    query_date = business_date
                    logger.info(
                        f"🌙 [我的记录-夜班] 正常查询当天: "
                        f"业务日期={business_date}, 查询日期={query_date}"
                    )
            else:
                if current_time_decimal < day_start_decimal:
                    query_date = business_date - timedelta(days=1)
                    logger.info(
                        f"🌙 [我的记录-白班] 凌晨查询前一天白班: "
                        f"当前时间={current_hour:02d}:{current_minute:02d}, "
                        f"白班开始={day_start_str}, 查询日期={query_date}"
                    )
                else:
                    query_date = business_date
                    logger.info(f"☀️ [我的记录-白班] 正常查询当天: {query_date}")

            rows = await conn.fetch(
                """
                SELECT activity_name, activity_count, accumulated_time, shift
                FROM user_activities
                WHERE chat_id = $1 AND user_id = $2 
                  AND activity_date = $3 AND shift = $4
                """,
                chat_id,
                uid,
                query_date,
                shift,
            )
            if is_handover and cycle_number == 2 and shift and cycle_start_time:
                logger.info(f"🔄 [周期2过滤] 用户 {uid} 只显示周期2开始后的活动")
                # 简化处理：周期2刚开始时显示空
                # 如果需要精确过滤，需要修改表结构或添加关联查询
                rows = []
                logger.info(f"🔄 [周期2] 用户 {uid} 周期2刚开始，显示空记录")
        else:
            if current_time_decimal < day_start_decimal:
                query_date = business_date - timedelta(days=1)
                logger.debug(
                    f"🌙 [我的记录-全部] 凌晨查询前一天所有数据: "
                    f"当前时间={current_hour:02d}:{current_minute:02d}, "
                    f"白班开始={day_start_str}, 查询日期={query_date}"
                )

                rows = await conn.fetch(
                    """
                    SELECT activity_name, activity_count, accumulated_time, shift
                    FROM user_activities
                    WHERE chat_id = $1 AND user_id = $2 
                      AND activity_date = $3
                    """,
                    chat_id,
                    uid,
                    query_date,
                )
            else:
                logger.info(f"☀️ [我的记录-全部] 正常查询当天: {business_date}")

                rows = await conn.fetch(
                    """
                    SELECT activity_name, activity_count, accumulated_time, shift
                    FROM user_activities
                    WHERE chat_id = $1 AND user_id = $2 
                      AND activity_date = $3
                    """,
                    chat_id,
                    uid,
                    business_date,
                )

    activities_by_shift = {"day": {}, "night": {}}

    for r in rows:
        s = r["shift"] or "day"
        act = r["activity_name"]

        if act not in activities_by_shift[s]:
            activities_by_shift[s][act] = {"count": 0, "time": 0}

        activities_by_shift[s][act]["count"] += r["activity_count"]
        activities_by_shift[s][act]["time"] += r["accumulated_time"]

    if shift:
        shift_data = activities_by_shift.get(shift, {})
        total_time_all = sum(info["time"] for info in shift_data.values())
        total_count_all = sum(info["count"] for info in shift_data.values())
        display_activities = shift_data
    else:
        total_time_all = 0
        total_count_all = 0
        for s_data in activities_by_shift.values():
            for info in s_data.values():
                total_time_all += info["time"]
                total_count_all += info["count"]

        display_activities = {}
        for s, acts in activities_by_shift.items():
            for act, info in acts.items():
                if act not in display_activities:
                    display_activities[act] = {"count": 0, "time": 0}
                display_activities[act]["count"] += info["count"]
                display_activities[act]["time"] += info["time"]

    text += "🎯 <b>活动记录</b>\n"

    def render_activity_block(act_map):
        nonlocal has_records
        block = ""
        for act in activity_limits.keys():
            info = act_map.get(act)
            if not info or (info["count"] == 0 and info["time"] == 0):
                continue
            count = info["count"]
            total_time = info["time"]
            max_times = activity_limits[act]["max_times"]
            status = "✅" if max_times == 0 or count < max_times else "❌"
            block += (
                f"• <code>{act}</code>："
                f"<code>{MessageFormatter.format_time(int(total_time))}</code>，"
                f"次数：<code>{count}</code>/<code>{max_times}</code> {status}\n"
            )
            has_records = True
        return block

    if shift:
        shift_display = render_activity_block(activities_by_shift.get(shift, {}))
        if shift_display:
            text += shift_display
    elif is_dual_mode:
        for s in ("day", "night"):
            block = render_activity_block(activities_by_shift.get(s, {}))
            if block:
                text += f"\n【{'白班' if s == 'day' else '夜班'}】\n{block}"
    else:
        text += render_activity_block(display_activities)

    if shift:
        shift_text = "白班" if shift == "day" else "夜班"
        text += (
            f"\n📈 当前周期【{shift_text}】统计：\n"
            f"• {shift_text}累计时间：<code>{MessageFormatter.format_time(int(total_time_all))}</code>\n"
            f"• {shift_text}活动次数：<code>{total_count_all}</code> 次\n"
        )
    else:
        text += (
            f"\n📈 当前周期总统计：\n"
            f"• 总累计时间：<code>{MessageFormatter.format_time(int(total_time_all))}</code>\n"
            f"• 总活动次数：<code>{total_count_all}</code> 次\n"
        )

    async with db.pool.acquire() as conn:
        if shift:
            # 罚款统计使用与活动记录相同的日期逻辑
            if shift == "night":
                now = db.get_beijing_time()
                # 罚款统计使用与活动记录相同的日期逻辑
                if now.hour < 12:
                    fine_query_date = business_date - timedelta(days=1)
                    logger.info(
                        f"🌙 [罚款统计-夜班] 凌晨查询前一天: "
                        f"业务日期={business_date}, 罚款查询日期={fine_query_date}"
                    )
                else:
                    fine_query_date = business_date
                    logger.info(
                        f"🌙 [罚款统计-夜班] 正常查询当天: "
                        f"业务日期={business_date}, 罚款查询日期={fine_query_date}"
                    )
            else:  # day
                if current_time_decimal < day_start_decimal:
                    fine_query_date = business_date - timedelta(days=1)
                    logger.info(
                        f"🌙 [罚款统计-白班] 凌晨查询前一天: "
                        f"当前时间={current_hour:02d}:{current_minute:02d}, "
                        f"罚款查询日期={fine_query_date}"
                    )
                else:
                    fine_query_date = business_date
                    logger.info(f"☀️ [罚款统计-白班] 正常查询当天: {fine_query_date}")

            fine_total = (
                await conn.fetchval(
                    """
                    SELECT COALESCE(fine_amount, 0)
                    FROM daily_statistics
                    WHERE chat_id = $1 
                      AND user_id = $2 
                      AND record_date = $3 
                      AND shift = $4
                    """,
                    chat_id,
                    uid,
                    fine_query_date,
                    shift,
                )
                or 0
            )
        else:
            # 全部班次罚款统计（保持不变）
            if current_time_decimal < day_start_decimal:
                fine_query_date = business_date - timedelta(days=1)
                logger.info(f"🌙 [罚款统计-全部] 凌晨查询前一天: {fine_query_date}")
            else:
                fine_query_date = business_date
                logger.info(f"☀️ [罚款统计-全部] 正常查询当天: {fine_query_date}")

            fine_total = (
                await conn.fetchval(
                    """
                    SELECT COALESCE(SUM(fine_amount), 0)
                    FROM daily_statistics
                    WHERE chat_id = $1 
                      AND user_id = $2 
                      AND record_date = $3
                    """,
                    chat_id,
                    uid,
                    fine_query_date,
                )
                or 0
            )

    if fine_total > 0:
        if shift:
            shift_text = "白班" if shift == "day" else "夜班"
            text += f"💰 {shift_text}累计罚款：<code>{fine_total}</code> 泰铢\n"
        else:
            text += f"💰 累计罚款：<code>{fine_total}</code> 泰铢\n"

    if is_dual_mode and not shift:
        text += (
            "\n📊 <b>按班次查看</b>\n"
            "• /myinfoday - 点击查看白班记录\n"
            "• /myinfonight - 点击查看夜班记录\n"
        )

    if not has_records:
        text += "\n暂无记录，请先进行打卡活动"

    await message.answer(
        text,
        reply_markup=await get_main_keyboard(
            chat_id=chat_id, show_admin=await is_admin(uid)
        ),
        reply_to_message_id=message.message_id,
        parse_mode="HTML",
    )


async def show_rank(message: types.Message, shift: str = None):
    """显示排行榜"""

    chat_id = message.chat.id
    uid = message.from_user.id

    await db.init_group(chat_id)
    activity_limits = await db.get_activity_limits_cached()

    if not activity_limits:
        await message.answer(
            "⚠️ 当前没有配置任何活动，无法生成排行榜。",
            reply_to_message_id=message.message_id,
        )
        return

    business_date = await db.get_business_date(chat_id)
    current_hour = db.get_beijing_time().hour
    current_minute = db.get_beijing_time().minute
    current_time_decimal = current_hour + current_minute / 60

    shift_config = await db.get_shift_config(chat_id)
    day_start_str = shift_config.get("day_start", "09:00")
    day_start_hour = int(day_start_str.split(":")[0])
    day_start_minute = int(day_start_str.split(":")[1])
    day_start_decimal = day_start_hour + day_start_minute / 60

    group_data = await db.get_group_cached(chat_id)
    reset_hour = group_data.get("reset_hour", Config.DAILY_RESET_HOUR)
    reset_minute = group_data.get("reset_minute", Config.DAILY_RESET_MINUTE)

    if shift:
        shift_text = "白班" if shift == "day" else "夜班"
        title = f"🏆 【{shift_text}】活动排行榜"
    else:
        title = "🏆 当前周期活动排行榜"

    rank_text = (
        f"{title}\n"
        f"📅 统计周期：<code>{business_date.strftime('%Y-%m-%d')}</code>\n"
        f"⏰ 重置时间：<code>{reset_hour:02d}:{reset_minute:02d}</code>\n"
    )

    if shift:
        rank_text += f"📊 班次：<code>{'白班' if shift == 'day' else '夜班'}</code>\n\n"
    else:
        rank_text += "📊 班次：全部\n\n"

    found_any_data = False

    from handover_manager import handover_manager

    now = db.get_beijing_time()
    period = await handover_manager.determine_current_period(chat_id, now)
    is_handover = period["is_handover"]
    handover_type = period["period_type"]
    cycle_number = period["cycle"]  # 从 period 获取正确的 cycle
    cycle_start_time = None
    period_type = period["period_type"]

    if is_handover and shift:
        try:
            # 获取当前用户的周期信息（作为示例）
            cycle_data = await handover_manager.get_user_cycle(
                chat_id, uid, period["business_date"], period_type, cycle_number
            )

            if cycle_data and cycle_number == 2:
                cycle_start_time = cycle_data.get("cycle_start_time")
                logger.info(f"🏆 [排行榜] 当前周期: {cycle_number}, 班次: {shift}")

        except Exception as e:
            logger.error(f"获取换班周期信息失败: {e}")

    for act in activity_limits.keys():
        try:
            if shift:
                if shift == "night":
                    now = db.get_beijing_time()
                    # 如果是凌晨（0-12点），查询前一天；如果是下午/晚上，查询当天
                    if now.hour < 12:
                        query_date = business_date - timedelta(days=1)
                        logger.info(
                            f"🌙 [排行榜-夜班] 凌晨查询前一天: "
                            f"业务日期={business_date}, 查询日期={query_date}"
                        )
                    else:
                        query_date = business_date
                        logger.info(
                            f"🌙 [排行榜-夜班] 正常查询当天: "
                            f"业务日期={business_date}, 查询日期={query_date}"
                        )
                else:
                    if current_time_decimal < day_start_decimal:
                        query_date = business_date - timedelta(days=1)
                        logger.info(
                            f"🌙 [排行榜-白班] 凌晨查询前一天白班: "
                            f"当前时间={current_hour:02d}:{current_minute:02d}, "
                            f"白班开始={day_start_str}, 查询日期={query_date}"
                        )
                    else:
                        query_date = business_date
                        logger.info(f"☀️ [排行榜-白班] 正常查询当天: {query_date}")

                query = """
                    SELECT 
                        ua.user_id,
                        u.nickname,
                        SUM(ua.accumulated_time) AS total_time,
                        SUM(ua.activity_count) AS total_count,
                        CASE 
                            WHEN u.current_activity = $1 THEN TRUE 
                            ELSE FALSE 
                        END AS is_active
                    FROM user_activities ua
                    LEFT JOIN users u 
                        ON ua.chat_id = u.chat_id 
                        AND ua.user_id = u.user_id
                    WHERE ua.chat_id = $2
                      AND ua.activity_date = $3
                      AND ua.activity_name = $4
                      AND ua.shift = $5
                    GROUP BY ua.user_id, u.nickname, u.current_activity
                    HAVING SUM(ua.accumulated_time) > 0 
                        OR u.current_activity = $1
                    ORDER BY 
                        is_active DESC,
                        total_time DESC
                    LIMIT 10
                """
                params = [act, chat_id, query_date, act, shift]
            else:
                if current_time_decimal < day_start_decimal:
                    query_date = business_date - timedelta(days=1)
                    logger.debug(
                        f"🌙 [排行榜-全部] 凌晨查询前一天所有数据: "
                        f"当前时间={current_hour:02d}:{current_minute:02d}, "
                        f"白班开始={day_start_str}, 查询日期={query_date}"
                    )

                    query = """
                        SELECT 
                            ua.user_id,
                            u.nickname,
                            SUM(ua.accumulated_time) AS total_time,
                            SUM(ua.activity_count) AS total_count,
                            CASE 
                                WHEN u.current_activity = $1 
                                THEN TRUE 
                                ELSE FALSE 
                            END AS is_active
                        FROM user_activities ua
                        LEFT JOIN users u 
                            ON ua.chat_id = u.chat_id 
                            AND ua.user_id = u.user_id
                        WHERE ua.chat_id = $2
                          AND ua.activity_date = $3
                          AND ua.activity_name = $4
                        GROUP BY ua.user_id, u.nickname, u.current_activity
                        HAVING SUM(ua.accumulated_time) > 0 OR u.current_activity = $1
                        ORDER BY total_time DESC
                        LIMIT 10
                    """
                    params = [act, chat_id, query_date, act]
                else:
                    logger.debug(f"☀️ [排行榜-全部] 正常查询当天: {business_date}")

                    query = """
                            SELECT 
                                ua.user_id,
                                u.nickname,
                                SUM(ua.accumulated_time) AS total_time,
                                SUM(ua.activity_count) AS total_count,
                                CASE 
                                    WHEN u.current_activity = $1 
                                    THEN TRUE 
                                    ELSE FALSE 
                                END AS is_active
                            FROM user_activities ua
                            LEFT JOIN users u 
                                ON ua.chat_id = u.chat_id 
                                AND ua.user_id = u.user_id
                            WHERE ua.chat_id = $2
                              AND ua.activity_date = $3
                              AND ua.activity_name = $4
                            GROUP BY ua.user_id, u.nickname, u.current_activity
                            HAVING SUM(ua.accumulated_time) > 0 OR u.current_activity = $1
                            ORDER BY total_time DESC
                            LIMIT 10
                    """
                    params = [act, chat_id, business_date, act]

            rows = await db.execute_with_retry(
                "获取活动排行榜", query, *params, fetch=True
            )
            if is_handover and cycle_number == 2 and shift and cycle_start_time:
                logger.info(f"🏆 [周期2过滤] 只显示周期2开始后的活动")
                # 简化处理：周期2刚开始时排行榜为空
                rows = []
                logger.info(f"🏆 [周期2] 排行榜显示空")

            if not rows:
                continue

            found_any_data = True
            rank_text += f"📈 <code>{act}</code>：\n"

            for i, row in enumerate(rows, 1):
                user_id = row["user_id"]
                nickname = row["nickname"] or f"用户{user_id}"
                total_time = row["total_time"] or 0
                total_count = row["total_count"] or 0
                is_active = row["is_active"]

                if is_active:
                    rank_text += (
                        f"  <code>{i}.</code> 🟡 "
                        f"{MessageFormatter.format_user_link(user_id, nickname)} - 进行中\n"
                    )
                elif total_time > 0:
                    time_str = MessageFormatter.format_time(int(total_time))
                    rank_text += (
                        f"  <code>{i}.</code> 🟢 "
                        f"{MessageFormatter.format_user_link(user_id, nickname)} "
                        f"- {time_str} ({total_count}次)\n"
                    )

            rank_text += "\n"

        except Exception as e:
            logger.error(f"查询活动 {act} 排行榜失败: {e}")
            continue

    if not found_any_data:
        if shift:
            rank_text = (
                f"🏆 【{'白班' if shift == 'day' else '夜班'}】活动排行榜\n"
                f"📅 统计周期：<code>{business_date.strftime('%Y-%m-%d')}</code>\n\n"
                f"📊 当前班次还没有活动记录\n"
                f"💪 开始第一个活动吧！\n\n"
            )
        else:
            rank_text = (
                f"🏆 当前周期活动排行榜\n"
                f"📅 统计周期：<code>{business_date.strftime('%Y-%m-%d')}</code>\n"
                f"⏰ 重置时间：<code>{reset_hour:02d}:{reset_minute:02d}</code>\n\n"
                f"📊 当前周期还没有活动记录\n"
                f"💪 开始第一个活动吧！\n\n"
                f"💡 提示：开始活动后会立即显示在这里"
            )

    if not shift:
        shift_config = await db.get_shift_config(chat_id)
        if shift_config.get("dual_mode"):
            rank_text += (
                "💡 按班次查看：\n"
                "• /rankingday - 白班排行榜\n"
                "• /rankingnight - 夜班排行榜\n"
            )

    await message.answer(
        rank_text,
        reply_markup=await get_main_keyboard(chat_id, await is_admin(uid)),
        parse_mode="HTML",
        reply_to_message_id=message.message_id,
    )


async def handle_quick_back(callback_query: types.CallbackQuery):
    """处理快速回座按钮"""
    try:
        data_parts = callback_query.data.split(":")

        if len(data_parts) < 4:
            logger.warning(f"⚠️ 快速回座数据格式错误: {callback_query.data}")
            await callback_query.answer("❌ 按钮数据格式错误", show_alert=True)
            return

        chat_id = int(data_parts[1])
        uid = int(data_parts[2])
        shift = data_parts[3] if len(data_parts) > 3 else "day"

        msg_ts = callback_query.message.date.timestamp()
        if time.time() - msg_ts > 600:
            await callback_query.answer(
                "⚠️ 此按钮已过期，请重新输入回座", show_alert=True
            )
            return

        if callback_query.from_user.id != uid:
            await callback_query.answer("❌ 这不是您的回座按钮！", show_alert=True)
            return

        logger.info(f"🔄 快速回座: 用户{uid}, 群组{chat_id}, 班次{shift}")

        user_lock = await user_lock_manager.get_lock(chat_id, uid)
        async with user_lock:
            user_data = await db.get_user_cached(chat_id, uid)

            if not user_data or not user_data.get("current_activity"):
                await callback_query.answer("❌ 您当前没有活动在进行", show_alert=True)
                return

            await _process_back_locked(callback_query.message, chat_id, uid, shift)

        try:
            await callback_query.message.edit_reply_markup(reply_markup=None)
        except Exception as e:
            logger.warning(f"无法更新按钮状态: {e}")

        await callback_query.answer("✅ 已成功回座")

    except ValueError as e:
        logger.error(f"❌ 快速回座参数解析失败: {e}")
        await callback_query.answer("❌ 数据格式错误", show_alert=True)
    except Exception as e:
        logger.error(f"❌ 快速回座失败: {e}")
        await callback_query.answer("❌ 回座失败，请手动输入回座", show_alert=True)


