import asyncio
import logging
import re
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
    calculate_fine, get_quote_id, get_beijing_time,
)
from fault_tolerance import Watchdog
from handover_manager import handover_manager
from decorators import admin_required
from bot_manager import bot_manager

logger = logging.getLogger("GroupCheckInBot")

TIME_PATTERN = re.compile(r"^([0-1]?[0-9]|2[0-3]):([0-5][0-9])$")
PUSH_SETTING_KEYS = {
    "ch": "enable_channel_push",
    "channel": "enable_channel_push",
    "gr": "enable_group_push",
    "group": "enable_group_push",
    "ad": "enable_admin_push",
    "admin": "enable_admin_push",
}


from export_service import (
    export_and_push_csv, generate_monthly_report,
    get_monthly_stats_compatible, ensure_monthly_data_completeness,
)
from reset_service import (
    handle_hard_reset, reset_daily_data_if_needed,
    _export_yesterday_data_concurrent, _export_monthly_data_concurrent,
)
# ========== 管理员命令 ==========
@admin_required
@rate_limit(rate=5, per=60)
async def cmd_admin(message: types.Message):
    """管理员命令"""
    await message.answer(
        "👑 管理员面板",
        reply_markup=get_admin_keyboard(),
        reply_to_message_id=message.message_id,
    )


@admin_required
@rate_limit(rate=3, per=30)
async def cmd_setdualmode(message: types.Message):
    """设置双班模式"""
    args = message.text.split()
    chat_id = message.chat.id

    if len(args) < 2:
        await message.answer(
            "❌ 用法：\n"
            "• 开启双班: /setdualmode on <白班开始时间> <白班结束时间>\n"
            "• 关闭双班: /setdualmode off\n\n"
            "💡 示例:\n"
            "/setdualmode on 09:00 21:00\n"
            "/setdualmode off",
            reply_to_message_id=message.message_id,
        )
        return

    mode = args[1].lower()

    try:
        business_date = await db.get_business_date(chat_id)

        if mode == "on":
            if len(args) != 4:
                await message.answer(
                    "❌ 开启双班模式需要指定白班时间\n"
                    "📝 示例: /setdualmode on 09:00 21:00",
                    reply_to_message_id=message.message_id,
                )
                return

            day_start = args[2]
            day_end = args[3]

            time_pattern = TIME_PATTERN

            if not time_pattern.match(day_start) or not time_pattern.match(day_end):
                await message.answer(
                    "❌ 时间格式错误，请使用 HH:MM 格式",
                    reply_to_message_id=message.message_id,
                )
                return

            async with db.pool.acquire() as conn:
                async with conn.transaction():
                    delete_result = await conn.execute(
                        """
                        DELETE FROM group_shift_state
                        WHERE chat_id = $1
                        AND record_date < $2
                        """,
                        chat_id,
                        business_date,
                    )
                    deleted_count = parse_sql_row_count(delete_result, "DELETE")

                    active_count = (
                        await conn.fetchval(
                            """
                        SELECT COUNT(*)
                        FROM group_shift_state
                        WHERE chat_id = $1
                        AND record_date = $2
                        """,
                            chat_id,
                            business_date,
                        )
                        or 0
                    )

                    await db.update_group_dual_mode(chat_id, True, day_start, day_end)

            if deleted_count > 0:
                business_date_str = str(business_date)
                keys_to_remove = []

                for key in list(db._cache.keys()):
                    if not key.startswith(f"shift_state:{chat_id}:"):
                        continue

                    cache_key = key
                    if cache_key in db._cache_ttl:
                        keys_to_remove.append(cache_key)

                for key in keys_to_remove:
                    db._cache.pop(key, None)
                    db._cache_ttl.pop(key, None)

                logger.info(f"✅ 已清理 {len(keys_to_remove)} 个历史缓存")

            from keyboards import invalidate_main_keyboard_cache
            invalidate_main_keyboard_cache(chat_id)

            await message.answer(
                f"✅ 双班模式已开启\n\n"
                f"📊 配置信息:\n"
                f"• 白班时间: <code>{day_start} - {day_end}</code>\n"
                f"• 夜班时间: 自动推算\n\n"
                f"📈 状态清理:\n"
                f"• 清除历史状态: <code>{deleted_count}</code> 个\n"
                f"• 保留今天状态: <code>{active_count}</code> 个\n\n"
                f"💡 注意事项:\n"
                f"• 一个账号可支持两人轮班\n"
                f"• 上班行为创建班次状态\n"
                f"• 下班行为结束当前班次",
                parse_mode="HTML",
                reply_to_message_id=message.message_id,
            )

        elif mode == "off":
            async with db.pool.acquire() as conn:
                async with conn.transaction():
                    delete_result = await conn.execute(
                        """
                        DELETE FROM group_shift_state
                        WHERE chat_id = $1
                        AND record_date < $2
                        """,
                        chat_id,
                        business_date,
                    )
                    deleted_count = parse_sql_row_count(delete_result, "DELETE")

                    active_count = (
                        await conn.fetchval(
                            """
                        SELECT COUNT(*)
                        FROM group_shift_state
                        WHERE chat_id = $1
                        AND record_date = $2
                        """,
                            chat_id,
                            business_date,
                        )
                        or 0
                    )

                    await db.update_group_dual_mode(chat_id, False, None, None)

            if deleted_count > 0:
                business_date_str = str(business_date)
                keys_to_remove = []

                for key in list(db._cache.keys()):
                    if not key.startswith(f"shift_state:{chat_id}:"):
                        continue

                    cache_key = key
                    if cache_key in db._cache_ttl:
                        keys_to_remove.append(cache_key)

                for key in keys_to_remove:
                    db._cache.pop(key, None)
                    db._cache_ttl.pop(key, None)

                logger.info(f"✅ 已清理 {len(keys_to_remove)} 个历史缓存")

            from keyboards import invalidate_main_keyboard_cache
            invalidate_main_keyboard_cache(chat_id)

            if active_count > 0:
                await message.answer(
                    f"✅ 双班模式已关闭\n\n"
                    f"📈 状态清理:\n"
                    f"• 清除历史状态: <code>{deleted_count}</code> 个\n"
                    f"• <b>⚠️ 发现 {active_count} 个今天的活跃班次</b>\n"
                    f"• 这些班次会被保留，但切换到单班模式后可能需要手动结束\n\n"
                    f"💡 建议用户手动结束今天的班次，或等待系统自动清理",
                    parse_mode="HTML",
                    reply_to_message_id=message.message_id,
                )
            else:
                await message.answer(
                    f"✅ 双班模式已关闭\n\n"
                    f"📈 状态清理:\n"
                    f"• 清除历史状态: <code>{deleted_count}</code> 个\n"
                    f"• 没有今天的活跃状态",
                    parse_mode="HTML",
                    reply_to_message_id=message.message_id,
                )

        else:
            await message.answer(
                "❌ 参数错误，请使用 'on' 或 'off'",
                reply_to_message_id=message.message_id,
            )

    except Exception as e:
        logger.exception(f"设置双班模式失败: {e}")
        await message.answer(
            f"❌ 设置失败: {str(e)[:200]}",
            reply_to_message_id=message.message_id,
        )


@admin_required
@rate_limit(rate=3, per=30)
async def cmd_setshiftgrace(message: types.Message):
    """设置时间宽容窗口"""
    args = message.text.split()
    chat_id = message.chat.id

    if len(args) != 3:
        await message.answer(
            "❌ 用法: /setshiftgrace <上班前允许分钟> <下班后允许分钟>\n"
            "💡 示例: /setshiftgrace 120 360\n\n"
            "📊 默认值:\n"
            "• 上班前: 120 分钟 (2小时)\n"
            "• 下班后: 360 分钟 (6小时)",
            reply_to_message_id=message.message_id,
        )
        return

    try:
        grace_before = int(args[1])
        grace_after = int(args[2])

        if grace_before < 0 or grace_after < 0:
            await message.answer(
                "❌ 时间窗口不能为负数", reply_to_message_id=message.message_id
            )
            return

        await db.update_shift_grace_window(chat_id, grace_before, grace_after)

        await message.answer(
            f"✅ 时间宽容窗口已更新\n\n"
            f"📊 新设置:\n"
            f"• 上班前允许: <code>{grace_before}</code> 分钟\n"
            f"• 下班后允许: <code>{grace_after}</code> 分钟\n\n"
            f"💡 此设置影响双班模式下的打卡时间判定",
            parse_mode="HTML",
            reply_to_message_id=message.message_id,
        )

    except ValueError:
        await message.answer(
            "❌ 请输入有效的数字", reply_to_message_id=message.message_id
        )
    except Exception as e:
        logger.error(f"设置时间窗口失败: {e}")
        await message.answer(
            f"❌ 设置失败: {e}", reply_to_message_id=message.message_id
        )


@admin_required
@rate_limit(rate=3, per=30)
async def cmd_setworkendgrace(message: types.Message):
    """设置下班专用时间窗口"""
    args = message.text.split()
    chat_id = message.chat.id

    if len(args) != 3:
        await message.answer(
            "❌ 用法: /setworkendgrace <下班前允许分钟> <下班后允许分钟>\n"
            "💡 示例: /setworkendgrace 120 360\n\n"
            "📊 默认值:\n"
            "• 下班前: 120 分钟 (2小时)\n"
            "• 下班后: 360 分钟 (6小时)",
            reply_to_message_id=message.message_id,
        )
        return

    try:
        before = int(args[1])
        after = int(args[2])

        if before < 0 or after < 0:
            await message.answer(
                "❌ 时间窗口不能为负数", reply_to_message_id=message.message_id
            )
            return

        await db.update_workend_grace_window(chat_id, before, after)

        await message.answer(
            f"✅ 下班时间窗口已更新\n\n"
            f"📊 新设置:\n"
            f"• 下班前允许: <code>{before}</code> 分钟\n"
            f"• 下班后允许: <code>{after}</code> 分钟",
            parse_mode="HTML",
            reply_to_message_id=message.message_id,
        )

    except ValueError:
        await message.answer(
            "❌ 请输入有效的数字", reply_to_message_id=message.message_id
        )
    except Exception as e:
        logger.error(f"设置下班时间窗口失败: {e}")
        await message.answer(
            f"❌ 设置失败: {e}", reply_to_message_id=message.message_id
        )


@admin_required
@rate_limit(rate=2, per=60)
async def cmd_fix_message_refs(message: types.Message):
    """修复消息引用（清除所有消息ID）"""
    chat_id = message.chat.id

    try:
        await message.answer("⏳ 正在清除所有消息引用记录...")

        result = await db.execute_with_retry(
            "修复消息引用",
            """
            UPDATE users 
            SET checkin_message_id = NULL, updated_at = CURRENT_TIMESTAMP 
            WHERE chat_id = $1 AND checkin_message_id IS NOT NULL
            """,
            chat_id,
        )

        updated_count = 0
        if result and result.startswith("UPDATE"):
            parts = result.split()
            if len(parts) >= 2:
                updated_count = int(parts[-1])

        await message.answer(
            f"✅ 已清除 {updated_count} 个消息引用记录\n"
            f"💡 下次打卡将重新建立正确的消息引用",
            reply_markup=await get_main_keyboard(chat_id=chat_id, show_admin=True),
            parse_mode="HTML",
        )
        logger.info(
            f"管理员 {message.from_user.id} 清除了群组 {chat_id} 的 {updated_count} 个消息引用"
        )

    except Exception as e:
        logger.error(f"修复消息引用失败: {e}")
        await message.answer(
            f"❌ 修复失败：{str(e)[:200]}",
            reply_markup=await get_main_keyboard(chat_id=chat_id, show_admin=True),
        )


