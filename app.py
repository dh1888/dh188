import asyncio
import gc
import logging
import os
import time
import traceback
from contextlib import suppress

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, types, BaseMiddleware
from aiogram.filters import Command
from aiogram.types import BotCommand, BotCommandScopeAllChatAdministrators
from aiogram.fsm.storage.memory import MemoryStorage

import constants
from constants import start_time
from config import Config
from database import db
from bot_manager import bot_manager
from constants import WORK_BUTTONS, SPECIAL_BUTTONS, AdminStates
from keyboards import get_main_keyboard, get_admin_keyboard, is_admin
from utils import (
    timer_manager, heartbeat_manager, notification_service,
    shift_state_manager, init_notification_service, performance_optimizer,
    user_lock_manager,
)
from activity_service import (
    activity_timer, recover_expired_activities, send_startup_notification,
    send_shutdown_notification,
)
from reset_service import recover_shift_states, check_missed_resets_on_startup
from user_handlers import (
    cmd_start, cmd_menu, cmd_help, cmd_ci, cmd_at, cmd_workstart, cmd_workend,
    handle_myinfo_command, handle_ranking_command, handle_ranking_shift_command,
    handle_ranking_day_command, handle_ranking_night_command,
    handle_myinfo_day_command, handle_myinfo_night_command,
    handle_back_command, handle_work_buttons, handle_export_button,
    handle_my_record, handle_rank, handle_admin_panel_button,
    handle_back_to_main_menu, handle_all_text_messages, handle_fixed_activity,
    handle_quick_back,
)
from admin_commands import (
    cmd_admin, cmd_setdualmode, cmd_setshiftgrace, cmd_setworkendgrace,
    cmd_fix_message_refs, cmd_cleanup_monthly, cmd_monthly_stats_status,
    cmd_cleanup_inactive, cmd_reset_user, cmd_export, cmd_monthlyreport,
    cmd_exportmonthly, cmd_addactivity, cmd_delactivity, cmd_setworktime,
    cmd_setresettime, cmd_resettime, cmd_delwork_clear, cmd_setchannel,
    cmd_setgroup, cmd_addextraworkgroup, cmd_clearextraworkgroup,
    cmd_showeverypush, cmd_actnum, cmd_actstatus, cmd_setfines_all, cmd_setfine,
    cmd_finesstatus, cmd_checkdualsetup, cmd_handover_status, cmd_set_handover_day,
    cmd_set_handover_hours, cmd_handover_config, cmd_testgroupaccess,
    cmd_checkbotpermissions, cmd_setworkfine, cmd_showsettings, cmd_worktime,
)
from scheduler import daily_reset_task, memory_cleanup_task, health_monitoring_task, monthly_maintenance_task

logger = logging.getLogger("GroupCheckInBot")

# ========== 日志中间件 ==========
class LoggingMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: types.Message, data):
        if event.text:
            logger.info(
                f"📨 收到消息: chat_id={event.chat.id}, uid={event.from_user.id}, text='{event.text}'"
            )
        return await handler(event, data)


# ========== Web服务器 ==========
async def health_check(request):
    """增强版健康检查接口"""
    try:
        db_healthy = await db.health_check()

        bot_healthy = (
            bot_manager.is_healthy() if hasattr(bot_manager, "is_healthy") else True
        )

        memory_info = performance_optimizer.get_memory_info()
        memory_ok = memory_info.get("status") == "healthy"

        status = "healthy" if all([db_healthy, bot_healthy, memory_ok]) else "degraded"

        return web.json_response(
            {
                "status": status,
                "timestamp": time.time(),
                "services": {
                    "database": db_healthy,
                    "bot": bot_healthy,
                    "memory": memory_info,
                },
                "version": "1.0",
                "environment": os.environ.get("BOT_MODE", "polling"),
            }
        )
    except Exception as e:
        logger.error(f"健康检查失败: {e}")
        return web.json_response(
            {"status": "unhealthy", "error": str(e), "timestamp": time.time()},
            status=500,
        )


