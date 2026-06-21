import asyncio
import logging
import time
import traceback
from datetime import datetime, timedelta, date

from config import Config
from database import db
from utils import performance_optimizer
from reset_service import (
    handle_hard_reset,
    process_all_pending_resets,
    is_near_reset_window,
)

logger = logging.getLogger("GroupCheckInBot")

# ========== 定时任务 ==========
async def daily_reset_task():
    """每日重置监控任务 - 纯双班模式"""
    logger.info("🚀 每日重置监控任务已启动")

    # 等待数据库完全初始化
    max_wait = 30
    waited = 0
    while waited < max_wait:
        if db._initialized and db.pool:
            try:
                async with db.pool.acquire() as test_conn:
                    await test_conn.fetchval("SELECT 1")
                logger.info(f"✅ 数据库已就绪，重置任务开始工作 (等待 {waited}s)")
                break
            except Exception as e:
                logger.warning(f"数据库连接测试失败: {e}")

        logger.debug(f"⏳ 等待数据库初始化... ({waited}s)")
        await asyncio.sleep(1)
        waited += 1

    if waited >= max_wait:
        logger.error("❌ 数据库初始化超时，重置任务退出")
        return

    sem = asyncio.Semaphore(10)
    TASK_TIMEOUT = 300
    last_pending_check = 0.0

    async def process_group_reset(chat_id: int, now: datetime):
        """处理单个群组的重置（委托给 handle_hard_reset 统一窗口逻辑）"""
        start_time = time.time()

        async with sem:
            try:
                async with asyncio.timeout(TASK_TIMEOUT):
                    group_data = await db.get_group_cached(chat_id)
                    if not group_data:
                        return

                    result = await handle_hard_reset(chat_id, None)
                    if result:
                        elapsed = time.time() - start_time
                        if elapsed > 10:
                            logger.info(
                                f"⏱️ 群组 {chat_id} 重置完成，耗时: {elapsed:.2f}秒"
                            )

            except asyncio.TimeoutError:
                logger.error(f"❌ 群组 {chat_id} 重置超时（>{TASK_TIMEOUT}秒）")
            except Exception as e:
                logger.error(f"❌ 处理群组 {chat_id} 重置失败: {e}")
                logger.error(traceback.format_exc())

    loop_count = 0
    while True:
        try:
            loop_start = time.time()
            loop_count += 1

            if not db._initialized or not db.pool:
                logger.error("❌ 数据库连接已断开，重置任务暂停")
                await asyncio.sleep(60)
                continue

            if loop_count % 10 == 0:
                try:
                    async with db.pool.acquire() as test_conn:
                        await test_conn.fetchval("SELECT 1")
                except Exception as e:
                    logger.error(f"❌ 数据库连接测试失败: {e}")
                    await asyncio.sleep(60)
                    continue

            now = db.get_beijing_time()

            try:
                all_groups = await db.get_all_groups()
            except Exception as e:
                logger.error(f"❌ 获取群组列表失败: {e}")
                await asyncio.sleep(60)
                continue

            near_window = False
            for chat_id in all_groups:
                if await is_near_reset_window(chat_id, now):
                    near_window = True
                    break

            if near_window:
                if now.minute in [0, 30]:
                    logger.debug(
                        f"🔄 重置窗口附近，检查 {len(all_groups)} 个群组 "
                        f"({now.strftime('%H:%M')})"
                    )
                tasks = [process_group_reset(cid, now) for cid in all_groups]
                await asyncio.gather(*tasks, return_exceptions=True)
                sleep_seconds = 30
            else:
                now_ts = time.time()
                if now_ts - last_pending_check >= 3600:
                    try:
                        pending_stats = await process_all_pending_resets(
                            max_dates_per_group=3
                        )
                        if pending_stats.get("dates_cleared"):
                            logger.info(
                                f"📋 非窗口期补归档: "
                                f"{pending_stats['dates_cleared']} 个日期"
                            )
                    except Exception as e:
                        logger.error(f"非窗口期补归档失败: {e}")
                    last_pending_check = now_ts
                sleep_seconds = 300

            loop_elapsed = time.time() - loop_start
            if loop_elapsed > 10:
                logger.info(f"⏱️ 重置检查循环耗时: {loop_elapsed:.2f}秒")

        except Exception as e:
            logger.error(f"❌ 重置任务主循环出错: {e}")
            logger.error(traceback.format_exc())
            await asyncio.sleep(60)
            continue

        await asyncio.sleep(sleep_seconds)