@admin_required
@rate_limit(rate=2, per=60)
async def cmd_cleanup_monthly(message: types.Message):
    """清理月度统计数据"""
    args = message.text.split()

    target_date = None
    if len(args) >= 3:
        try:
            year = int(args[1])
            month = int(args[2])
            if month < 1 or month > 12:
                await message.answer("❌ 月份必须在1-12之间")
                return
            target_date = date(year, month, 1)
        except ValueError:
            await message.answer("❌ 请输入有效的年份和月份")
            return
    elif len(args) == 2 and args[1].lower() == "all":
        await message.answer(
            "⚠️ <b>危险操作确认</b>\n\n"
            "您即将删除<u>所有</u>月度统计数据！\n"
            "此操作不可恢复！\n\n"
            "请输入 <code>/cleanup_monthly confirm_all</code> 确认执行",
            parse_mode="HTML",
            reply_to_message_id=message.message_id,
        )
        return
    elif len(args) == 2 and args[1].lower() == "confirm_all":
        try:
            async with db.pool.acquire() as conn:
                result = await conn.execute("DELETE FROM monthly_statistics")
                deleted_count = (
                    int(result.split()[-1])
                    if result and result.startswith("DELETE")
                    else 0
                )

            await message.answer(
                f"🗑️ <b>已清理所有月度统计数据</b>\n"
                f"删除记录: <code>{deleted_count}</code> 条\n\n"
                f"⚠️ 所有月度统计已被清空，月度报告将无法生成历史数据",
                parse_mode="HTML",
                reply_to_message_id=message.message_id,
            )
            logger.warning(f"👑 管理员 {message.from_user.id} 清理了所有月度统计数据")
            return
        except Exception as e:
            await message.answer(
                f"❌ 清理所有数据失败: {e}", reply_to_message_id=message.message_id
            )
            return

    await message.answer(
        "⏳ 正在清理月度统计数据...", reply_to_message_id=message.message_id
    )

    try:
        if target_date:
            deleted_count = await db.cleanup_specific_month(
                target_date.year, target_date.month
            )
            date_str = target_date.strftime("%Y年%m月")
            await message.answer(
                f"✅ <b>月度统计清理完成</b>\n"
                f"📅 清理月份: <code>{date_str}</code>\n"
                f"🗑️ 删除记录: <code>{deleted_count}</code> 条",
                parse_mode="HTML",
                reply_to_message_id=message.message_id,
            )
        else:
            deleted_count = await db.cleanup_monthly_data()
            today = get_beijing_time()
            cutoff_date = (today - timedelta(days=90)).date().replace(day=1)
            cutoff_str = cutoff_date.strftime("%Y年%m月")

            await message.answer(
                f"✅ <b>月度统计自动清理完成</b>\n"
                f"📅 清理截止: <code>{cutoff_str}</code> 之前\n"
                f"🗑️ 删除记录: <code>{deleted_count}</code> 条\n\n"
                f"💡 保留了最近3个月的月度统计数据",
                parse_mode="HTML",
                reply_to_message_id=message.message_id,
            )

    except Exception as e:
        logger.error(f"❌ 清理月度数据失败: {e}")
        await message.answer(
            f"❌ 清理月度数据失败: {e}", reply_to_message_id=message.message_id
        )


@admin_required
@rate_limit(rate=5, per=60)
async def cmd_monthly_stats_status(message: types.Message):
    """查看月度统计数据状态"""
    chat_id = message.chat.id

    try:
        async with db.pool.acquire() as conn:
            monthly_rows = await conn.fetch(
                """
                SELECT
                    DATE_TRUNC('month', statistic_date) AS month,
                    COUNT(*) AS total_records,
                    COUNT(DISTINCT user_id) AS monthly_users,
                    COUNT(DISTINCT activity_name) AS monthly_activities
                FROM monthly_statistics
                WHERE chat_id = $1
                GROUP BY month
                ORDER BY month DESC
                """,
                chat_id,
            )

            total_records = await conn.fetchval(
                "SELECT COUNT(*) FROM monthly_statistics WHERE chat_id = $1",
                chat_id,
            )
            total_users = await conn.fetchval(
                "SELECT COUNT(DISTINCT user_id) FROM monthly_statistics WHERE chat_id = $1",
                chat_id,
            )
            total_activities = await conn.fetchval(
                "SELECT COUNT(DISTINCT activity_name) FROM monthly_statistics WHERE chat_id = $1",
                chat_id,
            )

        if not monthly_rows:
            await message.answer(
                "📊 <b>月度统计数据状态</b>\n\n暂无月度统计数据",
                parse_mode="HTML",
                reply_to_message_id=message.message_id,
            )
            return

        earliest = min(row["month"] for row in monthly_rows)
        latest = max(row["month"] for row in monthly_rows)

        status_text = (
            f"📊 <b>月度统计数据状态</b>\n\n"
            f"📅 数据范围: <code>{earliest.strftime('%Y年%m月')}</code> - <code>{latest.strftime('%Y年%m月')}</code>\n"
            f"👥 总用户数: <code>{total_users}</code> 人\n"
            f"📝 活动类型总数: <code>{total_activities}</code> 种\n"
            f"💾 总记录数: <code>{total_records}</code> 条\n\n"
            f"<b>最近12个月数据量:</b>\n"
        )

        for row in monthly_rows[:12]:
            month_str = row["month"].strftime("%Y年%m月")
            total = row["total_records"]
            users = row["monthly_users"]
            acts = row["monthly_activities"]
            status_text += f"• {month_str}: <code>{total}</code> 条, 用户 <code>{users}</code> 人, 活动类型 <code>{acts}</code> 种\n"

        if len(monthly_rows) > 12:
            status_text += f"• ... 还有 {len(monthly_rows) - 12} 个月份\n"

        status_text += (
            "\n💡 <b>可用命令:</b>\n"
            "• <code>/cleanup_monthly</code> - 自动清理（保留最近3个月）\n"
            "• <code>/cleanup_monthly 年 月</code> - 清理指定月份\n"
            "• <code>/cleanup_monthly all</code> - 清理所有数据（危险）"
        )

        await message.answer(
            status_text,
            parse_mode="HTML",
            reply_to_message_id=message.message_id,
        )

    except Exception as e:
        logger.error(f"❌ 查看月度统计状态失败: {e}")
        await message.answer(
            "❌ 查看月度统计状态失败，请稍后重试",
            reply_to_message_id=message.message_id,
        )


@admin_required
@rate_limit(rate=1, per=60)
async def cmd_cleanup_inactive(message: types.Message):
    """清理长期未活动的用户数据"""
    args = message.text.split()
    days = 30

    if len(args) > 1:
        try:
            days = int(args[1])
            if days < 7:
                await message.answer(
                    "❌ 天数不能少于7天，避免误删活跃用户",
                    reply_to_message_id=message.message_id,
                )
                return
        except ValueError:
            await message.answer(
                "❌ 天数必须是数字，例如：/cleanup_inactive 60",
                reply_to_message_id=message.message_id,
            )
            return

    await message.answer(
        f"⏳ 正在清理 {days} 天未活动的用户，请稍候...",
        reply_to_message_id=message.message_id,
    )

    cutoff_date = (get_beijing_time() - timedelta(days=days)).date()

    try:
        async with db.pool.acquire() as conn:
            result_users = await conn.execute(
                "DELETE FROM users WHERE last_updated < $1", cutoff_date
            )
            deleted_users = (
                int(result_users.split()[-1])
                if result_users.startswith("DELETE")
                else 0
            )

            result_activities = await conn.execute(
                "DELETE FROM user_activities WHERE activity_date < $1", cutoff_date
            )
            deleted_activities = (
                int(result_activities.split()[-1])
                if result_activities.startswith("DELETE")
                else 0
            )

            result_work = await conn.execute(
                "DELETE FROM work_records WHERE record_date < $1", cutoff_date
            )
            deleted_work_records = (
                int(result_work.split()[-1]) if result_work.startswith("DELETE") else 0
            )

        total_deleted = deleted_users + deleted_activities + deleted_work_records

        await message.answer(
            f"🧹 <b>长期未活动用户清理完成</b>\n\n"
            f"📅 清理截止: <code>{cutoff_date}</code> 之前\n"
            f"🗑️ 删除用户: <code>{deleted_users}</code> 个\n"
            f"🗑️ 删除活动记录: <code>{deleted_activities}</code> 条\n"
            f"🗑️ 删除工作记录: <code>{deleted_work_records}</code> 条\n\n"
            f"📊 总计删除: <code>{total_deleted}</code> 条记录\n"
            f"⚠️ 此操作不可撤销",
            parse_mode="HTML",
            reply_to_message_id=message.message_id,
        )

        logger.info(
            f"👑 管理员 {message.from_user.id} 清理 {days} 天未活动用户: "
            f"{deleted_users} 用户, {deleted_activities} 活动, {deleted_work_records} 工作记录"
        )

    except Exception as e:
        logger.exception("❌ 清理未活动用户失败")
        await message.answer(
            f"❌ 清理未活动用户失败: {e}", reply_to_message_id=message.message_id
        )


@admin_required
@rate_limit(rate=3, per=30)
async def cmd_reset_user(message: types.Message):
    """重置指定用户的今日数据"""
    args = message.text.split()
    if len(args) < 2:
        await message.answer(
            "❌ 用法：/resetuser <用户ID> [confirm]\n"
            "💡 示例：/resetuser 123456789 confirm",
            reply_markup=await get_main_keyboard(
                chat_id=message.chat.id, show_admin=True
            ),
            reply_to_message_id=message.message_id,
        )
        return

    try:
        chat_id = message.chat.id
        target_user_id = int(args[1])
        confirm = len(args) == 3 and args[2].lower() == "confirm"

        if not confirm:
            await message.answer(
                f"⚠️ 确认重置用户 <code>{target_user_id}</code> 的今日数据？\n"
                f"请输入 <code>/resetuser {target_user_id} confirm</code> 执行",
                parse_mode="HTML",
                reply_to_message_id=message.message_id,
            )
            return

        await message.answer(
            f"⏳ 正在重置用户 {target_user_id} 的今日数据...",
            reply_to_message_id=message.message_id,
        )

        success = await db.reset_user_daily_data(chat_id, target_user_id)

        if success:
            await message.answer(
                f"✅ 已重置用户 <code>{target_user_id}</code> 的今日数据\n\n"
                f"🗑️ 已清除：今日活动记录 | 今日统计计数 | 当前活动状态 | 罚款计数（保留总罚款）",
                parse_mode="HTML",
                reply_markup=await get_main_keyboard(chat_id=chat_id, show_admin=True),
                reply_to_message_id=message.message_id,
            )
            logger.info(
                f"👑 管理员 {message.from_user.id} 在群 {chat_id} 重置了用户 {target_user_id} 的今日数据"
            )
        else:
            await message.answer(
                f"❌ 重置用户 {target_user_id} 数据失败",
                reply_markup=await get_main_keyboard(chat_id=chat_id, show_admin=True),
                reply_to_message_id=message.message_id,
            )

    except ValueError:
        await message.answer(
            "❌ 用户ID必须是数字",
            reply_markup=await get_main_keyboard(
                chat_id=message.chat.id, show_admin=True
            ),
            reply_to_message_id=message.message_id,
        )
    except Exception as e:
        logger.exception(f"重置用户数据失败")
        await message.answer(
            f"❌ 重置失败：{e}",
            reply_markup=await get_main_keyboard(
                chat_id=message.chat.id, show_admin=True
            ),
            reply_to_message_id=message.message_id,
        )


@admin_required
@rate_limit(rate=5, per=60)
async def cmd_export(message: types.Message):
    """导出数据"""
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