async def start_health_server():
    """优化后的健康检查服务器"""
    port = int(os.environ.get("PORT", 10000))
    app = web.Application()

    async def root_handle(request):
        return web.Response(text="Bot is running!", status=200)

    app.router.add_get("/", root_handle)
    app.router.add_get("/health", health_check)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"✅ 健康检查服务器已在端口 {port} 启动: / 和 /health")
    return site


# ========== 服务初始化 ==========
async def initialize_services():
    """初始化所有服务"""
    logger.info("🔄 初始化服务...")

    try:
        # ===== 1. 数据库初始化 =====
        await db.initialize()
        logger.info("✅ 数据库初始化完成")

        # 确保数据库完全就绪
        max_wait = 30
        waited = 0
        initialization_success = False

        while waited < max_wait:
            if db._initialized and db.pool:
                try:
                    # 测试数据库连接
                    async with db.pool.acquire() as test_conn:
                        await test_conn.fetchval("SELECT 1")
                    logger.info(f"✅ 数据库连接测试通过 (等待 {waited}s)")
                    initialization_success = True
                    break
                except Exception as e:
                    logger.warning(f"数据库连接测试失败: {e}")
                    # ✅ 关键修复：标记为未初始化
                    db._initialized = False
                    # ✅ 尝试重新连接
                    try:
                        await db._reconnect()
                        logger.info("🔄 数据库重新连接成功")
                    except Exception as reconnect_error:
                        logger.error(f"❌ 数据库重新连接失败: {reconnect_error}")

            logger.debug(f"⏳ 等待数据库完全初始化... ({waited}s)")
            await asyncio.sleep(1)
            waited += 1

        if not initialization_success:
            raise RuntimeError("数据库初始化失败 - 无法建立稳定连接")

        if waited >= max_wait:
            raise RuntimeError("数据库初始化超时")

        # ===== 2. 启动数据库维护任务 =====
        await db.start_connection_maintenance()
        logger.info("✅ 数据库维护任务已启动")

        # ===== 3. Bot管理器初始化 =====
        await bot_manager.initialize()
        logger.info("✅ Bot管理器初始化完成")

        constants.bot = bot_manager.bot
        constants.dp = bot_manager.dispatcher

        init_notification_service(
            bot_manager_instance=bot_manager, bot_instance=constants.bot
        )

        if not notification_service.bot_manager:
            logger.error("❌ notification_service.bot_manager 设置失败")
        if not notification_service.bot:
            logger.error("❌ notification_service.bot 设置失败")
        else:
            logger.info(
                f"✅ 通知服务配置完成: bot_manager={notification_service.bot_manager is not None}, bot={notification_service.bot is not None}"
            )

        # ===== 5. 定时器管理器配置 =====
        timer_manager.set_activity_timer_callback(activity_timer)
        logger.info("✅ 定时器管理器配置完成")

        # ===== 6. 心跳管理器初始化 =====
        await heartbeat_manager.initialize()
        logger.info("✅ 心跳管理器初始化完成")

        # ===== 7. Bot健康监控启动 =====
        await bot_manager.start_health_monitor()
        logger.info("✅ Bot健康监控已启动")

        # ===== 8. 日志中间件注册 =====
        constants.dp.message.middleware(LoggingMiddleware())
        logger.info("✅ 日志中间件已注册")

        # ===== 9. 消息处理器注册 =====
        await register_handlers()
        logger.info("✅ 消息处理器注册完成")

        # ===== 10. 班次状态管理器启动 =====
        from utils import shift_state_manager

        await shift_state_manager.start()
        logger.info("✅ 班次状态管理器已启动")

        # ===== 11. 用户锁管理器启动 =====
        await user_lock_manager.start()
        logger.info("✅ 用户锁管理器清理任务已启动")

        # ===== 12. 过期活动恢复 =====
        recovered_count = await recover_expired_activities()
        logger.info(f"✅ 过期活动恢复完成: {recovered_count} 个活动已处理")

        # ===== 13. 班次状态恢复 =====
        from reset_service import recover_shift_states

        shift_recovered = await recover_shift_states()
        logger.info(f"✅ 班次状态恢复完成: {shift_recovered} 个群组")

        # ===== 14. 检查未完成的重置 =====
        from reset_service import check_missed_resets_on_startup

        asyncio.create_task(check_missed_resets_on_startup())
        logger.info("✅ 未完成重置检查任务已创建")

        # ===== 15. 服务健康检查 =====
        health_status = await check_services_health()
        if all(health_status.values()):
            logger.info("🎉 所有服务初始化完成且健康")
        else:
            warning_services = [
                k for k, v in health_status.items() if not v and k != "timestamp"
            ]
            logger.warning(f"⚠️ 服务初始化完成但有警告: {warning_services}")

        # ===== 16. 月度维护任务由 main() 后台任务统一启动 =====
        from config import Config

        logger.info(
            f"📅 月度维护任务配置:\n"
            f"   ├─ 清理时间: 每天 {getattr(Config, 'CLEANUP_HOUR', 3):02d}:{getattr(Config, 'CLEANUP_MINUTE', 0):02d}\n"
            f"   ├─ 日常保留: {getattr(Config, 'DATA_RETENTION_DAYS', 90)} 天\n"
            f"   ├─ 月度保留: {getattr(Config, 'MONTHLY_DATA_RETENTION_DAYS', 90)} 天\n"
            f"   └─ 导出时间: 每月1号 {getattr(Config, 'MONTHLY_EXPORT_HOUR', 2):02d}:{getattr(Config, 'MONTHLY_EXPORT_MINUTE', 0):02d}"
        )

    except Exception as e:
        logger.error(f"❌ 服务初始化失败: {e}")
        logger.error(
            f"调试信息 - bot: {constants.bot}, bot_manager: {bot_manager}"
        )
        logger.error(
            f"调试信息 - notification_service.bot_manager: {getattr(notification_service, 'bot_manager', '未设置')}"
        )
        logger.error(
            f"调试信息 - notification_service.bot: {getattr(notification_service, 'bot', '未设置')}"
        )
        import traceback

        logger.error(traceback.format_exc())
        raise


