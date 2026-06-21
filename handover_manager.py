# handover_manager.py

import logging
import asyncio
import time
from datetime import datetime, date, timedelta, time as dt_time
from typing import Dict, Optional, Tuple, Any, List, Union

from database import db
from config import beijing_tz, Config

logger = logging.getLogger("GroupCheckInBot.HandoverManager")


class HandoverManager:
    """换班管理器 - 处理月末夜班跨月和月初白班跨日的18小时工作制"""

    def __init__(self):
        # ===== 1. 基础缓存（已有TTL）=====
        self._cache = {}
        self._cache_ttl = {}

        # ===== 2. 周期缓存（已有TTL）=====
        self._period_cache = {}
        self._period_cache_ttl = {}

        # ===== 3. 换班日缓存（已有TTL）=====
        self._handover_cache = {}
        self._handover_cache_ttl = {}

        # ===== 4. 用户周期缓存（需要修复）=====
        self._user_cycle_cache = {}  # 数据缓存
        self._user_cycle_cache_ttl = {}  # TTL缓存
        self._user_cycle_access = {}  # 最后访问时间（用于LRU）

        # ===== 5. 缓存配置 =====
        self._user_cycle_max_size = 2000  # 最大缓存条目
        self._user_cycle_default_ttl = 300  # 默认5分钟
        self._last_cleanup = time.time()
        self._cleanup_interval = 600  # 10分钟清理一次
        self._lock = asyncio.Lock()  # 并发控制

        # ===== 6. 活动次数短缓存（减少打卡路径上的重复查询）=====
        self._activity_count_cache = {}
        self._activity_count_cache_ttl = {}
        self._activity_count_cache_ttl_seconds = 20

    # ========== 用户周期缓存管理（新增）==========

    async def _cleanup_user_cycle_cache(self):
        """清理过期的用户周期缓存（TTL + LRU）"""
        now = time.time()

        # 控制清理频率
        if now - self._last_cleanup < self._cleanup_interval:
            return

        async with self._lock:
            # 1. 清理过期缓存（基于TTL）
            expired = [
                key
                for key, expiry in self._user_cycle_cache_ttl.items()
                if now > expiry
            ]

            for key in expired:
                self._user_cycle_cache.pop(key, None)
                self._user_cycle_cache_ttl.pop(key, None)
                self._user_cycle_access.pop(key, None)

            # 2. LRU淘汰（如果仍然过大）
            if len(self._user_cycle_cache) > self._user_cycle_max_size:
                # 按最后访问时间排序
                sorted_keys = sorted(
                    self._user_cycle_access.items(), key=lambda x: x[1]
                )

                # 淘汰最旧的20%
                evict_count = max(1, len(sorted_keys) // 5)
                keys_to_evict = [k for k, _ in sorted_keys[:evict_count]]

                for key in keys_to_evict:
                    self._user_cycle_cache.pop(key, None)
                    self._user_cycle_cache_ttl.pop(key, None)
                    self._user_cycle_access.pop(key, None)

                logger.debug(f"🧹 LRU淘汰: 移除了 {evict_count} 个用户周期缓存")

            if expired:
                logger.debug(f"🧹 清理了 {len(expired)} 个过期用户周期缓存")

            self._last_cleanup = now

    async def _get_user_cycle_cached(self, key: str) -> Optional[Dict]:
        """安全获取用户周期缓存"""
        async with self._lock:
            now = time.time()

            # 检查TTL
            if key in self._user_cycle_cache_ttl:
                if now < self._user_cycle_cache_ttl[key]:
                    # 更新访问时间
                    self._user_cycle_access[key] = now
                    return self._user_cycle_cache.get(key)
                else:
                    # 过期删除
                    self._user_cycle_cache.pop(key, None)
                    self._user_cycle_cache_ttl.pop(key, None)
                    self._user_cycle_access.pop(key, None)

            return None

    async def _set_user_cycle_cached(self, key: str, value: Dict, ttl: int = None):
        """安全设置用户周期缓存"""
        if ttl is None:
            ttl = self._user_cycle_default_ttl

        async with self._lock:
            now = time.time()

            # 检查大小，必要时执行LRU
            if len(self._user_cycle_cache) >= self._user_cycle_max_size:
                await self._evict_lru()

            self._user_cycle_cache[key] = value
            self._user_cycle_cache_ttl[key] = now + ttl
            self._user_cycle_access[key] = now

    async def _evict_lru(self):
        """LRU淘汰（内部方法，调用时需持有锁）"""
        if len(self._user_cycle_access) <= 100:  # 太小就不淘汰
            return

        # 按最后访问时间排序
        sorted_keys = sorted(self._user_cycle_access.items(), key=lambda x: x[1])

        # 淘汰最旧的20%
        evict_count = max(1, len(sorted_keys) // 5)
        keys_to_evict = [k for k, _ in sorted_keys[:evict_count]]

        for key in keys_to_evict:
            self._user_cycle_cache.pop(key, None)
            self._user_cycle_cache_ttl.pop(key, None)
            self._user_cycle_access.pop(key, None)

    async def _invalidate_user_cycle_cache(self, chat_id: int, user_id: int):
        """使特定用户的周期缓存失效"""
        prefix = f"user_cycle:{chat_id}:{user_id}:"

        async with self._lock:
            keys_to_remove = [
                key for key in self._user_cycle_cache.keys() if key.startswith(prefix)
            ]

            for key in keys_to_remove:
                self._user_cycle_cache.pop(key, None)
                self._user_cycle_cache_ttl.pop(key, None)
                self._user_cycle_access.pop(key, None)

    async def invalidate_chat_cycle_cache(self, chat_id: int):
        """使群组内所有用户的换班周期缓存失效"""
        prefix = f"user_cycle:{chat_id}:"

        async with self._lock:
            keys_to_remove = [
                key for key in self._user_cycle_cache.keys() if key.startswith(prefix)
            ]

            for key in keys_to_remove:
                self._user_cycle_cache.pop(key, None)
                self._user_cycle_cache_ttl.pop(key, None)
                self._user_cycle_access.pop(key, None)

    # ========== 配置管理（原样保留）==========

    async def init_handover_config(self, chat_id: int) -> bool:
        """初始化换班配置"""
        try:
            await db.execute_with_retry(
                "初始化换班配置",
                """
                INSERT INTO shift_handover_configs 
                (chat_id, handover_enabled, night_start_time, day_start_time,
                 handover_night_hours, handover_day_hours, normal_night_hours, normal_day_hours)
                VALUES ($1, TRUE, '21:00', '09:00', 18, 18, 12, 12)
                ON CONFLICT (chat_id) DO NOTHING
                """,
                chat_id,
            )
            self._invalidate_cache(chat_id)
            return True
        except Exception as e:
            logger.error(f"初始化换班配置失败 {chat_id}: {e}")
            return False

    async def get_handover_config(self, chat_id: int) -> Dict[str, Any]:
        """获取换班配置"""
        cache_key = f"handover_config:{chat_id}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        try:
            row = await db.execute_with_retry(
                "获取换班配置",
                "SELECT * FROM shift_handover_configs WHERE chat_id = $1",
                chat_id,
                fetchrow=True,
            )

            if not row:
                await self.init_handover_config(chat_id)
                row = await db.execute_with_retry(
                    "获取换班配置",
                    "SELECT * FROM shift_handover_configs WHERE chat_id = $1",
                    chat_id,
                    fetchrow=True,
                )

            result = dict(row) if row else self._get_default_config()
            self._set_cached(cache_key, result, 300)
            return result

        except Exception as e:
            logger.error(f"获取换班配置失败 {chat_id}: {e}")
            return self._get_default_config()

    def _get_default_config(self) -> Dict[str, Any]:
        """获取默认配置"""
        return {
            "chat_id": 0,
            "handover_enabled": True,
            "night_start_time": "21:00",
            "day_start_time": "09:00",
            "handover_night_hours": 18,
            "handover_day_hours": 18,
            "normal_night_hours": 12,
            "normal_day_hours": 12,
        }

    def _get_reset_threshold_hours(self, config: dict) -> float:
        """换班日活动次数重置阈值（默认12小时，可通过环境变量配置）"""
        return float(
            config.get("handover_reset_threshold_hours")
            or Config.HANDOVER_RESET_THRESHOLD_HOURS
        )

    async def get_user_effective_cycle(
        self,
        chat_id: int,
        user_id: int,
        period: Dict[str, Any],
        config: Optional[dict] = None,
    ) -> int:
        """
        获取用户当前有效周期（1 或 2）

        换班18小时制：前 threshold 小时为周期1，之后为周期2（活动次数重置）
        优先按挂钟时间，也支持个人累计工时提前进入周期2
        """
        if not period.get("is_handover"):
            return 1

        if config is None:
            config = await self.get_handover_config(chat_id)

        threshold_seconds = int(self._get_reset_threshold_hours(config) * 3600)

        if period.get("cycle", 1) >= 2:
            return 2

        cycle1 = await self.get_user_cycle(
            chat_id,
            user_id,
            period["business_date"],
            period["period_type"],
            1,
        )
        if cycle1 and cycle1.get("total_work_seconds", 0) >= threshold_seconds:
            return 2

        cycle2 = await self.get_user_cycle(
            chat_id,
            user_id,
            period["business_date"],
            period["period_type"],
            2,
        )
        if cycle2:
            return 2

        return 1

    async def get_handover_status_hint(
        self,
        chat_id: int,
        user_id: int,
        current_time: Optional[datetime] = None,
    ) -> Optional[str]:
        """换班日打卡/活动提示文案"""
        if current_time is None:
            current_time = db.get_beijing_time()

        period = await self.determine_current_period(chat_id, current_time)
        if not period.get("is_handover"):
            return None

        config = await self.get_handover_config(chat_id)
        threshold = self._get_reset_threshold_hours(config)
        total_hours = period.get("total_hours", 18)
        effective_cycle = await self.get_user_effective_cycle(
            chat_id, user_id, period, config
        )

        if effective_cycle == 1:
            remaining = max(0.0, threshold - period.get("hours_elapsed", 0))
            reset_at = period.get("next_reset_time")
            reset_str = (
                reset_at.strftime("%H:%M") if reset_at else f"{threshold:.0f}小时后"
            )
            return (
                f"🔄 <b>换班日</b>（{total_hours:.0f}小时制）· 第1段\n"
                f"⏱️ 活动次数将在 <code>{reset_str}</code> 重置（约 {remaining:.1f}h 后）"
            )

        second_hours = max(0.0, total_hours - threshold)
        return (
            f"🔄 <b>换班日</b>（{total_hours:.0f}小时制）· 第2段\n"
            f"✅ 活动次数已重置，本段约 {second_hours:.0f} 小时"
        )

    # ========== 核心时间判定（原样保留）==========

    async def determine_current_period(
        self,
        chat_id: int,
        current_time: Optional[datetime] = None,
        use_cache: bool = True,
    ) -> Dict[str, Any]:
        """
        根据时间判定当前属于哪个时期（支持自定义换班日期）
        """
        if current_time is None:
            current_time = db.get_beijing_time()

        # ===== 1. 缓存检查 =====
        cache_key = f"period:{chat_id}:{current_time.strftime('%Y%m%d%H')}"
        if use_cache:
            cached = await self._get_period_cache(cache_key)
            if cached:
                logger.debug(f"📊 周期缓存命中: {cache_key}")
                return cached

        # ===== 2. 获取配置 =====
        config = await self.get_handover_config(chat_id)

        # ===== 3. 快速路径：非换班日直接返回 =====
        if not config.get("handover_enabled", True):
            result = await self._get_normal_period(chat_id, current_time, config)
            if use_cache:
                await self._set_period_cache(cache_key, result)
            return result

        # ===== 4. 预计算时间值（避免重复计算） =====
        night_start = config.get("night_start_time", "21:00")
        day_start = config.get("day_start_time", "09:00")

        night_h, night_m = map(int, night_start.split(":"))
        day_h, day_m = map(int, day_start.split(":"))

        current_date = current_time.date()
        current_decimal = current_time.hour + current_time.minute / 60
        night_decimal = night_h + night_m / 60
        day_decimal = day_h + day_m / 60

        # ===== 5. 判断是否是换班日（批量计算） =====
        is_handover, is_next_handover = await self._check_handover_days(
            chat_id, current_date, config
        )

        # ===== 6. 换班夜班判定 =====
        if is_handover and current_decimal >= night_decimal:
            result = await self._calculate_handover_night(
                current_time, current_date, night_h, night_m, config
            )
        elif is_next_handover and current_decimal < 15:
            result = await self._calculate_handover_night_cross(
                current_time, current_date, night_h, night_m, config
            )
        # ===== 7. 换班白班判定 =====
        elif is_next_handover and current_decimal >= 15:
            result = await self._calculate_handover_day(
                current_time, current_date, config
            )
        # ===== 8. 正常班次 =====
        else:
            result = await self._calculate_normal_period(
                current_time, current_date, night_decimal, day_decimal, config
            )

        # ===== 9. 缓存结果 =====
        if use_cache:
            await self._set_period_cache(cache_key, result)

        return result

    async def _check_handover_days(
        self, chat_id: int, current_date: date, config: dict
    ) -> tuple[bool, bool]:
        """批量判断换班日（使用缓存）"""
        cache_key = f"handover_check:{chat_id}:{current_date}"

        # 尝试从缓存获取
        cached = await self._get_handover_check_cache(cache_key)
        if cached:
            logger.debug(f"📊 换班日缓存命中: {cache_key}")
            return cached

        handover_day = config.get("handover_day", 31)
        handover_month = config.get("handover_month", 0)

        # 批量计算今天和明天
        is_handover = self._is_handover_date(current_date, handover_day, handover_month)
        is_next_handover = self._is_handover_date(
            current_date + timedelta(days=1), handover_day, handover_month
        )

        result = (is_handover, is_next_handover)

        # 缓存结果（24小时）
        await self._set_handover_check_cache(cache_key, result, ttl=86400)

        return result

    def _is_handover_date(
        self, check_date: date, handover_day: int, handover_month: int
    ) -> bool:
        """判断指定日期是否是换班日"""
        if handover_day == 0:  # 月末最后一天
            if check_date.month == 12:
                next_month = date(check_date.year + 1, 1, 1)
            else:
                next_month = date(check_date.year, check_date.month + 1, 1)
            last_day = (next_month - timedelta(days=1)).day
            return check_date.day == last_day
        else:
            if handover_month == 0:  # 每月
                return check_date.day == handover_day
            else:  # 指定月份
                return (
                    check_date.month == handover_month
                    and check_date.day == handover_day
                )

    async def _calculate_normal_period(
        self,
        current_time: datetime,
        current_date: date,
        night_decimal: float,
        day_decimal: float,
        config: dict,
    ) -> Dict[str, Any]:
        """计算正常班次"""
        from datetime import time as dt_time

        current_decimal = current_time.hour + current_time.minute / 60

        # 正常夜班
        if current_decimal >= night_decimal or current_decimal < day_decimal:
            if current_decimal >= night_decimal:
                business_date = current_date
                start_dt = datetime.combine(
                    current_date, self._time_from_decimal(night_decimal)
                ).replace(tzinfo=current_time.tzinfo)
            else:
                business_date = current_date - timedelta(days=1)
                start_dt = datetime.combine(
                    current_date - timedelta(days=1),
                    self._time_from_decimal(night_decimal),
                ).replace(tzinfo=current_time.tzinfo)

            elapsed_hours = (current_time - start_dt).total_seconds() / 3600
            next_reset = start_dt + timedelta(
                hours=config.get("normal_night_hours", 12)
            )

            return {
                "period_type": "normal_night",
                "business_date": business_date,
                "actual_date": current_date,
                "cycle": 1,
                "hours_elapsed": elapsed_hours,
                "total_hours": config.get("normal_night_hours", 12),
                "is_handover": False,
                "next_reset_time": next_reset,
            }

        # 正常白班
        start_dt = datetime.combine(
            current_date, self._time_from_decimal(day_decimal)
        ).replace(tzinfo=current_time.tzinfo)

        elapsed_hours = (current_time - start_dt).total_seconds() / 3600
        next_reset = start_dt + timedelta(hours=config.get("normal_day_hours", 12))

        return {
            "period_type": "normal_day",
            "business_date": current_date,
            "actual_date": current_date,
            "cycle": 1,
            "hours_elapsed": elapsed_hours,
            "total_hours": config.get("normal_day_hours", 12),
            "is_handover": False,
            "next_reset_time": next_reset,
        }

    async def _calculate_handover_night(
        self,
        current_time: datetime,
        current_date: date,
        night_h: int,
        night_m: int,
        config: dict,
    ) -> Dict[str, Any]:
        """计算换班夜班"""
        from datetime import time as dt_time

        start_dt = datetime.combine(current_date, dt_time(night_h, night_m)).replace(
            tzinfo=current_time.tzinfo
        )

        elapsed_hours = (current_time - start_dt).total_seconds() / 3600
        threshold = self._get_reset_threshold_hours(config)
        total_hours = config.get("handover_night_hours", 18)
        cycle = 1 if elapsed_hours < threshold else 2
        next_reset = (
            start_dt + timedelta(hours=threshold)
            if cycle == 1
            else start_dt + timedelta(hours=total_hours)
        )

        return {
            "period_type": "handover_night",
            "business_date": current_date,
            "actual_date": current_date,
            "cycle": cycle,
            "hours_elapsed": elapsed_hours,
            "total_hours": total_hours,
            "is_handover": True,
            "next_reset_time": next_reset,
            "period_start_time": start_dt,
            "reset_threshold_hours": threshold,
        }

    async def _calculate_handover_night_cross(
        self,
        current_time: datetime,
        current_date: date,
        night_h: int,
        night_m: int,
        config: dict,
    ) -> Dict[str, Any]:
        """计算跨天换班夜班"""
        from datetime import time as dt_time

        handover_date = current_date - timedelta(days=1)
        start_dt = datetime.combine(handover_date, dt_time(night_h, night_m)).replace(
            tzinfo=current_time.tzinfo
        )

        elapsed_hours = (current_time - start_dt).total_seconds() / 3600
        threshold = self._get_reset_threshold_hours(config)
        total_hours = config.get("handover_night_hours", 18)
        cycle = 1 if elapsed_hours < threshold else 2
        next_reset = (
            start_dt + timedelta(hours=threshold)
            if cycle == 1
            else start_dt + timedelta(hours=total_hours)
        )

        return {
            "period_type": "handover_night",
            "business_date": handover_date,
            "actual_date": current_date,
            "cycle": cycle,
            "hours_elapsed": elapsed_hours,
            "total_hours": total_hours,
            "is_handover": True,
            "next_reset_time": next_reset,
            "period_start_time": start_dt,
            "reset_threshold_hours": threshold,
        }

    async def _calculate_handover_day(
        self, current_time: datetime, current_date: date, config: dict
    ) -> Dict[str, Any]:
        """计算换班白班"""
        from datetime import time as dt_time

        start_dt = datetime.combine(current_date, dt_time(15, 0)).replace(
            tzinfo=current_time.tzinfo
        )

        elapsed_hours = (current_time - start_dt).total_seconds() / 3600
        threshold = self._get_reset_threshold_hours(config)
        total_hours = config.get("handover_day_hours", 18)
        cycle = 1 if elapsed_hours < threshold else 2
        next_reset = (
            start_dt + timedelta(hours=threshold)
            if cycle == 1
            else start_dt + timedelta(hours=total_hours)
        )

        return {
            "period_type": "handover_day",
            "business_date": current_date,
            "actual_date": current_date,
            "cycle": cycle,
            "hours_elapsed": elapsed_hours,
            "total_hours": total_hours,
            "is_handover": True,
            "next_reset_time": next_reset,
            "period_start_time": start_dt,
            "reset_threshold_hours": threshold,
        }

    def _time_from_decimal(self, decimal_hours: float):
        """将小数小时转换为时间对象"""
        from datetime import time as dt_time

        hours = int(decimal_hours)
        minutes = int((decimal_hours - hours) * 60)
        return dt_time(hours, minutes)

    async def _get_normal_period(
        self, chat_id: int, current_time: datetime, config: dict
    ) -> Dict[str, Any]:
        """获取正常班次时期"""
        night_start = config.get("night_start_time", "21:00")
        day_start = config.get("day_start_time", "09:00")

        night_h, night_m = map(int, night_start.split(":"))
        day_h, day_m = map(int, day_start.split(":"))

        current_date = current_time.date()
        night_decimal = night_h + night_m / 60
        day_decimal = day_h + day_m / 60

        return await self._calculate_normal_period(
            current_time, current_date, night_decimal, day_decimal, config
        )

    # ========== 缓存方法（原样保留）==========

    async def _get_period_cache(self, key: str):
        """获取周期缓存"""
        import time

        if hasattr(self, "_period_cache") and key in self._period_cache_ttl:
            if time.time() < self._period_cache_ttl[key]:
                return self._period_cache.get(key)
            else:
                # 清理过期缓存
                self._period_cache.pop(key, None)
                self._period_cache_ttl.pop(key, None)
        return None

    async def _set_period_cache(self, key: str, value: dict, ttl: int = 3600):
        """设置周期缓存（1小时）"""
        import time

        if not hasattr(self, "_period_cache"):
            self._period_cache = {}
            self._period_cache_ttl = {}

        self._period_cache[key] = value
        self._period_cache_ttl[key] = time.time() + ttl

    async def _get_handover_check_cache(self, key: str):
        """获取换班日检查缓存"""
        import time

        if hasattr(self, "_handover_cache") and key in self._handover_cache_ttl:
            if time.time() < self._handover_cache_ttl[key]:
                return self._handover_cache.get(key)
            else:
                # 清理过期缓存
                self._handover_cache.pop(key, None)
                self._handover_cache_ttl.pop(key, None)
        return None

    async def _set_handover_check_cache(self, key: str, value: tuple, ttl: int = 86400):
        """设置换班日检查缓存（24小时）"""
        import time

        if not hasattr(self, "_handover_cache"):
            self._handover_cache = {}
            self._handover_cache_ttl = {}

        self._handover_cache[key] = value
        self._handover_cache_ttl[key] = time.time() + ttl

    # ========== 用户周期管理（修改后的方法）==========
    async def get_user_cycle(
        self,
        chat_id: int,
        user_id: int,
        business_date: date,
        period_type: str,
        cycle: int,
    ) -> Optional[Dict[str, Any]]:
        """获取用户指定周期的数据（带缓存保护）"""

        # 定期清理过期缓存
        await self._cleanup_user_cycle_cache()

        cache_key = (
            f"user_cycle:{chat_id}:{user_id}:{business_date}:{period_type}:{cycle}"
        )

        # 尝试从缓存获取
        cached = await self._get_user_cycle_cached(cache_key)
        if cached:
            return cached

        try:
            shift_type = "night" if "night" in period_type else "day"

            row = await db.execute_with_retry(
                "获取用户周期",
                """
                SELECT * FROM user_handover_cycles 
                WHERE chat_id = $1 AND user_id = $2 
                  AND handover_date = $3 AND shift_type = $4 AND cycle_number = $5
                """,
                chat_id,
                user_id,
                business_date,
                shift_type,
                cycle,
                fetchrow=True,
            )

            if row:
                result = dict(row)
                await self._set_user_cycle_cached(cache_key, result)

                # ===== 在这里添加防御性代码 =====
                if cycle == 2:
                    # 检查周期1是否存在且是否真的结束了
                    cycle1_data = await self.get_user_cycle(
                        chat_id, user_id, business_date, period_type, 1
                    )
                    if cycle1_data:
                        total_work_seconds = cycle1_data.get("total_work_seconds", 0)
                        config = await self.get_handover_config(chat_id)
                        threshold = int(
                            self._get_reset_threshold_hours(config) * 3600
                        )
                        if total_work_seconds < threshold:
                            logger.warning(
                                f"⚠️ 周期2已存在但周期1未满阈值: "
                                f"用户={user_id}, 日期={business_date}, "
                                f"周期1累计={total_work_seconds//60}分钟"
                            )
                # ===== 防御性代码结束 =====

                return result
            return None

        except Exception as e:
            logger.error(f"获取用户周期失败 {chat_id}-{user_id}: {e}")
            return None

    async def create_user_cycle(
        self,
        chat_id: int,
        user_id: int,
        business_date: date,
        period_type: str,
        cycle: int,
        start_time: Optional[datetime] = None,
    ) -> bool:
        """创建用户新周期（自动使缓存失效）"""
        if start_time is None:
            start_time = db.get_beijing_time()

        # ===== 修复：转换为无时区时间 =====
        # 确保插入数据库的时间不带 tzinfo，防止 PostgreSQL 驱动进行不必要的时区转换
        if start_time.tzinfo is not None:
            # 转换为无时区时间（去除时区信息）
            start_time = start_time.replace(tzinfo=None)

        # 根据 period_type 判定白班或晚班
        shift_type = "night" if "night" in period_type else "day"

        try:
            # 使用带重试机制的执行器
            await db.execute_with_retry(
                "创建用户周期",
                """
                INSERT INTO user_handover_cycles 
                (chat_id, user_id, handover_date, shift_type, cycle_number, 
                 cycle_start_time, total_work_seconds)
                VALUES ($1, $2, $3, $4, $5, $6, 0)
                ON CONFLICT (chat_id, user_id, handover_date, shift_type, cycle_number) 
                DO NOTHING
                """,
                chat_id,
                user_id,
                business_date,
                shift_type,
                cycle,
                start_time,  # 传入已处理的无时区时间
            )

            # 联动操作：使该用户的周期相关缓存失效，确保下次查询时从数据库拉取最新状态
            await self._invalidate_user_cycle_cache(chat_id, user_id)
            return True

        except Exception as e:
            logger.error(f"❌ 创建用户周期失败 {chat_id}-{user_id}: {e}")
            return False

    async def update_user_cycle_time(
        self,
        chat_id: int,
        user_id: int,
        business_date: date,
        period_type: str,
        cycle: int,
        elapsed_seconds: int,
    ) -> Tuple[int, bool]:
        """
        更新用户周期工作时间（稳定版）

        返回: (新累计时间, 是否达到周期切换点)
        """

        cycle_data = await self.get_user_cycle(
            chat_id, user_id, business_date, period_type, cycle
        )

        if not cycle_data and cycle == 1:
            await self.create_user_cycle(
                chat_id, user_id, business_date, period_type, 1
            )
            cycle_data = await self.get_user_cycle(
                chat_id, user_id, business_date, period_type, 1
            )

        if not cycle_data and cycle == 2:
            await self.create_user_cycle(
                chat_id, user_id, business_date, period_type, 2
            )
            cycle_data = await self.get_user_cycle(
                chat_id, user_id, business_date, period_type, 2
            )

        if not cycle_data:
            logger.warning(f"用户 {user_id} 周期 {cycle} 不存在")
            return 0, False

        config = await self.get_handover_config(chat_id)
        current_total = cycle_data["total_work_seconds"]
        threshold_seconds = int(self._get_reset_threshold_hours(config) * 3600)

        # ===== 异常检测 =====
        # 检查单次增加是否过大
        if elapsed_seconds > 4 * 3600:  # 单次超过4小时？
            logger.warning(
                f"⚠️ 单次增加过大 user={user_id} "
                f"cycle={cycle} add={elapsed_seconds/3600:.2f}小时"
            )

        try:
            shift_type = "night" if "night" in period_type else "day"

            # ===== 原子更新（防并发覆盖）=====
            row = await db.execute_with_retry(
                "原子更新周期时间",
                """
                UPDATE user_handover_cycles
                SET total_work_seconds = total_work_seconds + $1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE chat_id = $2 AND user_id = $3
                  AND handover_date = $4 AND shift_type = $5
                  AND cycle_number = $6
                RETURNING total_work_seconds
                """,
                elapsed_seconds,
                chat_id,
                user_id,
                business_date,
                shift_type,
                cycle,
                fetchrow=True,
            )

            if not row:
                # 更新失败（可能记录被删？）
                logger.error(f"周期更新无返回 user={user_id} cycle={cycle}")
                return current_total, False

            new_total = row["total_work_seconds"]

            # ===== 最终异常检测 =====
            if new_total > 13 * 3600:
                logger.error(
                    f"❌ 周期累计异常 user={user_id} "
                    f"cycle={cycle} hours={new_total/3600:.2f}"
                )

            await self._invalidate_user_cycle_cache(chat_id, user_id)

            reached_threshold = (
                cycle == 1
                and current_total < threshold_seconds
                and new_total >= threshold_seconds
            )

            if reached_threshold:
                await self.create_user_cycle(
                    chat_id,
                    user_id,
                    business_date,
                    period_type,
                    2,
                    start_time=db.get_beijing_time(),
                )
                logger.info(
                    f"🔄 用户 {user_id} 进入换班第2段，活动次数将重置"
                )

            return new_total, reached_threshold

        except Exception as e:
            logger.error(f"更新用户周期失败 {chat_id}-{user_id}: {e}")
            return current_total, False

    def _activity_count_cache_key(
        self,
        chat_id: int,
        user_id: int,
        target_date: date,
        final_shift: Optional[str],
        activities: List[str],
    ) -> str:
        shift_part = final_shift or "*"
        act_part = ",".join(sorted(activities))
        return f"act_cnt:{chat_id}:{user_id}:{target_date}:{shift_part}:{act_part}"

    def invalidate_activity_count_cache(
        self, chat_id: int, user_id: Optional[int] = None
    ) -> None:
        """活动次数变更后清除短缓存"""
        prefix = f"act_cnt:{chat_id}:"
        if user_id is not None:
            prefix = f"act_cnt:{chat_id}:{user_id}:"
        keys = [
            key
            for key in self._activity_count_cache
            if key.startswith(prefix)
        ]
        for key in keys:
            self._activity_count_cache.pop(key, None)
            self._activity_count_cache_ttl.pop(key, None)

    def _get_activity_count_cached(
        self, cache_key: str
    ) -> Optional[Union[int, Dict[str, int]]]:
        expiry = self._activity_count_cache_ttl.get(cache_key)
        if expiry and time.time() < expiry:
            return self._activity_count_cache.get(cache_key)
        self._activity_count_cache.pop(cache_key, None)
        self._activity_count_cache_ttl.pop(cache_key, None)
        return None

    def _set_activity_count_cached(
        self, cache_key: str, value: Union[int, Dict[str, int]]
    ) -> None:
        self._activity_count_cache[cache_key] = value
        self._activity_count_cache_ttl[cache_key] = (
            time.time() + self._activity_count_cache_ttl_seconds
        )

    async def _apply_handover_count_reset(
        self,
        chat_id: int,
        user_id: int,
        period: Dict[str, Any],
        target_date: date,
        activities: List[str],
        result: Dict[str, int],
    ) -> None:
        if not period.get("is_handover", False):
            logger.debug(f"📊 [计数] 正常日: {result}")
            return

        effective_cycle = await self.get_user_effective_cycle(
            chat_id, user_id, period
        )
        if effective_cycle >= 2:
            logger.debug(
                f"🔄 [计数-周期2] 业务日期 {target_date}, "
                f"用户={user_id}, 计数重置"
            )
            for act_name in activities:
                result[act_name] = 0
        else:
            logger.debug(
                f"🔄 [计数-周期1] 业务日期 {target_date}, 计数: {result}"
            )

    # ========== 对外核心接口（原样保留）==========
    async def get_activity_count(
        self,
        chat_id: int,
        user_id: int,
        activity: Union[str, List[str]],
        shift: Optional[str] = None,
        query_date: Optional[date] = None,
        current_time: Optional[datetime] = None,
        period: Optional[Dict[str, Any]] = None,
    ) -> Union[int, Dict[str, int]]:

        if current_time is None:
            current_time = db.get_beijing_time()

        # ===== 1. 参数处理 =====
        if isinstance(activity, str):
            activities = [activity]
            single_mode = True
        elif isinstance(activity, list) and all(isinstance(a, str) for a in activity):
            activities = activity
            single_mode = False
        else:
            raise TypeError("activity 必须是 str 或 List[str]")

        if shift is not None and not isinstance(shift, str):
            raise TypeError("shift 必须是字符串或 None")
        if query_date is not None:
            if isinstance(query_date, datetime):
                # datetime 是 date 的子类，需单独处理以免跳过换班周期逻辑
                if current_time is None:
                    current_time = query_date
                query_date = None
            elif not isinstance(query_date, date):
                raise TypeError("query_date 必须是 date 类型或 None")

        # ===== 2. 获取业务日期（period 最多只算一次）=====
        if query_date:
            target_date = query_date
            logger.debug(f"📅 使用传入查询日期: {target_date}")
        else:
            if period is None:
                period = await self.determine_current_period(chat_id, current_time)
            target_date = period["business_date"]
            logger.debug(
                f"📅 使用换班业务日期: {target_date}, "
                f"period_type={period.get('period_type')}, "
                f"is_handover={period.get('is_handover')}"
            )

        # ===== 3. 规范化班次 =====
        final_shift = None
        if shift:
            shift_clean = shift.strip().lower()
            if shift_clean.startswith("night"):
                final_shift = "night"
            elif shift_clean == "day":
                final_shift = "day"

        cache_key = self._activity_count_cache_key(
            chat_id, user_id, target_date, final_shift, activities
        )
        cached = self._get_activity_count_cached(cache_key)
        if cached is not None:
            logger.debug(f"📊 活动次数缓存命中: {cache_key}")
            if single_mode:
                return cached if isinstance(cached, int) else cached[activities[0]]
            return cached

        # ===== 4. 查询数据库 =====
        result = {act_name: 0 for act_name in activities}
        if activities:
            try:
                if single_mode:
                    params = [chat_id, user_id, target_date, activities[0]]
                    query_sql = """
                        SELECT COALESCE(SUM(activity_count), 0)
                        FROM user_activities
                        WHERE chat_id = $1
                          AND user_id = $2
                          AND activity_date = $3
                          AND activity_name = $4
                    """
                    if final_shift:
                        query_sql += " AND shift = $5"
                        params.append(final_shift)

                    count = await db.execute_with_retry(
                        "获取活动次数(1个)",
                        query_sql,
                        *params,
                        fetchval=True,
                        slow_threshold=0.5,
                    )
                    result[activities[0]] = int(count or 0)
                else:
                    params = [chat_id, user_id, target_date, activities]
                    query_sql = """
                        SELECT activity_name, SUM(activity_count) AS total_count
                        FROM user_activities
                        WHERE chat_id = $1
                          AND user_id = $2
                          AND activity_date = $3
                          AND activity_name = ANY($4::text[])
                    """
                    if final_shift:
                        query_sql += " AND shift = $5"
                        params.append(final_shift)
                    query_sql += " GROUP BY activity_name"

                    rows = await db.execute_with_retry(
                        f"获取活动次数({len(activities)}个)",
                        query_sql,
                        *params,
                        fetch=True,
                        slow_threshold=0.5,
                    )

                    for row in rows:
                        result[row["activity_name"]] = row["total_count"] or 0

            except Exception as e:
                logger.error(f"❌ 获取活动次数失败: {e}")

        # ===== 5. 换班日周期处理 =====
        if period is None:
            period = await self.determine_current_period(chat_id, current_time)

        await self._apply_handover_count_reset(
            chat_id, user_id, period, target_date, activities, result
        )

        if single_mode:
            final_value = result[activities[0]]
            self._set_activity_count_cached(cache_key, final_value)
            return final_value

        self._set_activity_count_cached(cache_key, dict(result))
        return result

    async def record_activity(
        self,
        chat_id: int,
        user_id: int,
        activity: str,
        elapsed_seconds: int,
        current_time: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """
        记录一次活动完成

        返回: {
            'business_date': date,      # 应该使用的业务日期
            'cycle': int,                # 当前周期
            'should_reset_count': bool,  # 是否应该重置计数（达到12小时）
            'period_type': str           # 时期类型
        }
        """
        if current_time is None:
            current_time = db.get_beijing_time()

        self.invalidate_activity_count_cache(chat_id, user_id)

        # 获取当前时期信息
        period = await self.determine_current_period(chat_id, current_time)

        result = {
            "business_date": period["business_date"],
            "cycle": period["cycle"],
            "should_reset_count": False,
            "period_type": period["period_type"],
            "is_handover": period["is_handover"],
        }

        if not period["is_handover"]:
            return result

        effective_cycle = await self.get_user_effective_cycle(
            chat_id, user_id, period
        )
        result["cycle"] = effective_cycle

        # 换班日，更新周期时间（写入对应周期累计）
        new_total, reached_threshold = await self.update_user_cycle_time(
            chat_id,
            user_id,
            period["business_date"],
            period["period_type"],
            effective_cycle,
            elapsed_seconds,
        )

        if reached_threshold:
            effective_cycle = 2
            result["cycle"] = 2

        result["should_reset_count"] = reached_threshold or effective_cycle >= 2

        logger.info(
            f"📝 [换班记录] 用户{user_id} {period['period_type']} "
            f"周期{period['cycle']} 累计 {new_total//60}分钟, "
            f"阈值达到: {reached_threshold}"
        )

        return result

    # ========== 统一业务日期 API ==========

    async def get_business_date(
        self,
        chat_id: int,
        current_time: Optional[datetime] = None,
    ) -> date:
        """统一业务日期入口（基于换班周期判定）"""
        period = await self.determine_current_period(chat_id, current_time)
        return period["business_date"]

    async def get_business_date_range(
        self,
        chat_id: int,
        current_time: Optional[datetime] = None,
    ) -> Dict[str, date]:
        """统一业务日期范围"""
        if current_time is None:
            current_time = db.get_beijing_time()

        business_today = await self.get_business_date(chat_id, current_time)
        return {
            "business_today": business_today,
            "business_yesterday": business_today - timedelta(days=1),
            "business_day_before": business_today - timedelta(days=2),
            "natural_today": current_time.date(),
        }

    async def get_period_info(
        self,
        chat_id: int,
        current_time: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """获取当前换班周期完整信息"""
        return await self.determine_current_period(chat_id, current_time)

    # ========== 缓存管理（原样保留）==========

    def _get_cached(self, key: str):
        import time

        if key in self._cache_ttl and time.time() < self._cache_ttl[key]:
            return self._cache.get(key)
        return None

    def _set_cached(self, key: str, value: Any, ttl: int = 300):
        import time

        self._cache[key] = value
        self._cache_ttl[key] = time.time() + ttl

    def _invalidate_cache(self, chat_id: int):
        cache_key = f"handover_config:{chat_id}"
        self._cache.pop(cache_key, None)
        self._cache_ttl.pop(cache_key, None)

    async def update_handover_config(self, chat_id: int, **kwargs) -> bool:
        """
        更新换班配置（带参数验证和事务支持）

        参数:
            chat_id: 群组ID
            **kwargs: 要更新的字段

        支持的字段:
            - handover_enabled: bool
            - night_start_time: str (HH:MM)
            - day_start_time: str (HH:MM)
            - handover_night_hours: int (1-24)
            - handover_day_hours: int (1-24)
            - normal_night_hours: int (1-24)
            - normal_day_hours: int (1-24)
            - handover_day: int (1-31, 0表示月末)
            - handover_month: int (1-12, 0表示每月)

        返回:
            bool: 是否更新成功
        """
        try:
            # ===== 1. 参数验证 =====
            if not kwargs:
                logger.warning(f"没有提供要更新的参数: {chat_id}")
                return False

            # 定义允许的字段及其验证规则
            allowed_fields = {
                "handover_enabled": lambda v: isinstance(v, bool),
                "night_start_time": lambda v: self._validate_time_format(v),
                "day_start_time": lambda v: self._validate_time_format(v),
                "handover_night_hours": lambda v: isinstance(v, int) and 1 <= v <= 24,
                "handover_day_hours": lambda v: isinstance(v, int) and 1 <= v <= 24,
                "normal_night_hours": lambda v: isinstance(v, int) and 1 <= v <= 24,
                "normal_day_hours": lambda v: isinstance(v, int) and 1 <= v <= 24,
                "handover_day": lambda v: isinstance(v, int) and 0 <= v <= 31,
                "handover_month": lambda v: isinstance(v, int) and 0 <= v <= 12,
            }

            # 验证并过滤参数
            valid_updates = {}
            for key, value in kwargs.items():
                if key not in allowed_fields:
                    logger.warning(f"忽略未知字段: {key}")
                    continue

                if not allowed_fields[key](value):
                    logger.error(f"字段 {key} 的值无效: {value}")
                    return False

                valid_updates[key] = value

            if not valid_updates:
                logger.warning(f"没有有效的更新参数: {chat_id}")
                return False

            # ===== 2. 构建更新语句 =====
            set_clauses = []
            values = []
            param_index = 2  # chat_id 是 $1

            for key, value in valid_updates.items():
                set_clauses.append(f"{key} = ${param_index}")
                values.append(value)
                param_index += 1

            # ===== 3. 执行更新（带事务）=====
            updated = 0
            async with db.pool.acquire() as conn:
                async with conn.transaction():
                    # 先检查记录是否存在
                    exists = await conn.fetchval(
                        """
                        SELECT 1 FROM shift_handover_configs 
                        WHERE chat_id = $1
                    """,
                        chat_id,
                    )

                    if not exists:
                        # 如果不存在，先插入默认配置
                        await conn.execute(
                            """
                            INSERT INTO shift_handover_configs (chat_id)
                            VALUES ($1)
                            ON CONFLICT (chat_id) DO NOTHING
                        """,
                            chat_id,
                        )

                    # 执行更新
                    query = f"""
                        UPDATE shift_handover_configs 
                        SET {', '.join(set_clauses)}, updated_at = CURRENT_TIMESTAMP
                        WHERE chat_id = $1
                    """

                    result = await conn.execute(query, chat_id, *values)

                    # 解析更新结果
                    if result and result.startswith("UPDATE"):
                        try:
                            updated = int(result.split()[-1])
                        except (ValueError, IndexError):
                            pass

            # ===== 4. 清除缓存 =====
            if updated > 0:
                self._invalidate_cache(chat_id)
                logger.info(
                    f"✅ 更新换班配置成功: {chat_id}, 字段: {list(valid_updates.keys())}"
                )
                return True
            else:
                logger.warning(f"⚠️ 没有更新任何配置: {chat_id}")
                return False

        except Exception as e:
            logger.error(f"❌ 更新换班配置失败 {chat_id}: {e}")
            return False

    # ========== 辅助方法 ==========
    def _validate_time_format(self, time_str: str) -> bool:
        """验证时间格式 HH:MM"""
        try:
            if not isinstance(time_str, str):
                return False
            datetime.strptime(time_str, "%H:%M")
            return True
        except ValueError:
            return False


# 全局实例
handover_manager = HandoverManager()