@admin_required
@rate_limit(rate=2, per=60)
async def cmd_monthlyreport(message: types.Message):
    """生成月度报告（使用新架构导出）"""
    args = message.text.split()
    chat_id = message.chat.id

    year = None
    month = None

    if len(args) >= 3:
        try:
            year = int(args[1])
            month = int(args[2])
            if month < 1 or month > 12:
                await message.answer(
                    "❌ 月份必须在1-12之间", reply_to_message_id=message.message_id
                )
                return
        except ValueError:
            await message.answer(
                "❌ 请输入有效的年份和月份", reply_to_message_id=message.message_id
            )
            return

    await message.answer(
        "⏳ 正在生成月度报告，请稍候...", reply_to_message_id=message.message_id
    )

    try:
        # 先生成报告（保持原样）
        report = await generate_monthly_report(chat_id, year, month)

        if report:
            # 发送报告
            await message.answer(
                report, parse_mode="HTML", reply_to_message_id=message.message_id
            )

            # ✅ 统一使用新架构导出数据
            if year and month:
                logger.info(
                    f"📊 管理员 {message.from_user.id} 请求导出 {year}年{month}月 数据"
                )

                # 使用新架构导出
                success = await _export_monthly_data_concurrent(
                    chat_id=chat_id, year=year, month=month
                )

                if success:
                    await message.answer(
                        "✅ 月度数据已导出并推送！",
                        reply_to_message_id=message.message_id,
                    )
                else:
                    await message.answer(
                        "⚠️ 数据导出失败，但报告已生成\n" "请检查日志或联系开发人员",
                        reply_to_message_id=message.message_id,
                    )
            else:
                # 如果没有指定年月，使用当前月份
                today = db.get_beijing_time()
                year = today.year
                month = today.month

                success = await _export_monthly_data_concurrent(
                    chat_id=chat_id, year=year, month=month
                )

                if success:
                    await message.answer(
                        f"✅ {year}年{month}月数据已导出并推送！",
                        reply_to_message_id=message.message_id,
                    )
        else:
            time_desc = f"{year}年{month}月" if year and month else "最近一个月"
            await message.answer(
                f"⚠️ {time_desc}没有数据需要报告", reply_to_message_id=message.message_id
            )

    except Exception as e:
        logger.error(f"❌ 生成月度报告失败: {e}")
        logger.error(traceback.format_exc())
        await message.answer(
            f"❌ 生成月度报告失败：{e}", reply_to_message_id=message.message_id
        )


async def ensure_monthly_data_completeness(stats: List[Dict]) -> List[Dict]:
    """确保月度统计数据的完整性"""
    if not stats:
        return []

    result = []
    for stat in stats:
        stat.setdefault("work_start_count", 0)
        stat.setdefault("work_end_count", 0)
        stat.setdefault("work_start_fines", 0)
        stat.setdefault("work_end_fines", 0)
        stat.setdefault("late_count", 0)
        stat.setdefault("early_count", 0)
        stat.setdefault("work_days", 0)
        stat.setdefault("work_hours", 0)

        if "activities" not in stat or not isinstance(stat["activities"], dict):
            stat["activities"] = {}

        result.append(stat)

    return result


async def get_monthly_stats_compatible(chat_id: int, target_date: date) -> List[Dict]:
    """兼容函数：获取月度统计数据并确保完整性"""
    try:
        month_start = target_date.replace(day=1)
        monthly_stats = await db.get_monthly_statistics(
            chat_id, month_start.year, month_start.month
        )

        if not monthly_stats:
            return []

        return await ensure_monthly_data_completeness(monthly_stats)

    except Exception as e:
        logger.error(f"获取兼容月度数据失败: {e}")
        return []


@admin_required
@rate_limit(rate=2, per=60)
async def cmd_exportmonthly(message: types.Message):
    """导出月度数据（使用新架构）"""
    args = message.text.split()
    chat_id = message.chat.id
    uid = message.from_user.id

    year = None
    month = None

    if len(args) >= 3:
        try:
            year = int(args[1])
            month = int(args[2])
            if month < 1 or month > 12:
                await message.answer(
                    "❌ 月份必须在1-12之间", reply_to_message_id=message.message_id
                )
                return
        except ValueError:
            await message.answer(
                "❌ 请输入有效的年份和月份", reply_to_message_id=message.message_id
            )
            return
    else:
        # 如果没有指定，默认导出上个月
        today = db.get_beijing_time()
        if today.month == 1:
            year = today.year - 1
            month = 12
        else:
            year = today.year
            month = today.month - 1

        await message.answer(
            f"📅 未指定月份，默认导出 {year}年{month}月 数据",
            reply_to_message_id=message.message_id,
        )

    await message.answer(
        f"⏳ 正在导出 {year}年{month}月 数据，请稍候...",
        reply_to_message_id=message.message_id,
    )

    try:
        # ✅ 使用新架构导出
        success = await _export_monthly_data_concurrent(
            chat_id=chat_id, year=year, month=month
        )

        if success:
            logger.info(
                f"👑 管理员 {uid} 成功导出群组 {chat_id} 的 {year}年{month}月 数据"
            )
            await message.answer(
                f"✅ {year}年{month}月 数据已导出并推送！",
                reply_to_message_id=message.message_id,
            )
        else:
            logger.error(
                f"❌ 管理员 {uid} 导出群组 {chat_id} 的 {year}年{month}月 数据失败"
            )
            await message.answer(
                f"❌ {year}年{month}月 数据导出失败，请检查日志",
                reply_to_message_id=message.message_id,
            )

    except Exception as e:
        logger.error(f"❌ 导出月度数据失败: {e}")
        logger.error(traceback.format_exc())
        await message.answer(
            f"❌ 导出月度数据失败：{e}", reply_to_message_id=message.message_id
        )


@admin_required
@rate_limit(rate=3, per=30)
async def cmd_addactivity(message: types.Message):
    """添加新活动"""
    args = message.text.split()
    if len(args) != 4:
        await message.answer(
            Config.MESSAGES["addactivity_usage"],
            reply_markup=await get_main_keyboard(
                chat_id=message.chat.id, show_admin=True
            ),
            reply_to_message_id=message.message_id,
        )
        return

    try:
        act, max_times, time_limit = args[1], int(args[2]), int(args[3])
        existed = await db.activity_exists(act)
        command_slug = await db.update_activity_config(act, max_times, time_limit)
        await db.force_refresh_activity_cache()

        from activity_commands import sync_bot_commands
        from bot_manager import bot_manager

        if bot_manager.bot:
            await sync_bot_commands(bot_manager.bot)

        if existed:
            await message.answer(
                f"✅ 已修改活动 <code>{act}</code>\n"
                f"• 次数上限：<code>{max_times}</code>\n"
                f"• 时间限制：<code>{time_limit}</code> 分钟\n"
                f"• / 命令：<code>/{command_slug}</code>",
                reply_markup=await get_main_keyboard(
                    chat_id=message.chat.id, show_admin=True
                ),
                reply_to_message_id=message.message_id,
                parse_mode="HTML",
            )
        else:
            await message.answer(
                f"✅ 已添加新活动 <code>{act}</code>\n"
                f"• 次数上限：<code>{max_times}</code>\n"
                f"• 时间限制：<code>{time_limit}</code> 分钟\n"
                f"• / 命令已自动生成：<code>/{command_slug}</code>",
                reply_markup=await get_main_keyboard(
                    chat_id=message.chat.id, show_admin=True
                ),
                reply_to_message_id=message.message_id,
                parse_mode="HTML",
            )
    except Exception as e:
        await message.answer(
            f"❌ 添加/修改活动失败：{e}", reply_to_message_id=message.message_id
        )


@admin_required
@rate_limit(rate=3, per=30)
async def cmd_delactivity(message: types.Message):
    """删除活动"""
    args = message.text.split()
    if len(args) != 2:
        await message.answer(
            "❌ 用法：/delactivity <活动名>",
            reply_markup=await get_main_keyboard(
                chat_id=message.chat.id, show_admin=True
            ),
            reply_to_message_id=message.message_id,
        )
        return
    act = args[1]
    if not await db.activity_exists(act):
        await message.answer(
            f"❌ 活动 <code>{act}</code> 不存在",
            reply_markup=await get_main_keyboard(
                chat_id=message.chat.id, show_admin=True
            ),
            reply_to_message_id=message.message_id,
            parse_mode="HTML",
        )
        return

    await db.delete_activity_config(act)
    await db.force_refresh_activity_cache()

    from activity_commands import sync_bot_commands
    from bot_manager import bot_manager

    if bot_manager.bot:
        await sync_bot_commands(bot_manager.bot)

    await message.answer(
        f"✅ 活动 <code>{act}</code> 已删除",
        reply_markup=await get_main_keyboard(chat_id=message.chat.id, show_admin=True),
        reply_to_message_id=message.message_id,
        parse_mode="HTML",
    )
    logger.info(f"删除活动: {act}")


@admin_required
@rate_limit(rate=3, per=30)
async def cmd_setworktime(message: types.Message):
    """设置上下班时间"""
    args = message.text.split()
    if len(args) != 3:
        await message.answer(
            "❌ 用法：/setworktime <上班时间> <下班时间>\n"
            "📝 示例：/setworktime 09:00 18:00\n"
            "💡 时间格式：HH:MM (24小时制)",
            reply_markup=await get_main_keyboard(
                chat_id=message.chat.id, show_admin=True
            ),
            reply_to_message_id=message.message_id,
        )
        return

    try:
        work_start = args[1]
        work_end = args[2]

        time_pattern = TIME_PATTERN

        if not time_pattern.match(work_start) or not time_pattern.match(work_end):
            await message.answer(
                "❌ 时间格式错误！请使用 HH:MM 格式（24小时制）\n"
                "📝 示例：09:00、18:30",
                reply_markup=await get_main_keyboard(
                    chat_id=message.chat.id, show_admin=True
                ),
                reply_to_message_id=message.message_id,
            )
            return

        chat_id = message.chat.id
        await db.update_group_work_time(chat_id, work_start, work_end)

        # ===== 新增：强制刷新缓存 =====
        # 清除群组缓存，确保下次获取最新配置
        cache_key = f"work_time:{chat_id}"
        db._cache.pop(cache_key, None)
        db._cache_ttl.pop(cache_key, None)

        # 清除群组主缓存
        db._cache.pop(f"group:{chat_id}", None)
        db._cache_ttl.pop(f"group:{chat_id}", None)

        logger.info(f"✅ 已清除工作时间缓存: {chat_id}")
        from keyboards import invalidate_main_keyboard_cache
        invalidate_main_keyboard_cache(chat_id)
        # ===== 新增结束 =====

        # 发送成功消息时，立即生成带有上班/下班按钮的键盘
        await message.answer(
            f"✅ 上下班时间设置成功！\n\n"
            f"🟢 上班时间：<code>{work_start}</code>\n"
            f"🔴 下班时间：<code>{work_end}</code>\n\n"
            f"💡 上下班打卡功能已启用，按钮将在下次操作时显示",
            reply_markup=await get_main_keyboard(
                chat_id=message.chat.id, show_admin=True
            ),
            reply_to_message_id=message.message_id,
            parse_mode="HTML",
        )

    except Exception as e:
        logger.error(f"设置工作时间失败: {e}")
        await message.answer(
            f"❌ 设置失败：{e}",
            reply_markup=await get_main_keyboard(
                chat_id=message.chat.id, show_admin=True
            ),
            reply_to_message_id=message.message_id,
        )