async def check_services_health():
    """完整的服务健康检查"""
    health_status = {
        "database": await db.health_check(),
        "bot_manager_exists": bot_manager is not None,
        "bot_manager_has_bot": hasattr(bot_manager, "bot") if bot_manager else False,
        "bot_instance": constants.bot is not None,
        "notification_service_bot_manager": notification_service.bot_manager
        is not None,
        "notification_service_bot": notification_service.bot is not None,
        "notification_service_has_methods": all(
            hasattr(notification_service, attr)
            for attr in ["_last_notification_time", "_rate_limit_window"]
        ),
        "timestamp": time.time(),
    }

    healthy_services = [k for k, v in health_status.items() if v]
    unhealthy_services = [
        k for k, v in health_status.items() if not v and k != "timestamp"
    ]

    if unhealthy_services:
        logger.warning(f"⚠️ 不健康服务: {unhealthy_services}")
    else:
        logger.info(f"✅ 所有服务健康: {healthy_services}")

    return health_status


async def register_handlers():
    """注册所有消息处理器"""
    dp = constants.dp
    if dp is None:
        raise RuntimeError("Dispatcher 未初始化，请先调用 initialize_services()")

    dp.message.register(cmd_start, Command("start"))
    dp.message.register(cmd_menu, Command("menu"))
    dp.message.register(cmd_help, Command("help"))
    dp.message.register(cmd_ci, Command("ci"))
    dp.message.register(cmd_at, Command("at"))
    dp.message.register(cmd_workstart, Command("workstart"))
    dp.message.register(cmd_workend, Command("workend"))
    dp.message.register(cmd_admin, Command("admin"))

    dp.message.register(handle_fixed_activity, Command("wc"))
    dp.message.register(handle_fixed_activity, Command("bigwc"))
    dp.message.register(handle_fixed_activity, Command("eat"))
    dp.message.register(handle_fixed_activity, Command("smoke"))
    dp.message.register(handle_fixed_activity, Command("rest"))
    dp.message.register(handle_myinfo_command, Command("myinfo"))
    dp.message.register(handle_ranking_command, Command("ranking"))

    dp.message.register(cmd_export, Command("export"))
    dp.message.register(cmd_monthlyreport, Command("monthlyreport"))
    dp.message.register(cmd_exportmonthly, Command("exportmonthly"))
    dp.message.register(cmd_addactivity, Command("addactivity"))
    dp.message.register(cmd_delactivity, Command("delactivity"))
    dp.message.register(cmd_setworktime, Command("setworktime"))
    dp.message.register(cmd_setresettime, Command("setresettime"))
    dp.message.register(cmd_resettime, Command("resettime"))
    dp.message.register(cmd_setchannel, Command("setchannel"))
    dp.message.register(cmd_setgroup, Command("setgroup"))
    dp.message.register(cmd_actnum, Command("actnum"))
    dp.message.register(cmd_actstatus, Command("actstatus"))
    dp.message.register(cmd_setfines_all, Command("setfines_all"))
    dp.message.register(cmd_setfine, Command("setfine"))
    dp.message.register(cmd_finesstatus, Command("finesstatus"))
    dp.message.register(cmd_setworkfine, Command("setworkfine"))
    dp.message.register(cmd_showsettings, Command("showsettings"))
    dp.message.register(cmd_worktime, Command("worktime"))
    dp.message.register(cmd_delwork_clear, Command("delwork_clear"))
    dp.message.register(cmd_cleanup_monthly, Command("cleanup_monthly"))
    dp.message.register(cmd_monthly_stats_status, Command("monthly_stats_status"))
    dp.message.register(cmd_cleanup_inactive, Command("cleanup_inactive"))
    dp.message.register(cmd_reset_user, Command("resetuser"))
    dp.message.register(cmd_fix_message_refs, Command("fixmessages"))

    dp.message.register(cmd_setdualmode, Command("setdualmode"))
    dp.message.register(cmd_setshiftgrace, Command("setshiftgrace"))
    dp.message.register(handle_ranking_shift_command, Command("ranking"))
    dp.message.register(handle_ranking_day_command, Command("rankingday"))
    dp.message.register(handle_ranking_night_command, Command("rankingnight"))
    dp.message.register(handle_myinfo_day_command, Command("myinfoday"))
    dp.message.register(handle_myinfo_night_command, Command("myinfonight"))
    dp.message.register(cmd_addextraworkgroup, Command("addextraworkgroup"))
    dp.message.register(cmd_clearextraworkgroup, Command("clearextraworkgroup"))
    dp.message.register(cmd_showeverypush, Command("showeverypush"))
    dp.message.register(cmd_checkdualsetup, Command("checkdual"))
    dp.message.register(cmd_testgroupaccess, Command("testgroupaccess"))
    dp.message.register(cmd_checkbotpermissions, Command("checkperms"))
    dp.message.register(cmd_setworkendgrace, Command("setworkendgrace"))
    dp.message.register(cmd_handover_status, Command("handover"))
    dp.message.register(cmd_handover_config, Command("handoverconfig"))
    dp.message.register(cmd_set_handover_day, Command("sethandoverday"))
    dp.message.register(cmd_set_handover_hours, Command("sethour"))

    dp.message.register(
        handle_back_command,
        lambda message: message.text and message.text.strip() in ["✅ 回座", "回座"],
    )
    dp.message.register(
        handle_work_buttons,
        lambda message: message.text
        and message.text.strip() in WORK_BUTTONS,
    )
    dp.message.register(
        handle_export_button,
        lambda message: message.text and message.text.strip() in ["📤 导出数据"],
    )
    dp.message.register(
        handle_my_record,
        lambda message: message.text and message.text.strip() in ["📊 我的记录"],
    )
    dp.message.register(
        handle_rank,
        lambda message: message.text and message.text.strip() in ["🏆 排行榜"],
    )
    dp.message.register(
        handle_admin_panel_button,
        lambda message: message.text and message.text.strip() in ["👑 管理员面板"],
    )
    dp.message.register(
        handle_back_to_main_menu,
        lambda message: message.text and message.text.strip() in ["🔙 返回主菜单"],
    )
    dp.message.register(
        handle_all_text_messages, lambda message: message.text and message.text.strip()
    )

    dp.callback_query.register(
        handle_quick_back, lambda c: c.data.startswith("quick_back:")
    )

    logger.info("✅ 所有消息处理器注册完成")


