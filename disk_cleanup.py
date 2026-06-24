"""磁盘使用率监控与兜底历史数据清理。"""

import asyncio
import glob
import logging
import os
import time

import psutil

from config import Config
from database import db

logger = logging.getLogger("GroupCheckInBot")

_last_emergency_run = 0.0


def get_local_disk_usage_percent() -> float | None:
    """读取本机磁盘使用率（取工作目录与根目录中的较高值）。"""
    try:
        candidates = []
        cwd = os.getcwd()
        if os.path.exists(cwd):
            candidates.append(cwd)
        if os.name != "nt" and os.path.exists("/"):
            candidates.append("/")

        if not candidates:
            return None

        return max(psutil.disk_usage(path).percent for path in candidates)
    except Exception as e:
        logger.warning(f"无法读取本地磁盘使用率: {e}")
        return None


async def get_database_usage_percent() -> float | None:
    """读取 PostgreSQL 相对配额的使用率（需配置 DB_DISK_QUOTA_GB）。"""
    quota_gb = Config.DB_DISK_QUOTA_GB
    if quota_gb <= 0:
        return None

    try:
        size_bytes = await db.get_database_size_bytes()
        quota_bytes = quota_gb * (1024**3)
        if quota_bytes <= 0:
            return None
        return (size_bytes / quota_bytes) * 100
    except Exception as e:
        logger.warning(f"无法读取数据库磁盘使用率: {e}")
        return None


async def get_max_disk_usage_percent() -> tuple[float | None, str]:
    """返回 (最高使用率, 来源 local|database|unknown)。"""
    local = get_local_disk_usage_percent()
    db_pct = await get_database_usage_percent()

    if local is None and db_pct is None:
        return None, "unknown"
    if db_pct is None:
        return local, "local"
    if local is None:
        return db_pct, "database"
    if db_pct >= local:
        return db_pct, "database"
    return local, "local"


def cleanup_temp_files() -> int:
    """清理导出残留的临时文件。"""
    removed = 0
    for pattern in ("temp_*.xlsx", "temp_*.csv", "temp_*.xls"):
        for path in glob.glob(pattern):
            try:
                os.remove(path)
                removed += 1
            except OSError as e:
                logger.warning(f"删除临时文件失败 {path}: {e}")

    if removed:
        logger.info(f"🧹 磁盘兜底: 删除 {removed} 个临时文件")
    return removed


async def run_emergency_disk_cleanup(*, force: bool = False) -> dict:
    """磁盘超阈值时的兜底清理：临时文件 → 日志 → 历史日/月表。"""
    global _last_emergency_run

    stats = {
        "temp_files": 0,
        "reset_logs": 0,
        "event_logs": 0,
        "daily": 0,
        "monthly": 0,
        "passes": 0,
    }

    now = time.time()
    if (
        not force
        and _last_emergency_run
        and now - _last_emergency_run < Config.DISK_EMERGENCY_COOLDOWN_SEC
    ):
        logger.info("磁盘兜底清理仍在冷却期内，跳过")
        return stats

    _last_emergency_run = now
    logger.warning(
        f"🚨 磁盘使用率 ≥ {Config.DISK_USAGE_THRESHOLD_PERCENT}%，"
        f"启动兜底历史数据清理"
    )

    stats["temp_files"] = cleanup_temp_files()

    try:
        await db.cleanup_cache()
    except Exception as e:
        logger.warning(f"磁盘兜底: 缓存清理失败: {e}")

    try:
        from reset_service import process_all_pending_resets

        pending = await process_all_pending_resets(max_dates_per_group=3)
        if pending.get("dates_cleared"):
            logger.info(
                f"磁盘兜底: 补归档 {pending['dates_cleared']} 个待重置日期"
            )
    except Exception as e:
        logger.warning(f"磁盘兜底: 补归档失败: {e}")

    log_days = Config.DISK_EMERGENCY_LOG_RETENTION_DAYS
    stats["reset_logs"] = await db.cleanup_old_reset_logs(days=log_days)
    stats["event_logs"] = await db.cleanup_event_logs(days=log_days)

    retention_plan = [
        (
            Config.DISK_EMERGENCY_DAILY_RETENTION_DAYS,
            Config.DISK_EMERGENCY_MONTHLY_RETENTION_DAYS,
        ),
        (
            Config.DISK_EMERGENCY_MIN_RETENTION_DAYS,
            Config.DISK_EMERGENCY_MIN_RETENTION_DAYS,
        ),
    ]

    for daily_days, monthly_days in retention_plan:
        stats["passes"] += 1
        stats["daily"] += await db.cleanup_old_data(daily_days)
        stats["monthly"] += await db.cleanup_monthly_data(monthly_days)

        usage, _ = await get_max_disk_usage_percent()
        if usage is None or usage < Config.DISK_USAGE_THRESHOLD_PERCENT:
            break

    usage, source = await get_max_disk_usage_percent()
    usage_text = f"{usage:.1f}%" if usage is not None else "未知"

    logger.warning(
        f"🚨 磁盘兜底清理完成 ({source}={usage_text})\n"
        f"   ├─ 临时文件: {stats['temp_files']}\n"
        f"   ├─ reset_logs: {stats['reset_logs']}\n"
        f"   ├─ event_logs: {stats['event_logs']}\n"
        f"   ├─ 日表: {stats['daily']}\n"
        f"   └─ 月表: {stats['monthly']}"
    )

    try:
        from utils import notification_service

        await notification_service.send_notification(
            chat_id=None,
            text=(
                f"🚨 磁盘兜底清理完成\n\n"
                f"来源: {source}\n"
                f"当前使用率: {usage_text}\n"
                f"阈值: {Config.DISK_USAGE_THRESHOLD_PERCENT}%\n\n"
                f"临时文件: {stats['temp_files']}\n"
                f"reset_logs: {stats['reset_logs']}\n"
                f"event_logs: {stats['event_logs']}\n"
                f"日表删除: {stats['daily']}\n"
                f"月表删除: {stats['monthly']}"
            ),
            notification_type="admin",
        )
    except Exception as e:
        logger.warning(f"磁盘兜底通知发送失败: {e}")

    return stats


async def disk_monitor_task():
    """周期性检查磁盘使用率，超阈值触发兜底清理。"""
    logger.info(
        f"💾 磁盘监控已启动 "
        f"(阈值 {Config.DISK_USAGE_THRESHOLD_PERCENT}%, "
        f"间隔 {Config.DISK_CHECK_INTERVAL_SEC}s)"
    )

    while True:
        try:
            if not Config.DISK_CLEANUP_ENABLED:
                await asyncio.sleep(Config.DISK_CHECK_INTERVAL_SEC)
                continue

            if not db._initialized or not db.pool:
                await asyncio.sleep(30)
                continue

            usage, source = await get_max_disk_usage_percent()
            threshold = Config.DISK_USAGE_THRESHOLD_PERCENT

            if usage is not None:
                if usage >= threshold:
                    logger.warning(
                        f"⚠️ {source} 磁盘 {usage:.1f}% ≥ {threshold}%，触发兜底清理"
                    )
                    await run_emergency_disk_cleanup()
                elif usage >= threshold - 10:
                    logger.info(f"💾 {source} 磁盘使用率 {usage:.1f}%")

            await asyncio.sleep(Config.DISK_CHECK_INTERVAL_SEC)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"磁盘监控任务失败: {e}")
            await asyncio.sleep(60)
