"""机器人被加入群组/频道时自动发送 Chat ID，便于管理员绑定。"""

import logging

from aiogram import types
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.types import ChatMemberUpdated

from bot_manager import bot_manager
from database import db

logger = logging.getLogger("GroupCheckInBot")

_INACTIVE_STATUSES = frozenset(
    {
        ChatMemberStatus.LEFT,
        ChatMemberStatus.KICKED,
    }
)
_ACTIVE_STATUSES = frozenset(
    {
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.CREATOR,
        ChatMemberStatus.RESTRICTED,
    }
)


def _bot_was_added(update: ChatMemberUpdated) -> bool:
    old_status = update.old_chat_member.status
    new_status = update.new_chat_member.status
    return old_status in _INACTIVE_STATUSES and new_status in _ACTIVE_STATUSES


def _build_channel_welcome(chat: types.Chat) -> str:
    cid = chat.id
    title = chat.title or "本频道"
    return (
        f"🤖 <b>机器人已加入频道</b>\n\n"
        f"📢 频道：<code>{title}</code>\n"
        f"🆔 频道 ID：<code>{cid}</code>\n\n"
        f"📌 <b>绑定步骤</b>\n"
        f"1. 在<b>打卡群</b>（不是频道里）执行：\n"
        f"   <code>/setchannel {cid}</code>\n"
        f"2. 确认频道推送已开启：<code>/setpush ch on</code>\n\n"
        f"💡 绑定后可接收：活动超时、迟到/早退、吃饭等通知\n"
        f"⚠️ 请确保机器人为频道管理员并具有<b>发消息</b>权限"
    )


def _build_group_welcome(chat: types.Chat) -> str:
    cid = chat.id
    title = chat.title or "本群"
    chat_type = "超级群" if chat.type == ChatType.SUPERGROUP else "群组"
    return (
        f"🤖 <b>机器人已加入{chat_type}</b>\n\n"
        f"💬 名称：<code>{title}</code>\n"
        f"🆔 群组 ID：<code>{cid}</code>\n\n"
        f"📌 <b>ID 用法</b>\n"
        f"• 若这是<b>打卡群</b>：绑定频道 → <code>/setchannel &lt;频道ID&gt;</code>\n"
        f"• 若这是<b>通知群</b>：在打卡群执行 → <code>/setgroup {cid}</code>\n"
        f"• 查看当前配置 → <code>/showsettings</code>\n\n"
        f"💡 也可随时发送 <code>/chatid</code> 查看本聊天 ID"
    )


async def on_my_chat_member(update: ChatMemberUpdated):
    """机器人被加入群组或频道时发送 Chat ID"""
    if not _bot_was_added(update):
        return

    chat = update.chat
    chat_id = chat.id

    if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        try:
            await db.init_group(chat_id)
        except Exception as e:
            logger.warning(f"初始化群组 {chat_id} 失败: {e}")
        text = _build_group_welcome(chat)
    elif chat.type == ChatType.CHANNEL:
        text = _build_channel_welcome(chat)
    else:
        logger.debug(f"忽略 chat_member 更新: type={chat.type}, id={chat_id}")
        return

    try:
        bot = bot_manager.bot
        if bot is None:
            logger.warning("bot 未初始化，无法发送加入欢迎消息")
            return

        await bot.send_message(chat_id, text, parse_mode="HTML")
        logger.info(
            f"✅ 已发送 Chat ID 提示: type={chat.type}, id={chat_id}, "
            f"title={chat.title!r}"
        )
    except Exception as e:
        logger.error(
            f"❌ 发送加入提示失败: chat_id={chat_id}, type={chat.type}, err={e}"
        )
        if chat.type == ChatType.CHANNEL:
            logger.info(
                "💡 频道内发送失败时，请先将机器人设为管理员并授予发消息权限，"
                "或在打卡群使用 /chatid 手动获取频道 ID"
            )


async def cmd_chatid(message: types.Message):
    """返回当前聊天 ID（群/频道/私聊）"""
    chat = message.chat
    cid = chat.id

    if chat.type == ChatType.CHANNEL:
        hint = (
            f"🆔 频道 ID：<code>{cid}</code>\n\n"
            f"在打卡群执行：<code>/setchannel {cid}</code>"
        )
    elif chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        hint = (
            f"🆔 群组 ID：<code>{cid}</code>\n\n"
            f"• 打卡群绑定频道：<code>/setchannel &lt;频道ID&gt;</code>\n"
            f"• 绑定本群为通知群（在打卡群执行）：<code>/setgroup {cid}</code>"
        )
    else:
        hint = f"🆔 当前聊天 ID：<code>{cid}</code>"

    title = chat.title or chat.username or str(cid)
    await message.answer(
        f"📋 <b>{title}</b>\n{hint}",
        parse_mode="HTML",
        reply_to_message_id=message.message_id,
    )
