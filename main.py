"""Telegram 打卡机器人入口"""
import asyncio
import logging
import os
import sys
from contextlib import suppress

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8", mode="a"),
    ],
)
logger = logging.getLogger("GroupCheckInBot")
logging.getLogger("aiohttp").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)

from app import main  # noqa: E402

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("机器人已被用户中断")
    except Exception as e:
        logger.error(f"机器人运行异常: {e}")
        sys.exit(1)