@admin_required
@rate_limit(rate=3, per=30)
async def cmd_setresettime(message: types.Message):
    """设置每日重置时间"""
    args = message.text.split()
    if len(args) != 3:
        await message.answer(
            Config.MESSAGES["setresettime_usage"],
            reply_markup=await get_main_keyboard(
                chat_id=message.chat.id, show_admin=True
            ),
            reply_to_message_id=message.message_id,
        )
        return

    try:
        hour = int(args[1])
        minute = int(args[2])

        if 0 <= hour <= 23 and 0 <= minute <= 59:
            chat_id = message.chat.id
            await db.init_group(chat_id)
            await db.update_group_reset_time(chat_id, hour, minute)

            await handle_hard_reset(chat_id, message.from_user.id)

            await message.answer(
                f"✅ 每日重置时间已设置为：<code>{hour:02d}:{minute:02d}</code>\n\n"
                f"💡 每天此时将自动重置所有用户的打卡数据",
                reply_markup=await get_main_keyboard(
                    chat_id=message.chat.id, show_admin=True
                ),
                reply_to_message_id=message.message_id,
                parse_mode="HTML",
            )
            logger.info(f"重置时间设置成功: 群组 {chat_id} -> {hour:02d}:{minute:02d}")
        else:
            await message.answer(
                "❌ 小时必须在0-23之间，分钟必须在0-59之间！\n"
                "💡 示例：/setresettime 0 0 （午夜重置）",
                reply_markup=await get_main_keyboard(
                    chat_id=message.chat.id, show_admin=True
                ),
                reply_to_message_id=message.message_id,
            )
    except ValueError:
        await message.answer(
            "❌ 请输入有效的数字！\n" "💡 示例：/setresettime 4 0 （凌晨4点重置）",
            reply_markup=await get_main_keyboard(
                chat_id=message.chat.id, show_admin=True
            ),
            reply_to_message_id=message.message_id,
        )
    except Exception as e:
        logger.error(f"设置重置时间失败: {e}")
        await message.answer(
            f"❌ 设置失败：{e}",
            reply_markup=await get_main_keyboard(
                chat_id=message.chat.id, show_admin=True
            ),
            reply_to_message_id=message.message_id,
        )


@admin_required
@rate_limit(rate=5, per=60)
async def cmd_resettime(message: types.Message):
    """查看当前重置时间"""
    chat_id = message.chat.id
    try:
        group_data = await db.get_group_cached(chat_id)
        reset_hour = group_data.get("reset_hour", Config.DAILY_RESET_HOUR)
        reset_minute = group_data.get("reset_minute", Config.DAILY_RESET_MINUTE)

        await message.answer(
            f"⏰ 当前重置时间设置\n\n"
            f"🕒 重置时间：<code>{reset_hour:02d}:{reset_minute:02d}</code>\n"
            f"📅 每天此时自动重置用户数据\n\n"
            f"💡 使用 /setresettime <小时> <分钟> 修改",
            reply_markup=await get_main_keyboard(chat_id=chat_id, show_admin=True),
            parse_mode="HTML",
            reply_to_message_id=message.message_id,
        )
    except Exception as e:
        logger.error(f"查看重置时间失败: {e}")
        await message.answer(
            f"❌ 获取重置时间失败：{e}", reply_to_message_id=message.message_id
        )


@admin_required
@rate_limit(rate=3, per=30)
async def cmd_delwork_clear(message: types.Message):
    """移除上下班功能并清除所有记录"""
    chat_id = message.chat.id

    if not await db.has_work_hours_enabled(chat_id):
        await message.answer(
            "❌ 当前群组没有设置上下班功能",
            reply_markup=await get_main_keyboard(chat_id=chat_id, show_admin=True),
            reply_to_message_id=message.message_id,
        )
        return

    work_hours = await db.get_group_work_time(chat_id)
    old_start = work_hours.get("work_start")
    old_end = work_hours.get("work_end")

    await message.answer("⏳ 正在移除上下班功能并清除记录...")

    try:
        await db.update_group_work_time(
            chat_id,
            Config.DEFAULT_WORK_HOURS["work_start"],
            Config.DEFAULT_WORK_HOURS["work_end"],
        )

        records_cleared = 0
        try:
            result = await db.execute_with_retry(
                "清除工作记录", "DELETE FROM work_records WHERE chat_id = $1", chat_id
            )
            records_cleared = (
                int(result.split()[-1]) if result and result.startswith("DELETE") else 0
            )
        except Exception as e:
            logger.warning(f"清除工作记录时出现异常: {e}")

        try:
            await db.execute_with_retry(
                "清理月度工作统计",
                """
                UPDATE monthly_statistics 
                SET 
                    work_days = 0,
                    work_hours = 0,
                    work_start_count = 0,
                    work_end_count = 0,
                    work_start_fines = 0,
                    work_end_fines = 0,
                    late_count = 0,
                    early_count = 0,
                    updated_at = CURRENT_TIMESTAMP
                WHERE chat_id = $1
                """,
                chat_id,
            )
        except Exception as e:
            logger.warning(f"清理月度工作统计时出现异常: {e}")

        await db.force_refresh_activity_cache()
        db._cache.pop(f"group:{chat_id}", None)

        success_msg = (
            f"✅ <b>上下班功能已移除</b>\n\n"
            f"🗑️ <b>删除的设置：</b>\n"
            f"   • 上班时间: <code>{old_start}</code>\n"
            f"   • 下班时间: <code>{old_end}</code>\n"
            f"   • 清除记录: <code>{records_cleared}</code> 条\n\n"
            f"🔧 <b>当前状态：</b>\n"
            f"   • 上下班按钮已隐藏\n"
            f"   • 工作相关统计已重置\n"
            f"   • 可正常进行其他活动打卡\n\n"
            f"💡 如需重新启用，请使用 /setworktime 命令"
        )

        await message.answer(
            success_msg,
            reply_markup=await get_main_keyboard(chat_id=chat_id, show_admin=True),
            parse_mode="HTML",
            reply_to_message_id=message.message_id,
        )

        logger.info(
            f"👤 管理员 {message.from_user.id} 移除了群组 {chat_id} 的上下班功能，清除 {records_cleared} 条记录"
        )

    except Exception as e:
        logger.error(f"移除上下班功能失败: {e}")
        await message.answer(
            f"❌ 移除失败：{e}",
            reply_markup=await get_main_keyboard(chat_id=chat_id, show_admin=True),
            reply_to_message_id=message.message_id,
        )


@admin_required
@rate_limit(rate=3, per=30)
async def cmd_setchannel(message: types.Message):
    """绑定提醒频道"""
    chat_id = message.chat.id
    args = message.text.split(maxsplit=1)

    if len(args) < 2:
        await message.answer(
            Config.MESSAGES["setchannel_usage"],
            reply_markup=await get_main_keyboard(
                chat_id=message.chat.id, show_admin=True
            ),
            reply_to_message_id=message.message_id,
        )
        return

    try:
        channel_id = int(args[1].strip())

        if channel_id > 0:
            await message.answer(
                "❌ 频道ID应该是负数格式（如 -100xxx）",
                reply_markup=await get_main_keyboard(
                    chat_id=message.chat.id, show_admin=True
                ),
                reply_to_message_id=message.message_id,
            )
            return

        await db.init_group(chat_id)
        await db.update_group_channel(chat_id, channel_id)

        await message.answer(
            f"✅ 已绑定超时提醒推送频道：<code>{channel_id}</code>\n\n"
            f"💡 超时打卡和迟到/早退通知将推送到此频道\n"
            f"⚠️ 如果推送失败，请检查：\n"
            f"• 频道ID是否正确\n"
            f"• 机器人是否已加入频道\n"
            f"• 机器人是否有发送消息权限",
            reply_markup=await get_main_keyboard(
                chat_id=message.chat.id, show_admin=True
            ),
            reply_to_message_id=message.message_id,
            parse_mode="HTML",
        )

        logger.info(f"频道绑定成功: 群组 {chat_id} -> 频道 {channel_id}")

    except ValueError:
        await message.answer(
            "❌ 频道ID必须是数字格式\n" "💡 示例：/setchannel -1001234567890",
            reply_markup=await get_main_keyboard(
                chat_id=message.chat.id, show_admin=True
            ),
            reply_to_message_id=message.message_id,
        )
    except Exception as e:
        logger.error(f"设置频道失败: {e}")
        await message.answer(
            f"❌ 绑定频道失败：{e}",
            reply_markup=await get_main_keyboard(
                chat_id=message.chat.id, show_admin=True
            ),
            reply_to_message_id=message.message_id,
        )


@admin_required
@rate_limit(rate=3, per=30)
async def cmd_setgroup(message: types.Message):
    """绑定通知群组"""
    chat_id = message.chat.id
    args = message.text.split(maxsplit=1)

    if len(args) < 2:
        await message.answer(
            Config.MESSAGES["setgroup_usage"],
            reply_markup=await get_main_keyboard(
                chat_id=message.chat.id, show_admin=True
            ),
            reply_to_message_id=message.message_id,
        )
        return

    try:
        group_id = int(args[1].strip())
        await db.init_group(chat_id)
        await db.update_group_notification(chat_id, group_id)

        await message.answer(
            f"✅ 已绑定通知群组：<code>{group_id}</code>\n\n"
            f"💡 打卡通知将推送到此群组",
            reply_markup=await get_main_keyboard(
                chat_id=message.chat.id, show_admin=True
            ),
            reply_to_message_id=message.message_id,
            parse_mode="HTML",
        )

        logger.info(f"群组绑定成功: 主群组 {chat_id} -> 通知群组 {group_id}")

    except ValueError:
        await message.answer(
            "❌ 群组ID必须是数字格式",
            reply_markup=await get_main_keyboard(
                chat_id=message.chat.id, show_admin=True
            ),
            reply_to_message_id=message.message_id,
        )
    except Exception as e:
        logger.error(f"设置群组失败: {e}")
        await message.answer(
            f"❌ 绑定群组失败：{e}",
            reply_markup=await get_main_keyboard(
                chat_id=message.chat.id, show_admin=True
            ),
            reply_to_message_id=message.message_id,
        )


@admin_required
@rate_limit(rate=3, per=30)
async def cmd_addextraworkgroup(message: types.Message):
    """添加上下班通知额外推送群组"""
    chat_id = message.chat.id
    args = message.text.split(maxsplit=1)

    if len(args) < 2:
        await message.answer(
            "❌ 用法：/addextraworkgroup <群组ID>\n"
            "📝 示例：/addextraworkgroup -1001234567890\n\n"
            "💡 设置后，上下班打卡通知会额外推送到该群组\n"
            "   原有的所有推送（群组、频道）保持不变",
            reply_markup=await get_main_keyboard(
                chat_id=message.chat.id, show_admin=True
            ),
            reply_to_message_id=message.message_id,
        )
        return

    try:
        extra_group_id = int(args[1].strip())

        if extra_group_id > 0:
            await message.answer(
                "❌ 群组ID应该是负数格式（如 -100xxx）",
                reply_markup=await get_main_keyboard(
                    chat_id=message.chat.id, show_admin=True
                ),
                reply_to_message_id=message.message_id,
            )
            return

        await db.init_group(chat_id)

        group_data = await db.get_group_cached(chat_id)
        channel_id = group_data.get("channel_id") if group_data else None
        notify_group_id = (
            group_data.get("notification_group_id") if group_data else None
        )

        await db.update_group_extra_work_group(chat_id, extra_group_id)

        channel_text = f"频道 <code>{channel_id}</code>" if channel_id else "未设置"
        notify_text = (
            f"群组 <code>{notify_group_id}</code>" if notify_group_id else "当前群组"
        )

        await message.answer(
            f"✅ 已添加上下班通知额外推送群组\n\n"
            f"📊 <b>当前推送配置：</b>\n"
            f"• 原有推送（保持不变）：\n"
            f"  └─ 超时通知 → {channel_text}\n"
            f"  └─ 吃饭通知 → {notify_text}\n"
            f"  └─ 上下班通知 → 当前群组 + {channel_text}\n\n"
            f"• <b>新增额外推送：</b>\n"
            f"  └─ 上下班通知 → 额外群组 <code>{extra_group_id}</code>\n\n"
            f"💡 现在每次上下班打卡，都会额外发送一份到该群组\n"
            f"   如需清除，使用 /clearextraworkgroup 命令",
            reply_markup=await get_main_keyboard(
                chat_id=message.chat.id, show_admin=True
            ),
            reply_to_message_id=message.message_id,
            parse_mode="HTML",
        )

        logger.info(
            f"添加上下班额外通知群组成功: 主群组 {chat_id} -> 额外群组 {extra_group_id}"
        )

    except ValueError:
        await message.answer(
            "❌ 群组ID必须是数字格式",
            reply_markup=await get_main_keyboard(
                chat_id=message.chat.id, show_admin=True
            ),
            reply_to_message_id=message.message_id,
        )
    except Exception as e:
        logger.error(f"添加上下班额外通知群组失败: {e}")
        await message.answer(
            f"❌ 设置失败：{e}",
            reply_markup=await get_main_keyboard(
                chat_id=message.chat.id, show_admin=True
            ),
            reply_to_message_id=message.message_id,
        )