async def keepalive_loop():
    """生产级保活循环（防休眠 + DB自愈 + 死锁检测 + 内存维护）"""

    external_url = os.environ.get("RENDER_EXTERNAL_URL") or getattr(
        Config, "WEBHOOK_URL", None
    )
    if external_url:
        external_url = external_url.rstrip("/")

    port = int(os.environ.get("PORT", 10000))

    logger.info(f"🚀 保活循环启动 | 外部URL: {external_url or '未设置'} | 端口: {port}")

    db_fail_count = 0
    MAX_DB_FAIL = 3

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=20),
        headers={"User-Agent": "Bot-KeepAlive-Service"},
    ) as session:

        while True:
            try:
                await asyncio.sleep(300)  # 5分钟

                # =========================
                # 1 外部URL保活(Render)
                # =========================
                if external_url:
                    try:
                        async with session.get(f"{external_url}/health") as resp:
                            if resp.status != 200:
                                logger.warning(
                                    f"🌍 外部保活异常 | 状态码: {resp.status}"
                                )
                            else:
                                logger.debug("🌍 外部保活成功")
                    except Exception as e:
                        logger.warning(f"🌍 外部保活失败: {e}")

                # =========================
                # 2 本地健康检查
                # =========================
                try:
                    async with session.get(f"http://127.0.0.1:{port}/health") as resp:
                        if resp.status != 200:
                            logger.warning(
                                f"🏠 内部健康检查异常 | 状态码: {resp.status}"
                            )
                        else:
                            logger.debug("🏠 内部健康检查成功")
                except Exception as e:
                    logger.warning(f"🏠 内部健康检查失败: {e}")

                # =========================
                # 3 数据库连接池检测
                # =========================
                if hasattr(db, "pool") and db.pool:

                    try:
                        async with db.pool.acquire() as conn:
                            await conn.fetchval("SELECT 1")

                        db_fail_count = 0
                        logger.debug("🗄️ 数据库连接池正常")

                    except Exception as e:

                        db_fail_count += 1
                        logger.error(
                            f"🗄️ 数据库连接异常 ({db_fail_count}/{MAX_DB_FAIL}) : {e}"
                        )

                        # 连续失败才重建连接池
                        if db_fail_count >= MAX_DB_FAIL:
                            try:
                                logger.warning("♻️ 尝试重建数据库连接池...")

                                await db.close()
                                await db.initialize()

                                db_fail_count = 0

                                logger.info("✅ 数据库连接池重建成功")

                            except Exception as rebuild_error:
                                logger.error(
                                    f"❌ 数据库连接池重建失败: {rebuild_error}"
                                )

                # =========================
                # 4 用户锁死锁检测
                # =========================
                if hasattr(user_lock_manager, "_locks"):

                    now = time.time()
                    long_locks = []

                    for key, lock in user_lock_manager._locks.items():
                        if lock.locked():

                            last_access = user_lock_manager._access_times.get(key, 0)

                            if now - last_access > 300:
                                long_locks.append(key)

                    if long_locks:
                        logger.warning(
                            f"⚠️ 检测到长时间锁 ({len(long_locks)}) : {long_locks[:5]}"
                        )

                # =========================
                # 5 垃圾回收
                # =========================
                try:
                    collected = gc.collect()
                    if collected > 0:
                        logger.debug(f"🧹 GC回收对象: {collected}")
                except Exception:
                    pass

            except asyncio.CancelledError:
                logger.info("🛑 保活循环已取消")
                break

            except Exception as e:
                logger.error(f"⚠️ 保活循环异常: {e}")

                # 防止异常循环
                await asyncio.sleep(60)


