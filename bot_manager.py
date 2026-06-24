import asyncio
import logging
import os
import sys
import time
from typing import Optional

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from config import Config
from fault_tolerance import telegram_circuit_breaker

logger = logging.getLogger("GroupCheckInBot")

_HAS_FCNTL = sys.platform != "win32"
if _HAS_FCNTL:
    import fcntl


class RobustBotManager:
    """健壮的Bot管理器 - 带自动重连"""

    def __init__(self, token: str):
        self.lock_fd = None

        if os.environ.get("RENDER"):
            render_instance_index = os.environ.get("RENDER_INSTANCE_INDEX", "0")
            if render_instance_index != "0":
                print(f"⏭️ Render 非主实例 (index={render_instance_index})，退出")
                time.sleep(3)
                sys.exit(0)
            print("✅ Render 主实例 (index=0) 继续运行")

        if _HAS_FCNTL:
            lock_file = "/tmp/bot_instance.lock"
            try:
                self.lock_fd = open(lock_file, "w")
                fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.lock_fd.write(str(os.getpid()))
                self.lock_fd.flush()
                print(f"✅ 获取进程锁成功，PID: {os.getpid()}")
            except (IOError, OSError):
                try:
                    with open(lock_file, "r") as f:
                        existing_pid = f.read().strip()
                    print(f"❌ 另一个机器人实例正在运行 (PID: {existing_pid})！退出...")
                except Exception:
                    print("❌ 另一个机器人实例正在运行！退出...")
                if os.environ.get("RENDER"):
                    time.sleep(5)
                sys.exit(1)
        else:
            logger.info("Windows 环境：跳过 Unix 进程锁")

        self.token = token
        self.bot: Optional[Bot] = None
        self.dispatcher: Optional[Dispatcher] = None
        self._is_running = False
        self._polling_active = False
        self._max_retries = 10
        self._base_delay = 2.0
        self._current_retry = 0
        self._last_successful_connection = 0.0
        self._connection_check_interval = 300

    def __del__(self):
        if self.lock_fd and _HAS_FCNTL:
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
                self.lock_fd.close()
            except Exception:
                pass

    async def initialize(self):
        """初始化Bot"""
        self.bot = Bot(token=self.token)
        self.dispatcher = Dispatcher(storage=MemoryStorage())
        logger.info("Bot管理器初始化完成")

    def _verify_process_lock(self) -> None:
        """轮询前确认单实例锁仍有效（仅 Unix）。"""
        if not _HAS_FCNTL:
            return
        if not self.lock_fd:
            logger.error("❌ 进程锁不存在，拒绝启动")
            sys.exit(1)
        try:
            os.fsync(self.lock_fd.fileno())
        except Exception as e:
            logger.error(f"❌ 进程锁已失效: {e}，退出以防止多开")
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
                self.lock_fd.close()
            except Exception:
                pass
            sys.exit(1)

    async def start_polling_with_retry(self):
        """带重试的轮询启动"""
        self._verify_process_lock()

        self._is_running = True
        self._current_retry = 0

        while self._is_running and self._current_retry < self._max_retries:
            try:
                self._current_retry += 1
                logger.info(
                    f"🤖 启动 Bot 轮询 (尝试 {self._current_retry}/{self._max_retries})"
                )

                self._verify_process_lock()

                await self.bot.delete_webhook(drop_pending_updates=True)
                logger.info("✅ Webhook 已删除，切换至长轮询模式")

                self._polling_active = True
                self._last_successful_connection = time.time()
                logger.info("✅ Bot 长轮询已开始，等待消息...")

                await self.dispatcher.start_polling(
                    self.bot,
                    skip_updates=True,
                    allowed_updates=[
                        "message",
                        "callback_query",
                        "chat_member",
                        "my_chat_member",
                    ],
                )

                logger.info("Bot 轮询正常结束")
                break

            except asyncio.CancelledError:
                logger.info("Bot 轮询由于任务取消而停止")
                break

            except Exception as e:
                logger.error(f"❌ Bot 轮询异常 (尝试 {self._current_retry}): {e}")

                error_str = str(e).lower()
                if (
                    "conflict" in error_str
                    or "terminated by other getupdates" in error_str
                ):
                    logger.critical(
                        "🚨 检测到 Telegram 多实例冲突，本进程退出"
                    )
                    sys.exit(1)

                if self._current_retry >= self._max_retries:
                    logger.critical(
                        f"🚨 Bot 启动重试 {self._max_retries} 次后全部失败"
                    )
                    break

                delay = min(
                    self._base_delay * (2 ** (self._current_retry - 1)), 300
                )
                logger.info(
                    f"⏳ {delay:.1f} 秒后开始第 {self._current_retry + 1} 次重试..."
                )
                await asyncio.sleep(delay)

            finally:
                self._polling_active = False

        self._is_running = False

    async def stop(self):
        """停止 Bot 并释放资源"""
        self._is_running = False
        self._polling_active = False

        if self.bot and self.bot.session:
            await self.bot.session.close()
            logger.info("Bot 会话已关闭")

        if self.lock_fd and _HAS_FCNTL:
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
                self.lock_fd.close()
                self.lock_fd = None
                logger.info("✅ 进程锁已释放")
            except Exception as e:
                logger.error(f"❌ 释放锁失败: {e}")

    async def send_message_with_retry(self, chat_id: int, text: str, **kwargs) -> bool:
        """带重试的消息发送"""
        max_attempts = 3
        base_delay = 2

        for attempt in range(1, max_attempts + 1):
            try:
                await self.bot.send_message(chat_id, text, **kwargs)
                return True
            except Exception as e:
                error_msg = str(e).lower()
                if any(
                    keyword in error_msg
                    for keyword in [
                        "timeout",
                        "connection",
                        "network",
                        "flood",
                        "retry",
                        "cannot connect",
                        "connectorerror",
                        "ssl",
                        "socket",
                    ]
                ):
                    if attempt == max_attempts:
                        logger.error(f"📤 发送消息重试{max_attempts}次后失败: {e}")
                        return False
                    delay = min(base_delay * (2 ** (attempt - 1)), 30)
                    logger.warning(
                        f"📤 发送消息失败(网络问题)，{delay}秒后重试: {e}"
                    )
                    await asyncio.sleep(delay)
                    continue

                if any(
                    keyword in error_msg
                    for keyword in [
                        "forbidden",
                        "blocked",
                        "unauthorized",
                        "chat not found",
                        "bot was blocked",
                        "user is deactivated",
                    ]
                ):
                    logger.warning(f"📤 发送消息失败(权限问题): {e}")
                    return False

                if attempt == max_attempts:
                    logger.error(f"📤 发送消息重试{max_attempts}次后失败: {e}")
                    return False
                delay = base_delay * attempt
                logger.warning(f"📤 发送消息失败，{delay}秒后重试: {e}")
                await asyncio.sleep(delay)

        return False

    async def send_document_with_retry(
        self, chat_id: int, document, caption: str = "", **kwargs
    ) -> bool:
        """带重试的文档发送"""
        max_attempts = 3

        for attempt in range(1, max_attempts + 1):
            try:
                await self.bot.send_document(
                    chat_id, document, caption=caption, **kwargs
                )
                return True
            except Exception as e:
                error_msg = str(e).lower()
                if any(
                    keyword in error_msg
                    for keyword in [
                        "timeout",
                        "connection",
                        "network",
                        "flood",
                        "retry",
                    ]
                ):
                    if attempt == max_attempts:
                        logger.error(f"📎 发送文档重试{max_attempts}次后失败: {e}")
                        return False
                    delay = attempt * 2
                    logger.warning(f"📎 发送文档失败，{delay}秒后重试: {e}")
                    await asyncio.sleep(delay)
                    continue
                logger.error(f"📎 发送文档失败（不重试）: {e}")
                return False

        return False

    async def send_message_with_protection(
        self, chat_id: int, text: str, **kwargs
    ) -> bool:
        """带熔断器保护的消息发送"""

        async def _send():
            await self.bot.send_message(chat_id, text, **kwargs)
            return True

        try:
            return await telegram_circuit_breaker.call(_send)
        except Exception as e:
            logger.error(f"❌ 熔断器保护的消息发送失败: {e}")
            return False

    def is_healthy(self) -> bool:
        """轮询活跃或近期 Telegram API 探测成功则视为健康。"""
        if self._polling_active:
            return True
        if not self._last_successful_connection:
            return False
        return (
            time.time() - self._last_successful_connection
            < self._connection_check_interval
        )

    async def start_health_monitor(self):
        """后台探测 Telegram API；禁止在轮询中重复 start_polling。"""
        asyncio.create_task(self._health_monitor_loop())

    async def _health_monitor_loop(self):
        """健康监控：仅探测，不并发启动第二个 polling。"""
        while True:
            try:
                await asyncio.sleep(60)

                if not self.bot:
                    continue

                if self._polling_active:
                    try:
                        await self.bot.get_me()
                        self._last_successful_connection = time.time()
                    except Exception as e:
                        logger.error(f"⚠️ Bot API 探测失败（轮询仍运行）: {e}")
                    continue

                if not self.is_healthy():
                    logger.warning(
                        "⚠️ Bot 轮询未运行且 API 探测超时，"
                        "请检查主进程或 Render 日志"
                    )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"健康监控异常: {e}")
                await asyncio.sleep(30)


bot_manager = RobustBotManager(Config.TOKEN)