@admin_required
@rate_limit(rate=3, per=30)
async def cmd_clearextraworkgroup(message: types.Message):
    """清除额外的上下班通知群组"""
    chat_id = message.chat.id

    try:
        extra_group_id = await db.get_extra_work_group(chat_id)

        if not extra_group_id:
            await message.answer(
                "⚠️ 当前没有设置额外的上下班通知群组",
                reply_markup=await get_main_keyboard(
                    chat_id=message.chat.id, show_admin=True
                ),
                reply_to_message_id=message.message_id,
            )
            return

        await db.clear_extra_work_group(chat_id)

        await message.answer(
            f"✅ 已清除额外的上下班通知群组 <code>{extra_group_id}</code>\n\n"
            f"📊 现在上下班通知将恢复原有推送逻辑，不再额外推送",
            reply_markup=await get_main_keyboard(
                chat_id=message.chat.id, show_admin=True
            ),
            reply_to_message_id=message.message_id,
            parse_mode="HTML",
        )

        logger.info(f"已清除群组 {chat_id} 的额外上下班通知群组 {extra_group_id}")

    except Exception as e:
        logger.error(f"清除额外上下班通知群组失败: {e}")
        await message.answer(
            f"❌ 清除失败：{e}",
            reply_markup=await get_main_keyboard(
                chat_id=message.chat.id, show_admin=True
            ),
            reply_to_message_id=message.message_id,
        )


@admin_required
@rate_limit(rate=5, per=60)
async def cmd_showeverypush(message: types.Message):
    """显示所有推送配置"""
    chat_id = message.chat.id

    try:
        group_data = await db.get_group_cached(chat_id) or {}
        channel_id = group_data.get("channel_id")
        notify_group_id = group_data.get("notification_group_id")
        extra_work_group_id = await db.get_extra_work_group(chat_id)

        config_text = (
            f"📢 <b>当前推送配置总览</b>\n\n"
            f"<b>🔴 超时通知：</b>\n"
            f"• 推送目标：{f'频道 <code>{channel_id}</code>' if channel_id else '未设置'}\n\n"
            f"<b>🍽️ 吃饭通知：</b>\n"
            f"• 推送目标：{f'群组 <code>{notify_group_id}</code>' if notify_group_id else '当前群组'}\n\n"
            f"<b>🕒 上下班通知：</b>\n"
            f"• 原有推送：当前群组 + {f'频道 <code>{channel_id}</code>' if channel_id else '无'}\n"
            f"• 额外推送：{f'群组 <code>{extra_work_group_id}</code>' if extra_work_group_id else '未设置'}\n\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"💡 <b>管理命令：</b>\n"
            f"• /addextraworkgroup - 添加上下班额外推送群组\n"
            f"• /clearextraworkgroup - 清除额外推送\n"
            f"• /setchannel - 设置超时通知频道\n"
            f"• /setgroup - 设置吃饭通知群组"
        )

        await message.answer(
            config_text,
            reply_markup=await get_main_keyboard(chat_id=chat_id, show_admin=True),
            reply_to_message_id=message.message_id,
            parse_mode="HTML",
        )

    except Exception as e:
        logger.error(f"显示推送配置失败: {e}")
        await message.answer(
            f"❌ 获取配置失败：{e}",
            reply_markup=await get_main_keyboard(chat_id=chat_id, show_admin=True),
            reply_to_message_id=message.message_id,
        )


@admin_required
@rate_limit(rate=3, per=30)
async def cmd_setpush(message: types.Message):
    """设置全局推送开关"""
    args = message.text.split()
    chat_id = message.chat.id

    if len(args) != 3:
        await message.answer(
            Config.MESSAGES["setpush_usage"],
            reply_markup=await get_main_keyboard(chat_id=chat_id, show_admin=True),
            reply_to_message_id=message.message_id,
        )
        return

    target = args[1].lower()
    switch = args[2].lower()
    setting_key = PUSH_SETTING_KEYS.get(target)

    if not setting_key:
        await message.answer(
            "❌ 无效目标，请使用 channel(ch) / group(gr) / admin(ad)",
            reply_markup=await get_main_keyboard(chat_id=chat_id, show_admin=True),
            reply_to_message_id=message.message_id,
        )
        return

    if switch not in ("on", "off"):
        await message.answer(
            "❌ 无效开关，请使用 on 或 off",
            reply_markup=await get_main_keyboard(chat_id=chat_id, show_admin=True),
            reply_to_message_id=message.message_id,
        )
        return

    try:
        enabled = switch == "on"
        await db.update_push_setting(setting_key, enabled)

        labels = {
            "enable_channel_push": "频道推送",
            "enable_group_push": "群组推送",
            "enable_admin_push": "管理员推送",
        }
        await message.answer(
            f"✅ {labels[setting_key]} 已{'开启' if enabled else '关闭'}",
            reply_markup=await get_main_keyboard(chat_id=chat_id, show_admin=True),
            reply_to_message_id=message.message_id,
        )
    except Exception as e:
        logger.error(f"设置推送开关失败: {e}")
        await message.answer(
            f"❌ 设置失败：{e}",
            reply_markup=await get_main_keyboard(chat_id=chat_id, show_admin=True),
            reply_to_message_id=message.message_id,
        )


@admin_required
@rate_limit(rate=2, per=60)
async def cmd_checkdb(message: types.Message):
    """数据库体检"""
    chat_id = message.chat.id

    try:
        conn_ok = await db.connection_health_check()
        healthy = await db.health_check()
        pool_stats = await db.get_pool_stats()

        overall = healthy and conn_ok
        lines = [
            "🏥 <b>数据库体检报告</b>",
            "━━━━━━━━━━━━━━━━",
            f"📊 总体状态：{'✅ 正常' if overall else '❌ 异常'}",
            f"🔗 连接检查：{'✅' if conn_ok else '❌'}",
            f"📋 表访问检查：{'✅' if healthy else '❌'}",
            "",
            "<b>连接池</b>",
            f"• 已初始化：{'是' if pool_stats.get('initialized') else '否'}",
            f"• 连接池存在：{'是' if pool_stats.get('pool_exists') else '否'}",
        ]

        if "total_connections" in pool_stats:
            lines.extend(
                [
                    f"• 总连接数：{pool_stats.get('total_connections', 0)}",
                    f"• 活跃连接：{pool_stats.get('active_connections', 0)}",
                    f"• 空闲连接：{pool_stats.get('idle_connections', 0)}",
                ]
            )
        if pool_stats.get("reconnect_attempts"):
            lines.append(f"• 重连次数：{pool_stats['reconnect_attempts']}")

        await message.answer(
            "\n".join(lines),
            reply_markup=await get_main_keyboard(chat_id=chat_id, show_admin=True),
            reply_to_message_id=message.message_id,
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"数据库体检失败: {e}")
        await message.answer(
            f"❌ 体检失败：{e}",
            reply_markup=await get_main_keyboard(chat_id=chat_id, show_admin=True),
            reply_to_message_id=message.message_id,
        )


@admin_required
@rate_limit(rate=3, per=30)
async def cmd_actnum(message: types.Message):
    """设置活动人数限制"""
    args = message.text.split()
    if len(args) != 3:
        await message.answer(
            "❌ 用法：/actnum <活动名> <人数限制>\n"
            "例如：/actnum 小厕 3\n"
            "💡 设置为0表示取消限制",
            reply_markup=await get_main_keyboard(
                chat_id=message.chat.id, show_admin=True
            ),
            reply_to_message_id=message.message_id,
        )
        return

    try:
        activity = args[1]
        max_users = int(args[2])

        if not await db.activity_exists(activity):
            await message.answer(
                f"❌ 活动 '<code>{activity}</code>' 不存在！",
                reply_markup=await get_main_keyboard(
                    chat_id=message.chat.id, show_admin=True
                ),
                reply_to_message_id=message.message_id,
                parse_mode="HTML",
            )
            return

        if max_users < 0:
            await message.answer(
                "❌ 人数限制不能为负数！",
                reply_markup=await get_main_keyboard(
                    chat_id=message.chat.id, show_admin=True
                ),
            )
            return

        chat_id = message.chat.id

        if max_users == 0:
            await db.remove_activity_user_limit(activity)
            await message.answer(
                f"✅ 已取消活动 '<code>{activity}</code>' 的人数限制",
                reply_markup=await get_main_keyboard(chat_id=chat_id, show_admin=True),
                parse_mode="HTML",
                reply_to_message_id=message.message_id,
            )
            logger.info(f"取消活动人数限制: {activity}")
        else:
            await db.set_activity_user_limit(activity, max_users)

            current_users = await db.get_current_activity_users(chat_id, activity)

            await message.answer(
                f"✅ 已设置活动 '<code>{activity}</code>' 的人数限制为 <code>{max_users}</code> 人\n\n"
                f"📊 当前状态：\n"
                f"• 限制人数：<code>{max_users}</code> 人\n"
                f"• 当前进行：<code>{current_users}</code> 人\n"
                f"• 剩余名额：<code>{max_users - current_users}</code> 人",
                reply_markup=await get_main_keyboard(chat_id=chat_id, show_admin=True),
                parse_mode="HTML",
                reply_to_message_id=message.message_id,
            )
            logger.info(f"设置活动人数限制: {activity} -> {max_users}人")

    except ValueError:
        await message.answer(
            "❌ 人数限制必须是数字！",
            reply_markup=await get_main_keyboard(
                chat_id=message.chat.id, show_admin=True
            ),
            reply_to_message_id=message.message_id,
        )
    except Exception as e:
        logger.error(f"设置活动人数限制失败: {e}")
        await message.answer(
            f"❌ 设置失败：{e}",
            reply_markup=await get_main_keyboard(
                chat_id=message.chat.id, show_admin=True
            ),
            reply_to_message_id=message.message_id,
        )