async def on_startup():
    """启动时执行"""
    logger.info("🎯 机器人启动中...")
    try:
        await bot_manager.bot.delete_webhook(drop_pending_updates=True)

        user_commands = [
            BotCommand(command="wc", description="🚽 小厕"),
            BotCommand(command="bigwc", description="🚻 大厕"),
            BotCommand(command="eat", description="🍚 吃饭"),
            BotCommand(command="smoke", description="🚬 抽烟"),
            BotCommand(command="rest", description="🛌 休息"),
            BotCommand(command="workstart", description="🟢 上班打卡"),
            BotCommand(command="workend", description="🔴 下班打卡"),
            BotCommand(command="at", description="✅ 回座"),
            BotCommand(command="myinfo", description="📊 我的记录"),
            BotCommand(command="ranking", description="🏆 排行榜"),
            BotCommand(command="help", description="❓ 使用帮助"),
        ]

        admin_commands = user_commands + [
            BotCommand(command="actstatus", description="📊 活跃活动统计"),
            BotCommand(command="showsettings", description="⚙️ 查看系统配置"),
            BotCommand(command="finesstatus", description="📈 罚款费率查询"),
            BotCommand(command="worktime", description="⌚ 考勤时间设置"),
            BotCommand(command="export", description="📤 导出今日报表"),
            BotCommand(command="checkdb", description="🏥 数据库体检"),
            BotCommand(command="admin", description="🛠 管理员全指令指南"),
        ]

        logger.info(f"📋 要注册的命令列表: {[cmd.command for cmd in user_commands]}")

        res_user = await bot_manager.bot.set_my_commands(commands=user_commands)
        logger.info(f"✅ 普通用户命令注册结果: {res_user}")

        res_admin = await bot_manager.bot.set_my_commands(
            commands=admin_commands, scope=BotCommandScopeAllChatAdministrators()
        )
        logger.info(f"✅ 管理员指令菜单注册结果: {res_admin}")

        if hasattr(db, "initialize"):
            await db.initialize()

        await send_startup_notification()
        logger.info("✅ 系统启动完成，准备接收消息")

    except Exception as e:
        logger.error(f"❌ 启动过程异常: {e}")
        raise


