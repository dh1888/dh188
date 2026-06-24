"""磁盘使用率监控与兜底清理（≥ 阈值时清 temp/日志并缩短数据保留期）。"""

import asyncio
import glob
import logging
import os
import time
from typing import Optional

import psutil

from config import Config
from database import db

logger = logging.getLogger("GroupCheckInBot")

_last_emergency_run = 0.0
_EMERGENCY_COOLDOWN_SEC = 3600


def get_local_disk_usage_percent() -> Optional[float]:
    """读取本机磁盘使用率（工作目录与根目录取较高值）。"""
    try:
        usages = []
        for path in {os.getcwd(), os.path.abspath(os.sep)}:
            try:
                usages.append(psutil.disk_usage(path).percent)
            except OSError:
                pass
        return max(usages) if usages else None
    except Exception as e:
        logger.debug(f"读取磁盘使用率失败: {e}")
        return None


async def _get_db_disk_usage_percent() -> Optional[float]:
    """PostgreSQL 占用相对 DB_DISK_QUOTA_GB 的百分比；未配置配额则返回 None。"""
    quota_gb = Config.DB_DISK_QUOTA_GB
    if quota_gb <= 0 or not db._initialized or not db.pool:
        return None
    try:
        async with db.pool.acquire() as conn:
            size_bytes = await conn.fetchval(
                "SELECT pg_database_size(current_database())"
            )
        if not size_bytes:
            return None
        used_gb = size_bytes / (1024**3)
        return min(100.0, (used_gb / quota_gb) * 100.0)
    except Exception as e:
        logger.debug(f"读取 PostgreSQL 磁盘占用失败: {e}")
        return None


def _cleanup_temp_files() -> int:
    """删除导出残留的 temp_*.xlsx / temp_*.csv。"""
    removed = 0
    for pattern in ("temp_*.xlsx", "temp_*.csv"):
        for path in glob.glob(pattern):
            try:
                os.remove(path)
                removed += 1
            except OSError as e:
                logger.debug(f"删除临时文件失败 {path}: {e}")
    return removed


def _trim_log_file(max_mb: float = 5.0) -> bool:
    """bot.log 过大时保留最近若干行。"""
    log_path = "bot.log"
    try:
        if not os.path.isfile(log_path):
            return False
        size_mb = os.path.getsize(log_path) / (1024 * 1024)
        if size_mb <= max_mb:
            return False
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        keep = lines[-5000:] if len(lines) > 5000 else lines
        with open(log_path, "w", encoding="utf-8") as f:
            f.writelines(keep)
        logger.info(
            f"🧹 bot.log 已从 {size_mb:.1f}MB 截断为最近 {len(keep)} 行"
        )
        return True
    except Exception as e:
        logger.warning(f"截断 bot.log 失败: {e}")
        return False


async def run_emergency_disk_cleanup(*, force_aggressive: bool = False) -> dict:
    """执行一轮紧急磁盘清理。"""
    global _last_emergency_run

    stats = {
        "temp_files": 0,
        "log_trimmed": False,
        "daily_deleted": 0,
        "monthly_deleted": 0,
        "reset_logs_deleted": 0,
    }

    stats["temp_files"] = _cleanup_temp_files()
    stats["log_trimmed"] = _trim_log_file()

    daily_days = Config.DISK_EMERGENCY_DAILY_RETENTION_DAYS
    monthly_days = Config.DISK_EMERGENCY_MONTHLY_RETENTION_DAYS
    if force_aggressive:
        daily_days = Config.DISK_EMERGENCY_MIN_DAILY_RETENTION_DAYS
        monthly_days = Config.DISK_EMERGENCY_MIN_MONTHLY_RETENTION_DAYS

    if db._initialized and db.pool:
        try:
            from reset_service import process_all_pending_resets

            pending = await process_all_pending_resets(max_dates_per_group=3)
            if pending.get("dates_cleared"):
                logger.info(
                    f"🧹 兜底清理前补归档: {pending['dates_cleared']} 个日期"
                )
        except Exception as e:
            logger.warning(f"兜底清理前补归档失败: {e}")

        try:
            stats["reset_logs_deleted"] = await db.cleanup_old_reset_logs(days=30)
        except Exception as e:
            logger.warning(f"清理 reset_logs 失败: {e}")

        try:
            stats["daily_deleted"] = await db.cleanup_old_data(daily_days)
        except Exception as e:
            logger.warning(f"紧急清理日表失败: {e}")

        try:
            stats["monthly_deleted"] = await db.cleanup_monthly_data(monthly_days)
        except Exception as e:
            logger.warning(f"紧急清理月表失败: {e}")

    _last_emergency_run = time.time()
    return stats