@admin_required
@rate_limit(rate=5, per=60)
async def cmd_actstatus(message: types.Message):
    """查看活动人数状态"""
    chat_id = message.chat.id

    try:
        activity_limits = await db.get_all_activity_limits()

        if not activity_limits:
            await message.answer(
                "📊 当前没有设置任何活动人数限制\n"
                "💡 使用 /actnum <活动名> <人数> 来设置限制",
                reply_markup=await get_main_keyboard(chat_id=chat_id, show_admin=True),
                reply_to_message_id=message.message_id,
            )
            return

        status_text = "📊 活动人数限制状态\n\n"

        for activity, max_users in activity_limits.items():
            current_users = await db.get_current_activity_users(chat_id, activity)
            remaining = max(0, max_users - current_users) if max_users > 0 else "无限制"

            status_icon = "🟢" if remaining == "无限制" or remaining > 0 else "🔴"
            limit_display = f"{max_users}" if max_users > 0 else "无限制"

            status_text += (
                f"{status_icon} <code>{activity}</code>\n"
                f"   • 限制：<code>{limit_display}</code>\n"
                f"   • 当前：<code>{current_users}</code> 人\n"
                f"   • 剩余：<code>{remaining}</code> 人\n\n"
            )

        status_text += "💡 绿色表示还有名额，红色表示已满员"

        await message.answer(
            status_text,
            reply_markup=await get_main_keyboard(chat_id=chat_id, show_admin=True),
            parse_mode="HTML",
            reply_to_message_id=message.message_id,
        )

        logger.info(f"查看活动状态: {chat_id}")

    except Exception as e:
        logger.error(f"获取活动状态失败: {e}")
        await message.answer(
            f"❌ 获取状态失败：{e}",
            reply_markup=await get_main_keyboard(chat_id=chat_id, show_admin=True),
            reply_to_message_id=message.message_id,
        )


@admin_required
@rate_limit(rate=3, per=30)
async def cmd_setfines_all(message: types.Message):
    """为所有活动统一设置分段罚款"""
    args = message.text.split()
    if len(args) < 3 or (len(args) - 1) % 2 != 0:
        await message.answer(
            Config.MESSAGES["setfines_all_usage"],
            reply_markup=await get_main_keyboard(
                chat_id=message.chat.id, show_admin=True
            ),
            reply_to_message_id=message.message_id,
        )
        return

    try:
        pairs = args[1:]
        segments = {}
        for i in range(0, len(pairs), 2):
            t = int(pairs[i])
            f = int(pairs[i + 1])
            if t <= 0 or f < 0:
                await message.answer(
                    "❌ 时间段必须为正整数，罚款金额不能为负数",
                    reply_markup=await get_main_keyboard(
                        chat_id=message.chat.id, show_admin=True
                    ),
                    reply_to_message_id=message.message_id,
                )
                return
            segments[t] = f

        activity_limits = await db.get_activity_limits_cached()
        if not activity_limits:
            await message.answer(
                "⚠️ 当前没有活动，无法设置罚款",
                reply_markup=await get_main_keyboard(
                    chat_id=message.chat.id, show_admin=True
                ),
                reply_to_message_id=message.message_id,
            )
            return

        for act in activity_limits.keys():
            for time_segment, amount in segments.items():
                await db.update_fine_config(act, str(time_segment), amount)

        segments_text = " ".join(
            [f"<code>{t}</code>:<code>{f}</code>" for t, f in segments.items()]
        )
        await message.answer(
            f"✅ 已为所有活动设置分段罚款：{segments_text}",
            reply_markup=await get_main_keyboard(
                chat_id=message.chat.id, show_admin=True
            ),
            reply_to_message_id=message.message_id,
            parse_mode="HTML",
        )

        logger.info(f"群 {message.chat.id} 已统一设置所有活动罚款: {segments_text}")

    except ValueError:
        await message.answer(
            "❌ 时间段和金额必须是数字！",
            reply_markup=await get_main_keyboard(
                chat_id=message.chat.id, show_admin=True
            ),
            reply_to_message_id=message.message_id,
        )
    except Exception as e:
        logger.error(f"设置所有活动罚款失败: {e}")
        await message.answer(
            f"❌ 设置失败：{e}",
            reply_markup=await get_main_keyboard(
                chat_id=message.chat.id, show_admin=True
            ),
            reply_to_message_id=message.message_id,
        )


@admin_required
@rate_limit(rate=3, per=30)
async def cmd_setfine(message: types.Message):
    """设置单个活动的罚款费率"""
    args = message.text.split()
    if len(args) != 4:
        await message.answer(
            Config.MESSAGES["setfine_usage"],
            reply_markup=await get_main_keyboard(
                chat_id=message.chat.id, show_admin=True
            ),
            reply_to_message_id=message.message_id,
        )
        return

    try:
        activity = args[1]
        time_segment = int(args[2])
        amount = int(args[3])

        if not await db.activity_exists(activity):
            await message.answer(
                f"❌ 活动 '<code>{activity}</code>' 不存在！",
                reply_markup=await get_main_keyboard(
                    chat_id=message.chat.id, show_admin=True
                ),
                reply_to_message_id=message.message_id,
                parse_mode="HTML",
            )
            return

        if time_segment <= 0 or amount < 0:
            await message.answer(
                "❌ 时间段必须为正整数，罚款金额不能为负数",
                reply_markup=await get_main_keyboard(
                    chat_id=message.chat.id, show_admin=True
                ),
                reply_to_message_id=message.message_id,
            )
            return

        await db.update_fine_config(activity, str(time_segment), amount)

        await message.answer(
            f"✅ 已设置活动 '<code>{activity}</code>' 的罚款：\n"
            f"⏱️ 时间段：<code>{time_segment}</code>\n"
            f"💰 金额：<code>{amount}</code> 分",
            reply_markup=await get_main_keyboard(
                chat_id=message.chat.id, show_admin=True
            ),
            reply_to_message_id=message.message_id,
            parse_mode="HTML",
        )

        logger.info(
            f"群 {message.chat.id} 已设置活动罚款: {activity} {time_segment} -> {amount} 泰铢"
        )

    except ValueError:
        await message.answer(
            "❌ 时间段和金额必须是数字！",
            reply_markup=await get_main_keyboard(
                chat_id=message.chat.id, show_admin=True
            ),
            reply_to_message_id=message.message_id,
        )
    except Exception as e:
        logger.error(f"设置单个活动罚款失败: {e}")
        await message.answer(
            f"❌ 设置失败：{e}",
            reply_markup=await get_main_keyboard(
                chat_id=message.chat.id, show_admin=True
            ),
            reply_to_message_id=message.message_id,
        )


@admin_required
@rate_limit(rate=5, per=60)
async def cmd_finesstatus(message: types.Message):
    """查看所有活动的罚款设置状态"""
    chat_id = message.chat.id
    try:
        activity_limits = await db.get_activity_limits_cached()
        fine_rates = await db.get_fine_rates()

        if not activity_limits:
            await message.answer(
                "⚠️ 当前没有配置任何活动",
                reply_markup=await get_main_keyboard(chat_id=chat_id, show_admin=True),
                reply_to_message_id=message.message_id,
            )
            return

        status_text = "💰 活动罚款设置状态\n\n"

        for activity in activity_limits.keys():
            activity_fines = fine_rates.get(activity, {})
            status_text += f"🔹 <code>{activity}</code>\n"

            if activity_fines:
                for time_seg, amount in sorted(
                    activity_fines.items(), key=lambda x: int(x[0])
                ):
                    status_text += f"   • 时间段 <code>{time_seg}</code> 分钟：<code>{amount}</code> 分\n"
            else:
                status_text += f"   • 未设置罚款\n"

            status_text += "\n"

        status_text += "💡 设置命令：\n"
        status_text += "• /setfine <活动> <时间> <金额> - 设置单个活动\n"
        status_text += "• /setfines_all <t1> <f1> [t2 f2...] - 统一设置所有活动"

        await message.answer(
            status_text,
            reply_markup=await get_main_keyboard(chat_id=chat_id, show_admin=True),
            reply_to_message_id=message.message_id,
            parse_mode="HTML",
        )

        logger.info(f"群 {chat_id} 查看了活动罚款状态")

    except Exception as e:
        logger.error(f"查看罚款状态失败: {e}")
        await message.answer(
            f"❌ 获取罚款状态失败：{e}",
            reply_markup=await get_main_keyboard(chat_id=chat_id, show_admin=True),
            reply_to_message_id=message.message_id,
        )


@admin_required
@rate_limit(rate=2, per=60)
async def cmd_checkdualsetup(message: types.Message):
    """检查双班重置配置"""
    chat_id = message.chat.id

    try:
        group_data = await db.get_group_cached(chat_id)
        if not group_data:
            await message.answer("❌ 群组未初始化")
            return

        reset_hour = group_data.get("reset_hour", Config.DAILY_RESET_HOUR)
        reset_minute = group_data.get("reset_minute", Config.DAILY_RESET_MINUTE)

        shift_config = await db.get_shift_config(chat_id)
        is_dual = shift_config.get("dual_mode", True)

        now = db.get_beijing_time()
        reset_time_today = now.replace(
            hour=reset_hour, minute=reset_minute, second=0, microsecond=0
        )
        execute_time = reset_time_today + timedelta(hours=2)

        text = (
            f"🔍 <b>双班重置配置检查</b>\n\n"
            f"• 群组ID: <code>{chat_id}</code>\n"
            f"• 双班模式: {'✅ 开启' if is_dual else '❌ 关闭'}\n"
            f"• 重置时间: <code>{reset_hour:02d}:{reset_minute:02d}</code>\n"
            f"• 执行时间: <code>{execute_time.strftime('%H:%M')}</code>\n"
            f"• 当前时间: <code>{now.strftime('%H:%M:%S')}</code>\n\n"
        )

        if is_dual:
            if now < execute_time:
                time_left = execute_time - now
                minutes = int(time_left.total_seconds() / 60)
                seconds = int(time_left.total_seconds() % 60)
                text += f"⏳ 距离下次执行还有: <code>{minutes}分{seconds}秒</code>"
            else:
                text += f"✅ 当前在执行窗口内"
        else:
            text += f"💡 群组未开启双班模式，无需检查"

        await message.answer(
            text, parse_mode="HTML", reply_to_message_id=message.message_id
        )

    except Exception as e:
        await message.answer(
            f"❌ 检查失败: {e}", reply_to_message_id=message.message_id
        )


@admin_required
@rate_limit(rate=2, per=60)
async def cmd_handover_status(message: types.Message):
    """查看当前换班状态"""
    chat_id = message.chat.id
    uid = message.from_user.id

    from handover_manager import handover_manager

    now = db.get_beijing_time()
    period = await handover_manager.determine_current_period(chat_id, now)

    # 获取用户当前周期信息
    count = await handover_manager.get_activity_count(
        chat_id,
        uid,
        "小厕",
        "night" if "night" in period["period_type"] else "day",
        current_time=now,
    )

    effective_cycle = 1
    if period["is_handover"]:
        effective_cycle = await handover_manager.get_user_effective_cycle(
            chat_id, uid, period
        )

    # 获取周期累计时间
    cycle_data = None
    if period["is_handover"]:
        cycle_data = await handover_manager.get_user_cycle(
            chat_id,
            uid,
            period["business_date"],
            period["period_type"],
            effective_cycle,
        )

    period_names = {
        "handover_night": "🌙 换班夜班",
        "handover_day": "☀️ 换班白班",
        "normal_night": "🌙 正常夜班",
        "normal_day": "☀️ 正常白班",
    }

    text = f"🔄 <b>换班状态</b>\n\n"
    text += f"📅 当前时间: {now.strftime('%Y-%m-%d %H:%M:%S')}\n"
    text += f"📊 当前时期: {period_names.get(period['period_type'], '未知')}\n"
    text += f"📅 业务日期: {period['business_date']}\n"
    text += (
        f"⏱️ 已过时间: {period['hours_elapsed']:.1f} / {period['total_hours']} 小时\n"
    )
    text += f"🔄 当前周期: {effective_cycle}"
    if period["is_handover"] and effective_cycle != period.get("cycle"):
        text += f"（挂钟周期 {period['cycle']}）"
    text += "\n"

    if period["is_handover"] and cycle_data:
        text += f"⏱️ 周期累计: {cycle_data['total_work_seconds'] // 60} 分钟\n"

    text += f"\n👤 <b>您的计数示例</b>\n"
    text += f"• 小厕当前计数: {count}\n"

    text += f"\n⏰ 下次重置时间: {period['next_reset_time'].strftime('%m/%d %H:%M')}\n"

    await message.answer(
        text, parse_mode="HTML", reply_to_message_id=message.message_id
    )


