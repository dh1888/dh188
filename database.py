import logging
import asyncio
import time as system_time
import json
from datetime import datetime, timedelta, date
from datetime import time as dt_time
from typing import Dict, Any, List, Optional, Union
from config import Config, beijing_tz
import asyncpg
from asyncpg.pool import Pool

logger = logging.getLogger("GroupCheckInBot")


class PostgreSQLDatabase:
    """PostgreSQL数据库管理器"""

    def __init__(self, database_url: str = None):
        self.database_url = database_url or Config.DATABASE_URL
        self.pool: Optional[Pool] = None
        self._initialized = False
        self._cache = {}
        self._cache_ttl = {}

        # 重连相关属性
        self._last_connection_check = 0
        self._connection_check_interval = 30
        self._reconnect_attempts = 0
        self._max_reconnect_attempts = 5
        self._reconnect_base_delay = 1.0

        self._maintenance_running = False
        self._maintenance_task = None
        self._connection_maintenance_task = None

        self._cache_max_size = 1000
        self._cache_access_order = []

    # ========== 重连机制 ==========
    async def _ensure_healthy_connection(self):
        """确保连接健康"""
        current_time = system_time.time()
        if current_time - self._last_connection_check < self._connection_check_interval:
            return True

        try:
            is_healthy = await self.connection_health_check()
            if not is_healthy:
                logger.warning("数据库连接不健康，尝试重连...")
                await self._reconnect()

            self._last_connection_check = current_time
            return True
        except Exception as e:
            logger.error(f"数据库连接检查失败: {e}")
            return False

    async def _reconnect(self):
        """重新建立数据库连接"""
        self._reconnect_attempts += 1

        if self._reconnect_attempts > self._max_reconnect_attempts:
            logger.error(
                f"数据库重连尝试次数超过上限 ({self._max_reconnect_attempts})，停止重连"
            )
            raise ConnectionError("数据库重连失败")

        try:
            delay = self._reconnect_base_delay * (
                2 ** (self._reconnect_attempts - 1)
            )  # 指数退避
            logger.info(f"{delay}秒后尝试第{self._reconnect_attempts}次数据库重连...")
            await asyncio.sleep(delay)

            # 关闭旧连接
            if self.pool:
                await self.pool.close()

            # 重新初始化
            self.pool = None
            self._initialized = False
            await self._initialize_impl()

            # 重置重连计数
            self._reconnect_attempts = 0
            logger.info("✅ 数据库重连成功")

        except Exception as e:
            logger.error(f"数据库第{self._reconnect_attempts}次重连失败: {e}")
            if self._reconnect_attempts >= self._max_reconnect_attempts:
                logger.critical("数据库重连最终失败，服务可能无法正常工作")
            raise

    async def execute_with_retry(
        self,
        operation_name: str,
        query: str,
        *args,
        fetch: bool = False,
        fetchrow: bool = False,
        fetchval: bool = False,  # 🆕 新增 fetchval 支持
        max_retries: int = 2,
        timeout: int = 30,
        slow_threshold: float = 1.0,  # 🆕 可配置慢查询阈值
    ):
        """带重试和超时的查询执行 - 终极优化版"""
        if not await self._ensure_healthy_connection():
            raise ConnectionError("数据库连接不健康")

        # 🆕 验证参数组合
        if sum([fetch, fetchrow, fetchval]) > 1:
            raise ValueError("只能指定一种查询类型: fetch, fetchrow 或 fetchval")

        for attempt in range(max_retries + 1):
            start_time = system_time.time()
            try:
                async with self.pool.acquire() as conn:
                    await conn.execute(f"SET statement_timeout = {timeout * 1000}")

                    if fetch:
                        result = await conn.fetch(query, *args)
                    elif fetchrow:
                        result = await conn.fetchrow(query, *args)
                    elif fetchval:
                        result = await conn.fetchval(query, *args)
                    else:
                        result = await conn.execute(query, *args)

                    execution_time = system_time.time() - start_time

                    # 🆕 动态慢查询日志
                    if execution_time > slow_threshold:
                        log_level = (
                            logging.WARNING if execution_time < 5.0 else logging.ERROR
                        )
                        logger.log(
                            log_level,
                            f"⏱️ 慢查询: {operation_name} 耗时 {execution_time:.3f}秒 "
                            f"(SQL: {query[:100]}{'...' if len(query) > 100 else ''})",
                        )

                    return result

            except (
                asyncpg.PostgresConnectionError,
                asyncpg.ConnectionDoesNotExistError,
                asyncpg.InterfaceError,
                ConnectionError,
            ) as e:
                if attempt == max_retries:
                    logger.error(
                        f"{operation_name} 数据库重试{max_retries}次后失败: {e}"
                    )
                    raise

                retry_delay = min(1 * (2**attempt), 5)
                logger.warning(
                    f"{operation_name} 数据库连接异常，{retry_delay}秒后第{attempt + 1}次重试: {e}"
                )
                await self._reconnect()
                await asyncio.sleep(retry_delay)

            except asyncpg.QueryCanceledError:
                logger.error(f"{operation_name} 查询超时被取消 (超时设置: {timeout}秒)")
                if attempt == max_retries:
                    raise
                await asyncio.sleep(1)

            except Exception as e:
                # 🆕 区分数据库错误和其他错误
                if "database" in str(e).lower() or "sql" in str(e).lower():
                    logger.error(f"{operation_name} 数据库操作失败: {e}")
                else:
                    logger.error(f"{operation_name} 操作失败: {e}")
                raise

    # ========== 定期维护任务 ==========
    async def start_connection_maintenance(self):
        """启动连接维护任务"""
        if hasattr(self, "_maintenance_running") and self._maintenance_running:
            logger.info("连接维护任务已在运行")
            return

        self._maintenance_running = True
        self._maintenance_task = asyncio.create_task(
            self._connection_maintenance_loop()
        )
        logger.info("✅ 数据库连接维护任务已启动")

    async def stop_connection_maintenance(self):
        """停止连接维护任务"""
        self._maintenance_running = False
        if hasattr(self, "_maintenance_task") and self._maintenance_task:
            self._maintenance_task.cancel()
            try:
                await self._maintenance_task
            except asyncio.CancelledError:
                pass
            self._maintenance_task = None
        logger.info("数据库连接维护任务已停止")

    async def _connection_maintenance_loop(self):
        """连接维护循环"""
        logger.info("开始数据库连接维护循环...")

        while self._maintenance_running:
            try:
                await asyncio.sleep(60)  # 每分钟检查一次

                # 执行连接健康检查
                if not await self._ensure_healthy_connection():
                    logger.warning("连接维护: 数据库连接不健康")

                # 清理过期缓存
                await self.cleanup_cache()

                # 定期清理月度数据（可选）
                current_time = system_time.time()
                if current_time % 3600 < 60:  # 每小时执行一次
                    try:
                        await self.cleanup_old_data(days=Config.DATA_RETENTION_DAYS)
                        logger.debug("定期数据清理完成")
                    except Exception as e:
                        logger.error(f"定期数据清理失败: {e}")

            except asyncio.CancelledError:
                logger.info("数据库连接维护任务被取消")
                break
            except Exception as e:
                logger.error(f"连接维护任务异常: {e}")
                await asyncio.sleep(30)  # 异常后等待30秒再继续

    # ========== 时区相关方法 ==========
    def get_beijing_time(self):
        """获取北京时间"""
        return datetime.now(beijing_tz)

    def get_beijing_date(self):
        """获取北京日期"""
        return self.get_beijing_time().date()

    #  """判断群组是否启用双班模式"""
    async def is_dual_mode_enabled(self, chat_id: int) -> bool:
        group_data = await self.get_group_cached(chat_id)
        return bool(group_data and group_data.get("dual_mode", False))

    async def get_business_date_range(
        self, chat_id: int, current_dt: datetime = None
    ) -> Dict[str, date]:
        """
        获取业务日期范围（今天、昨天、前天）
        用于重置时统一使用业务日期
        """
        if current_dt is None:
            current_dt = self.get_beijing_time()

        business_today = await self.get_business_date(chat_id, current_dt)
        business_yesterday = business_today - timedelta(days=1)
        business_day_before = business_today - timedelta(days=2)

        # 自然日期仅用于日志
        natural_today = current_dt.date()

        logger.debug(
            f"📅 业务日期范围:\n"
            f"   • 自然今天: {natural_today}\n"
            f"   • 业务今天: {business_today}\n"
            f"   • 业务昨天: {business_yesterday}"
        )

        return {
            "business_today": business_today,
            "business_yesterday": business_yesterday,
            "business_day_before": business_day_before,
            "natural_today": natural_today,
        }

    # ========== 初始化方法 ==========
    async def initialize(self):
        """初始化数据库"""
        if self._initialized:
            return

        max_retries = 5
        for attempt in range(max_retries):
            try:
                logger.info(f"连接PostgreSQL数据库 (尝试 {attempt + 1}/{max_retries})")
                await self._initialize_impl()
                logger.info("PostgreSQL数据库初始化完成")
                self._initialized = True
                return
            except Exception as e:
                logger.warning(f"数据库初始化第 {attempt + 1} 次失败: {e}")
                if attempt == max_retries - 1:
                    logger.error(f"数据库初始化重试{max_retries}次后失败: {e}")
                    # 尝试强制重建表
                    try:
                        await self._force_recreate_tables()
                        self._initialized = True
                        logger.info("✅ 数据库表强制重建成功")
                        return
                    except Exception as rebuild_error:
                        logger.error(f"数据库表强制重建失败: {rebuild_error}")
                        raise e
                await asyncio.sleep(2**attempt)

    async def _initialize_impl(self):
        """实际的数据库初始化实现"""
        self.pool = await asyncpg.create_pool(
            self.database_url,
            min_size=Config.DB_MIN_CONNECTIONS,
            max_size=Config.DB_MAX_CONNECTIONS,
            max_inactive_connection_lifetime=Config.DB_POOL_RECYCLE,
            command_timeout=Config.DB_CONNECTION_TIMEOUT,
            timeout=60,
        )
        logger.info("PostgreSQL连接池创建成功")

        async with self.pool.acquire() as conn:
            await conn.execute("SET statement_timeout = 30000")
            await conn.execute("SET idle_in_transaction_session_timeout = 60000")

        # 创建表和索引 - 添加重试机制
        max_retries = 3
        for attempt in range(max_retries):
            try:
                await self._create_tables()
                await self._create_indexes()
                await self._initialize_default_data()
                logger.info("✅ 数据库表初始化完成")
                break
            except Exception as e:
                logger.warning(f"数据库表初始化第 {attempt + 1} 次失败: {e}")
                if attempt == max_retries - 1:
                    logger.error("数据库表初始化最终失败，尝试强制重建...")
                    await self._force_recreate_tables()
                await asyncio.sleep(1)

    async def _force_recreate_tables(self):
        """强制重新创建所有表（用于修复损坏的数据库）"""
        logger.warning("🔄 强制重新创建数据库表...")

        async with self.pool.acquire() as conn:
            # 删除所有表（按依赖顺序）
            tables = [
                "monthly_statistics",
                "activity_user_limits",
                "push_settings",
                "work_fine_configs",
                "fine_configs",
                "activity_configs",
                "work_records",
                "user_activities",
                "users",
                "groups",
            ]

            for table in tables:
                try:
                    await conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
                    logger.info(f"✅ 删除表: {table}")
                except Exception as e:
                    logger.warning(f"删除表 {table} 失败: {e}")

            # 重新创建表
            await self._create_tables()
            await self._create_indexes()
            await self._initialize_default_data()
            logger.info("🎉 数据库表强制重建完成")

    def _extract_table_name(self, table_sql: str) -> str:
        """安全提取表名"""
        try:
            # 使用更稳定的方式提取表名
            words = table_sql.upper().split()
            if "TABLE" in words:
                table_index = words.index("TABLE") + 1
                if table_index < len(words) and words[table_index] == "IF":
                    table_index += 3  # 跳过 IF NOT EXISTS
                elif table_index < len(words) and words[table_index] == "NOT":
                    table_index += 2  # 跳过 NOT EXISTS
                return words[table_index] if table_index < len(words) else "unknown"
        except Exception:
            pass
        return "unknown"

    async def _create_tables(self):
        """创建所有必要的表"""
        async with self.pool.acquire() as conn:
            tables = [
                # 1. groups表 (已集成 dual_mode 等新字段)
                """
                CREATE TABLE IF NOT EXISTS groups (
                    chat_id BIGINT PRIMARY KEY,
                    channel_id BIGINT,
                    notification_group_id BIGINT,
                    extra_work_notification_group BIGINT,
                    reset_hour INTEGER DEFAULT 0,
                    reset_minute INTEGER DEFAULT 0,
                    soft_reset_hour INTEGER DEFAULT 0,
                    soft_reset_minute INTEGER DEFAULT 0,
                    work_start_time TEXT DEFAULT '09:00',
                    work_end_time TEXT DEFAULT '18:00',
                    dual_mode BOOLEAN DEFAULT FALSE,
                    dual_day_start TEXT,
                    dual_day_end TEXT,
                    shift_grace_before INTEGER DEFAULT 120,
                    shift_grace_after INTEGER DEFAULT 360,
                    workend_grace_before INTEGER DEFAULT 120,
                    workend_grace_after INTEGER DEFAULT 360,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """,
                # 2. users表
                """
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    chat_id BIGINT NOT NULL,
                    user_id BIGINT NOT NULL,
                    nickname TEXT,
                    current_activity TEXT,
                    activity_start_time TEXT,
                    shift TEXT DEFAULT 'day',
                    checkin_message_id BIGINT DEFAULT NULL,
                    total_accumulated_time INTEGER DEFAULT 0,
                    total_activity_count INTEGER DEFAULT 0,
                    total_fines INTEGER DEFAULT 0,
                    overtime_count INTEGER DEFAULT 0,
                    total_overtime_time INTEGER DEFAULT 0,
                    last_updated DATE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(chat_id, user_id)
                )
                """,
                # 3. user_activities表
                """
                CREATE TABLE IF NOT EXISTS user_activities (
                    id SERIAL PRIMARY KEY,
                    chat_id BIGINT,
                    user_id BIGINT,
                    activity_date DATE,
                    activity_name TEXT,
                    activity_count INTEGER DEFAULT 0,
                    accumulated_time INTEGER DEFAULT 0,
                    shift TEXT DEFAULT 'day',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(chat_id, user_id, activity_date, activity_name, shift)
                )
                """,
                # 4. work_records表
                """
                CREATE TABLE IF NOT EXISTS work_records (
                    id SERIAL PRIMARY KEY,
                    chat_id BIGINT,
                    user_id BIGINT,
                    record_date DATE,
                    checkin_type TEXT,
                    checkin_time TEXT,
                    status TEXT,
                    time_diff_minutes REAL,
                    fine_amount INTEGER DEFAULT 0,
                    shift TEXT DEFAULT 'day',
                    shift_detail TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(chat_id, user_id, record_date, checkin_type, shift)
                )
                """,
                # 5. activity_configs表
                """
                CREATE TABLE IF NOT EXISTS activity_configs (
                    activity_name TEXT PRIMARY KEY,
                    max_times INTEGER,
                    time_limit INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """,
                # 6. fine_configs表
                """
                CREATE TABLE IF NOT EXISTS fine_configs (
                    id SERIAL PRIMARY KEY,
                    activity_name TEXT,
                    time_segment TEXT,
                    fine_amount INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(activity_name, time_segment)
                )
                """,
                # 7. work_fine_configs表
                """
                CREATE TABLE IF NOT EXISTS work_fine_configs (
                    id SERIAL PRIMARY KEY,
                    checkin_type TEXT,
                    time_segment TEXT,
                    fine_amount INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(checkin_type, time_segment)
                )
                """,
                # 8. push_settings表
                """
                CREATE TABLE IF NOT EXISTS push_settings (
                    setting_key TEXT PRIMARY KEY,
                    setting_value INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """,
                # 9. monthly_statistics表
                """
                CREATE TABLE IF NOT EXISTS monthly_statistics (
                    id SERIAL PRIMARY KEY,
                    chat_id BIGINT,
                    user_id BIGINT,
                    statistic_date DATE,
                    activity_name TEXT,
                    activity_count INTEGER DEFAULT 0,
                    accumulated_time INTEGER DEFAULT 0,
                    shift TEXT DEFAULT 'day',
                    work_days INTEGER DEFAULT 0,
                    work_hours INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(chat_id, user_id, statistic_date, activity_name, shift)
                )
                """,
                # 10. activity_user_limits表
                """
                CREATE TABLE IF NOT EXISTS activity_user_limits (
                    activity_name TEXT PRIMARY KEY,
                    max_users INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """,
                # 11. daily_statistics表
                """
                CREATE TABLE IF NOT EXISTS daily_statistics(
                    id SERIAL PRIMARY KEY,
                    chat_id BIGINT,
                    user_id BIGINT,
                    record_date DATE,
                    activity_name TEXT,
                    activity_count INTEGER DEFAULT 0,
                    accumulated_time INTEGER DEFAULT 0,
                    fine_amount INTEGER DEFAULT 0,
                    overtime_count INTEGER DEFAULT 0,
                    overtime_time INTEGER DEFAULT 0,
                    work_days INTEGER DEFAULT 0,
                    work_hours INTEGER DEFAULT 0,
                    shift TEXT DEFAULT 'day',
                    is_soft_reset BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(chat_id, user_id, record_date, activity_name, is_soft_reset, shift)
                )
                """,
                # 12. group_shift_state表 (新增表)
                """
                CREATE TABLE IF NOT EXISTS group_shift_state (
                    chat_id BIGINT NOT NULL,
                    user_id BIGINT NOT NULL,
                    shift TEXT NOT NULL,
                    record_date DATE NOT NULL,
                    shift_start_time TIMESTAMP WITH TIME ZONE NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (chat_id, user_id, shift)
                )


                """,
            ]

            for table_sql in tables:
                try:
                    await conn.execute(table_sql)
                    table_name = self._extract_table_name(table_sql)
                    logger.info(f"✅ 创建表: {table_name}")
                except Exception as e:
                    logger.error(f"❌ 创建表失败: {e}")
                    logger.error(f"失败的SQL: {table_sql[:100]}...")
                    raise

            logger.info("🚀 数据库所有表及字段初始化完成")

    async def _create_indexes(self):
        """优化的索引方案"""
        async with self.pool.acquire() as conn:
            indexes = [
                # ========== 核心业务索引 ==========
                "CREATE INDEX IF NOT EXISTS idx_users_primary ON users (chat_id, user_id)",
                "CREATE INDEX IF NOT EXISTS idx_users_current_activity ON users (chat_id, current_activity) WHERE current_activity IS NOT NULL",
                "CREATE INDEX IF NOT EXISTS idx_users_checkin_message ON users (chat_id, checkin_message_id) WHERE checkin_message_id IS NOT NULL",
                # ========== 活动记录索引 ==========
                "CREATE INDEX IF NOT EXISTS idx_user_activities_main ON user_activities (chat_id, user_id, activity_date, shift)",
                "CREATE INDEX IF NOT EXISTS idx_user_activities_cleanup ON user_activities (chat_id, created_at)",
                # ========== 工作记录索引 ==========
                "CREATE INDEX IF NOT EXISTS idx_work_records_main ON work_records (chat_id, user_id, record_date, shift)",
                "CREATE INDEX IF NOT EXISTS idx_work_records_night ON work_records (chat_id, user_id, shift, created_at)",
                "CREATE INDEX IF NOT EXISTS idx_work_records_cleanup ON work_records (chat_id, created_at)",
                # ========== 统计表索引 ==========
                "CREATE INDEX IF NOT EXISTS idx_daily_stats_main ON daily_statistics (chat_id, record_date, user_id, shift)",
                "CREATE INDEX IF NOT EXISTS idx_monthly_stats_main ON monthly_statistics (chat_id, statistic_date, user_id, shift)",
                # ========== 配置表索引 ==========
                "CREATE INDEX IF NOT EXISTS idx_groups_config ON groups (chat_id, dual_mode, reset_hour, reset_minute)",
                "CREATE INDEX IF NOT EXISTS idx_fine_configs_lookup ON fine_configs (activity_name, time_segment)",
                # ========== 班次状态索引 ==========
                "CREATE INDEX IF NOT EXISTS idx_shift_state ON group_shift_state (chat_id, current_shift, shift_start_time)",
            ]

            created_count = 0
            for index_sql in indexes:
                try:
                    await conn.execute(index_sql)
                    created_count += 1
                    logger.debug(
                        f"✅ 创建索引: {index_sql.split()[5] if len(index_sql.split()) > 5 else '索引'}"
                    )
                except Exception as e:
                    logger.warning(f"创建索引失败: {e}")

            logger.info(f"数据库索引优化完成，共 {created_count} 个索引")

    async def _initialize_default_data(self):
        """初始化默认数据"""
        async with self.pool.acquire() as conn:
            # 初始化活动配置
            for activity, limits in Config.DEFAULT_ACTIVITY_LIMITS.items():
                await conn.execute(
                    "INSERT INTO activity_configs (activity_name, max_times, time_limit) VALUES ($1, $2, $3) ON CONFLICT (activity_name) DO NOTHING",
                    activity,
                    limits["max_times"],
                    limits["time_limit"],
                )
                logger.info(f"✅ 初始化活动配置: {activity}")

            # 初始化罚款配置
            for activity, fines in Config.DEFAULT_FINE_RATES.items():
                for time_segment, amount in fines.items():
                    await conn.execute(
                        "INSERT INTO fine_configs (activity_name, time_segment, fine_amount) VALUES ($1, $2, $3) ON CONFLICT (activity_name, time_segment) DO NOTHING",
                        activity,
                        time_segment,
                        amount,
                    )
                logger.info(f"✅ 初始化罚款配置: {activity}")

            # 初始化推送设置
            for key, value in Config.AUTO_EXPORT_SETTINGS.items():
                await conn.execute(
                    "INSERT INTO push_settings (setting_key, setting_value) VALUES ($1, $2) ON CONFLICT (setting_key) DO NOTHING",
                    key,
                    1 if value else 0,
                )
                logger.info(f"✅ 初始化推送设置: {key}")

            logger.info("默认数据初始化完成")

    async def health_check(self) -> bool:
        """完整的数据库健康检查 - 增强版"""
        if not self.pool or not self._initialized:
            logger.warning("数据库未初始化")
            return False

        try:
            async with self.pool.acquire() as conn:
                # 检查连接是否有效
                result = await conn.fetchval("SELECT 1")
                if result != 1:
                    return False

                # 检查关键表是否存在且可访问
                critical_tables = ["users", "groups", "activity_configs"]
                for table in critical_tables:
                    try:
                        await conn.fetchval(f"SELECT 1 FROM {table} LIMIT 1")
                    except Exception as e:
                        logger.error(f"❌ 关键表 {table} 访问失败: {e}")
                        return False

                return True
        except Exception as e:
            logger.error(f"❌ 数据库健康检查失败: {e}")
            return False

    # ========== 连接管理 ==========
    def _ensure_pool_initialized(self):
        """确保连接池已初始化"""
        if not self.pool or not self._initialized:
            raise RuntimeError("数据库连接池尚未初始化，请先调用 initialize() 方法")

    async def get_connection(self):
        """获取数据库连接"""
        self._ensure_pool_initialized()
        return await self.pool.acquire()

    async def release_connection(self, conn):
        """释放数据库连接"""
        if self.pool:
            await self.pool.release(conn)

    async def close(self):
        """关闭数据库连接"""
        try:
            if self.pool:
                await self.pool.close()
                logger.info("PostgreSQL连接池已关闭")
        except Exception as e:
            logger.warning(f"关闭数据库连接时出现异常: {e}")

    # ========== 缓存管理 ==========
    def _get_cached(self, key: str):
        """获取缓存数据"""
        if key in self._cache_ttl and system_time.time() < self._cache_ttl[key]:
            return self._cache.get(key)
        else:
            # 清理过期缓存
            if key in self._cache:
                del self._cache[key]
            if key in self._cache_ttl:
                del self._cache_ttl[key]
            return None

    def _set_cached(self, key: str, value: Any, ttl: int = 60):
        """设置缓存数据"""
        self._cache[key] = value
        self._cache_ttl[key] = system_time.time() + ttl

    async def cleanup_cache(self):
        """🆕 增强的缓存清理 - 过期清理 + LRU清理"""
        current_time = system_time.time()
        expired_keys = [
            key for key, expiry in self._cache_ttl.items() if current_time >= expiry
        ]

        for key in expired_keys:
            self._cache.pop(key, None)
            self._cache_ttl.pop(key, None)
            if key in self._cache_access_order:
                self._cache_access_order.remove(key)

        # 🆕 额外清理：如果缓存仍然过大，移除最旧的一些条目
        if len(self._cache) > self._cache_max_size * 0.8:  # 80%阈值
            excess = len(self._cache) - int(self._cache_max_size * 0.7)  # 清理到70%
            if excess > 0 and self._cache_access_order:
                keys_to_remove = self._cache_access_order[:excess]
                for key in keys_to_remove:
                    self._cache.pop(key, None)
                    self._cache_ttl.pop(key, None)
                self._cache_access_order = self._cache_access_order[excess:]
                logger.info(f"LRU强制清理: 移除了 {len(keys_to_remove)} 个旧缓存")

        if expired_keys:
            logger.debug(
                f"缓存清理完成: {len(expired_keys)}个过期, 当前大小: {len(self._cache)}"
            )

    async def force_refresh_activity_cache(self):
        """强制刷新活动配置缓存"""
        cache_keys_to_remove = ["activity_limits", "push_settings", "fine_rates"]
        for key in cache_keys_to_remove:
            self._cache.pop(key, None)
            self._cache_ttl.pop(key, None)
        await self.get_activity_limits()
        await self.get_fine_rates()
        logger.info("活动配置缓存已强制刷新")

    # ========== 群组相关操作 ==========
    async def init_group(self, chat_id: int):
        """初始化群组 - 带重试"""
        await self.execute_with_retry(
            "初始化群组",
            "INSERT INTO groups (chat_id) VALUES ($1) ON CONFLICT (chat_id) DO NOTHING",
            chat_id,
        )
        self._cache.pop(f"group:{chat_id}", None)

    async def get_group(self, chat_id: int) -> Optional[Dict]:
        """获取群组配置"""
        cache_key = f"group:{chat_id}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        self._ensure_pool_initialized()
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM groups WHERE chat_id = $1", chat_id
            )
            if row:
                result = dict(row)
                self._set_cached(cache_key, result, 300)
                return result
            return None

    async def get_group_cached(self, chat_id: int) -> Optional[Dict]:
        """带缓存的获取群组配置"""
        return await self.get_group(chat_id)

    async def update_group_channel(self, chat_id: int, channel_id: int):
        """更新群组频道ID"""
        self._ensure_pool_initialized()
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE groups SET channel_id = $1, updated_at = CURRENT_TIMESTAMP WHERE chat_id = $2",
                channel_id,
                chat_id,
            )
            self._cache.pop(f"group:{chat_id}", None)

    async def update_group_notification(self, chat_id: int, group_id: int):
        """更新群组通知群组ID"""
        self._ensure_pool_initialized()
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE groups SET notification_group_id = $1, updated_at = CURRENT_TIMESTAMP WHERE chat_id = $2",
                group_id,
                chat_id,
            )
            self._cache.pop(f"group:{chat_id}", None)

    async def update_group_reset_time(self, chat_id: int, hour: int, minute: int):
        """更新群组重置时间"""
        self._ensure_pool_initialized()
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE groups SET reset_hour = $1, reset_minute = $2, updated_at = CURRENT_TIMESTAMP WHERE chat_id = $3",
                hour,
                minute,
                chat_id,
            )
            self._cache.pop(f"group:{chat_id}", None)

    async def update_group_work_time(
        self, chat_id: int, work_start: str, work_end: str
    ):
        """更新群组上下班时间"""
        self._ensure_pool_initialized()
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE groups SET work_start_time = $1, work_end_time = $2, updated_at = CURRENT_TIMESTAMP WHERE chat_id = $3",
                work_start,
                work_end,
                chat_id,
            )
            self._cache.pop(f"group:{chat_id}", None)

    # ========== 群组相关操作 - 添加新方法 ==========
    async def update_group_extra_work_group(
        self, chat_id: int, extra_work_group_id: int
    ):
        """设置额外的上下班通知群组"""
        self._ensure_pool_initialized()
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE groups 
                SET extra_work_notification_group = $1, 
                    updated_at = CURRENT_TIMESTAMP 
                WHERE chat_id = $2
                """,
                extra_work_group_id,
                chat_id,
            )
            self._cache.pop(f"group:{chat_id}", None)

    async def get_extra_work_group(self, chat_id: int) -> Optional[int]:
        """获取额外的上下班通知群组ID"""
        group_data = await self.get_group_cached(chat_id)
        return group_data.get("extra_work_notification_group") if group_data else None

    async def clear_extra_work_group(self, chat_id: int):
        """清除额外的上下班通知群组"""
        self._ensure_pool_initialized()
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE groups 
                SET extra_work_notification_group = NULL, 
                    updated_at = CURRENT_TIMESTAMP 
                WHERE chat_id = $1
                """,
                chat_id,
            )
            self._cache.pop(f"group:{chat_id}", None)

    async def get_group_work_time(self, chat_id: int) -> Dict[str, str]:
        """获取群组上下班时间 - 带重试"""
        row = await self.execute_with_retry(
            "获取工作时间",
            "SELECT work_start_time, work_end_time FROM groups WHERE chat_id = $1",
            chat_id,
            fetchrow=True,  # 👈 只需要添加这个参数
        )
        if row and row["work_start_time"] and row["work_end_time"]:
            return {
                "work_start": row["work_start_time"],
                "work_end": row["work_end_time"],
            }
        return Config.DEFAULT_WORK_HOURS.copy()

    async def has_work_hours_enabled(self, chat_id: int) -> bool:
        """检查是否启用了上下班功能"""
        work_hours = await self.get_group_work_time(chat_id)
        return (
            work_hours["work_start"] != Config.DEFAULT_WORK_HOURS["work_start"]
            or work_hours["work_end"] != Config.DEFAULT_WORK_HOURS["work_end"]
        )

    # ========== 用户相关操作 ==========
    async def init_user(self, chat_id: int, user_id: int, nickname: str = None):
        """初始化用户 - 带重试"""
        today = await self.get_business_date(chat_id)
        await self.execute_with_retry(
            "初始化用户",
            """
            INSERT INTO users (chat_id, user_id, nickname, last_updated) 
            VALUES ($1, $2, $3, $4) 
            ON CONFLICT (chat_id, user_id) 
            DO UPDATE SET 
                nickname = COALESCE($3, users.nickname),
                last_updated = $4,
                updated_at = CURRENT_TIMESTAMP
            """,
            chat_id,
            user_id,
            nickname,
            today,
        )
        self._cache.pop(f"user:{chat_id}:{user_id}", None)

    async def update_user_last_updated(
        self, chat_id: int, user_id: int, update_date: date
    ):
        """更新用户最后更新时间"""
        self._ensure_pool_initialized()
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET last_updated = $1 WHERE chat_id = $2 AND user_id = $3",
                update_date,
                chat_id,
                user_id,
            )

    async def get_user(self, chat_id: int, user_id: int) -> Optional[Dict]:
        cache_key = f"user:{chat_id}:{user_id}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        row = await self.execute_with_retry(
            "获取用户数据",
            """
            SELECT user_id, nickname, current_activity, activity_start_time, 
                total_accumulated_time, total_activity_count, total_fines,
                overtime_count, total_overtime_time, last_updated,
                checkin_message_id  
            FROM users WHERE chat_id = $1 AND user_id = $2
            """,
            chat_id,
            user_id,
            fetchrow=True,
            timeout=10,
            slow_threshold=0.5,
        )

        if row:
            result = dict(row)
            self._set_cached(cache_key, result, 30)
            return result
        return None

    # ========== 按照班次活动查询 =========
    async def get_user_activity_count_by_shift(
        self,
        chat_id: int,
        user_id: int,
        activity: str,
        shift: Optional[str] = None,
        query_date: Optional[date] = None,  # 🆕 新增参数
    ) -> int:
        """
        按班次获取用户活动次数（生产级版本）

        特性：
        - 支持单班模式 (shift=None)
        - 支持双班模式 (shift="day" 或 "night")
        - 自动容错转换 (night_last/night_tonight → night)
        - 支持传入自定义查询日期（用于夜班凌晨查询）
        - 严格的参数类型检查
        - 详细的调试日志
        """

        # ========= 1️⃣ 严格的参数类型检查 =========
        if not isinstance(chat_id, int):
            raise TypeError(
                f"❌ chat_id 必须是 int，但收到了 {type(chat_id)}: {chat_id}"
            )

        if not isinstance(user_id, int):
            raise TypeError(
                f"❌ user_id 必须是 int，但收到了 {type(user_id)}: {user_id}"
            )

        if not isinstance(activity, str):
            raise TypeError(
                f"❌ activity 必须是 str，但收到了 {type(activity)}: {activity}"
            )

        if query_date is not None and not isinstance(query_date, date):
            raise TypeError(
                f"❌ query_date 必须是 date 类型，但收到了 {type(query_date)}: {query_date}"
            )

        # shift 可以是 None 或字符串
        if shift is not None and not isinstance(shift, str):
            # 如果是字典，给出明确的错误信息和修复建议
            if isinstance(shift, dict):
                error_msg = (
                    f"❌ shift 参数错误：传入了字典，但期望字符串或 None\n"
                    f"   收到的字典: {shift}\n"
                    f"   你应该从字典中提取 'shift' 字段，例如：shift_info.get('shift')\n"
                    f"   调用堆栈：\n"
                )
                import traceback

                error_msg += "".join(traceback.format_stack()[:-1])
                logger.error(error_msg)
                raise TypeError("shift 参数必须是字符串或 None，不能是字典")
            else:
                raise TypeError(
                    f"❌ shift 必须是 str 或 None，但收到了 {type(shift)}: {shift}"
                )

        # ========= 2️⃣ 获取业务日期（支持传入自定义日期）=========
        if query_date:
            target_date = query_date
            logger.debug(f"📅 使用传入查询日期: {target_date}")
        else:
            target_date = await self.get_business_date(chat_id)
            # 🚨 夜班特殊处理：如果没有传入日期，根据当前时间自动调整
            if shift == "night":
                current_hour = self.get_beijing_time().hour
                if current_hour < 12:
                    target_date = target_date - timedelta(days=1)
                    logger.info(
                        f"🌙 [自动调整] 夜班凌晨查询: "
                        f"原始业务日期={target_date + timedelta(days=1)}, "
                        f"调整后={target_date}"
                    )
            logger.debug(f"📅 使用业务日期: {target_date}")

        # ========= 3️⃣ 构建查询 =========
        query = """
            SELECT activity_count
            FROM user_activities
            WHERE chat_id = $1
              AND user_id = $2
              AND activity_date = $3
              AND activity_name = $4
        """
        params = [chat_id, user_id, target_date, activity]

        # ========= 4️⃣ 处理班次参数 =========
        final_shift = None
        if shift is not None:
            shift = shift.strip()

            # 自动转换夜班详情值
            if shift in {"night_last", "night_tonight"}:
                logger.debug(f"🔄 自动转换班次值: {shift} -> night")
                final_shift = "night"
            elif shift in {"day", "night"}:
                final_shift = shift
            else:
                raise ValueError(
                    f"❌ 无效的班次值: '{shift}'，必须是 'day', 'night', 或 None"
                )

            query += " AND shift = $5"
            params.append(final_shift)

        # ========= 5️⃣ 调试日志 =========
        logger.debug(
            f"🔎 查询活动次数: chat_id={chat_id}, "
            f"user_id={user_id}, date={target_date}, "
            f"activity={activity}, shift={final_shift or '所有班次'}"
        )

        # ========= 6️⃣ 执行查询 =========
        try:
            count = await self.execute_with_retry(
                "按班次获取活动次数",
                query,
                *params,
                fetchval=True,
            )
        except Exception as e:
            logger.error(f"❌ 数据库查询失败: {e}")
            raise

        # ========= 7️⃣ 返回结果 =========
        return count if count is not None else 0

    async def get_user_cached(self, chat_id: int, user_id: int) -> Optional[Dict]:
        """带缓存的获取用户数据 - 优化版"""
        cache_key = f"user:{chat_id}:{user_id}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        # ✅ 修复：添加 shift 字段
        row = await self.execute_with_retry(
            "获取用户数据",
            """
            SELECT user_id, nickname, current_activity, activity_start_time, 
                   total_accumulated_time, total_activity_count, total_fines,
                   overtime_count, total_overtime_time, last_updated, 
                   checkin_message_id, shift  -- ✅ 添加 shift 字段
            FROM users 
            WHERE chat_id = $1 AND user_id = $2
            """,
            chat_id,
            user_id,
            fetchrow=True,  # 👈 只需要添加这个参数
        )

        if row:
            result = dict(row)
            # 确保 shift 字段存在
            if "shift" not in result or result["shift"] is None:
                result["shift"] = "day"
                logger.warning(f"用户 {user_id} 的 shift 字段为 None，使用默认值 'day'")

            self._set_cached(cache_key, result, 30)  # 30秒缓存
            logger.debug(f"获取用户缓存: {user_id}, shift={result['shift']}")
            return result
        return None

    async def update_user_activity(
        self,
        chat_id: int,
        user_id: int,
        activity: str,
        start_time: str,
        nickname: str = None,
        shift: str = "day",
    ):
        """更新用户活动状态 - 确保时间格式正确（完整融合稳定版）"""
        try:
            original_type = type(start_time).__name__

            # 🎯 统一转换为标准 ISO 时间字符串（带时区）
            if hasattr(start_time, "isoformat"):
                if start_time.tzinfo is None:
                    # 注意：确保 beijing_tz 在类外部或作为成员变量 self.beijing_tz 可用
                    start_time = beijing_tz.localize(start_time)
                start_time_str = start_time.isoformat()

            elif isinstance(start_time, str):
                try:
                    clean_str = start_time.strip()

                    if clean_str.endswith("Z"):
                        clean_str = clean_str.replace("Z", "+00:00")

                    dt = datetime.fromisoformat(clean_str)

                    if dt.tzinfo is None:
                        dt = beijing_tz.localize(dt)

                    start_time_str = dt.isoformat()

                except ValueError:
                    start_time_str = start_time
                    logger.warning(f"⚠️ 时间字符串格式可能无效: {start_time_str}")

            else:
                start_time_str = str(start_time)
                logger.debug(
                    f"🔄 转换其他类型为字符串: {original_type} -> {start_time_str}"
                )

            logger.info(
                f"💾 保存活动时间: 用户{user_id}, 活动{activity}, 标准化时间: {start_time_str}, 班次: {shift}"
            )

            if nickname:
                await self.execute_with_retry(
                    "更新用户活动",
                    """
                    UPDATE users 
                    SET current_activity = $1, activity_start_time = $2, nickname = $3, shift = $4, updated_at = CURRENT_TIMESTAMP 
                    WHERE chat_id = $5 AND user_id = $6
                    """,
                    activity,
                    start_time_str,
                    nickname,
                    shift,
                    chat_id,
                    user_id,
                )
            else:
                await self.execute_with_retry(
                    "更新用户活动",
                    """
                    UPDATE users 
                    SET current_activity = $1, activity_start_time = $2, shift = $3, updated_at = CURRENT_TIMESTAMP 
                    WHERE chat_id = $4 AND user_id = $5
                    """,
                    activity,
                    start_time_str,
                    shift,
                    chat_id,
                    user_id,
                )

            # 清理缓存
            self._cache.pop(f"user:{chat_id}:{user_id}", None)

            logger.debug(
                f"✅ 用户活动更新成功: {chat_id}-{user_id} -> {activity}（班次: {shift}）"
            )

        except Exception as e:
            logger.error(f"❌ 更新用户活动失败 {chat_id}-{user_id}: {e}")
            logger.error(
                f"❌ 失败时的参数 - activity: {activity}, start_time: {start_time}, nickname: {nickname}, shift: {shift}"
            )
            raise

    async def update_user_checkin_message(
        self, chat_id: int, user_id: int, message_id: int
    ):
        """更新用户的打卡消息ID"""
        self._ensure_pool_initialized()
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE users 
                SET checkin_message_id = $1, updated_at = CURRENT_TIMESTAMP 
                WHERE chat_id = $2 AND user_id = $3
                """,
                message_id,
                chat_id,
                user_id,
            )
        # ✅ 强制清除缓存，确保下次读取能获取最新数据
        cache_key = f"user:{chat_id}:{user_id}"
        self._cache.pop(cache_key, None)
        self._cache_ttl.pop(cache_key, None)
        logger.info(f"✅ 已更新用户 {user_id} 的打卡消息ID为 {message_id}，并清除缓存")

    async def get_user_checkin_message_id(
        self, chat_id: int, user_id: int
    ) -> Optional[int]:
        """获取用户的打卡消息ID"""
        user_data = await self.get_user_cached(chat_id, user_id)
        return user_data.get("checkin_message_id") if user_data else None

    async def clear_user_checkin_message(self, chat_id: int, user_id: int):
        """清除用户的打卡消息ID"""
        self._ensure_pool_initialized()
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE users 
                SET checkin_message_id = NULL, updated_at = CURRENT_TIMESTAMP 
                WHERE chat_id = $1 AND user_id = $2
                """,
                chat_id,
                user_id,
            )
        self._cache.pop(f"user:{chat_id}:{user_id}", None)

    # ====== 核心业务方法 ======
    async def complete_user_activity(
        self,
        chat_id: int,
        user_id: int,
        activity: str,
        elapsed_time: int,
        fine_amount: int = 0,
        is_overtime: bool = False,
        shift: Optional[str] = None,
        forced_date: Optional[date] = None,
    ) -> None:
        """完成用户活动 - 支持班次、软重置、统计、超时、罚款"""

        # ===== 1️⃣ 班次处理 =====
        # 如果外部没有传入 shift，尝试从用户状态获取
        if shift is None:
            # 获取用户当前的班次状态
            user_shift_state = await self.get_user_active_shift(chat_id, user_id)
            if user_shift_state:
                shift = user_shift_state["shift"]  # ✅ 直接从字典获取 shift 字段
                logger.debug(f"📅 从用户班次状态获取班次: {shift}")
            else:
                # 降级使用时间判定
                now = self.get_beijing_time()
                shift_info = await self.determine_shift_for_time(chat_id, now)
                shift = shift_info.get("shift", "day") if shift_info else "day"
                logger.debug(f"📅 降级使用时间判定班次: {shift}")

        # ===== 2️⃣ 时间计算 =====
        # 🎯 确定目标日期
        if forced_date:
            target_date = forced_date
            logger.debug(f"📅 使用强制日期: {target_date}")
        else:
            target_date = await self.get_business_date(chat_id)
            logger.debug(f"📅 使用业务日期: {target_date}")

        statistic_date = target_date.replace(day=1)
        now = self.get_beijing_time()

        overtime_seconds = 0
        if is_overtime:
            time_limit = await self.get_activity_time_limit(activity)
            overtime_seconds = max(0, elapsed_time - time_limit * 60)

        self._ensure_pool_initialized()

        async with self.pool.acquire() as conn:
            async with conn.transaction():

                # ===== 3️⃣ 软重置判断（只有非强制日期才检查）=====
                current_soft_reset = False
                if not forced_date:
                    has_soft_reset_record = await conn.fetchval(
                        """
                        SELECT EXISTS (
                            SELECT 1 FROM daily_statistics
                            WHERE chat_id = $1
                              AND user_id = $2
                              AND record_date = $3
                              AND is_soft_reset = TRUE
                              AND shift = $4
                        )
                        """,
                        chat_id,
                        user_id,
                        target_date,
                        shift,
                    )

                    should_be_soft_reset = False
                    if not has_soft_reset_record:
                        hour, minute = await self.get_group_soft_reset_time(chat_id)
                        if hour > 0 or minute > 0:
                            reset_time = now.replace(
                                hour=hour, minute=minute, second=0, microsecond=0
                            )
                            if now >= reset_time:
                                should_be_soft_reset = True

                    current_soft_reset = bool(
                        has_soft_reset_record or should_be_soft_reset
                    )

                    if should_be_soft_reset:
                        await conn.execute(
                            """
                            INSERT INTO daily_statistics
                            (chat_id, user_id, record_date, activity_name,
                             activity_count, accumulated_time, is_soft_reset, shift)
                            VALUES ($1, $2, $3, 'soft_reset', 0, 0, TRUE, $4)
                            ON CONFLICT (chat_id, user_id, record_date,
                                         activity_name, is_soft_reset, shift)
                            DO NOTHING
                            """,
                            chat_id,
                            user_id,
                            target_date,
                            shift,
                        )

                # ===== 4️⃣ users 基础行 =====
                await conn.execute(
                    """
                    INSERT INTO users (chat_id, user_id, last_updated)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (chat_id, user_id)
                    DO UPDATE SET last_updated = EXCLUDED.last_updated
                    """,
                    chat_id,
                    user_id,
                    target_date,
                )

                # ===== 5️⃣ user_activities =====
                await conn.execute(
                    """
                    INSERT INTO user_activities
                    (chat_id, user_id, activity_date, activity_name,
                     activity_count, accumulated_time, shift)
                    VALUES ($1, $2, $3, $4, 1, $5, $6)
                    ON CONFLICT (chat_id, user_id, activity_date,
                                 activity_name, shift)
                    DO UPDATE SET
                        activity_count = user_activities.activity_count + 1,
                        accumulated_time = user_activities.accumulated_time
                                           + EXCLUDED.accumulated_time,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    chat_id,
                    user_id,
                    target_date,
                    activity,
                    elapsed_time,
                    shift,
                )

                # ===== 6️⃣ daily_statistics =====
                soft_reset_flag = current_soft_reset if not forced_date else False

                await conn.execute(
                    """
                    INSERT INTO daily_statistics
                    (chat_id, user_id, record_date, activity_name,
                     activity_count, accumulated_time, is_soft_reset, shift)
                    VALUES ($1, $2, $3, $4, 1, $5, $6, $7)
                    ON CONFLICT (chat_id, user_id, record_date,
                                 activity_name, is_soft_reset, shift)
                    DO UPDATE SET
                        activity_count = daily_statistics.activity_count + 1,
                        accumulated_time = daily_statistics.accumulated_time
                                           + EXCLUDED.accumulated_time,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    chat_id,
                    user_id,
                    target_date,
                    activity,
                    elapsed_time,
                    soft_reset_flag,
                    shift,
                )

                if is_overtime:
                    await conn.execute(
                        """
                        INSERT INTO daily_statistics
                        (chat_id, user_id, record_date, activity_name,
                         activity_count, is_soft_reset, shift)
                        VALUES ($1, $2, $3, 'overtime_count', 1, $4, $5)
                        ON CONFLICT (chat_id, user_id, record_date,
                                     activity_name, is_soft_reset, shift)
                        DO UPDATE SET
                            activity_count = daily_statistics.activity_count + 1,
                            updated_at = CURRENT_TIMESTAMP
                        """,
                        chat_id,
                        user_id,
                        target_date,
                        soft_reset_flag,
                        shift,
                    )

                    await conn.execute(
                        """
                        INSERT INTO daily_statistics
                        (chat_id, user_id, record_date, activity_name,
                         accumulated_time, is_soft_reset, shift)
                        VALUES ($1, $2, $3, 'overtime_time', $4, $5, $6)
                        ON CONFLICT (chat_id, user_id, record_date,
                                     activity_name, is_soft_reset, shift)
                        DO UPDATE SET
                            accumulated_time = daily_statistics.accumulated_time
                                               + EXCLUDED.accumulated_time,
                            updated_at = CURRENT_TIMESTAMP
                        """,
                        chat_id,
                        user_id,
                        target_date,
                        overtime_seconds,
                        soft_reset_flag,
                        shift,
                    )

                if fine_amount > 0:
                    await conn.execute(
                        """
                        INSERT INTO daily_statistics
                        (chat_id, user_id, record_date, activity_name,
                         accumulated_time, is_soft_reset, shift)
                        VALUES ($1, $2, $3, 'total_fines', $4, $5, $6)
                        ON CONFLICT (chat_id, user_id, record_date,
                                     activity_name, is_soft_reset, shift)
                        DO UPDATE SET
                            accumulated_time = daily_statistics.accumulated_time
                                               + EXCLUDED.accumulated_time,
                            updated_at = CURRENT_TIMESTAMP
                        """,
                        chat_id,
                        user_id,
                        target_date,
                        fine_amount,
                        soft_reset_flag,
                        shift,
                    )

                # ===== 7️⃣ monthly_statistics =====
                await conn.execute(
                    """
                    INSERT INTO monthly_statistics
                    (chat_id, user_id, statistic_date, activity_name,
                     activity_count, accumulated_time, shift)
                    VALUES ($1, $2, $3, $4, 1, $5, $6)
                    ON CONFLICT (chat_id, user_id, statistic_date,
                                 activity_name, shift)
                    DO UPDATE SET
                        activity_count = monthly_statistics.activity_count + 1,
                        accumulated_time = monthly_statistics.accumulated_time
                                           + EXCLUDED.accumulated_time,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    chat_id,
                    user_id,
                    statistic_date,
                    activity,
                    elapsed_time,
                    shift,
                )

                if fine_amount > 0:
                    await conn.execute(
                        """
                        INSERT INTO monthly_statistics
                        (chat_id, user_id, statistic_date,
                         activity_name, accumulated_time, shift)
                        VALUES ($1, $2, $3, 'total_fines', $4, $5)
                        ON CONFLICT (chat_id, user_id, statistic_date,
                                     activity_name, shift)
                        DO UPDATE SET
                            accumulated_time = monthly_statistics.accumulated_time
                                               + EXCLUDED.accumulated_time,
                            updated_at = CURRENT_TIMESTAMP
                        """,
                        chat_id,
                        user_id,
                        statistic_date,
                        fine_amount,
                        shift,
                    )

                if is_overtime and overtime_seconds > 0:
                    await conn.execute(
                        """
                        INSERT INTO monthly_statistics
                        (chat_id, user_id, statistic_date,
                         activity_name, activity_count, shift)
                        VALUES ($1, $2, $3, 'overtime_count', 1, $4)
                        ON CONFLICT (chat_id, user_id, statistic_date,
                                     activity_name, shift)
                        DO UPDATE SET
                            activity_count = monthly_statistics.activity_count + 1,
                            updated_at = CURRENT_TIMESTAMP
                        """,
                        chat_id,
                        user_id,
                        statistic_date,
                        shift,
                    )

                    await conn.execute(
                        """
                        INSERT INTO monthly_statistics
                        (chat_id, user_id, statistic_date,
                         activity_name, accumulated_time, shift)
                        VALUES ($1, $2, $3, 'overtime_time', $4, $5)
                        ON CONFLICT (chat_id, user_id, statistic_date,
                                     activity_name, shift)
                        DO UPDATE SET
                            accumulated_time = monthly_statistics.accumulated_time
                                               + EXCLUDED.accumulated_time,
                            updated_at = CURRENT_TIMESTAMP
                        """,
                        chat_id,
                        user_id,
                        statistic_date,
                        overtime_seconds,
                        shift,
                    )

                # ===== 8️⃣ users 总账（动态安全参数）=====
                update_fields = [
                    "total_accumulated_time = total_accumulated_time + $1",
                    "total_activity_count = total_activity_count + 1",
                    "current_activity = NULL",
                    "activity_start_time = NULL",
                    "checkin_message_id = NULL",
                    "last_updated = $2",
                ]
                params = [elapsed_time, target_date]

                if fine_amount > 0:
                    idx = len(params) + 1
                    update_fields.append(f"total_fines = total_fines + ${idx}")
                    params.append(fine_amount)

                if is_overtime:
                    update_fields.append("overtime_count = overtime_count + 1")
                    idx = len(params) + 1
                    update_fields.append(
                        f"total_overtime_time = total_overtime_time + ${idx}"
                    )
                    params.append(overtime_seconds)

                update_fields.append("updated_at = CURRENT_TIMESTAMP")
                params.extend([chat_id, user_id])

                query = f"""
                    UPDATE users
                    SET {", ".join(update_fields)}
                    WHERE chat_id = ${len(params) - 1}
                      AND user_id = ${len(params)}
                """
                await conn.execute(query, *params)

        # ===== 9️⃣ 清理缓存 =====
        self._cache.pop(f"user:{chat_id}:{user_id}", None)
        self._cache_ttl.pop(f"user:{chat_id}:{user_id}", None)

        date_source = "强制日期" if forced_date else "业务日期"
        logger.info(
            f"✅ 四表同步完成: {chat_id}-{user_id} - {activity} "
            f"(日期: {target_date} [{date_source}], 时长: {elapsed_time}s, "
            f"罚款: {fine_amount}, 超时: {is_overtime} {overtime_seconds}s, "
            f"软重置: {current_soft_reset}, 班次: {shift})"
        )

    # ========= 重置前批量完成所有未结束活动 =========
    async def complete_all_pending_activities_before_reset(
        self, chat_id: int, reset_time: datetime
    ) -> Dict[str, Any]:
        """在重置前批量完成所有未结束活动 - 完整版本"""
        try:
            completed_count = 0
            total_fines = 0

            self._ensure_pool_initialized()
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    # 🎯 批量获取所有未结束活动
                    active_users = await conn.fetch(
                        """
                        SELECT user_id, nickname, current_activity, activity_start_time 
                        FROM users 
                        WHERE chat_id = $1 AND current_activity IS NOT NULL
                    """,
                        chat_id,
                    )

                    if not active_users:
                        return {"completed_count": 0, "total_fines": 0, "details": []}

                    completion_details = []
                    statistic_date = reset_time.date().replace(day=1)

                    for user in active_users:
                        user_id = user["user_id"]
                        nickname = user["nickname"]
                        activity = user["current_activity"]
                        start_time_str = user["activity_start_time"]

                        try:
                            # 解析开始时间
                            start_time = datetime.fromisoformat(start_time_str)

                            # 计算活动时长（到重置时间为止）
                            elapsed = int((reset_time - start_time).total_seconds())

                            # 计算超时和罚款
                            time_limit = await self.get_activity_time_limit(activity)
                            time_limit_seconds = time_limit * 60
                            is_overtime = elapsed > time_limit_seconds
                            overtime_seconds = max(0, elapsed - time_limit_seconds)
                            overtime_minutes = overtime_seconds / 60

                            fine_amount = 0
                            if is_overtime and overtime_seconds > 0:
                                fine_amount = await self.calculate_fine_for_activity(
                                    activity, overtime_minutes
                                )

                            # 🎯 更新月度统计表
                            await self._update_monthly_statistics_for_activity(
                                conn,
                                chat_id,
                                user_id,
                                statistic_date,
                                activity,
                                elapsed,
                                fine_amount,
                                is_overtime,
                                overtime_seconds,
                            )

                            completed_count += 1
                            total_fines += fine_amount

                            completion_details.append(
                                {
                                    "user_id": user_id,
                                    "nickname": nickname,
                                    "activity": activity,
                                    "elapsed_time": elapsed,
                                    "fine_amount": fine_amount,
                                    "is_overtime": is_overtime,
                                }
                            )

                            logger.info(
                                f"重置前结束活动: {chat_id}-{user_id} - {activity} (时长: {elapsed}秒, 罚款: {fine_amount}元)"
                            )

                        except Exception as e:
                            logger.error(f"结束用户活动失败 {chat_id}-{user_id}: {e}")
                            # 记录错误但继续处理其他用户

                    # 🎯 批量清空活动状态
                    await conn.execute(
                        """
                        UPDATE users 
                        SET current_activity = NULL, activity_start_time = NULL 
                        WHERE chat_id = $1 AND current_activity IS NOT NULL
                    """,
                        chat_id,
                    )

                    return {
                        "completed_count": completed_count,
                        "total_fines": total_fines,
                        "details": completion_details,
                    }

        except Exception as e:
            logger.error(f"批量结束活动失败 {chat_id}: {e}")
            return {"completed_count": 0, "total_fines": 0, "details": []}

    async def _update_monthly_statistics_for_activity(
        self,
        conn,
        chat_id: int,
        user_id: int,
        statistic_date: date,
        activity: str,
        elapsed: int,
        fine_amount: int,
        is_overtime: bool,
        overtime_seconds: int,
    ):
        """更新月度统计的辅助方法"""
        # 更新活动统计
        await conn.execute(
            """
            INSERT INTO monthly_statistics 
            (chat_id, user_id, statistic_date, activity_name, activity_count, accumulated_time)
            VALUES ($1, $2, $3, $4, 1, $5)
            ON CONFLICT (chat_id, user_id, statistic_date, activity_name) 
            DO UPDATE SET 
                activity_count = monthly_statistics.activity_count + 1,
                accumulated_time = monthly_statistics.accumulated_time + EXCLUDED.accumulated_time,
                updated_at = CURRENT_TIMESTAMP
        """,
            chat_id,
            user_id,
            statistic_date,
            activity,
            elapsed,
        )

        # 更新罚款统计
        if fine_amount > 0:
            await conn.execute(
                """
                INSERT INTO monthly_statistics 
                (chat_id, user_id, statistic_date, activity_name, accumulated_time)
                VALUES ($1, $2, $3, 'total_fines', $4)
                ON CONFLICT (chat_id, user_id, statistic_date, activity_name) 
                DO UPDATE SET 
                    accumulated_time = monthly_statistics.accumulated_time + EXCLUDED.accumulated_time,
                    updated_at = CURRENT_TIMESTAMP
            """,
                chat_id,
                user_id,
                statistic_date,
                fine_amount,
            )

        # 更新超时统计
        if is_overtime:
            await conn.execute(
                """
                INSERT INTO monthly_statistics 
                (chat_id, user_id, statistic_date, activity_name, activity_count)
                VALUES ($1, $2, $3, 'overtime_count', 1)
                ON CONFLICT (chat_id, user_id, statistic_date, activity_name) 
                DO UPDATE SET 
                    activity_count = monthly_statistics.activity_count + 1,
                    updated_at = CURRENT_TIMESTAMP
            """,
                chat_id,
                user_id,
                statistic_date,
            )

            await conn.execute(
                """
                INSERT INTO monthly_statistics 
                (chat_id, user_id, statistic_date, activity_name, accumulated_time)
                VALUES ($1, $2, $3, 'overtime_time', $4)
                ON CONFLICT (chat_id, user_id, statistic_date, activity_name) 
                DO UPDATE SET 
                    accumulated_time = monthly_statistics.accumulated_time + EXCLUDED.accumulated_time,
                    updated_at = CURRENT_TIMESTAMP
            """,
                chat_id,
                user_id,
                statistic_date,
                overtime_seconds,
            )

    # ───────────────────────── 硬置时间配置 ─────────────────────────
    async def reset_user_daily_data(
        self, chat_id: int, user_id: int, target_date: date | None = None
    ):
        """
        🧬 硬重置用户数据 - 完整融合版本
        1. 结算跨天活动 → 更新月度统计
        2. 清空 daily_statistics 表当日记录
        3. 清空 user_activities 和 work_records 表
        4. 重置 users 表展示字段
        """
        try:
            # ────────────────── ① 日期校验 ──────────────────
            if target_date is None:
                target_date = await self.get_business_date(chat_id)
            elif not isinstance(target_date, date):
                raise ValueError(
                    f"target_date必须是date类型，得到: {type(target_date)}"
                )

            # 获取重置前状态用于日志
            user_before = await self.get_user(chat_id, user_id)
            activities_before = await self.get_user_all_activities(chat_id, user_id)

            cross_day = {"activity": None, "duration": 0, "fine": 0}
            new_date = max(target_date, await self.get_business_date(chat_id))

            async with self.pool.acquire() as conn:
                async with conn.transaction():

                    # ─────────────── ② 跨天结算与月度统计（保留第一个代码的逻辑） ───────────────
                    if user_before and user_before.get("current_activity"):
                        act = user_before["current_activity"]
                        start_str = user_before.get("activity_start_time")

                        if start_str:
                            try:
                                start = datetime.fromisoformat(start_str)
                                now = self.get_beijing_time()
                                elapsed = int((now - start).total_seconds())

                                limit_min = await self.get_activity_time_limit(act)
                                limit_sec = limit_min * 60

                                overtime_sec = max(0, elapsed - limit_sec)
                                overtime_min = overtime_sec / 60

                                fine = 0
                                if overtime_sec > 0:
                                    rates = await self.get_fine_rates_for_activity(act)
                                    if rates:
                                        segments = []
                                        for k in rates:
                                            try:
                                                v = int(
                                                    str(k).lower().replace("min", "")
                                                )
                                                segments.append(v)
                                            except:
                                                pass
                                        segments.sort()
                                        for s in segments:
                                            if overtime_min <= s:
                                                fine = rates.get(
                                                    str(s), rates.get(f"{s}min", 0)
                                                )
                                                break
                                        if fine == 0 and segments:
                                            m = segments[-1]
                                            fine = rates.get(
                                                str(m), rates.get(f"{m}min", 0)
                                            )

                                activity_month = start.date().replace(day=1)

                                await conn.execute(
                                    """
                                    INSERT INTO monthly_statistics 
                                    (chat_id, user_id, statistic_date, activity_name, activity_count, accumulated_time)
                                    VALUES ($1, $2, $3, $4, 1, $5)
                                    ON CONFLICT (chat_id, user_id, statistic_date, activity_name)
                                    DO UPDATE SET
                                        activity_count = monthly_statistics.activity_count + 1,
                                        accumulated_time = monthly_statistics.accumulated_time + EXCLUDED.accumulated_time,
                                        updated_at = CURRENT_TIMESTAMP
                                """,
                                    chat_id,
                                    user_id,
                                    activity_month,
                                    act,
                                    elapsed,
                                )

                                if fine > 0:
                                    await conn.execute(
                                        """
                                        UPDATE users SET total_fines = total_fines + $1
                                        WHERE chat_id = $2 AND user_id = $3
                                    """,
                                        fine,
                                        chat_id,
                                        user_id,
                                    )

                                if overtime_sec > 0:
                                    await conn.execute(
                                        """
                                        UPDATE users SET
                                            overtime_count = overtime_count + 1,
                                            total_overtime_time = total_overtime_time + $1
                                        WHERE chat_id = $2 AND user_id = $3
                                    """,
                                        overtime_sec,
                                        chat_id,
                                        user_id,
                                    )

                                cross_day.update(
                                    {"activity": act, "duration": elapsed, "fine": fine}
                                )

                            except Exception as e:
                                logger.error(f"❌ 跨天结算失败: {e}")

                    # ─────────────── ③ 清空四表数据（融合第二个代码） ───────────────
                    # 3.1 清理 daily_statistics 表
                    daily_deleted = await conn.execute(
                        """
                        DELETE FROM daily_statistics
                        WHERE chat_id = $1 AND user_id = $2 AND record_date = $3
                    """,
                        chat_id,
                        user_id,
                        target_date,
                    )

                    # 3.2 清理 user_activities 表
                    activities_deleted = await conn.execute(
                        """
                        DELETE FROM user_activities
                        WHERE chat_id = $1 AND user_id = $2 AND activity_date = $3
                    """,
                        chat_id,
                        user_id,
                        target_date,
                    )

                    # 3.3 清理 work_records 表
                    work_deleted = await conn.execute(
                        """
                        DELETE FROM work_records
                        WHERE chat_id = $1 AND user_id = $2 AND record_date = $3
                    """,
                        chat_id,
                        user_id,
                        target_date,
                    )

                    # ─────────────── ④ 清空用户状态（保留条件判断） ───────────────
                    users_updated = await conn.execute(
                        """
                        UPDATE users SET
                            total_activity_count = 0,
                            total_accumulated_time = 0,
                            total_fines = 0,
                            total_overtime_time = 0,
                            overtime_count = 0,
                            current_activity = NULL,
                            activity_start_time = NULL,
                            checkin_message_id = NULL,                          
                            last_updated = $3,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE chat_id = $1 AND user_id = $2
                        AND (
                            total_activity_count > 0 OR
                            total_accumulated_time > 0 OR
                            total_fines > 0 OR
                            overtime_count > 0 OR
                            current_activity IS NOT NULL OR
                            checkin_message_id IS NOT NULL                           
                        )
                    """,
                        chat_id,
                        user_id,
                        new_date,
                    )

            # ─────────────── ⑤ 缓存全清 ───────────────
            for key in (
                f"user:{chat_id}:{user_id}",
                f"group:{chat_id}",
                "activity_limits",
            ):
                self._cache.pop(key, None)
                self._cache_ttl.pop(key, None)

            # ─────────────── ⑥ SQL 返回安全解析 ───────────────
            def parse_count(result):
                if not result:
                    return 0
                parts = result.split()
                return (
                    int(parts[-1])
                    if len(parts) > 1 and parts[0] in ("DELETE", "UPDATE")
                    else 0
                )

            daily_del_count = parse_count(daily_deleted)
            activities_del_count = parse_count(activities_deleted)
            work_del_count = parse_count(work_deleted)
            users_upd_count = parse_count(users_updated)

            # ─────────────── ⑦ 生产级日志 ───────────────
            log = (
                f"✅ [硬重置完成] 用户:{user_id} 群:{chat_id}\n"
                f"📅 日期:{new_date}\n"
                f"🗑 删除记录: daily_statistics({daily_del_count}), "
                f"user_activities({activities_del_count}), "
                f"work_records({work_del_count})\n"
                f"🔄 更新用户: {users_upd_count} 次\n"
                f"💾 月度统计: 已安全持久化\n"
            )
            if cross_day["activity"]:
                log += f"🌙 跨天结算: {cross_day['activity']} {self.format_seconds_to_hms(cross_day['duration'])}"
                if cross_day["fine"] > 0:
                    log += f" 💰罚款:{cross_day['fine']}元\n"
            log += (
                f"📊 重置前状态: 次数{user_before.get('total_activity_count', 0) if user_before else 0} "
                f"时长{user_before.get('total_accumulated_time', 0) if user_before else 0}s "
                f"罚款{user_before.get('total_fines', 0) if user_before else 0} "
                f"超时{user_before.get('overtime_count', 0) if user_before else 0} "
                f"当前:{user_before.get('current_activity', '无') if user_before else '无'} "
                f"活动种类:{len(activities_before)}"
            )

            logger.info(log)
            return True

        except Exception as e:
            logger.error(f"❌ 硬重置失败 {chat_id}-{user_id}: {e}")
            return False

    # ───────────────────────── 软重置时间配置 ─────────────────────────

    async def update_group_soft_reset_time(self, chat_id: int, hour: int, minute: int):
        """更新群组软重置时间（仅影响显示与策略，不影响业务周期）"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE groups
                SET soft_reset_hour = $1,
                    soft_reset_minute = $2,
                    updated_at = CURRENT_TIMESTAMP
                WHERE chat_id = $3
                """,
                hour,
                minute,
                chat_id,
            )
            self._cache.pop(f"group:{chat_id}", None)

    async def get_group_soft_reset_time(self, chat_id: int) -> tuple[int, int]:
        """获取群组软重置时间"""
        group_data = await self.get_group_cached(chat_id)
        hour = group_data.get("soft_reset_hour", 0)
        minute = group_data.get("soft_reset_minute", 0)
        return hour, minute

    # ========= 软重置(二次重置) =========
    async def reset_user_soft_daily_data(self, chat_id: int, user_id: int):
        """
        🧽 软重置用户数据
        1. 不处理 daily_statistics 表（保持原样）
        2. 清空 user_activities 表的当日记录
        3. 重置 users 表的展示字段
        """
        try:
            today = await self.get_business_date(chat_id)

            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    # ========== 1. 检查是否真的有数据需要重置 ==========
                    user_data = await conn.fetchrow(
                        """
                        SELECT total_activity_count, total_accumulated_time, total_fines,
                               overtime_count, current_activity
                        FROM users 
                        WHERE chat_id = $1 AND user_id = $2
                        """,
                        chat_id,
                        user_id,
                    )

                    if not user_data:
                        logger.info(f"用户 {chat_id}-{user_id} 不存在，无需软重置")
                        return True

                    has_data = (
                        user_data["total_activity_count"] > 0
                        or user_data["total_accumulated_time"] > 0
                        or user_data["total_fines"] > 0
                        or user_data["overtime_count"] > 0
                        or user_data["current_activity"] is not None
                    )

                    if not has_data:
                        logger.info(f"用户 {chat_id}-{user_id} 没有数据需要软重置")
                        return True

                    # ========== 2. 删除 user_activities 表的当日记录 ==========
                    activities_deleted_result = await conn.execute(
                        """
                        DELETE FROM user_activities 
                        WHERE chat_id = $1 AND user_id = $2 AND activity_date = $3
                        """,
                        chat_id,
                        user_id,
                        today,
                    )

                    # ========== 3. 重置 users 表的展示字段 ==========
                    users_updated_result = await conn.execute(
                        """
                        UPDATE users SET
                            total_activity_count = 0,
                            total_accumulated_time = 0,
                            total_fines = 0,
                            total_overtime_time = 0,
                            overtime_count = 0,
                            current_activity = NULL,
                            activity_start_time = NULL,
                            checkin_message_id = NULL,
                            last_updated = $3,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE chat_id = $1 AND user_id = $2
                        AND (
                            total_activity_count > 0 OR
                            total_accumulated_time > 0 OR
                            total_fines > 0 OR
                            overtime_count > 0 OR
                            current_activity IS NOT NULL
                        )
                        """,
                        chat_id,
                        user_id,
                        today,
                    )

            # ========== 4. 完整缓存清理 ==========
            for key in (
                f"user:{chat_id}:{user_id}",
                f"group:{chat_id}",
                "activity_limits",
            ):
                self._cache.pop(key, None)
                self._cache_ttl.pop(key, None)

            # ========== 5. SQL 返回安全解析 ==========
            def parse_count(result):
                if not result:
                    return 0
                parts = result.split()
                return (
                    int(parts[-1])
                    if len(parts) > 1 and parts[0] in ["UPDATE", "DELETE"]
                    else 0
                )

            activities_deleted = parse_count(activities_deleted_result)
            users_updated = parse_count(users_updated_result)

            logger.info(
                f"🧽 [软重置完成] 用户:{user_id} 群:{chat_id} | "
                f"删除活动记录: {activities_deleted} 条 | "
                f"重置用户字段: {users_updated} 次"
            )
            return True

        except Exception as e:
            logger.error(f"❌ 软重置失败 {chat_id}-{user_id}: {e}")
            return False

    async def get_user_all_activities(
        self, chat_id: int, user_id: int, target_date: date = None  # ✅ 新增可选参数
    ) -> Dict[str, Dict]:
        """获取用户活动数据，可指定日期"""
        if target_date is None:
            target_date = await self.get_business_date(chat_id)  # 默认使用业务日期

        self._ensure_pool_initialized()
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT activity_name, activity_count, accumulated_time 
                FROM user_activities 
                WHERE chat_id = $1 AND user_id = $2 AND activity_date = $3
                """,
                chat_id,
                user_id,
                target_date,  # ✅ 使用传入的日期
            )

            activities = {}
            for row in rows:
                activities[row["activity_name"]] = {
                    "count": row["activity_count"],
                    "time": row["accumulated_time"],
                }
            return activities

    # ========== 上下班记录操作 ==========
    async def add_work_record(
        self,
        chat_id: int,
        user_id: int,
        record_date,
        checkin_type: str,
        checkin_time: str,
        status: str,
        time_diff_minutes: float,
        fine_amount: int = 0,
        shift: str = "day",
        shift_detail: str = None,
    ):
        """
        添加上下班记录 - 生产级最终版（含班次计数器，无重复统计）
        """

        business_date = record_date
        statistic_date = business_date.replace(day=1)
        work_duration_seconds = 0
        work_duration_delta = 0
        old_duration = 0

        if shift is None:
            try:
                checkin_time_obj = datetime.strptime(checkin_time, "%H:%M").time()
                full_datetime = datetime.combine(business_date, checkin_time_obj)
                shift = (
                    await self.determine_shift_for_time(
                        chat_id, full_datetime, checkin_type
                    )
                    or "day"
                )
            except Exception as e:
                logger.error(f"班次判定失败: {e}")
                shift = "day"

        self._ensure_pool_initialized()

        async with self.pool.acquire() as conn:
            async with conn.transaction():

                # 锁定 soft reset
                current_soft_reset = False
                soft_reset_row = await conn.fetchrow(
                    """
                    SELECT is_soft_reset FROM daily_statistics
                    WHERE chat_id=$1 AND user_id=$2
                      AND record_date=$3 AND shift=$4
                    FOR UPDATE
                    """,
                    chat_id,
                    user_id,
                    business_date,
                    shift,
                )
                if soft_reset_row:
                    current_soft_reset = soft_reset_row["is_soft_reset"]

                # 锁定现有记录
                old_record = await conn.fetchrow(
                    """
                    SELECT fine_amount, checkin_time
                    FROM work_records
                    WHERE chat_id=$1 AND user_id=$2
                      AND record_date=$3 AND checkin_type=$4 AND shift=$5
                    FOR UPDATE
                    """,
                    chat_id,
                    user_id,
                    business_date,
                    checkin_type,
                    shift,
                )
                old_fine = old_record["fine_amount"] if old_record else 0
                old_checkin_time = old_record["checkin_time"] if old_record else None
                is_new_record = old_record is None

                # 上班打卡（工时计算用）
                start_row = None
                if checkin_type == "work_end":
                    start_row = await conn.fetchrow(
                        """
                        SELECT checkin_time FROM work_records
                        WHERE chat_id=$1 AND user_id=$2
                          AND record_date=$3 AND checkin_type='work_start'
                          AND shift=$4
                        FOR UPDATE
                        """,
                        chat_id,
                        user_id,
                        business_date,
                        shift,
                    )

                # 工时计算
                if checkin_type == "work_end" and start_row:
                    try:
                        start_dt = datetime.strptime(start_row["checkin_time"], "%H:%M")
                        end_dt = datetime.strptime(checkin_time, "%H:%M")
                        diff_minutes = (end_dt - start_dt).total_seconds() / 60
                        if diff_minutes < 0:
                            diff_minutes += 1440
                        work_duration_seconds = int(diff_minutes * 60)

                        if old_checkin_time:
                            old_end_dt = datetime.strptime(old_checkin_time, "%H:%M")
                            old_diff = (old_end_dt - start_dt).total_seconds() / 60
                            if old_diff < 0:
                                old_diff += 1440
                            old_duration = int(old_diff * 60)
                            work_duration_delta = work_duration_seconds - old_duration
                        else:
                            work_duration_delta = work_duration_seconds
                    except Exception as e:
                        logger.error(f"工时计算失败: {e}")
                        work_duration_seconds = 0
                        work_duration_delta = 0

                # 插入/更新 work_records
                await conn.execute(
                    """
                    INSERT INTO work_records
                    (chat_id, user_id, record_date, checkin_type,
                     checkin_time, status, time_diff_minutes,
                     fine_amount, shift, shift_detail)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                    ON CONFLICT (chat_id, user_id, record_date, checkin_type, shift)
                    DO UPDATE SET
                        checkin_time = EXCLUDED.checkin_time,
                        status = EXCLUDED.status,
                        time_diff_minutes = EXCLUDED.time_diff_minutes,
                        fine_amount = EXCLUDED.fine_amount,
                        shift_detail = EXCLUDED.shift_detail,
                        created_at = CURRENT_TIMESTAMP
                    """,
                    chat_id,
                    user_id,
                    business_date,
                    checkin_type,
                    checkin_time,
                    status,
                    time_diff_minutes,
                    fine_amount,
                    shift,
                    shift_detail,
                )

                fine_delta = fine_amount - old_fine

                # ========= 统一统计更新（无重复）=========

                # 1. 日统计 - 罚款（所有打卡）
                if fine_delta != 0:
                    await conn.execute(
                        """
                        INSERT INTO daily_statistics
                        (chat_id, user_id, record_date, activity_name,
                         accumulated_time, is_soft_reset, shift)
                        VALUES ($1,$2,$3,$4,$5,$6,$7)
                        ON CONFLICT (chat_id, user_id, record_date, activity_name, is_soft_reset, shift)
                        DO UPDATE SET
                            accumulated_time = daily_statistics.accumulated_time + EXCLUDED.accumulated_time,
                            updated_at = CURRENT_TIMESTAMP
                        """,
                        chat_id,
                        user_id,
                        business_date,
                        f"{checkin_type}_fines",
                        fine_delta,
                        current_soft_reset,
                        shift,
                    )

                # 2. 日统计 - 班次计数器（新记录才加）
                if is_new_record:
                    await conn.execute(
                        """
                        INSERT INTO daily_statistics
                        (chat_id, user_id, record_date, activity_name,
                         activity_count, is_soft_reset, shift)
                        VALUES ($1,$2,$3,$4,1,$5,$6)
                        ON CONFLICT (chat_id, user_id, record_date, activity_name, is_soft_reset, shift)
                        DO UPDATE SET
                            activity_count = daily_statistics.activity_count + 1,
                            updated_at = CURRENT_TIMESTAMP
                        """,
                        chat_id,
                        user_id,
                        business_date,
                        f"{checkin_type}_count",
                        current_soft_reset,
                        shift,
                    )

                # 3. 下班打卡特殊统计
                if checkin_type == "work_end":
                    # 3.1 工作天数（新记录才加）
                    if is_new_record:
                        await conn.execute(
                            """
                            INSERT INTO daily_statistics
                            (chat_id, user_id, record_date, activity_name,
                             activity_count, is_soft_reset, shift)
                            VALUES ($1,$2,$3,'work_days',1,$4,$5)
                            ON CONFLICT (chat_id, user_id, record_date, activity_name, is_soft_reset, shift)
                            DO UPDATE SET
                                activity_count = daily_statistics.activity_count + 1,
                                updated_at = CURRENT_TIMESTAMP
                            """,
                            chat_id,
                            user_id,
                            business_date,
                            current_soft_reset,
                            shift,
                        )

                    # 3.2 工时（使用差值）
                    if work_duration_delta != 0:
                        await conn.execute(
                            """
                            INSERT INTO daily_statistics
                            (chat_id, user_id, record_date, activity_name,
                             accumulated_time, is_soft_reset, shift)
                            VALUES ($1,$2,$3,'work_hours',$4,$5,$6)
                            ON CONFLICT (chat_id, user_id, record_date, activity_name, is_soft_reset, shift)
                            DO UPDATE SET
                                accumulated_time = daily_statistics.accumulated_time + EXCLUDED.accumulated_time,
                                updated_at = CURRENT_TIMESTAMP
                            """,
                            chat_id,
                            user_id,
                            business_date,
                            work_duration_delta,
                            current_soft_reset,
                            shift,
                        )

                # 4. 月统计 - 班次计数器（新记录才加）
                if is_new_record:
                    await conn.execute(
                        """
                        INSERT INTO monthly_statistics
                        (chat_id, user_id, statistic_date, activity_name,
                         activity_count, shift)
                        VALUES ($1,$2,$3,$4,1,$5)
                        ON CONFLICT (chat_id, user_id, statistic_date, activity_name, shift)
                        DO UPDATE SET
                            activity_count = monthly_statistics.activity_count + 1,
                            updated_at = CURRENT_TIMESTAMP
                        """,
                        chat_id,
                        user_id,
                        statistic_date,
                        f"{checkin_type}_count",
                        shift,
                    )

                # 5. 月统计 - 下班打卡特殊统计
                if checkin_type == "work_end":
                    # 5.1 工作天数（新记录才加）
                    if is_new_record:
                        await conn.execute(
                            """
                            INSERT INTO monthly_statistics
                            (chat_id, user_id, statistic_date, activity_name,
                             activity_count, shift)
                            VALUES ($1,$2,$3,'work_days',1,$4)
                            ON CONFLICT (chat_id, user_id, statistic_date, activity_name, shift)
                            DO UPDATE SET
                                activity_count = monthly_statistics.activity_count + 1,
                                updated_at = CURRENT_TIMESTAMP
                            """,
                            chat_id,
                            user_id,
                            statistic_date,
                            shift,
                        )
                    # 5.2 工时（使用差值）
                    if work_duration_delta != 0:
                        await conn.execute(
                            """
                            INSERT INTO monthly_statistics
                            (chat_id, user_id, statistic_date, activity_name,
                             accumulated_time, shift)
                            VALUES ($1,$2,$3,'work_hours',$4,$5)
                            ON CONFLICT (chat_id, user_id, statistic_date, activity_name, shift)
                            DO UPDATE SET
                                accumulated_time = monthly_statistics.accumulated_time + EXCLUDED.accumulated_time,
                                updated_at = CURRENT_TIMESTAMP
                            """,
                            chat_id,
                            user_id,
                            statistic_date,
                            work_duration_delta,
                            shift,
                        )

                # 6. 月统计 - 罚款（所有打卡）
                if fine_delta != 0:
                    await conn.execute(
                        """
                        INSERT INTO monthly_statistics
                        (chat_id, user_id, statistic_date, activity_name,
                         accumulated_time, shift)
                        VALUES ($1,$2,$3,$4,$5,$6)
                        ON CONFLICT (chat_id, user_id, statistic_date, activity_name, shift)
                        DO UPDATE SET
                            accumulated_time = monthly_statistics.accumulated_time + EXCLUDED.accumulated_time,
                            updated_at = CURRENT_TIMESTAMP
                        """,
                        chat_id,
                        user_id,
                        statistic_date,
                        f"{checkin_type}_fines",
                        fine_delta,
                        shift,
                    )

                # 7. 更新用户总罚款
                if fine_delta != 0:
                    await conn.execute(
                        """
                        UPDATE users
                        SET total_fines = total_fines + $1
                        WHERE chat_id=$2 AND user_id=$3
                        """,
                        fine_delta,
                        chat_id,
                        user_id,
                    )

        self._cache.pop(f"user:{chat_id}:{user_id}", None)

        logger.info(
            f"✅ [上下班记录完成] 用户:{user_id} | 业务日期:{business_date} | "
            f"类型:{checkin_type} | 班次:{shift} | 时间:{checkin_time} | 状态:{status} | "
            f"罚款:{fine_amount}(Δ:{fine_delta}) | 工时:{work_duration_seconds}s(Δ:{work_duration_delta}) | "
            f"新记录:{is_new_record} | 软重置:{current_soft_reset}"
        )

    async def get_work_records_by_shift(
        self,
        chat_id: int,
        user_id: int,
        shift: str = None,
        start_date: Optional[date] = None,  # 🆕 新增开始日期
        end_date: Optional[date] = None,  # 🆕 新增结束日期
    ) -> Dict[str, List[Dict[str, Any]]]:
        """获取用户上下班记录（支持按班次过滤和日期范围）"""

        if start_date is None:
            start_date = await self.get_business_date(chat_id)
        if end_date is None:
            end_date = start_date

        query = """
            SELECT checkin_type, checkin_time, status, time_diff_minutes, 
                   fine_amount, shift, created_at, record_date
            FROM work_records 
            WHERE chat_id = $1 AND user_id = $2 
              AND record_date >= $3 AND record_date <= $4
        """
        params = [chat_id, user_id, start_date, end_date]

        if shift:
            # 将 "白班"/"夜班" 转换为 "day"/"night"
            if shift in ["day", "白班"]:
                shift_value = "day"
            elif shift in ["night", "夜班", "night_last", "night_tonight"]:
                shift_value = "night"
            else:
                raise ValueError(f"❌ 无效的班次值: {shift}")

            query += " AND shift = $5"
            params.append(shift_value)

        query += " ORDER BY created_at DESC"

        rows = await self.execute_with_retry(
            "按班次获取工作记录", query, *params, fetch=True
        )

        records: Dict[str, List[Dict[str, Any]]] = {}
        if rows:
            for row in rows:
                checkin_type = row["checkin_type"]
                if checkin_type not in records:
                    records[checkin_type] = []
                records[checkin_type].append(dict(row))

        return records

    # 在 database.py 中添加修复后的函数
    async def get_today_work_records_fixed(
        self, chat_id: int, user_id: int
    ) -> Dict[str, Dict]:
        """修复版：获取用户今天的上下班记录 - 每个群组独立重置时间"""
        try:
            # 获取群组重置时间
            group_data = await self.get_group_cached(chat_id)
            reset_hour = group_data.get("reset_hour", Config.DAILY_RESET_HOUR)
            reset_minute = group_data.get("reset_minute", Config.DAILY_RESET_MINUTE)

            now = self.get_beijing_time()

            # 计算今天的重置时间点
            reset_time_today = now.replace(
                hour=reset_hour, minute=reset_minute, second=0, microsecond=0
            )

            # 确定当前重置周期的开始时间
            if now < reset_time_today:
                period_start = reset_time_today - timedelta(days=1)
            else:
                period_start = reset_time_today

            # 查询从重置周期开始到现在的记录
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT * FROM work_records 
                    WHERE chat_id = $1 
                    AND user_id = $2 
                    AND record_date >= $3
                    AND record_date <= $4
                    ORDER BY record_date DESC, checkin_type
                    """,
                    chat_id,
                    user_id,
                    period_start.date(),
                    now.date(),  # 添加上限，避免查询未来日期
                )

                records = {}
                for row in rows:
                    # 按记录日期分组，只取每个类型的最新记录
                    record_key = f"{row['record_date']}_{row['checkin_type']}"
                    if (
                        row["checkin_type"] not in records
                        or row["record_date"]
                        > records[row["checkin_type"]]["record_date"]
                    ):
                        records[row["checkin_type"]] = dict(row)

                logger.debug(
                    f"工作记录查询: {chat_id}-{user_id}, 重置周期: {period_start.date()}, 记录数: {len(records)}"
                )
                return records

        except Exception as e:
            logger.error(f"获取工作记录失败 {chat_id}-{user_id}: {e}")
            return {}

    # ========== 活动配置操作 ==========
    async def get_activity_limits(self) -> Dict:
        """获取所有活动限制 - 优化版"""
        # 使用更长的缓存时间，因为这些数据不常变化
        cache_key = "activity_limits"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        # 检查数据库连接状态
        if not await self._ensure_healthy_connection():
            logger.warning("数据库连接不健康，返回默认活动配置")
            return Config.DEFAULT_ACTIVITY_LIMITS.copy()

        try:
            # 使用更快的查询，只获取需要的字段
            rows = await self.execute_with_retry(
                "获取活动限制",
                "SELECT activity_name, max_times, time_limit FROM activity_configs",
                fetch=True,  # 👈 只需要添加这个参数
            )
            limits = {
                row["activity_name"]: {
                    "max_times": row["max_times"],
                    "time_limit": row["time_limit"],
                }
                for row in rows
            }
            # 设置较长缓存时间，因为活动配置不常变化
            self._set_cached(cache_key, limits, 600)  # 10分钟缓存
            return limits
        except Exception as e:
            logger.error(f"获取活动配置失败: {e}，返回默认配置")
            return Config.DEFAULT_ACTIVITY_LIMITS.copy()

    async def get_activity_limits_cached(self) -> Dict:
        """带缓存的获取活动限制"""
        try:
            return await self.get_activity_limits()
        except Exception as e:
            logger.error(f"获取活动配置缓存失败: {e}，返回默认配置")
            return Config.DEFAULT_ACTIVITY_LIMITS.copy()

    async def get_activity_time_limit(self, activity: str) -> int:
        """获取活动时间限制"""
        limits = await self.get_activity_limits()
        return limits.get(activity, {}).get("time_limit", 0)

    async def get_activity_max_times(self, activity: str) -> int:
        """获取活动最大次数"""
        limits = await self.get_activity_limits()
        return limits.get(activity, {}).get("max_times", 0)

    async def activity_exists(self, activity: str) -> bool:
        """检查活动是否存在"""
        cache_key = "activity_limits"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return activity in cached

        self._ensure_pool_initialized()
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT 1 FROM activity_configs WHERE activity_name = $1", activity
            )
            return row is not None

    async def update_activity_config(
        self, activity: str, max_times: int, time_limit: int
    ):
        """更新活动配置"""
        self._ensure_pool_initialized()
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO activity_configs (activity_name, max_times, time_limit)
                VALUES ($1, $2, $3)
                ON CONFLICT (activity_name) 
                DO UPDATE SET 
                    max_times = EXCLUDED.max_times,
                    time_limit = EXCLUDED.time_limit,
                    created_at = CURRENT_TIMESTAMP
                """,
                activity,
                max_times,
                time_limit,
            )
        self._cache.pop("activity_limits", None)

    async def delete_activity_config(self, activity: str):
        """删除活动配置"""
        self._ensure_pool_initialized()
        async with self.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM activity_configs WHERE activity_name = $1", activity
            )
            await conn.execute(
                "DELETE FROM fine_configs WHERE activity_name = $1", activity
            )
        self._cache.pop("activity_limits", None)

    # ========== 罚款配置操作 ==========
    async def get_fine_rates(self) -> Dict:
        """获取所有罚款费率"""
        self._ensure_pool_initialized()
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM fine_configs")
            fines = {}
            for row in rows:
                activity = row["activity_name"]
                if activity not in fines:
                    fines[activity] = {}
                fines[activity][row["time_segment"]] = row["fine_amount"]
            return fines

    async def get_fine_rates_for_activity(self, activity: str) -> Dict:
        """获取指定活动的罚款费率"""
        self._ensure_pool_initialized()
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT time_segment, fine_amount FROM fine_configs WHERE activity_name = $1",
                activity,
            )
            return {row["time_segment"]: row["fine_amount"] for row in rows}

    async def update_fine_config(
        self, activity: str, time_segment: str, fine_amount: int
    ):
        """更新罚款配置"""
        self._ensure_pool_initialized()
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO fine_configs (activity_name, time_segment, fine_amount)
                VALUES ($1, $2, $3)
                ON CONFLICT (activity_name, time_segment) 
                DO UPDATE SET 
                    fine_amount = EXCLUDED.fine_amount,
                    created_at = CURRENT_TIMESTAMP
                """,
                activity,
                time_segment,
                fine_amount,
            )

    async def calculate_fine_for_activity(
        self, activity: str, overtime_minutes: float
    ) -> int:
        """计算活动罚款金额 - 数据库内部版本"""
        fine_rates = await self.get_fine_rates_for_activity(activity)
        if not fine_rates:
            return 0

        # 处理罚款时间段
        segments = []
        for time_key in fine_rates.keys():
            try:
                if isinstance(time_key, str) and "min" in time_key.lower():
                    time_value = int(time_key.lower().replace("min", "").strip())
                else:
                    time_value = int(time_key)
                segments.append(time_value)
            except (ValueError, TypeError):
                continue

        if not segments:
            return 0

        segments.sort()

        applicable_fine = 0
        for segment in segments:
            if overtime_minutes <= segment:
                original_key = str(segment)
                if original_key not in fine_rates:
                    original_key = f"{segment}min"
                applicable_fine = fine_rates.get(original_key, 0)
                break

        if applicable_fine == 0 and segments:
            max_segment = segments[-1]
            original_key = str(max_segment)
            if original_key not in fine_rates:
                original_key = f"{max_segment}min"
            applicable_fine = fine_rates.get(original_key, 0)

        return applicable_fine

    async def get_work_fine_rates(self) -> Dict:
        """获取上下班罚款费率"""
        self._ensure_pool_initialized()
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM work_fine_configs")
            fines = {}
            for row in rows:
                checkin_type = row["checkin_type"]
                if checkin_type not in fines:
                    fines[checkin_type] = {}
                fines[checkin_type][row["time_segment"]] = row["fine_amount"]
            return fines

    async def get_work_fine_rates_for_type(self, checkin_type: str) -> Dict:
        """获取指定类型的上下班罚款费率"""
        self._ensure_pool_initialized()
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT time_segment, fine_amount FROM work_fine_configs WHERE checkin_type = $1",
                checkin_type,
            )
            return {row["time_segment"]: row["fine_amount"] for row in rows}

    async def update_work_fine_rate(
        self, checkin_type: str, time_segment: str, fine_amount: int
    ):
        """更新上下班罚款费率"""
        self._ensure_pool_initialized()
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO work_fine_configs (checkin_type, time_segment, fine_amount)
                VALUES ($1, $2, $3)
                ON CONFLICT (checkin_type, time_segment)
                DO UPDATE SET fine_amount = EXCLUDED.fine_amount
                """,
                checkin_type,
                time_segment,
                fine_amount,
            )

    async def clear_work_fine_rates(self, checkin_type: str):
        """清空上下班罚款配置"""
        self._ensure_pool_initialized()
        async with self.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM work_fine_configs WHERE checkin_type = $1", checkin_type
            )

    # ========== 推送设置操作 ==========
    async def get_push_settings(self) -> Dict:
        """获取推送设置"""
        cache_key = "push_settings"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        self._ensure_pool_initialized()
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM push_settings")
            settings = {row["setting_key"]: bool(row["setting_value"]) for row in rows}
            self._set_cached(cache_key, settings, 300)
            return settings

    async def update_push_setting(self, key: str, value: bool):
        """更新推送设置"""
        self._ensure_pool_initialized()
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO push_settings (setting_key, setting_value)
                VALUES ($1, $2)
                ON CONFLICT (setting_key) 
                DO UPDATE SET 
                    setting_value = EXCLUDED.setting_value,
                    created_at = CURRENT_TIMESTAMP
                """,
                key,
                1 if value else 0,
            )
        self._cache.pop("push_settings", None)

    # ========== 统计和导出相关 ==========
    async def get_group_statistics(
        self, chat_id: int, target_date: Optional[date] = None
    ) -> List[Dict]:
        """获取群组统计信息 - 修复版：包含所有用户（包括未打卡）"""

        if target_date is None:
            target_date = await self.get_business_date(chat_id)

        self._ensure_pool_initialized()
        async with self.pool.acquire() as conn:
            # ===== 1. 先获取群组所有成员 =====
            all_users = await conn.fetch(
                """
                SELECT user_id, nickname 
                FROM users 
                WHERE chat_id = $1
                """,
                chat_id,
            )

            if not all_users:
                logger.info(f"群组 {chat_id} 没有用户")
                return []

            # ===== 2. 提前获取班次配置（只查一次）=====
            shift_config = await self.get_shift_config(chat_id)
            has_dual_mode = shift_config.get("dual_mode", False)

            # ===== 3. 获取有活动记录的用户统计（原有复杂查询保持不变）=====
            rows = await conn.fetch(
                """
                WITH user_stats AS (
                    SELECT 
                        ds.user_id,
                        ds.shift,
                        ds.is_soft_reset,
                        MAX(u.nickname) as nickname,
                        
                        -- 活动统计
                        SUM(CASE WHEN ds.activity_name NOT IN (
                            'work_days','work_hours',
                            'work_fines','work_start_fines','work_end_fines',
                            'overtime_count','overtime_time','total_fines'
                        ) THEN ds.activity_count ELSE 0 END) AS total_activity_count,
                        
                        SUM(CASE WHEN ds.activity_name NOT IN (
                            'work_days','work_hours',
                            'work_fines','work_start_fines','work_end_fines',
                            'overtime_count','overtime_time','total_fines'
                        ) THEN ds.accumulated_time ELSE 0 END) AS total_accumulated_time,
                        
                        -- 罚款统计
                        SUM(CASE WHEN ds.activity_name IN (
                            'total_fines', 
                            'work_fines', 
                            'work_start_fines', 
                            'work_end_fines'
                        ) THEN ds.accumulated_time ELSE 0 END) AS total_fines,
                        
                        -- 超时统计
                        SUM(CASE WHEN ds.activity_name = 'overtime_count'
                                 THEN ds.activity_count ELSE 0 END) AS overtime_count,
                        SUM(CASE WHEN ds.activity_name = 'overtime_time'
                                 THEN ds.accumulated_time ELSE 0 END) AS total_overtime_time
                        
                    FROM daily_statistics ds
                    LEFT JOIN users u 
                        ON ds.chat_id = u.chat_id 
                        AND ds.user_id = u.user_id
                    WHERE ds.chat_id = $1 
                      AND ds.record_date = $2
                    GROUP BY ds.user_id, ds.shift, ds.is_soft_reset
                ),
                
                activity_details AS (
                    SELECT
                        ds.user_id,
                        ds.shift,
                        ds.is_soft_reset,
                        ds.activity_name,
                        SUM(ds.activity_count) AS total_count,
                        SUM(ds.accumulated_time) AS total_time
                    FROM daily_statistics ds
                    WHERE ds.chat_id = $1 
                      AND ds.record_date = $2
                      AND ds.activity_name NOT IN (
                            'work_days','work_hours',
                            'work_fines','work_start_fines','work_end_fines',
                            'overtime_count','overtime_time','total_fines'
                      )
                    GROUP BY ds.user_id, ds.shift, ds.is_soft_reset, ds.activity_name
                ),
                
                work_stats AS (
                    SELECT
                        ds.user_id,
                        ds.shift,
                        ds.is_soft_reset,
                        MAX(CASE WHEN ds.activity_name = 'work_days'
                                 THEN ds.activity_count ELSE 0 END) AS work_days,
                        MAX(CASE WHEN ds.activity_name = 'work_hours'
                                 THEN ds.accumulated_time ELSE 0 END) AS work_hours
                    FROM daily_statistics ds
                    WHERE ds.chat_id = $1 
                      AND ds.record_date = $2
                      AND ds.activity_name IN ('work_days','work_hours')
                    GROUP BY ds.user_id, ds.shift, ds.is_soft_reset
                )
                
                SELECT 
                    us.user_id,
                    us.shift,
                    us.is_soft_reset,
                    us.nickname,
                    COALESCE(us.total_activity_count, 0) AS total_activity_count,
                    COALESCE(us.total_accumulated_time, 0) AS total_accumulated_time,
                    COALESCE(us.total_fines, 0) AS total_fines,
                    COALESCE(us.overtime_count, 0) AS overtime_count,
                    COALESCE(us.total_overtime_time, 0) AS total_overtime_time,
                    COALESCE(ws.work_days, 0) AS work_days,
                    COALESCE(ws.work_hours, 0) AS work_hours,
                    
                    jsonb_object_agg(
                        ad.activity_name,
                        jsonb_build_object(
                            'count', ad.total_count,
                            'time', ad.total_time
                        )
                    ) FILTER (WHERE ad.activity_name IS NOT NULL) AS activities
                    
                FROM user_stats us
                LEFT JOIN activity_details ad
                    ON us.user_id = ad.user_id
                    AND us.shift = ad.shift
                    AND us.is_soft_reset = ad.is_soft_reset
                LEFT JOIN work_stats ws
                    ON us.user_id = ws.user_id
                    AND us.shift = ws.shift
                    AND us.is_soft_reset = ws.is_soft_reset
                    
                GROUP BY us.user_id, us.shift, us.is_soft_reset, us.nickname,
                         us.total_activity_count, us.total_accumulated_time,
                         us.total_fines, us.overtime_count, us.total_overtime_time,
                         ws.work_days, ws.work_hours
                         
                ORDER BY us.user_id ASC, us.shift ASC, us.is_soft_reset ASC
                """,
                chat_id,
                target_date,
            )

            # ===== 4. 将查询结果转为字典方便查找 =====
            stats_dict = {}
            for row in rows:
                user_id = row["user_id"]
                if user_id not in stats_dict:
                    stats_dict[user_id] = []
                stats_dict[user_id].append(dict(row))

            # ===== 5. 为每个用户构建统计数据 =====
            result = []
            for user in all_users:
                user_id = user["user_id"]
                base_nickname = user["nickname"] or f"用户{user_id}"

                if user_id in stats_dict:
                    # 有活动记录的用户，使用查询结果（可能有多个班次）
                    for stat in stats_dict[user_id]:
                        data = stat.copy()  # 复制一份避免修改原数据

                        # 确保 nickname 使用最新的
                        data["nickname"] = base_nickname

                        # 确保班次字段存在
                        if "shift" not in data or data["shift"] is None:
                            data["shift"] = "day"

                        # 确保布尔值转换
                        is_soft_reset = data.get("is_soft_reset", False)
                        if isinstance(is_soft_reset, str):
                            data["is_soft_reset"] = is_soft_reset.lower() in (
                                "true",
                                "t",
                                "1",
                                "yes",
                            )
                        else:
                            data["is_soft_reset"] = bool(is_soft_reset)

                        # JSON 解析
                        raw_activities = data.get("activities")
                        parsed_activities = {}

                        if raw_activities:
                            if isinstance(raw_activities, str):
                                try:
                                    parsed_activities = json.loads(raw_activities)
                                except Exception as e:
                                    self.logger.error(f"JSON解析失败: {e}")
                            elif isinstance(raw_activities, dict):
                                parsed_activities = raw_activities

                        data["activities"] = parsed_activities
                        result.append(data)
                else:
                    # ===== 6. 没有活动记录的用户，根据模式创建空数据 =====
                    if has_dual_mode:
                        # 双班模式：为白班和夜班各创建一条记录
                        for shift in ["day", "night"]:
                            empty_data = {
                                "user_id": user_id,
                                "nickname": base_nickname,
                                "shift": shift,
                                "is_soft_reset": False,
                                "total_activity_count": 0,
                                "total_accumulated_time": 0,
                                "total_fines": 0,
                                "overtime_count": 0,
                                "total_overtime_time": 0,
                                "work_days": 0,
                                "work_hours": 0,
                                "activities": {},
                            }
                            result.append(empty_data)
                    else:
                        # 单班模式：只创建一条白班记录
                        empty_data = {
                            "user_id": user_id,
                            "nickname": base_nickname,
                            "shift": "day",
                            "is_soft_reset": False,
                            "total_activity_count": 0,
                            "total_accumulated_time": 0,
                            "total_fines": 0,
                            "overtime_count": 0,
                            "total_overtime_time": 0,
                            "work_days": 0,
                            "work_hours": 0,
                            "activities": {},
                        }
                        result.append(empty_data)

            # ===== 7. 按 user_id 和 shift 排序 =====
            result.sort(key=lambda x: (x["user_id"], x["shift"]))

            active_users = len(stats_dict)
            total_users = len(all_users)
            logger.info(
                f"数据库查询返回 {len(result)} 条记录 "
                f"（包含所有 {total_users} 个用户，其中 {active_users} 个有活动记录，"
                f"{total_users - active_users} 个未打卡）"
            )
            return result

    async def get_all_groups(self) -> List[int]:
        """获取所有群组ID"""
        self._ensure_pool_initialized()
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT chat_id FROM groups")
            return [row["chat_id"] for row in rows]

    async def get_group_members(self, chat_id: int) -> List[Dict]:
        """获取群组成员"""
        today = await self.get_business_date(chat_id)
        self._ensure_pool_initialized()
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT 
                    user_id, 
                    nickname, 
                    current_activity, 
                    activity_start_time, 
                    total_accumulated_time, 
                    total_activity_count, 
                    total_fines, 
                    overtime_count, 
                    total_overtime_time 
                FROM users 
                WHERE chat_id = $1 AND last_updated = $2
                """,
                chat_id,
                today,
            )
            return [dict(row) for row in rows]

    # ========== 月度统计 ==========
    async def get_monthly_statistics(
        self, chat_id: int, year: int = None, month: int = None
    ) -> List[Dict]:
        """
        增强版月度统计 - 正确处理跨天夜班的工作时长
        直接从 user_activities, work_records, daily_statistics 查询
        """
        if year is None or month is None:
            today = self.get_beijing_time()
            year = today.year
            month = today.month

        # 计算月份范围
        month_start = date(year, month, 1)
        if month == 12:
            month_end = date(year + 1, 1, 1)
        else:
            month_end = date(year, month + 1, 1)

        logger.info(
            f"📊 获取月度统计: {year}年{month}月, 范围 {month_start} 到 {month_end}"
        )

        self._ensure_pool_initialized()

        async with self.pool.acquire() as conn:
            # ===== 1. 获取该月有活动的所有用户 =====
            users = await conn.fetch(
                """
                SELECT DISTINCT user_id 
                FROM user_activities 
                WHERE chat_id = $1 
                AND activity_date >= $2 
                AND activity_date < $3
                UNION
                SELECT DISTINCT user_id 
                FROM work_records 
                WHERE chat_id = $1 
                AND record_date >= $2 
                AND record_date < $3
                """,
                chat_id,
                month_start,
                month_end,
            )

            result = []

            for user_row in users:
                user_id = user_row["user_id"]

                # ===== 2. 获取用户昵称 =====
                user_info = await conn.fetchrow(
                    "SELECT nickname FROM users WHERE chat_id = $1 AND user_id = $2",
                    chat_id,
                    user_id,
                )
                nickname = user_info["nickname"] if user_info else f"用户{user_id}"

                # ===== 3. 获取活动统计 =====
                activities_rows = await conn.fetch(
                    """
                    SELECT 
                        activity_name,
                        SUM(activity_count) as total_count,
                        SUM(accumulated_time) as total_time
                    FROM user_activities 
                    WHERE chat_id = $1 
                      AND user_id = $2 
                      AND activity_date >= $3 
                      AND activity_date < $4
                    GROUP BY activity_name
                    """,
                    chat_id,
                    user_id,
                    month_start,
                    month_end,
                )

                activities = {}
                total_activity_count = 0
                total_accumulated_time = 0

                for row in activities_rows:
                    activities[row["activity_name"]] = {
                        "count": row["total_count"],
                        "time": row["total_time"],
                    }
                    total_activity_count += row["total_count"]
                    total_accumulated_time += row["total_time"]

                # ===== 4. 获取罚款统计 =====
                fines = (
                    await conn.fetchval(
                        """
                    SELECT SUM(accumulated_time)
                    FROM daily_statistics
                    WHERE chat_id = $1
                      AND user_id = $2
                      AND record_date >= $3
                      AND record_date < $4
                      AND activity_name IN ('total_fines', 'work_fines', 
                                           'work_start_fines', 'work_end_fines')
                    """,
                        chat_id,
                        user_id,
                        month_start,
                        month_end,
                    )
                    or 0
                )

                # ===== 5. 获取超时统计 =====
                overtime = await conn.fetchrow(
                    """
                    SELECT 
                        SUM(CASE WHEN activity_name = 'overtime_count' 
                            THEN activity_count ELSE 0 END) as overtime_count,
                        SUM(CASE WHEN activity_name = 'overtime_time' 
                            THEN accumulated_time ELSE 0 END) as overtime_time
                    FROM daily_statistics
                    WHERE chat_id = $1
                      AND user_id = $2
                      AND record_date >= $3
                      AND record_date < $4
                    """,
                    chat_id,
                    user_id,
                    month_start,
                    month_end,
                )

                overtime_count = overtime["overtime_count"] if overtime else 0
                total_overtime_time = overtime["overtime_time"] if overtime else 0

                # ===== 6. ⭐ 关键修复：计算工作时长（处理跨天夜班）=====

                # 6.1 白班工作时长（直接从 daily_statistics 汇总）
                day_work = await conn.fetchrow(
                    """
                    SELECT 
                        SUM(CASE WHEN activity_name = 'work_days' 
                            THEN activity_count ELSE 0 END) as work_days,
                        SUM(CASE WHEN activity_name = 'work_hours' 
                            THEN accumulated_time ELSE 0 END) as work_hours
                    FROM daily_statistics
                    WHERE chat_id = $1
                      AND user_id = $2
                      AND record_date >= $3
                      AND record_date < $4
                      AND shift = 'day'
                    """,
                    chat_id,
                    user_id,
                    month_start,
                    month_end,
                )

                work_days = day_work["work_days"] if day_work else 0
                work_hours = day_work["work_hours"] if day_work else 0

                # 6.2 夜班工作时长（处理跨天）
                night_shifts = await conn.fetch(
                    """
                    SELECT 
                        wr1.record_date as start_date,
                        wr1.checkin_time as start_time,
                        wr2.record_date as end_date,
                        wr2.checkin_time as end_time
                    FROM work_records wr1
                    LEFT JOIN work_records wr2 ON 
                        wr1.chat_id = wr2.chat_id 
                        AND wr1.user_id = wr2.user_id
                        AND wr1.shift = wr2.shift
                        AND wr1.checkin_type = 'work_start'
                        AND wr2.checkin_type = 'work_end'
                    WHERE wr1.chat_id = $1
                      AND wr1.user_id = $2
                      AND wr1.shift = 'night'
                      AND wr1.record_date >= $3
                      AND wr1.record_date < $4
                    """,
                    chat_id,
                    user_id,
                    month_start,
                    month_end,
                )

                night_work_days = 0
                night_work_hours = 0

                # ===== 修复1：正确计算月份开始和结束时间 =====
                month_start_dt = beijing_tz.localize(
                    datetime.combine(month_start, datetime.min.time())
                )
                month_end_dt = beijing_tz.localize(
                    datetime.combine(month_end, datetime.min.time())
                )

                for shift in night_shifts:
                    if shift["end_date"] and shift["end_time"]:
                        start_dt = datetime.combine(
                            shift["start_date"],
                            datetime.strptime(shift["start_time"], "%H:%M").time(),
                        ).replace(tzinfo=beijing_tz)
                        end_dt = datetime.combine(
                            shift["end_date"],
                            datetime.strptime(shift["end_time"], "%H:%M").time(),
                        ).replace(tzinfo=beijing_tz)

                        # 处理跨天
                        if end_dt < start_dt:
                            end_dt += timedelta(days=1)

                        # 只计算在本月内的部分
                        work_start = max(start_dt, month_start_dt)
                        work_end = min(end_dt, month_end_dt)

                        if work_end > work_start:
                            night_work_hours += int(
                                (work_end - work_start).total_seconds()
                            )
                            night_work_days += 1

                # ===== 修复2：确保所有值都不是 None 再计算 =====
                work_days = work_days or 0
                night_work_days = night_work_days or 0
                work_hours = work_hours or 0
                night_work_hours = night_work_hours or 0

                # ===== 7. 合并白班和夜班的工作统计 =====
                total_work_days = work_days + night_work_days
                total_work_hours = work_hours + night_work_hours

                # ===== 8. 获取上班/下班次数统计 =====
                work_counts = await conn.fetchrow(
                    """
                    SELECT 
                        COUNT(CASE WHEN checkin_type = 'work_start' THEN 1 END) as work_start_count,
                        COUNT(CASE WHEN checkin_type = 'work_end' THEN 1 END) as work_end_count,
                        SUM(CASE WHEN checkin_type = 'work_start' THEN fine_amount ELSE 0 END) as work_start_fines,
                        SUM(CASE WHEN checkin_type = 'work_end' THEN fine_amount ELSE 0 END) as work_end_fines
                    FROM work_records
                    WHERE chat_id = $1
                      AND user_id = $2
                      AND record_date >= $3
                      AND record_date < $4
                    """,
                    chat_id,
                    user_id,
                    month_start,
                    month_end,
                )

                # ===== 9. 获取迟到早退次数 =====
                late_early = await self.get_user_late_early_counts(
                    chat_id, user_id, year, month
                )

                # ===== 10. 构建返回数据 =====
                user_data = {
                    "user_id": user_id,
                    "nickname": nickname,
                    "total_activity_count": total_activity_count,
                    "total_accumulated_time": total_accumulated_time,
                    "total_fines": fines,
                    "overtime_count": overtime_count,
                    "total_overtime_time": total_overtime_time,
                    "work_days": total_work_days,
                    "work_hours": total_work_hours,
                    "work_start_count": (
                        work_counts["work_start_count"] if work_counts else 0
                    ),
                    "work_end_count": (
                        work_counts["work_end_count"] if work_counts else 0
                    ),
                    "work_start_fines": (
                        work_counts["work_start_fines"] if work_counts else 0
                    ),
                    "work_end_fines": (
                        work_counts["work_end_fines"] if work_counts else 0
                    ),
                    "late_count": late_early.get("late_count", 0),
                    "early_count": late_early.get("early_count", 0),
                    "activities": activities,
                }

                result.append(user_data)

            logger.info(f"✅ 月度统计完成: {len(result)} 个用户")
            return result

    async def get_monthly_work_statistics(
        self, chat_id: int, year: int = None, month: int = None
    ) -> List[Dict]:
        """获取月度上下班统计"""
        if year is None or month is None:
            today = self.get_beijing_time()
            year = today.year
            month = today.month

        start_date = date(year, month, 1)
        if month == 12:
            end_date = date(year + 1, 1, 1)
        else:
            end_date = date(year, month + 1, 1)

        self._ensure_pool_initialized()
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT 
                    wr.user_id,
                    u.nickname,
                    COUNT(CASE WHEN wr.checkin_type = 'work_start' THEN 1 END) as work_start_count,
                    COUNT(CASE WHEN wr.checkin_type = 'work_end' THEN 1 END) as work_end_count,
                    SUM(CASE WHEN wr.checkin_type = 'work_start' THEN wr.fine_amount ELSE 0 END) as work_start_fines,
                    SUM(CASE WHEN wr.checkin_type = 'work_end' THEN wr.fine_amount ELSE 0 END) as work_end_fines
                FROM work_records wr
                JOIN users u ON wr.chat_id = u.chat_id AND wr.user_id = u.user_id
                WHERE wr.chat_id = $1 AND wr.record_date >= $2 AND wr.record_date < $3
                GROUP BY wr.user_id, u.nickname
                """,
                chat_id,
                start_date,
                end_date,
            )
            return [dict(row) for row in rows]

    async def get_monthly_activity_ranking(
        self, chat_id: int, year: int = None, month: int = None
    ) -> Dict[str, List]:
        """获取月度活动排行榜"""
        if year is None or month is None:
            today = self.get_beijing_time()
            year = today.year
            month = today.month

        statistic_date = date(year, month, 1)
        activity_limits = await self.get_activity_limits()

        self._ensure_pool_initialized()
        async with self.pool.acquire() as conn:
            rankings = {}
            for activity in activity_limits.keys():
                rows = await conn.fetch(
                    """
                    SELECT 
                        ms.user_id,
                        u.nickname,
                        ms.accumulated_time as total_time,
                        ms.activity_count as total_count
                    FROM monthly_statistics ms
                    JOIN users u ON ms.chat_id = u.chat_id AND ms.user_id = u.user_id
                    WHERE ms.chat_id = $1 AND ms.activity_name = $2 
                        AND ms.statistic_date = $3
                    ORDER BY ms.accumulated_time DESC
                    LIMIT 10
                    """,
                    chat_id,
                    activity,
                    statistic_date,
                )
                rankings[activity] = [dict(row) for row in rows]
            return rankings

    async def get_user_late_early_counts(
        self, chat_id: int, user_id: int, year: int, month: int
    ) -> Dict[str, int]:
        """获取用户的迟到早退次数统计"""
        start_date = date(year, month, 1)
        if month == 12:
            end_date = date(year + 1, 1, 1)
        else:
            end_date = date(year, month + 1, 1)

        async with self.pool.acquire() as conn:
            # 获取迟到次数（上班时间差>0）
            late_count = (
                await conn.fetchval(
                    """
                SELECT COUNT(*) FROM work_records 
                WHERE chat_id = $1 AND user_id = $2 
                AND record_date >= $3 AND record_date < $4
                AND checkin_type = 'work_start' AND time_diff_minutes > 0
                """,
                    chat_id,
                    user_id,
                    start_date,
                    end_date,
                )
                or 0
            )

            # 获取早退次数（下班时间差<0）
            early_count = (
                await conn.fetchval(
                    """
                SELECT COUNT(*) FROM work_records 
                WHERE chat_id = $1 AND user_id = $2 
                AND record_date >= $3 AND record_date < $4
                AND checkin_type = 'work_end' AND time_diff_minutes < 0
                """,
                    chat_id,
                    user_id,
                    start_date,
                    end_date,
                )
                or 0
            )

            return {"late_count": late_count, "early_count": early_count}

    # ========== 设置用户班次状态==========

    async def set_user_shift_state(
        self,
        chat_id: int,
        user_id: int,
        shift: str,
        record_date: date,
    ) -> bool:
        """
        设置用户班次状态（上班打卡）
        存在则更新时间，不存在则插入
        """
        try:
            now = self.get_beijing_time()
            await self.execute_with_retry(
                "设置用户班次状态",
                """
                INSERT INTO group_shift_state
                (chat_id, user_id, shift, record_date, shift_start_time)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (chat_id, user_id, shift)
                DO UPDATE SET
                    record_date = EXCLUDED.record_date,
                    shift_start_time = EXCLUDED.shift_start_time,
                    updated_at = CURRENT_TIMESTAMP
                """,
                chat_id,
                user_id,
                shift,
                record_date,
                now,
            )

            # 清理缓存
            cache_key = f"shift_state:{chat_id}:{user_id}:{shift}"
            self._cache.pop(cache_key, None)
            self._cache_ttl.pop(cache_key, None)
            return True

        except Exception as e:
            logger.error(f"设置用户班次状态失败: {e}")
            return False

    async def clear_user_shift_state(
        self,
        chat_id: int,
        user_id: int,
        shift: str,
    ) -> bool:
        """
        清除用户班次状态（下班打卡）
        直接删除记录
        """
        try:
            await self.execute_with_retry(
                "清除用户班次状态",
                """
                DELETE FROM group_shift_state
                WHERE chat_id = $1 AND user_id = $2 AND shift = $3
                """,
                chat_id,
                user_id,
                shift,
            )

            # 清理缓存
            cache_key = f"shift_state:{chat_id}:{user_id}:{shift}"
            self._cache.pop(cache_key, None)
            self._cache_ttl.pop(cache_key, None)
            return True

        except Exception as e:
            logger.error(f"清除用户班次状态失败: {e}")
            return False

    async def get_user_shift_state(
        self,
        chat_id: int,
        user_id: int,
        shift: str,
    ) -> Optional[Dict]:
        """
        获取用户班次状态
        存在 = 用户已上班且未下班
        不存在 = 用户未上班或已下班
        """
        cache_key = f"shift_state:{chat_id}:{user_id}:{shift}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT * FROM group_shift_state
                    WHERE chat_id = $1 AND user_id = $2 AND shift = $3
                    """,
                    chat_id,
                    user_id,
                    shift,
                )

                if row:
                    result = dict(row)
                    self._set_cached(cache_key, result, 30)  # 30秒缓存
                    return result
                return None

        except Exception as e:
            logger.error(f"获取用户班次状态失败: {e}")
            return None

    async def get_user_current_shift(
        self,
        chat_id: int,
        user_id: int,
    ) -> Optional[Dict]:
        """
        获取用户当前活跃的班次（基于 work_records）
        用于启动时恢复状态
        """
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT shift, record_date, created_at as shift_start_time
                    FROM work_records 
                    WHERE chat_id = $1 
                      AND user_id = $2 
                      AND checkin_type = 'work_start'
                      AND NOT EXISTS (
                          SELECT 1 FROM work_records wr2
                          WHERE wr2.chat_id = work_records.chat_id
                            AND wr2.user_id = work_records.user_id
                            AND wr2.shift = work_records.shift
                            AND wr2.record_date = work_records.record_date
                            AND wr2.checkin_type = 'work_end'
                      )
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    chat_id,
                    user_id,
                )

                if row:
                    return {
                        "shift": row["shift"],
                        "record_date": row["record_date"],
                        "shift_start_time": row["shift_start_time"],
                    }
                return None

        except Exception as e:
            logger.error(f"获取用户当前班次失败: {e}")
            return None

    async def cleanup_expired_shift_states(self):
        """
        清理过期的用户班次状态（超过16小时）
        """
        try:
            now = self.get_beijing_time()
            expired_time = now - timedelta(hours=16)

            async with self.pool.acquire() as conn:
                # 查询过期的班次状态（用于日志）
                rows = await conn.fetch(
                    """
                    SELECT chat_id, user_id, shift
                    FROM group_shift_state
                    WHERE shift_start_time < $1
                    """,
                    expired_time,
                )

                # 删除过期的班次状态
                result = await conn.execute(
                    """
                    DELETE FROM group_shift_state
                    WHERE shift_start_time < $1
                    """,
                    expired_time,
                )

                # 解析删除数量
                deleted = 0
                if result and result.startswith("DELETE"):
                    deleted = int(result.split()[-1])

                if deleted > 0:
                    logger.info(f"🧹 清理了 {deleted} 个过期的用户班次状态")

                    # 清理相关缓存
                    for row in rows:
                        cache_key = f"shift_state:{row['chat_id']}:{row['user_id']}:{row['shift']}"
                        self._cache.pop(cache_key, None)
                        self._cache_ttl.pop(cache_key, None)

                return deleted

        except Exception as e:
            logger.error(f"清理过期班次状态失败: {e}")
            return 0

    # ========== 用户当前班次辅助方法 ==========
    async def get_user_active_shift(self, chat_id: int, user_id: int) -> Optional[Dict]:
        """
        获取用户当前活跃的班次（任意班次）
        用于快速判断用户是否有进行中的班次
        """
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT shift, record_date, shift_start_time
                    FROM group_shift_state
                    WHERE chat_id = $1 AND user_id = $2
                    ORDER BY shift_start_time DESC
                    LIMIT 1
                    """,
                    chat_id,
                    user_id,
                )
                return dict(row) if row else None
        except Exception as e:
            logger.error(f"获取用户活跃班次失败: {e}")
            return None

    async def count_active_users_in_shift(self, chat_id: int, shift: str) -> int:
        """
        统计指定班次中的活跃用户数
        """
        try:
            async with self.pool.acquire() as conn:
                count = await conn.fetchval(
                    """
                    SELECT COUNT(*) FROM group_shift_state
                    WHERE chat_id = $1 AND shift = $2
                    """,
                    chat_id,
                    shift,
                )
                return count or 0
        except Exception as e:
            logger.error(f"统计班次活跃用户失败: {e}")
            return 0

    async def update_group_dual_mode(
        self, chat_id: int, enabled: bool, day_start: str = None, day_end: str = None
    ):
        """更新双班模式配置"""
        if enabled and (day_start is None or day_end is None):
            raise ValueError("开启双班模式必须提供白班开始和结束时间")

        await self.execute_with_retry(
            "更新双班模式",
            """
            UPDATE groups SET 
                dual_mode = $1,
                dual_day_start = $2,
                dual_day_end = $3,
                updated_at = CURRENT_TIMESTAMP
            WHERE chat_id = $4
            """,
            enabled,
            day_start if enabled else None,
            day_end if enabled else None,
            chat_id,
        )
        self._cache.pop(f"group:{chat_id}", None)

    async def update_shift_grace_window(
        self, chat_id: int, grace_before: int, grace_after: int
    ):
        """更新时间宽容窗口"""
        await self.execute_with_retry(
            "更新时间宽容窗口",
            """
            UPDATE groups SET 
                shift_grace_before = $1,
                shift_grace_after = $2,
                updated_at = CURRENT_TIMESTAMP
            WHERE chat_id = $3
            """,
            grace_before,
            grace_after,
            chat_id,
        )
        self._cache.pop(f"group:{chat_id}", None)

    async def get_shift_config(self, chat_id: int) -> Dict:
        """获取班次配置（包含分离的上下班时间窗口）"""
        group_data = await self.get_group_cached(chat_id)
        if not group_data:
            return {
                "dual_mode": False,
                "day_start": "09:00",
                "day_end": "21:00",
                "grace_before": Config.DEFAULT_GRACE_BEFORE,  # 上班前
                "grace_after": Config.DEFAULT_GRACE_AFTER,  # 上班后
                "workend_grace_before": Config.DEFAULT_WORKEND_GRACE_BEFORE,  # 下班前
                "workend_grace_after": Config.DEFAULT_WORKEND_GRACE_AFTER,  # 下班后
            }

        # 优先级1: /setworktime 设置
        work_hours = await self.get_group_work_time(chat_id)
        has_work_time = await self.has_work_hours_enabled(chat_id)

        if has_work_time:
            day_start = work_hours["work_start"]
            day_end = work_hours["work_end"]
        # 优先级2: /setdualmode 设置
        elif group_data.get("dual_mode"):
            day_start = group_data.get("dual_day_start", "09:00")
            day_end = group_data.get("dual_day_end", "21:00")
        # 优先级3: 默认值
        else:
            day_start = "09:00"
            day_end = "21:00"

        return {
            "dual_mode": bool(group_data.get("dual_mode", False)),
            "day_start": day_start,
            "day_end": day_end,
            "grace_before": group_data.get(
                "shift_grace_before", Config.DEFAULT_GRACE_BEFORE
            ),
            "grace_after": group_data.get(
                "shift_grace_after", Config.DEFAULT_GRACE_AFTER
            ),
            # 🆕 新增下班专用时间窗口
            "workend_grace_before": group_data.get(
                "workend_grace_before", Config.DEFAULT_WORKEND_GRACE_BEFORE
            ),
            "workend_grace_after": group_data.get(
                "workend_grace_after", Config.DEFAULT_WORKEND_GRACE_AFTER
            ),
        }

    def calculate_shift_window(
        self,
        shift_config: Dict[str, Any],
        checkin_type: str = None,
        now: Optional[datetime] = None,
        active_shift: Optional[str] = None,
        active_record_date: Optional[date] = None,  # 🆕 新增：状态日期
    ) -> Dict[str, Any]:

        if now is None:
            now = self.get_beijing_time()

        tz = now.tzinfo

        default_return = {
            "day_window": {},
            "night_window": {},
            "current_shift": None,
        }

        if not shift_config:
            return default_return

        try:
            day_start_time = datetime.strptime(
                shift_config.get("day_start", "09:00"), "%H:%M"
            ).time()
            day_end_time = datetime.strptime(
                shift_config.get("day_end", "21:00"), "%H:%M"
            ).time()
        except Exception:
            return default_return

        # ===== 🎯 确定使用的日期基础 =====
        if active_record_date:
            # 有状态：使用状态的日期
            base_date = active_record_date
            logger.debug(f"使用状态日期计算窗口: {base_date}")
        else:
            # 无状态：使用当前日期
            base_date = now.date()
            logger.debug(f"使用当前日期计算窗口: {base_date}")

        # 基于 base_date 构建时间点
        day_start_dt = datetime.combine(base_date, day_start_time).replace(tzinfo=tz)
        day_end_dt = datetime.combine(base_date, day_end_time).replace(tzinfo=tz)

        # =============================
        # 🎯 活动判定 - 修复版
        # =============================
        if checkin_type == "activity":
            # 核心原则：活动跟随活跃班次，不依赖时间窗口
            if active_shift:
                # 有活跃班次时，直接跟随
                if active_shift == "day":
                    current_shift_detail = "day"
                    logger.debug(
                        f"📊 activity跟随白班: active_shift={active_shift}, "
                        f"now={now.strftime('%H:%M')}"
                    )
                else:  # active_shift == "night"
                    # 夜班时需要判断是昨晚还是今晚
                    if now >= day_end_dt:
                        current_shift_detail = "night_tonight"  # 今晚夜班
                        logger.debug(
                            f"📊 activity跟随夜班(今晚): active_shift={active_shift}, "
                            f"now={now.strftime('%H:%M')} >= {day_end_dt.strftime('%H:%M')}"
                        )
                    else:
                        current_shift_detail = "night_last"  # 昨晚夜班
                        logger.debug(
                            f"📊 activity跟随夜班(昨晚): active_shift={active_shift}, "
                            f"now={now.strftime('%H:%M')} < {day_end_dt.strftime('%H:%M')}"
                        )
            else:
                # 没有活跃班次时，使用时间区间判定
                if day_start_dt <= now < day_end_dt:
                    current_shift_detail = "day"
                    logger.debug(
                        f"📊 activity无活跃班次，时间在白班区间: {now.strftime('%H:%M')}"
                    )
                elif now >= day_end_dt:
                    current_shift_detail = "night_tonight"  # 今晚夜班
                    logger.debug(
                        f"📊 activity无活跃班次，时间在夜班区间(今晚): {now.strftime('%H:%M')}"
                    )
                else:
                    current_shift_detail = "night_last"  # 昨晚夜班
                    logger.debug(
                        f"📊 activity无活跃班次，时间在夜班区间(昨晚): {now.strftime('%H:%M')}"
                    )

            return {
                "day_window": {},
                "night_window": {},
                "current_shift": current_shift_detail,
            }

        # =============================
        # 打卡窗口逻辑
        # =============================
        grace_before = shift_config.get("grace_before", Config.DEFAULT_GRACE_BEFORE)
        grace_after = shift_config.get("grace_after", Config.DEFAULT_GRACE_AFTER)
        workend_grace_before = shift_config.get(
            "workend_grace_before", Config.DEFAULT_WORKEND_GRACE_BEFORE
        )
        workend_grace_after = shift_config.get(
            "workend_grace_after", Config.DEFAULT_WORKEND_GRACE_AFTER
        )

        # ===== 白班窗口（基于 base_date）=====
        day_window = {
            "work_start": {
                "start": (day_start_dt - timedelta(minutes=grace_before)).replace(
                    tzinfo=tz
                ),
                "end": (day_start_dt + timedelta(minutes=grace_after)).replace(
                    tzinfo=tz
                ),
            },
            "work_end": {
                "start": (day_end_dt - timedelta(minutes=workend_grace_before)).replace(
                    tzinfo=tz
                ),
                "end": (day_end_dt + timedelta(minutes=workend_grace_after)).replace(
                    tzinfo=tz
                ),
            },
        }

        # ===== 昨晚夜班窗口 =====
        last_night_window = {
            "work_start": {
                "start": (
                    day_end_dt
                    - timedelta(days=1)
                    - timedelta(minutes=workend_grace_before)
                ).replace(tzinfo=tz),
                "end": (
                    day_end_dt
                    - timedelta(days=1)
                    + timedelta(minutes=workend_grace_after)
                ).replace(tzinfo=tz),
            },
            "work_end": {
                "start": (day_start_dt - timedelta(minutes=grace_before)).replace(
                    tzinfo=tz
                ),
                "end": (day_start_dt + timedelta(minutes=grace_after)).replace(
                    tzinfo=tz
                ),
            },
        }

        # ===== 今晚夜班窗口 =====
        tonight_window = {
            "work_start": {
                "start": (day_end_dt - timedelta(minutes=workend_grace_before)).replace(
                    tzinfo=tz
                ),
                "end": (day_end_dt + timedelta(minutes=workend_grace_after)).replace(
                    tzinfo=tz
                ),
            },
            "work_end": {
                "start": (
                    day_start_dt + timedelta(days=1) - timedelta(minutes=grace_before)
                ).replace(tzinfo=tz),
                "end": (
                    day_start_dt + timedelta(days=1) + timedelta(minutes=grace_after)
                ).replace(tzinfo=tz),
            },
        }

        # ===== 确定当前班次 =====
        current_shift = None

        if checkin_type in ("work_start", "work_end"):
            lookup = checkin_type

            # 检查白班窗口
            if day_window[lookup]["start"] <= now <= day_window[lookup]["end"]:
                current_shift = "day"
            # 检查昨晚夜班窗口
            elif (
                last_night_window[lookup]["start"]
                <= now
                <= last_night_window[lookup]["end"]
            ):
                current_shift = "night_last"
            # 检查今晚夜班窗口
            elif (
                tonight_window[lookup]["start"] <= now <= tonight_window[lookup]["end"]
            ):
                current_shift = "night_tonight"
            # 特殊处理：下班后到夜班开始前的时间段
            elif lookup == "work_start":
                afternoon_start = day_window["work_start"]["end"] + timedelta(minutes=1)
                afternoon_end = tonight_window["work_start"]["start"] - timedelta(
                    minutes=1
                )
                if afternoon_start <= now <= afternoon_end:
                    current_shift = "night_tonight"

        # 如果 active_shift 存在但没找到窗口，使用 active_shift 确定班次
        if current_shift is None and active_shift:
            if active_shift == "day":
                current_shift = "day"
            else:  # night
                if now >= day_end_dt:
                    current_shift = "night_tonight"
                else:
                    current_shift = "night_last"

        return {
            "day_window": day_window,
            "night_window": {
                "last_night": last_night_window,
                "tonight": tonight_window,
            },
            "current_shift": current_shift,
        }

    async def get_business_date(
        self,
        chat_id: int,
        current_dt: datetime = None,
        shift: str = None,
        checkin_type: str = None,
        shift_detail: str = None,
        record_date: Optional[date] = None,
    ) -> date:
        """
        获取业务日期 - 支持状态模型

        业务日期规则：
        - 状态模型优先：如果传入 record_date，直接使用
        - 业务参数优先：如果传入 shift_detail，优先使用
        - 否则使用时间模型计算（包含提前上班判定）
        """
        if current_dt is None:
            current_dt = self.get_beijing_time()

        today = current_dt.date()
        yesterday = today - timedelta(days=1)

        # ===== 🎯 第1优先级：状态模型优先（完全不变）=====
        if record_date is not None:
            # 有状态：直接使用状态的日期
            if shift == "night" and checkin_type == "work_end":
                # 夜班下班：业务日期 = 状态日期 + 1天
                business_date = record_date + timedelta(days=1)
                logger.debug(
                    f"📅 [业务日期-状态模型-夜班下班] "
                    f"chat_id={chat_id}, "
                    f"record_date={record_date}, "
                    f"business_date={business_date}"
                )
            else:
                # 其他情况：业务日期 = 状态日期
                business_date = record_date
                logger.debug(
                    f"📅 [业务日期-状态模型] "
                    f"chat_id={chat_id}, "
                    f"record_date={record_date}, "
                    f"shift={shift}, "
                    f"checkin_type={checkin_type}"
                )
            return business_date

        # ===== 判断双班模式 =====
        is_dual = await self.is_dual_mode_enabled(chat_id)

        # ===== 🎯 第2优先级：每月1号特殊处理（仅当无状态时）=====
        if is_dual and today.day == 1:
            current_hour = current_dt.hour

            # 情况1：凌晨0-15点 - 上月夜班
            if current_hour < 15 and shift == "night":
                business_date = yesterday
                logger.info(
                    f"📅 [每月1号特殊处理] 夜班时段 ({current_hour}:00)，"
                    f"业务日期={business_date}（上月最后一天）"
                )
                return business_date

            # 情况2：15点之后 - 新白班
            elif current_hour >= 15 and shift == "day":
                business_date = today
                logger.info(
                    f"📅 [每月1号特殊处理] 白班时段 ({current_hour}:00)，"
                    f"业务日期={business_date}（本月1号）"
                )
                return business_date

            # 情况3：夜班下班打卡（15点）
            elif current_hour >= 15 and checkin_type == "work_end" and shift == "night":
                business_date = yesterday
                logger.info(
                    f"📅 [每月1号特殊处理] 夜班下班 ({current_hour}:00)，"
                    f"业务日期={business_date}（上月最后一天）"
                )
                return yesterday

        # ===== 🎯 第3优先级：原有的双班模式逻辑（完全不变）=====
        if is_dual:
            # ===== 1️⃣ shift_detail 判定（业务参数优先）=====
            if shift_detail in ("night_last", "night_tonight", "day"):
                if shift_detail == "night_last":
                    business_date = today - timedelta(days=1)
                else:
                    business_date = today

                logger.debug(
                    f"📅 [业务日期-双班-detail] "
                    f"chat_id={chat_id}, "
                    f"time={current_dt.strftime('%H:%M:%S')}, "
                    f"shift_detail={shift_detail}, "
                    f"checkin_type={checkin_type}, "
                    f"result={business_date}"
                )
                return business_date

            # ===== 2️⃣ 获取班次配置用于时间窗口计算 =====
            shift_config = await self.get_shift_config(chat_id)
            day_start = shift_config.get("day_start", "09:00")
            grace_before = shift_config.get("grace_before", 120)

            # 解析白班开始时间
            day_start_time = datetime.strptime(day_start, "%H:%M").time()
            day_start_dt = datetime.combine(today, day_start_time).replace(
                tzinfo=current_dt.tzinfo
            )

            # 计算白班最早允许时间
            earliest_day_time = day_start_dt - timedelta(minutes=grace_before)

            # ===== 3️⃣ 提前上班判定（无参数时）=====
            if current_dt >= earliest_day_time:
                logger.debug(
                    f"📅 [提前上班判定] "
                    f"chat={chat_id}, "
                    f"time={current_dt.strftime('%H:%M')}, "
                    f"earliest={earliest_day_time.strftime('%H:%M')}, "
                    f"result={today}"
                )
                return today

            # ===== 4️⃣ 窗口计算兜底（无参数时）=====
            if shift and checkin_type:
                window_info = (
                    self.calculate_shift_window(
                        shift_config=shift_config,
                        checkin_type=checkin_type,
                        now=current_dt,
                    )
                    or {}
                )

                current_shift_detail = window_info.get("current_shift")

                if current_shift_detail == "night_last":
                    business_date = today - timedelta(days=1)
                elif current_shift_detail in ("night_tonight", "day"):
                    business_date = today
                else:
                    business_date = today  # 安全兜底

                logger.debug(
                    f"📅 业务日期(双班-window): chat_id={chat_id}, "
                    f"shift={shift}, checkin_type={checkin_type}, "
                    f"判定={current_shift_detail}, 日期={business_date}"
                )
                return business_date

            # ===== 5️⃣ 双班模式兜底 =====
            logger.debug(f"📅 [双班-fallback] chat={chat_id}, 日期={today}")
            return today

        # ===== 🎯 第4优先级：原有的单班模式逻辑（完全不变）=====
        group_data = await self.get_group_cached(chat_id)

        if group_data:
            reset_hour = group_data.get("reset_hour", Config.DAILY_RESET_HOUR)
            reset_minute = group_data.get("reset_minute", Config.DAILY_RESET_MINUTE)
        else:
            reset_hour = Config.DAILY_RESET_HOUR
            reset_minute = Config.DAILY_RESET_MINUTE

        reset_time_today = current_dt.replace(
            hour=reset_hour,
            minute=reset_minute,
            second=0,
            microsecond=0,
        )

        if current_dt < reset_time_today:
            business_date = (current_dt - timedelta(days=1)).date()
        else:
            business_date = today

        logger.debug(
            f"📅 业务日期(单班): chat_id={chat_id}, "
            f"当前时间={current_dt.strftime('%Y-%m-%d %H:%M')}, "
            f"重置时间={reset_time_today.strftime('%Y-%m-%d %H:%M')}, "
            f"日期={business_date}"
        )

        return business_date

    async def determine_shift_for_time(
        self,
        chat_id: int,
        current_time: Optional[datetime] = None,
        checkin_type: str = "work_start",
        active_shift: Optional[str] = None,
        active_record_date: Optional[date] = None,
    ) -> Dict[str, object]:
        """
        企业级终极班次判定函数

        特性：
        状态模型优先
        夜班跨天绝对正确
        record_date 永远正确
        activity 永远安全
        """

        now = current_time or self.get_beijing_time()
        today = now.date()
        yesterday = today - timedelta(days=1)

        shift_config = await self.get_shift_config(chat_id) or {}
        is_dual = shift_config.get("dual_mode", False)

        # ===== 🎯 第1优先级：状态模型（最高优先级）- 完全不变 =====
        if active_shift and active_record_date:
            if active_shift not in ("day", "night"):
                raise ValueError(f"非法 shift: {active_shift}")

            if not isinstance(active_record_date, date):
                raise TypeError("active_record_date 必须是 date")

            shift = active_shift
            record_date = active_record_date

            # 正确计算 shift_detail（关键修复）
            if shift == "day":
                shift_detail = "day"
            else:
                day_end_str = shift_config.get("day_end", "21:00")
                day_end_time = datetime.strptime(day_end_str, "%H:%M").time()
                night_start = datetime.combine(record_date, day_end_time).replace(
                    tzinfo=now.tzinfo
                )
                night_end = night_start + timedelta(days=1)

                if night_start <= now < night_end:
                    shift_detail = "night_tonight"
                else:
                    shift_detail = "night_last"

            # 获取窗口
            window_info = (
                self.calculate_shift_window(
                    shift_config=shift_config,
                    checkin_type=checkin_type,
                    now=now,
                    active_shift=shift,
                    active_record_date=record_date,
                )
                or {}
            )

            # activity 永远允许
            if checkin_type == "activity":
                in_window = True
            else:
                in_window = self._is_time_in_window(
                    now, shift, shift_detail, checkin_type, window_info
                )

            # 业务日期
            business_date = await self.get_business_date(
                chat_id=chat_id,
                current_dt=now,
                shift=shift,
                checkin_type=checkin_type,
                shift_detail=shift_detail,
                record_date=record_date,
            )

            return dict(
                shift=shift,
                shift_detail=shift_detail,
                business_date=business_date,
                record_date=record_date,
                is_dual=True,
                in_window=in_window,
                window_info=window_info,
                using_state=True,
            )

        # ============================================================
        # 单班模式 - 移到前面，因为不需要特殊处理
        # ============================================================
        if not is_dual:
            business_date = await self.get_business_date(
                chat_id=chat_id,
                current_dt=now,
            )
            return dict(
                shift="day",
                shift_detail="day",
                business_date=business_date,
                record_date=business_date,
                is_dual=False,
                in_window=True,
                window_info=None,
                using_state=False,
            )

        # ===== 🎯 第2优先级：每月1号特殊处理（仅当双班模式且无状态时）=====
        if today.day == 1:
            current_hour = now.hour

            # ✅ 获取完整的窗口信息，传入所有可能的参数
            window_info = (
                self.calculate_shift_window(
                    shift_config=shift_config,
                    checkin_type=checkin_type,
                    now=now,
                )
                or {}
            )

            # 情况1：凌晨0-15点 - 上月夜班
            if current_hour < 15:
                # ✅ 正确设置班次信息
                shift = "night"
                shift_detail = "night_last"
                record_date = yesterday

                # ✅ 正确计算 in_window
                if checkin_type == "activity":
                    in_window = True
                else:
                    in_window = self._is_time_in_window(
                        now, shift, shift_detail, checkin_type, window_info
                    )

                # ✅ 获取业务日期
                business_date = await self.get_business_date(
                    chat_id=chat_id,
                    current_dt=now,
                    shift=shift,
                    checkin_type=checkin_type,
                    shift_detail=shift_detail,
                    record_date=record_date,
                )

                logger.info(
                    f"📅 [每月1号特殊处理] {current_hour}:00 在夜班时段，"
                    f"shift={shift}, detail={shift_detail}, record_date={record_date}, "
                    f"business_date={business_date}, in_window={in_window}"
                )

                return {
                    "shift": shift,
                    "shift_detail": shift_detail,
                    "business_date": business_date,
                    "record_date": record_date,
                    "is_dual": True,
                    "in_window": in_window,
                    "window_info": window_info,
                    "using_state": False,
                    "is_special": True,
                }

            # 情况2：15点之后 - 新白班开始
            elif current_hour >= 15:
                shift = "day"
                shift_detail = "day"
                record_date = today

                if checkin_type == "activity":
                    in_window = True
                else:
                    in_window = self._is_time_in_window(
                        now, shift, shift_detail, checkin_type, window_info
                    )

                business_date = await self.get_business_date(
                    chat_id=chat_id,
                    current_dt=now,
                    shift=shift,
                    checkin_type=checkin_type,
                    shift_detail=shift_detail,
                    record_date=record_date,
                )

                logger.info(
                    f"📅 [每月1号特殊处理] {current_hour}:00 在白班时段，"
                    f"shift={shift}, detail={shift_detail}, record_date={record_date}, "
                    f"business_date={business_date}, in_window={in_window}"
                )

                return {
                    "shift": shift,
                    "shift_detail": shift_detail,
                    "business_date": business_date,
                    "record_date": record_date,
                    "is_dual": True,
                    "in_window": in_window,
                    "window_info": window_info,
                    "using_state": False,
                    "is_special": True,
                }

        # ============================================================
        # 第3优先级：无状态模式（原有双班逻辑）- 完全不变
        # ============================================================
        window_info = (
            self.calculate_shift_window(
                shift_config=shift_config,
                checkin_type=checkin_type,
                now=now,
            )
            or {}
        )

        shift_detail = window_info.get("current_shift")

        # fallback 安全计算
        if shift_detail is None:
            shift_detail = self._fallback_shift_detail(now, shift_config)

        shift = "night" if shift_detail.startswith("night") else "day"

        # 判断窗口
        if checkin_type == "activity":
            in_window = True
        else:
            in_window = self._is_time_in_window(
                now, shift, shift_detail, checkin_type, window_info
            )

        # record_date 正确计算
        record_date = await self.get_business_date(
            chat_id=chat_id,
            current_dt=now,
            shift=shift,
            checkin_type=checkin_type,
            shift_detail=shift_detail,
        )

        return dict(
            shift=shift,
            shift_detail=shift_detail,
            business_date=record_date,
            record_date=record_date,
            is_dual=True,
            in_window=in_window,
            window_info=window_info,
            using_state=False,
        )

    def _is_time_in_window(
        self,
        now: datetime,
        shift: str,
        shift_detail: str,
        checkin_type: str,
        window_info: dict,
    ) -> bool:
        """判断时间是否在窗口内"""
        try:
            if checkin_type == "work_start":
                if shift == "day":
                    day_window = window_info.get("day_window", {}).get("work_start", {})
                    return bool(
                        day_window.get("start")
                        and day_window.get("end")
                        and day_window["start"] <= now <= day_window["end"]
                    )
                else:  # night
                    night_window = window_info.get("night_window", {})
                    if shift_detail == "night_last":
                        target = night_window.get("last_night", {}).get(
                            "work_start", {}
                        )
                    else:  # night_tonight
                        target = night_window.get("tonight", {}).get("work_start", {})
                    return bool(
                        target.get("start")
                        and target.get("end")
                        and target["start"] <= now <= target["end"]
                    )
            else:  # work_end
                if shift == "day":
                    day_window = window_info.get("day_window", {}).get("work_end", {})
                    return bool(
                        day_window.get("start")
                        and day_window.get("end")
                        and day_window["start"] <= now <= day_window["end"]
                    )
                else:  # night
                    night_window = window_info.get("night_window", {})
                    if shift_detail == "night_last":
                        target = night_window.get("last_night", {}).get("work_end", {})
                    else:  # night_tonight
                        target = night_window.get("tonight", {}).get("work_end", {})
                    return bool(
                        target.get("start")
                        and target.get("end")
                        and target["start"] <= now <= target["end"]
                    )
        except Exception as e:
            logger.error(f"窗口检查失败: {e}")
            return False

    def _fallback_shift_detail(
        self,
        now,
        shift_config,
    ):

        day_start = shift_config.get("day_start", "09:00")

        day_end = shift_config.get("day_end", "21:00")

        day_start_dt = datetime.combine(
            now.date(),
            datetime.strptime(day_start, "%H:%M").time(),
        ).replace(tzinfo=now.tzinfo)

        day_end_dt = datetime.combine(
            now.date(),
            datetime.strptime(day_end, "%H:%M").time(),
        ).replace(tzinfo=now.tzinfo)

        if day_start_dt <= now < day_end_dt:

            return "day"

        elif now >= day_end_dt:

            return "night_tonight"

        else:

            return "night_last"

    # ========== 数据清理 ==========
    async def cleanup_old_data(self, days: int = 30):
        """清理旧数据"""
        cutoff_date = (self.get_beijing_time() - timedelta(days=days)).date()

        self._ensure_pool_initialized()
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "DELETE FROM user_activities WHERE activity_date < $1", cutoff_date
                )
                await conn.execute(
                    "DELETE FROM work_records WHERE record_date < $1", cutoff_date
                )
                await conn.execute(
                    "DELETE FROM users WHERE last_updated < $1", cutoff_date
                )

    async def cleanup_monthly_data(self, days_or_date=None):
        """
        清理月度统计数据

        Args:
            days_or_date: 可以是：
                - int: 清理多少天前的数据
                - date: 清理指定日期前的数据
                - None: 使用配置的默认天数

        Returns:
            删除的记录数
        """
        import traceback

        try:
            today = self.get_beijing_time()

            # ===== 确定截止日期 =====
            if days_or_date is None:
                # 没有参数：使用配置的默认天数
                cutoff_date = (
                    (today - timedelta(days=Config.MONTHLY_DATA_RETENTION_DAYS))
                    .date()
                    .replace(day=1)
                )
                logger.info(
                    f"📅 使用默认配置: {Config.MONTHLY_DATA_RETENTION_DAYS}天, "
                    f"截止日期={cutoff_date}"
                )

            elif isinstance(days_or_date, int):
                # 传入的是天数
                if days_or_date <= 0:
                    logger.warning(f"⚠️ 无效的天数: {days_or_date}，必须大于0")
                    return 0

                cutoff_date = (
                    (today - timedelta(days=days_or_date)).date().replace(day=1)
                )
                logger.info(
                    f"📅 按天数清理: {days_or_date}天前, 截止日期={cutoff_date}"
                )

            elif isinstance(days_or_date, date):
                # 传入的是日期
                cutoff_date = days_or_date
                logger.info(f"📅 按日期清理: 截止日期={cutoff_date}")

            else:
                logger.error(f"❌ 无效的参数类型: {type(days_or_date)}")
                return 0

            # ===== 验证日期 =====
            if cutoff_date > today.date():
                logger.warning(f"⚠️ 截止日期 {cutoff_date} 晚于今天，不会删除任何数据")
                return 0

            # ===== 执行清理 =====
            self._ensure_pool_initialized()
            async with self.pool.acquire() as conn:
                result = await conn.execute(
                    "DELETE FROM monthly_statistics WHERE statistic_date < $1",
                    cutoff_date,
                )

                deleted_count = 0
                if result and result.startswith("DELETE"):
                    try:
                        deleted_count = int(result.split()[-1])
                    except (ValueError, IndexError):
                        pass

                logger.info(
                    f"✅ 月度数据清理完成\n"
                    f"   ├─ 截止日期: {cutoff_date}\n"
                    f"   ├─ 删除记录: {deleted_count} 条\n"
                    f"   └─ 参数: {days_or_date or '默认'}"
                )

                return deleted_count

        except Exception as e:
            logger.error(f"❌ 月度数据清理失败: {e}")
            logger.error(traceback.format_exc())
            return 0

    async def cleanup_specific_month(self, year: int, month: int):
        """清理指定年月的月度统计数据"""
        target_date = date(year, month, 1)
        self._ensure_pool_initialized()
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM monthly_statistics WHERE statistic_date = $1", target_date
            )
            return (
                int(result.split()[-1]) if result and result.startswith("DELETE") else 0
            )

    async def cleanup_inactive_users(self, days: int = 30):
        """清理长期未活动用户及其记录（安全版）"""

        cutoff_date = (self.get_beijing_time() - timedelta(days=days)).date()

        async with self.pool.acquire() as conn:
            async with conn.transaction():

                # 找出要删除的用户列表（避免直接删）
                users_to_delete = await conn.fetch(
                    """
                        SELECT user_id 
                        FROM users
                        WHERE last_updated < $1
                        AND NOT EXISTS (
                            SELECT 1 FROM monthly_statistics 
                            WHERE monthly_statistics.chat_id = users.chat_id 
                            AND monthly_statistics.user_id = users.user_id
                        )
                        """,
                    cutoff_date,
                )

                user_ids = [u["user_id"] for u in users_to_delete]

                if not user_ids:
                    logger.info("🧹 无需清理用户")
                    return 0

                # 删除用户的日常记录
                await conn.execute(
                    "DELETE FROM user_activities WHERE user_id = ANY($1)",
                    user_ids,
                )

                # 删除上下班记录（如果你需要）
                await conn.execute(
                    "DELETE FROM work_records WHERE user_id = ANY($1)",
                    user_ids,
                )

                # 最后删除用户
                deleted_count = await conn.execute(
                    "DELETE FROM users WHERE user_id = ANY($1)",
                    user_ids,
                )

        logger.info(f"🧹 清理了 {deleted_count} 个长期未活动的用户以及他们的所有记录")
        return deleted_count

    # ========== 活动人数限制 ==========
    async def set_activity_user_limit(self, activity: str, max_users: int):
        """设置活动人数限制"""
        self._ensure_pool_initialized()
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO activity_user_limits (activity_name, max_users)
                VALUES ($1, $2)
                ON CONFLICT (activity_name)
                DO UPDATE SET 
                    max_users = EXCLUDED.max_users,
                    updated_at = CURRENT_TIMESTAMP
                """,
                activity,
                max_users,
            )
        self._cache.pop(f"activity_limit:{activity}", None)

    async def get_activity_user_limit(self, activity: str) -> int:
        """获取活动人数限制"""
        cache_key = f"activity_limit:{activity}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        self._ensure_pool_initialized()
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT max_users FROM activity_user_limits WHERE activity_name = $1",
                activity,
            )
            limit = row["max_users"] if row else 0
            self._set_cached(cache_key, limit, 60)
            return limit

    async def get_current_activity_users(self, chat_id: int, activity: str) -> int:
        """获取当前正在进行指定活动的用户数量"""
        self._ensure_pool_initialized()
        async with self.pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM users WHERE chat_id = $1 AND current_activity = $2",
                chat_id,
                activity,
            )
            return count or 0

    async def get_all_activity_limits(self) -> Dict[str, int]:
        """获取所有活动的人数限制"""
        self._ensure_pool_initialized()
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT activity_name, max_users FROM activity_user_limits"
            )
            return {row["activity_name"]: row["max_users"] for row in rows}

    async def remove_activity_user_limit(self, activity: str):
        """移除活动人数限制"""
        self._ensure_pool_initialized()
        async with self.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM activity_user_limits WHERE activity_name = $1", activity
            )
        self._cache.pop(f"activity_limit:{activity}", None)

    async def force_reset_all_users_in_group(
        self, chat_id: int, target_date: date = None
    ):
        """
        强制重置该群组所有用户的每日统计数据。
        修复：同时清理 target_date (昨天) 和 target_date + 1 (今天) 的数据，
        防止因修改重置时间导致的时间窗口重叠产生“幽灵数据”。
        """
        # 如果没有传入日期，则获取当前业务日期
        if target_date is None:
            target_date = self.get_beijing_date()

        # 计算下一天（即“今天”），用于清理潜在的重叠数据
        next_day = target_date + timedelta(days=1)

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # 1. 删除【结算日】的活动明细
                await conn.execute(
                    """
                    DELETE FROM user_activities 
                    WHERE chat_id = $1 AND activity_date = $2
                """,
                    chat_id,
                    target_date,
                )

                # 2. 【核心修复】删除【新的一天】可能存在的幽灵记录
                # 这是解决“修改时间后数据复活”的关键
                await conn.execute(
                    """
                    DELETE FROM user_activities 
                    WHERE chat_id = $1 AND activity_date = $2
                """,
                    chat_id,
                    next_day,
                )

                # 3. 更新所有用户的统计字段为0
                # 将 last_updated 设为 target_date，这样下次打卡时会触发正常的日期检查
                await conn.execute(
                    """
                    UPDATE users 
                    SET total_accumulated_time = 0, 
                        total_activity_count = 0, 
                        total_fines = 0,
                        last_updated = $2
                    WHERE chat_id = $1
                """,
                    chat_id,
                    target_date,
                )

            # 4. 清理缓存
            keys_to_remove = [
                f"group:{chat_id}",
                f"rank:{chat_id}",
                f"group_config:{chat_id}",
            ]
            for key in keys_to_remove:
                self._cache.pop(key, None)
                if key in self._cache_ttl:
                    del self._cache_ttl[key]

            logger.info(
                f"✅ 已强制重置群组 {chat_id} (清理日期: {target_date} 及 {next_day})"
            )

    # ========== 工具方法 ==========
    @staticmethod
    def format_seconds_to_hms(seconds: int) -> str:
        """将秒数格式化为小时:分钟:秒的字符串"""
        if not seconds:
            return "0秒"

        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        secs = seconds % 60

        if hours > 0:
            return f"{hours}小时{minutes}分{secs}秒"
        elif minutes > 0:
            return f"{minutes}分{secs}秒"
        else:
            return f"{secs}秒"

    @staticmethod
    def format_time_for_csv(seconds: int) -> str:
        """为CSV导出格式化时间显示"""
        if not seconds:
            return "0分0秒"

        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        secs = seconds % 60

        if hours > 0:
            return f"{hours}时{minutes}分{secs}秒"
        else:
            return f"{minutes}分{secs}秒"

    async def connection_health_check(self) -> bool:
        """快速连接健康检查"""
        if not self.pool:
            return False

        try:
            async with self.pool.acquire() as conn:
                result = await conn.fetchval("SELECT 1")
                return result == 1
        except Exception as e:
            logger.debug(f"数据库连接健康检查失败: {e}")
            return False


# 全局数据库实例
db = PostgreSQLDatabase()
