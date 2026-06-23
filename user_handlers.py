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

logger = logging.getLogger("GroupCheckInBot")

from work_checkin import process_work_checkin
from activity_service import (
    start_activity, process_back, check_activity_limit_by_shift,
    has_active_activity, _process_back_locked,
)
from activity_commands import is_activity_command, resolve_activity_command, extract_command
from reset_service import reset_daily_data_if_needed
from admin_panel import (
    build_admin_panel_text,
    is_admin_section_button,
    section_for_button,
)
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


@user_rate_limit(rate=5, per=60)
@message_deduplicate
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

    admin_text = build_admin_panel_text("full")

    await message.answer(
        admin_text,
        reply_markup=get_admin_keyboard(),
        reply_to_message_id=message.message_id,
        parse_mode="HTML",
    )


@rate_limit(rate=5, per=60)
async def handle_admin_section_button(message: types.Message):
    """管理员面板分类按钮"""
    if not await is_admin(message.from_user.id):
        markup = await get_main_keyboard(chat_id=message.chat.id, show_admin=False)
        await message.answer(
            Config.MESSAGES["no_permission"],
            reply_markup=markup,
            reply_to_message_id=message.message_id,
            parse_mode=None,
        )
        return

    canonical = resolve_button(message.text.strip())
    if not is_admin_section_button(canonical):
        return

    section = section_for_button(canonical)
    await message.answer(
        build_admin_panel_text(section),
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
            await start_activity(message, act, activity_limits=activity_limits)
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

    from handover_manager import handover_manager

    now = db.get_beijing_time()
    stats_query = await handover_manager.resolve_activity_stats_query(
        chat_id, shift, now
    )
    business_date = stats_query["business_date"]

    group_data = await db.get_group_cached(chat_id)
    reset_hour = group_data.get("reset_hour", Config.DAILY_RESET_HOUR)
    reset_minute = group_data.get("reset_minute", Config.DAILY_RESET_MINUTE)

    shift_config = await db.get_shift_config(chat_id)
    is_dual_mode = shift_config.get("dual_mode", True)

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

    has_records = False

    if stats_query.get("split_dual_shift"):
        work_start = stats_query["night_date"]
        work_end = stats_query["day_date"]
    else:
        work_start = work_end = stats_query["query_date"]

    work_records = await db.get_work_records_by_shift(
        chat_id, uid, shift, work_start, work_end
    )

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
    rows = await db.fetch_user_activity_stats(chat_id, uid, stats_query)

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

    current_activity = user_data.get("current_activity")
    if current_activity:
        start_time_str = user_data.get("activity_start_time")
        elapsed_hint = ""
        if start_time_str:
            try:
                start_time = datetime.fromisoformat(start_time_str)
                elapsed_sec = int((now - start_time).total_seconds())
                elapsed_hint = (
                    f"（已进行 {MessageFormatter.format_time(elapsed_sec)}）"
                )
            except Exception:
                pass
        text += (
            f"🔄 进行中：<code>{current_activity}</code>{elapsed_hint}\n"
        )
        has_records = True

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

    fine_total = await db.fetch_user_fine_total(chat_id, uid, stats_query)

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

    from handover_manager import handover_manager

    now = db.get_beijing_time()
    stats_query = await handover_manager.resolve_activity_stats_query(
        chat_id, shift, now
    )
    business_date = stats_query["business_date"]

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

    for act in activity_limits.keys():
        try:
            rows = await db.fetch_activity_rank_rows(chat_id, act, stats_query)

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
    """处理打卡消息下方的内联回座按钮"""
    try:
        data_parts = callback_query.data.split(":")

        if len(data_parts) < 4:
            logger.warning(f"⚠️ 快速回座数据格式错误: {callback_query.data}")
            await callback_query.answer("❌ 按钮数据格式错误", show_alert=True)
            return

        chat_id = int(data_parts[1])
        uid = int(data_parts[2])
        shift = data_parts[3] if len(data_parts) > 3 else "day"

        if callback_query.from_user.id != uid:
            await callback_query.answer("❌ 这不是您的回座按钮！", show_alert=True)
            return

        if callback_query.message.chat.id != chat_id:
            await callback_query.answer("❌ 群组不匹配", show_alert=True)
            return

        logger.info(f"🔄 内联回座: 用户{uid}, 群组{chat_id}, 班次{shift}")

        user_data = await db.get_user_cached(chat_id, uid)

        if not user_data or not user_data.get("current_activity"):
            await callback_query.answer("❌ 您当前没有活动在进行", show_alert=True)
            return

        await _process_back_locked(
            callback_query.message,
            chat_id,
            uid,
            shift,
            user_trigger_message=callback_query.message,
        )

        try:
            await callback_query.message.edit_reply_markup(reply_markup=None)
        except Exception as e:
            logger.warning(f"无法移除内联按钮: {e}")

        await callback_query.answer("✅ 已成功回座")

    except ValueError as e:
        logger.error(f"❌ 快速回座参数解析失败: {e}")
        await callback_query.answer("❌ 数据格式错误", show_alert=True)
    except Exception as e:
        logger.error(f"❌ 快速回座失败: {e}", exc_info=True)
        await callback_query.answer("❌ 回座失败，请点底部回座按钮或 /at", show_alert=True)