@admin_required
@rate_limit(rate=2, per=60)
async def cmd_set_handover_day(message: types.Message):
    """设置换班日期

    用法:
    /sethandoverday <日期> [月份]

    示例:
    /sethandoverday 15         - 每月15号换班
    /sethandoverday 31         - 每月最后一天换班
    /sethandoverday 15 12      - 只在12月15号换班
    /sethandoverday off        - 关闭换班功能
    /sethandoverday status     - 查看当前设置
    """
    chat_id = message.chat.id
    args = message.text.split()

    from handover_manager import handover_manager

    if len(args) < 2:
        await message.answer(
            "❌ 用法错误\n\n"
            "📝 正确用法：\n"
            "• `/sethandoverday 15` - 每月15号换班\n"
            "• `/sethandoverday 31` - 每月最后一天换班\n"
            "• `/sethandoverday 15 12` - 只在12月15号换班\n"
            "• `/sethandoverday off` - 关闭换班功能\n"
            "• `/sethandoverday status` - 查看当前设置\n\n"
            "💡 提示：日期为1-31，0表示每月最后一天",
            parse_mode="HTML",
            reply_to_message_id=message.message_id,
        )
        return

    action = args[1].lower()

    # 查看状态
    if action == "status":
        config = await handover_manager.get_handover_config(chat_id)

        if not config.get("handover_enabled"):
            status_text = "❌ 换班功能已关闭"
        else:
            handover_day = config.get("handover_day", 31)
            handover_month = config.get("handover_month", 0)

            if handover_day == 0:
                day_desc = "月末最后一天"
            else:
                day_desc = f"每月{handover_day}号"

            if handover_month > 0:
                month_desc = f"只在{handover_month}月"
                day_desc = f"{handover_month}月{handover_day}号"
            else:
                month_desc = "每月"

            status_text = (
                f"📊 当前换班配置\n\n"
                f"• 状态: {'✅ 已开启' if config.get('handover_enabled') else '❌ 已关闭'}\n"
                f"• 换班日期: {day_desc}\n"
                f"• 周期: {month_desc}\n"
                f"• 夜班开始: {config.get('night_start_time')}\n"
                f"• 白班开始: {config.get('day_start_time')}\n"
                f"• 换班夜班时长: {config.get('handover_night_hours')}小时\n"
                f"• 换班白班时长: {config.get('handover_day_hours')}小时"
            )

        await message.answer(
            status_text, parse_mode="HTML", reply_to_message_id=message.message_id
        )
        return

    # 关闭换班
    if action == "off":
        await handover_manager.update_handover_config(chat_id, handover_enabled=False)
        await message.answer(
            "✅ 换班功能已关闭\n\n"
            "💡 如需重新开启，请使用 `/sethandoverday <日期>` 命令",
            parse_mode="HTML",
            reply_to_message_id=message.message_id,
        )
        return

    # 设置换班日期
    try:
        handover_day = int(action)

        # 验证日期
        if handover_day < 0 or handover_day > 31:
            await message.answer(
                "❌ 日期必须为1-31之间的数字（0表示月末最后一天）",
                reply_to_message_id=message.message_id,
            )
            return

        handover_month = 0  # 默认每月
        month_desc = "每月"

        # 如果提供了月份参数
        if len(args) >= 3:
            handover_month = int(args[2])
            if handover_month < 1 or handover_month > 12:
                await message.answer(
                    "❌ 月份必须为1-12之间的数字",
                    reply_to_message_id=message.message_id,
                )
                return
            month_desc = f"{handover_month}月"

        # 更新配置
        await handover_manager.update_handover_config(
            chat_id,
            handover_enabled=True,
            handover_day=handover_day,
            handover_month=handover_month,
        )

        # 生成响应消息
        if handover_day == 0:
            day_desc = "月末最后一天"
            examples = "例如：1月31日、2月28/29日、3月31日"
        else:
            day_desc = f"{handover_day}号"
            examples = f"例如：{month_desc}{handover_day}号"

        success_msg = (
            f"✅ 换班日期设置成功！\n\n"
            f"📅 设置详情：\n"
            f"• 换班日期：{day_desc}\n"
            f"• 周期：{month_desc}\n"
            f"• 状态：已开启\n\n"
            f"📌 示例说明：\n"
            f"{examples} 21:00开始换班夜班\n"
            f"次日15:00开始换班白班\n\n"
            f"💡 其他命令：\n"
            f"• `/sethandoverday status` - 查看当前设置\n"
            f"• `/sethandoverday off` - 关闭换班功能\n"
            f"• `/sethour` - 设置工作时长"
        )

        await message.answer(
            success_msg, parse_mode="HTML", reply_to_message_id=message.message_id
        )

    except ValueError:
        await message.answer(
            "❌ 日期必须是数字\n\n" "正确用法：/sethandoverday 15",
            reply_to_message_id=message.message_id,
        )
    except Exception as e:
        logger.error(f"设置换班日期失败: {e}")
        await message.answer(
            f"❌ 设置失败：{e}", reply_to_message_id=message.message_id
        )


@admin_required
@rate_limit(rate=2, per=60)
async def cmd_set_handover_hours(message: types.Message):
    """设置换班工作时长

    用法:
    /sethour <类型> <小时数>

    类型:
    handover_night - 换班夜班时长
    handover_day   - 换班白班时长
    normal_night   - 正常夜班时长
    normal_day     - 正常白班时长

    示例:
    /sethour handover_night 18
    /sethour normal_day 12
    """
    chat_id = message.chat.id
    args = message.text.split()

    if len(args) != 3:
        await message.answer(
            "❌ 用法错误\n\n"
            "正确用法：/sethour <类型> <小时数>\n\n"
            "类型说明：\n"
            "• `handover_night` - 换班夜班时长（默认18）\n"
            "• `handover_day` - 换班白班时长（默认18）\n"
            "• `normal_night` - 正常夜班时长（默认12）\n"
            "• `normal_day` - 正常白班时长（默认12）\n\n"
            "示例：\n"
            "• `/sethour handover_night 18`\n"
            "• `/sethour normal_day 12`",
            parse_mode="HTML",
            reply_to_message_id=message.message_id,
        )
        return

    hour_type = args[1].lower()
    try:
        hours = int(args[2])

        if hours <= 0 or hours > 24:
            await message.answer(
                "❌ 小时数必须在1-24之间", reply_to_message_id=message.message_id
            )
            return

        from handover_manager import handover_manager

        update_kwargs = {}
        type_names = {
            "handover_night": "换班夜班",
            "handover_day": "换班白班",
            "normal_night": "正常夜班",
            "normal_day": "正常白班",
        }

        if hour_type == "handover_night":
            update_kwargs["handover_night_hours"] = hours
        elif hour_type == "handover_day":
            update_kwargs["handover_day_hours"] = hours
        elif hour_type == "normal_night":
            update_kwargs["normal_night_hours"] = hours
        elif hour_type == "normal_day":
            update_kwargs["normal_day_hours"] = hours
        else:
            await message.answer(
                "❌ 无效的类型，请使用：handover_night、handover_day、normal_night、normal_day",
                reply_to_message_id=message.message_id,
            )
            return

        await handover_manager.update_handover_config(chat_id, **update_kwargs)

        type_name = type_names.get(hour_type, hour_type)
        await message.answer(
            f"✅ 已设置{type_name}时长为 {hours} 小时",
            reply_to_message_id=message.message_id,
        )

    except ValueError:
        await message.answer(
            "❌ 小时数必须是数字", reply_to_message_id=message.message_id
        )
    except Exception as e:
        logger.error(f"设置工作时长失败: {e}")
        await message.answer(
            f"❌ 设置失败：{e}", reply_to_message_id=message.message_id
        )


@admin_required
@rate_limit(rate=2, per=60)
async def cmd_handover_config(message: types.Message):
    """查看/设置换班配置"""
    chat_id = message.chat.id
    args = message.text.split()

    from handover_manager import handover_manager

    if len(args) == 1:
        config = await handover_manager.get_handover_config(chat_id)

        text = (
            f"⚙️ <b>换班配置</b>\n\n"
            f"📊 状态: {'✅ 已启用' if config.get('handover_enabled') else '❌ 已禁用'}\n"
            f"• 夜班开始时间: <code>{config.get('night_start_time', '21:00')}</code>\n"
            f"• 白班开始时间: <code>{config.get('day_start_time', '09:00')}</code>\n"
            f"• 换班夜班时长: <code>{config.get('handover_night_hours', 18)}</code> 小时\n"
            f"• 换班白班时长: <code>{config.get('handover_day_hours', 18)}</code> 小时\n"
            f"• 正常夜班时长: <code>{config.get('normal_night_hours', 12)}</code> 小时\n"
            f"• 正常白班时长: <code>{config.get('normal_day_hours', 12)}</code> 小时\n\n"
            f"💡 修改命令:\n"
            f"• <code>/handover on|off</code>\n"
            f"• <code>/handover set_night_start 21:00</code>\n"
            f"• <code>/handover set_day_start 09:00</code>\n"
            f"• <code>/handover set_hours handover_night 18</code>\n"
            f"• <code>/handover set_hours handover_day 18</code>\n"
            f"• <code>/handover set_hours normal_night 12</code>\n"
            f"• <code>/handover set_hours normal_day 12</code>"
        )

        await message.answer(
            text, parse_mode="HTML", reply_to_message_id=message.message_id
        )
        return

    action = args[1].lower()

    try:
        if action == "on":
            await handover_manager.update_handover_config(
                chat_id, handover_enabled=True
            )
            await message.answer(
                "✅ 换班功能已开启", reply_to_message_id=message.message_id
            )

        elif action == "off":
            await handover_manager.update_handover_config(
                chat_id, handover_enabled=False
            )
            await message.answer(
                "✅ 换班功能已关闭", reply_to_message_id=message.message_id
            )

        elif action == "set_night_start" and len(args) >= 3:
            await handover_manager.update_handover_config(
                chat_id, night_start_time=args[2]
            )
            await message.answer(
                f"✅ 夜班开始时间已设置为 {args[2]}",
                reply_to_message_id=message.message_id,
            )

        elif action == "set_day_start" and len(args) >= 3:
            await handover_manager.update_handover_config(
                chat_id, day_start_time=args[2]
            )
            await message.answer(
                f"✅ 白班开始时间已设置为 {args[2]}",
                reply_to_message_id=message.message_id,
            )

        elif action == "set_hours" and len(args) >= 4:
            hour_type = args[2]
            hours = int(args[3])

            if hour_type == "handover_night":
                await handover_manager.update_handover_config(
                    chat_id, handover_night_hours=hours
                )
                await message.answer(
                    f"✅ 换班夜班时长已设置为 {hours} 小时",
                    reply_to_message_id=message.message_id,
                )
            elif hour_type == "handover_day":
                await handover_manager.update_handover_config(
                    chat_id, handover_day_hours=hours
                )
                await message.answer(
                    f"✅ 换班白班时长已设置为 {hours} 小时",
                    reply_to_message_id=message.message_id,
                )
            elif hour_type == "normal_night":
                await handover_manager.update_handover_config(
                    chat_id, normal_night_hours=hours
                )
                await message.answer(
                    f"✅ 正常夜班时长已设置为 {hours} 小时",
                    reply_to_message_id=message.message_id,
                )
            elif hour_type == "normal_day":
                await handover_manager.update_handover_config(
                    chat_id, normal_day_hours=hours
                )
                await message.answer(
                    f"✅ 正常白班时长已设置为 {hours} 小时",
                    reply_to_message_id=message.message_id,
                )
            else:
                await message.answer(
                    "❌ 未知时长类型", reply_to_message_id=message.message_id
                )
        else:
            await message.answer("❌ 未知命令", reply_to_message_id=message.message_id)

    except ValueError:
        await message.answer(
            "❌ 参数格式错误，请使用数字", reply_to_message_id=message.message_id
        )
    except Exception as e:
        logger.error(f"换班配置失败: {e}")
        await message.answer(
            f"❌ 操作失败: {e}", reply_to_message_id=message.message_id
        )