async def memory_cleanup_task():
    """定期内存清理任务"""
    while True:
        try:
            await asyncio.sleep(Config.CLEANUP_INTERVAL)
            await performance_optimizer.memory_cleanup()
            logger.debug("定期内存清理任务完成")
        except Exception as e:
            logger.error(f"内存清理任务失败: {e}")
            await asyncio.sleep(300)


async def health_monitoring_task():
    """健康监控任务"""
    while True:
        try:
            if not performance_optimizer.memory_usage_ok():
                logger.warning("内存使用过高，执行紧急清理")
                await performance_optimizer.memory_cleanup()

            await asyncio.sleep(60)
        except Exception as e:
            logger.error(f"健康监控任务失败: {e}")
            await asyncio.sleep(60)


async def monthly_maintenance_task():
    """每月维护任务 - 整合新导出架构"""
    logger.info("📅 月度维护任务已启动")

    last_cleanup_date = None
    last_export_date = None

    while True:
        try:
            now = db.get_beijing_time()
            today = now.date()

            if (
                now.hour == Config.CLEANUP_HOUR
                and now.minute == Config.CLEANUP_MINUTE
                and last_cleanup_date != today
            ):

                if Config.AUTO_CLEANUP_ENABLED:
                    logger.info(
                        f"🧹 开始自动清理\n"
                        f"   ├─ 日常保留: {Config.DATA_RETENTION_DAYS}天\n"
                        f"   └─ 月度保留: {Config.MONTHLY_DATA_RETENTION_DAYS}天"
                    )

                    pending_stats = await process_all_pending_resets(
                        max_dates_per_group=5
                    )
                    if pending_stats["dates_cleared"]:
                        logger.info(
                            f"📋 清理前补归档: "
                            f"{pending_stats['dates_cleared']} 个日期 / "
                            f"{pending_stats['groups']} 个群组"
                        )

                    daily_deleted = await db.cleanup_old_data(
                        Config.DATA_RETENTION_DAYS
                    )
                    monthly_deleted = await db.cleanup_monthly_data(
                        Config.MONTHLY_DATA_RETENTION_DAYS
                    )

                    logger.info(
                        f"✅ 自动清理完成\n"
                        f"   ├─ 日常数据: {daily_deleted} 条\n"
                        f"   └─ 月度数据: {monthly_deleted} 条"
                    )
                    last_cleanup_date = today

            if (
                now.day == 1
                and now.hour == Config.MONTHLY_EXPORT_HOUR
                and now.minute == Config.MONTHLY_EXPORT_MINUTE
                and last_export_date != today
            ):

                if Config.MONTHLY_EXPORT_ENABLED:
                    from reset_service import _export_yesterday_data_concurrent

                    if now.month == 1:
                        year = now.year - 1
                        month = 12
                    else:
                        year = now.year
                        month = now.month - 1

                    logger.info(f"📊 开始导出 {year}年{month}月 数据")

                    all_groups = await db.get_all_groups()
                    success_count = 0
                    failed_count = 0

                    for chat_id in all_groups:
                        try:
                            await db.init_group(chat_id)

                            target_date = date(year, month, 1)
                            success = await _export_yesterday_data_concurrent(
                                chat_id=chat_id,
                                target_date=target_date,
                                from_monthly=True,
                            )

                            if success:
                                success_count += 1
                                logger.info(f"✅ 群组 {chat_id} 月度导出成功")
                            else:
                                failed_count += 1
                                logger.error(f"❌ 群组 {chat_id} 月度导出失败")

                            await asyncio.sleep(1)

                        except Exception as e:
                            failed_count += 1
                            logger.error(f"❌ 群组 {chat_id} 导出失败: {e}")
                            logger.error(traceback.format_exc())

                    logger.info(
                        f"📊 月度导出完成\n"
                        f"   ├─ 成功: {success_count} 个群组\n"
                        f"   ├─ 失败: {failed_count} 个群组\n"
                        f"   └─ 总计: {len(all_groups)} 个群组"
                    )
                    last_export_date = today

            await asyncio.sleep(60)

        except Exception as e:
            logger.error(f"❌ 月度维护任务异常: {e}")
            logger.error(traceback.format_exc())
            await asyncio.sleep(60)