def _is_over_threshold(
    local_pct: Optional[float], db_pct: Optional[float]
) -> tuple[bool, list[str]]:
    threshold = Config.DISK_USAGE_THRESHOLD_PERCENT
    reasons = []
    if local_pct is not None and local_pct >= threshold:
        reasons.append(f"本机磁盘 {local_pct:.1f}%")
    if db_pct is not None and db_pct >= threshold:
        reasons.append(f"数据库 {db_pct:.1f}%")
    return bool(reasons), reasons


async def disk_monitor_task():
    """后台磁盘监控：超阈值时触发紧急清理（1 小时冷却）。"""
    logger.info(
        f"💾 磁盘监控已启动 "
        f"(阈值 {Config.DISK_USAGE_THRESHOLD_PERCENT}%, "
        f"间隔 {Config.DISK_CHECK_INTERVAL_SEC}s)"
    )

    while True:
        try:
            await asyncio.sleep(Config.DISK_CHECK_INTERVAL_SEC)

            if not Config.DISK_CLEANUP_ENABLED:
                continue

            local_pct = get_local_disk_usage_percent()
            db_pct = await _get_db_disk_usage_percent()
            triggered, reasons = _is_over_threshold(local_pct, db_pct)

            if not triggered:
                continue

            now = time.time()
            if now - _last_emergency_run < _EMERGENCY_COOLDOWN_SEC:
                logger.warning(
                    f"💾 磁盘超阈值 ({', '.join(reasons)})，冷却中跳过紧急清理"
                )
                continue

            logger.warning(
                f"🚨 磁盘使用率 ≥ {Config.DISK_USAGE_THRESHOLD_PERCENT}%: "
                f"{', '.join(reasons)}，开始紧急清理"
            )

            stats = await run_emergency_disk_cleanup(force_aggressive=False)

            local_after = get_local_disk_usage_percent()
            db_after = await _get_db_disk_usage_percent()
            still_high, _ = _is_over_threshold(local_after, db_after)
            if still_high:
                logger.warning("🚨 首次紧急清理后仍超阈值，执行更激进清理（7 天保留）")
                stats = await run_emergency_disk_cleanup(force_aggressive=True)

            logger.info(
                f"✅ 磁盘紧急清理完成: "
                f"temp={stats['temp_files']}, "
                f"log={'是' if stats['log_trimmed'] else '否'}, "
                f"daily={stats['daily_deleted']}, "
                f"monthly={stats['monthly_deleted']}, "
                f"reset_logs={stats['reset_logs_deleted']}"
            )

            try:
                from utils import notification_service

                await notification_service.send_notification(
                    chat_id=None,
                    text=(
                        f"⚠️ <b>磁盘紧急清理</b>\n\n"
                        f"原因: {', '.join(reasons)}\n"
                        f"临时文件: {stats['temp_files']}\n"
                        f"日表删除: {stats['daily_deleted']}\n"
                        f"月表删除: {stats['monthly_deleted']}\n"
                        f"reset_logs: {stats['reset_logs_deleted']}"
                    ),
                    notification_type="admin",
                )
            except Exception as e:
                logger.debug(f"磁盘清理通知发送失败: {e}")

        except asyncio.CancelledError:
            logger.info("💾 磁盘监控已停止")
            break
        except Exception as e:
            logger.error(f"💾 磁盘监控异常: {e}")
            await asyncio.sleep(60)