@admin_required
@rate_limit(rate=2, per=60)
async def cmd_testgroupaccess(message: types.Message):
    """测试机器人是否能访问指定群组"""
    chat_id = message.chat.id
    args = message.text.split()

    if len(args) < 2:
        await message.answer(
            "❌ 用法：/testgroupaccess <群组ID>\n"
            "📝 示例：/testgroupaccess -5187163421",
            reply_to_message_id=message.message_id,
        )
        return

    try:
        target_id = int(args[1])

        extra_group_id = await db.get_extra_work_group(chat_id)

        result_text = f"🔍 <b>群组访问测试</b>\n\n"

        try:
            chat_info = await bot_manager.bot.get_chat(target_id)
            result_text += f"✅ 目标群组 <code>{target_id}</code> 可访问\n"
            result_text += f"   • 标题：{chat_info.title}\n"
            result_text += f"   • 类型：{chat_info.type}\n"

            test_msg = await bot_manager.bot.send_message(
                target_id,
                f"🧪 这是一条测试消息\n发送时间：{db.get_beijing_time().strftime('%Y-%m-%d %H:%M:%S')}",
                parse_mode="HTML",
            )
            result_text += f"✅ 测试消息发送成功 (消息ID: {test_msg.message_id})\n"

        except Exception as e:
            result_text += f"❌ 目标群组 <code>{target_id}</code> 访问失败\n"
            result_text += f"   • 错误：{str(e)}\n"
            if "403" in str(e):
                result_text += "   • 原因：机器人不在群组中或没有权限\n"

        result_text += f"\n📊 当前额外群组配置：\n"
        result_text += f"• 配置的群组：<code>{extra_group_id or '未设置'}</code>\n"

        if extra_group_id and extra_group_id == target_id:
            result_text += f"✅ 测试的群组与配置一致\n"
        elif extra_group_id:
            result_text += f"⚠️ 测试的群组与配置不一致\n"

        await message.answer(
            result_text, parse_mode="HTML", reply_to_message_id=message.message_id
        )

    except ValueError:
        await message.answer(
            "❌ 群组ID必须是数字", reply_to_message_id=message.message_id
        )
    except Exception as e:
        await message.answer(
            f"❌ 测试失败：{e}", reply_to_message_id=message.message_id
        )


@admin_required
@rate_limit(rate=2, per=60)
async def cmd_checkbotpermissions(message: types.Message):
    """检查机器人在各个群组的权限"""
    chat_id = message.chat.id

    result_text = f"🔍 <b>机器人权限检查</b>\n\n"
    result_text += f"🤖 机器人ID: <code>{bot_manager.bot.id}</code>\n"
    result_text += f"🤖 机器人用户名: @{(await bot_manager.bot.me()).username}\n\n"

    try:
        bot_member = await bot_manager.bot.get_chat_member(chat_id, bot_manager.bot.id)
        result_text += f"📊 当前群组 <code>{chat_id}</code>:\n"
        result_text += f"   • 状态：{bot_member.status}\n"
        result_text += f"   • 是否为管理员：{'是' if bot_member.status in ['administrator', 'creator'] else '否'}\n"
    except Exception as e:
        result_text += f"❌ 无法获取当前群组权限: {e}\n"

    extra_group_id = await db.get_extra_work_group(chat_id)
    if extra_group_id:
        result_text += f"\n📊 额外群组 <code>{extra_group_id}</code>:\n"
        try:
            extra_member = await bot_manager.bot.get_chat_member(extra_group_id, bot_manager.bot.id)
            result_text += f"   • 状态：{extra_member.status}\n"
            result_text += f"   • 是否为管理员：{'是' if extra_member.status in ['administrator', 'creator'] else '否'}\n"
            result_text += f"   • 可发送消息：{'是' if extra_member.can_send_messages else '未知'}\n"
        except Exception as e:
            result_text += f"   ❌ 无法获取权限: {e}\n"
            if "403" in str(e):
                result_text += f"   • 原因：机器人不在该群组中\n"

    group_data = await db.get_group_cached(chat_id)
    channel_id = group_data.get("channel_id") if group_data else None
    if channel_id:
        result_text += f"\n📊 频道 <code>{channel_id}</code>:\n"
        try:
            channel_member = await bot_manager.bot.get_chat_member(channel_id, bot_manager.bot.id)
            result_text += f"   • 状态：{channel_member.status}\n"
        except Exception as e:
            result_text += f"   ❌ 无法获取权限: {e}\n"

    result_text += f"\n💡 <b>常见问题：</b>\n"
    result_text += f"• 如果机器人不在群组中，请手动添加\n"
    result_text += f"• 如果机器人不是管理员，可能受群组限制\n"
    result_text += f"• 群组设置了慢速模式可能延迟消息显示"

    await message.answer(
        result_text, parse_mode="HTML", reply_to_message_id=message.message_id
    )


@admin_required
@rate_limit(rate=3, per=30)
async def cmd_setworkfine(message: types.Message):
    """设置上下班罚款规则"""
    args = message.text.split()

    if len(args) < 4 or (len(args) - 2) % 2 != 0:
        await message.answer(
            "❌ 用法错误\n正确格式：/setworkfine <work_start|work_end> <分钟1> <罚款1> [分钟2 罚款2 ...]",
            reply_markup=get_admin_keyboard(),
            reply_to_message_id=message.message_id,
        )
        return

    checkin_type = args[1]
    if checkin_type not in ["work_start", "work_end"]:
        await message.answer(
            "❌ 类型必须是 work_start 或 work_end",
            reply_markup=get_admin_keyboard(),
            reply_to_message_id=message.message_id,
        )
        return

    fine_segments = {}
    try:
        for i in range(2, len(args), 2):
            minute = int(args[i])
            amount = int(args[i + 1])
            if minute <= 0 or amount < 0:
                await message.answer(
                    "❌ 分钟必须大于0，罚款金额不能为负数",
                    reply_markup=get_admin_keyboard(),
                    reply_to_message_id=message.message_id,
                )
                return
            fine_segments[str(minute)] = amount

        await db.clear_work_fine_rates(checkin_type)
        for minute_str, fine_amount in fine_segments.items():
            await db.update_work_fine_rate(checkin_type, minute_str, fine_amount)

        segments_text = "\n".join(
            [
                f"⏰ 超过 {m} 分钟 → 💰 {a} 分"
                for m, a in sorted(fine_segments.items(), key=lambda x: int(x[0]))
            ]
        )

        type_text = "上班迟到" if checkin_type == "work_start" else "下班早退"

        await message.answer(
            f"✅ 已设置{type_text}罚款规则：\n{segments_text}",
            reply_markup=get_admin_keyboard(),
            reply_to_message_id=message.message_id,
        )

        logger.info(f"设置上下班罚款成功: {checkin_type} -> {fine_segments}")

    except ValueError:
        await message.answer(
            "❌ 分钟和罚款必须是数字",
            reply_markup=get_admin_keyboard(),
            reply_to_message_id=message.message_id,
        )
    except Exception as e:
        logger.error(f"设置上下班罚款失败: {e}")
        await message.answer(
            f"❌ 设置失败：{e}",
            reply_markup=get_admin_keyboard(),
            reply_to_message_id=message.message_id,
        )


@admin_required
@rate_limit(rate=5, per=60)
async def cmd_showsettings(message: types.Message):
    """显示目前的设置"""
    chat_id = message.chat.id
    await db.init_group(chat_id)
    group_data = await db.get_group_cached(chat_id) or {}

    activity_limits = await db.get_activity_limits_cached()
    fine_rates = await db.get_fine_rates()
    work_fine_rates = await db.get_work_fine_rates()

    text = f"🔧 当前群设置（群ID {chat_id}）\n\n"

    text += "📋 基本设置：\n"
    text += f"• 绑定频道ID: <code>{group_data.get('channel_id', '未设置')}</code>\n"
    text += f"• 通知群组ID: <code>{group_data.get('notification_group_id', '未设置')}</code>\n\n"

    text += "⏰ 重置与工作时间：\n"
    text += f"• 每日重置时间: <code>{group_data.get('reset_hour', Config.DAILY_RESET_HOUR):02d}:{group_data.get('reset_minute', Config.DAILY_RESET_MINUTE):02d}</code>\n"
    text += f"• 上班时间: <code>{group_data.get('work_start_time', '09:00')}</code>\n"
    text += f"• 下班时间: <code>{group_data.get('work_end_time', '18:00')}</code>\n\n"

    text += "🎯 活动设置：\n"
    if activity_limits:
        for act, v in activity_limits.items():
            text += f"• <code>{act}</code>：次数上限 <code>{v['max_times']}</code>，时间限制 <code>{v['time_limit']}</code> 分钟\n"
    else:
        text += "• 暂无活动设置\n"

    text += "\n💰 活动罚款分段：\n"
    if fine_rates:
        for act, fr in fine_rates.items():
            if fr:
                try:
                    sorted_fines = sorted(
                        fr.items(), key=lambda x: int(x[0].replace("min", ""))
                    )
                    fines_text = " | ".join([f"{k}:{v}分" for k, v in sorted_fines])
                    text += f"• <code>{act}</code>：{fines_text}\n"
                except Exception:
                    text += f"• <code>{act}</code>：配置异常\n"
            else:
                text += f"• <code>{act}</code>：未设置\n"
    else:
        text += "• 暂无活动罚款设置\n"

    text += "\n⏰ 上下班罚款设置：\n"
    for key, label in [("work_start", "上班迟到"), ("work_end", "下班早退")]:
        wf = work_fine_rates.get(key, {})
        if wf:
            try:
                sorted_wf = sorted(wf.items(), key=lambda x: int(x[0]))
                wf_text = " | ".join([f"{k}分:{v}分" for k, v in sorted_wf])
                text += f"• {label}：{wf_text}\n"
            except Exception:
                text += f"• {label}：配置异常\n"
        else:
            text += f"• {label}：未设置\n"

    await message.answer(
        text,
        reply_markup=await get_main_keyboard(chat_id=chat_id, show_admin=True),
        parse_mode="HTML",
        reply_to_message_id=message.message_id,
    )


@admin_required
@rate_limit(rate=5, per=60)
async def cmd_worktime(message: types.Message):
    """查看当前工作时间设置"""
    chat_id = message.chat.id
    try:
        work_hours = await db.get_group_work_time(chat_id) or {}
        has_enabled = await db.has_work_hours_enabled(chat_id)

        work_start = work_hours.get("work_start", "09:00")
        work_end = work_hours.get("work_end", "18:00")
        status = "🟢 已启用" if has_enabled else "🔴 未启用（使用默认时间）"

        await message.answer(
            f"🕒 当前工作时间设置\n\n"
            f"📊 状态：{status}\n"
            f"🟢 上班时间：<code>{work_start}</code>\n"
            f"🔴 下班时间：<code>{work_end}</code>\n\n"
            f"💡 使用 /setworktime 09:00 18:00 来修改",
            reply_markup=await get_main_keyboard(chat_id=chat_id, show_admin=True),
            reply_to_message_id=message.message_id,
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"查看工作时间失败: {e}")
        await message.answer(
            "❌ 获取工作时间失败，请稍后重试",
            reply_markup=await get_main_keyboard(chat_id=chat_id, show_admin=True),
            reply_to_message_id=message.message_id,
        )
