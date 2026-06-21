"""根据数据库活动配置动态生成 Telegram / 命令菜单"""
import logging
import re
import zlib
from typing import Dict, List, Optional, Set, Tuple

from aiogram.types import BotCommand, BotCommandScopeAllChatAdministrators

from config import Config
from database import db

logger = logging.getLogger("GroupCheckInBot")

# 非活动类固定命令
STATIC_USER_COMMANDS: List[BotCommand] = [
    BotCommand(command="workstart", description="🟢 上班打卡"),
    BotCommand(command="workend", description="🔴 下班打卡"),
    BotCommand(command="at", description="✅ 回座"),
    BotCommand(command="myinfo", description="📊 我的记录"),
    BotCommand(command="ranking", description="🏆 排行榜"),
    BotCommand(command="help", description="❓ 使用帮助"),
]

STATIC_ADMIN_COMMANDS: List[BotCommand] = [
    BotCommand(command="actstatus", description="📊 活跃活动统计"),
    BotCommand(command="showsettings", description="⚙️ 查看系统配置"),
    BotCommand(command="finesstatus", description="📈 罚款费率查询"),
    BotCommand(command="worktime", description="⌚ 考勤时间设置"),
    BotCommand(command="export", description="📤 导出今日报表"),
    BotCommand(command="checkdb", description="🏥 数据库体检"),
    BotCommand(command="admin", description="🛠 管理员全指令指南"),
]

_command_to_activity: Dict[str, str] = {}


def generate_command_slug(activity: str, taken: Set[str]) -> str:
    """根据活动名生成 Telegram 命令 slug（仅算法，无硬编码映射）"""
    if re.match(r"^[a-zA-Z][a-zA-Z0-9_]{0,31}$", activity):
        slug = activity.lower()
    else:
        crc = zlib.crc32(activity.encode("utf-8")) & 0xFFFFFFFF
        slug = f"a{crc:07x}"

    slug = slug[:32]
    if slug not in taken:
        taken.add(slug)
        return slug

    base = slug[:28] or "a"
    idx = 2
    while True:
        candidate = f"{base}_{idx}"
        if candidate not in taken:
            taken.add(candidate)
            return candidate
        idx += 1


def get_command_map() -> Dict[str, str]:
    return dict(_command_to_activity)


def is_activity_command(cmd: str) -> bool:
    return cmd.lower() in _command_to_activity


def resolve_activity_command(cmd: str) -> Optional[str]:
    return _command_to_activity.get(cmd.lower())


def build_command_map_from_configs(activity_limits: Dict) -> Dict[str, str]:
    """{command_slug: activity_name}，完全来自数据库活动配置"""
    mapping: Dict[str, str] = {}
    taken: Set[str] = set()
    for activity, cfg in activity_limits.items():
        slug = (cfg or {}).get("command_slug")
        if not slug:
            slug = generate_command_slug(activity, taken)
        elif slug not in taken:
            taken.add(slug)
        mapping[slug] = activity
    return mapping


def build_activity_bot_commands(activity_limits: Dict) -> List[BotCommand]:
    commands: List[BotCommand] = []
    taken: Set[str] = set()
    for activity, cfg in sorted(activity_limits.items()):
        slug = (cfg or {}).get("command_slug")
        if not slug:
            slug = generate_command_slug(activity, taken)
        desc = activity
        if len(desc) > 256:
            desc = desc[:253] + "..."
        commands.append(BotCommand(command=slug, description=desc))
    return commands


async def reload_command_map() -> Dict[str, str]:
    global _command_to_activity
    try:
        activity_limits = await db.get_activity_limits_cached()
    except Exception as e:
        logger.error(f"加载活动命令映射失败: {e}")
        activity_limits = Config.DEFAULT_ACTIVITY_LIMITS.copy()

    _command_to_activity = build_command_map_from_configs(activity_limits)
    logger.info(
        f"📋 活动 / 命令已同步: {len(_command_to_activity)} 个 -> "
        f"{list(_command_to_activity.items())}"
    )
    return _command_to_activity


async def sync_bot_commands(bot) -> Tuple[bool, bool]:
    """将数据库活动同步到 Telegram / 命令菜单"""
    await reload_command_map()

    try:
        activity_limits = await db.get_activity_limits_cached()
    except Exception:
        activity_limits = Config.DEFAULT_ACTIVITY_LIMITS.copy()

    activity_cmds = build_activity_bot_commands(activity_limits)
    user_commands = activity_cmds + STATIC_USER_COMMANDS
    admin_commands = user_commands + STATIC_ADMIN_COMMANDS

    res_user = await bot.set_my_commands(commands=user_commands)
    res_admin = await bot.set_my_commands(
        commands=admin_commands,
        scope=BotCommandScopeAllChatAdministrators(),
    )
    logger.info(
        f"✅ / 菜单已更新: 活动 {len(activity_cmds)} 个, "
        f"合计用户 {len(user_commands)} 个"
    )
    return res_user, res_admin


def extract_command(text: str) -> Optional[str]:
    if not text or not text.startswith("/"):
        return None
    return text.strip().split()[0].split("@")[0][1:].lower()