async def on_shutdown():
    """关闭时执行"""
    logger.info("🛑 机器人正在关闭...")
    try:
        await db.stop_connection_maintenance()
        logger.info("✅ 数据库维护任务已停止")

        await bot_manager.stop()
        logger.info("✅ Bot管理器已停止")

        cancelled_count = await timer_manager.cancel_all_timers()
        logger.info(f"✅ 已取消 {cancelled_count} 个活动定时器")

        await heartbeat_manager.stop()
        logger.info("✅ 心跳管理器已停止")

        from utils import shift_state_manager

        await shift_state_manager.stop()
        logger.info("✅ 班次状态管理器已停止")

        await send_shutdown_notification()
        logger.info("✅ 关闭通知已发送")

        logger.info("🎉 所有服务已优雅关闭")
    except Exception as e:
        logger.error(f"关闭清理过程中出错: {e}")


async def main():
    """机器人主入口"""
    background_tasks = []

    try:
        logger.info("=" * 50)
        logger.info("🚀 GroupCheckInBot 启动")
        logger.info(
            f"🌐 模式: {Config.BOT_MODE} | "
            f"Render: {bool(os.environ.get('RENDER'))}"
        )
        logger.info("=" * 50)

        Config.validate_config()

        await start_health_server()
        await initialize_services()

        dp = constants.dp
        if dp is None:
            raise RuntimeError("Dispatcher 未初始化")

        dp.startup.register(on_startup)
        dp.shutdown.register(on_shutdown)

        background_tasks.extend(
            [
                asyncio.create_task(daily_reset_task(), name="daily_reset"),
                asyncio.create_task(memory_cleanup_task(), name="memory_cleanup"),
                asyncio.create_task(
                    health_monitoring_task(), name="health_monitoring"
                ),
                asyncio.create_task(
                    monthly_maintenance_task(), name="monthly_maintenance"
                ),
            ]
        )

        if Config.KEEPALIVE_ENABLED:
            background_tasks.append(
                asyncio.create_task(keepalive_loop(), name="keepalive")
            )

        logger.info(f"✅ 后台任务已启动: {len(background_tasks)} 个")
        await bot_manager.start_polling_with_retry()

    except asyncio.CancelledError:
        logger.info("主任务被取消")
    except Exception as e:
        logger.error(f"❌ 主程序异常: {e}")
        logger.error(traceback.format_exc())
        raise
    finally:
        for task in background_tasks:
            if not task.done():
                task.cancel()
        if background_tasks:
            await asyncio.gather(*background_tasks, return_exceptions=True)

        with suppress(Exception):
            await db.close()

        logger.info("👋 程序已退出")

