"""
确定性 Bootstrap 状态机：fail-fast，禁止隐式 lazy init。
顺序：CONFIG → DB_SCHEMA → DB_POOL → CACHE_WARM → SERVICES → HANDLERS → RECOVERY → BOT_READY
"""
import asyncio
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable, List

from config import Config

logger = logging.getLogger("GroupCheckInBot.Bootstrap")


class BootstrapPhase(str, Enum):
    CONFIG = "BOOTSTRAP_1_CONFIG"
    DB_SCHEMA = "BOOTSTRAP_2_DB_SCHEMA"
    DB_POOL = "BOOTSTRAP_3_DB_POOL"
    CACHE_WARM = "BOOTSTRAP_4_CACHE_WARM"
    SERVICES = "BOOTSTRAP_5_SERVICES"
    HANDLERS = "BOOTSTRAP_6_HANDLERS"
    RECOVERY = "BOOTSTRAP_7_RECOVERY"
    BOT_READY = "BOOTSTRAP_8_BOT_READY"


@dataclass
class BootstrapStep:
    phase: BootstrapPhase
    fn: Callable[[], Awaitable[None]]
    required: bool = True


async def run_bootstrap(steps: List[BootstrapStep]) -> None:
    from database import db

    for step in steps:
        t0 = time.perf_counter()
        db._bootstrap_phase = step.phase.value
        try:
            await step.fn()
        except Exception as e:
            logger.error(f"❌ {step.phase.value} 失败: {e}")
            if step.required:
                raise RuntimeError(f"{step.phase.value} failed: {e}") from e
            logger.warning(f"⚠️ {step.phase.value} 非致命失败，继续")
        elapsed_ms = (time.perf_counter() - t0) * 1000
        logger.info(f"✅ {step.phase.value} OK ({elapsed_ms:.1f}ms)")

    db._bootstrap_phase = BootstrapPhase.BOT_READY.value
    logger.info("🎉 Bootstrap 完成，系统进入 BOT_READY")


async def step_config_validate() -> None:
    Config.validate_config()


async def step_db_pool_init() -> None:
    from database import db

    await db.initialize()
    if not db._initialized or not db.pool:
        raise RuntimeError("数据库连接池未就绪")


async def step_db_schema_verify() -> None:
    from database import db

    await db.verify_schema()


async def step_cache_warm() -> None:
    from database import db

    await asyncio.gather(
        db.get_activity_limits_cached(),
        db.get_all_fine_rates_cached(),
        return_exceptions=True,
    )


async def build_default_steps(
    *,
    init_services_fn: Callable[[], Awaitable[None]],
    register_handlers_fn: Callable[[], Awaitable[None]],
    recovery_fn: Callable[[], Awaitable[None]],
) -> List[BootstrapStep]:
    return [
        BootstrapStep(BootstrapPhase.CONFIG, step_config_validate),
        BootstrapStep(BootstrapPhase.DB_POOL, step_db_pool_init),
        BootstrapStep(BootstrapPhase.DB_SCHEMA, step_db_schema_verify),
        BootstrapStep(BootstrapPhase.CACHE_WARM, step_cache_warm),
        BootstrapStep(BootstrapPhase.SERVICES, init_services_fn),
        BootstrapStep(BootstrapPhase.HANDLERS, register_handlers_fn),
        BootstrapStep(BootstrapPhase.RECOVERY, recovery_fn),
    ]
