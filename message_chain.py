"""个人消息引用链：每位用户独立闭环，互不引用他人消息。"""
from __future__ import annotations

import logging
from typing import Optional

from aiogram import types
from aiogram.types import ForceReply, ReplyKeyboardMarkup

from database import db

logger = logging.getLogger("GroupCheckInBot")


async def get_bot_chain_reply_id(chat_id: int, user_id: int) -> Optional[int]:
    """机器人下一条消息应引用的链内上一条机器人消息；首次无引用。"""
    pending = await db.get_pending_reply_message(chat_id, user_id)
    return int(pending) if pending else None


async def send_activity_card_with_reply_chain(
    message: types.Message,
    text: str,
    chain_reply_id: Optional[int],
    keyboard: ReplyKeyboardMarkup,
) -> types.Message:
    """
    发送活动卡片：
    - 机器人引用链内上一条自身消息（chain_reply_id，首次为 None）
    - ForceReply 使底部 Reply Keyboard「回座」自动引用本条活动消息
    - 紧跟一条仅用于恢复底部键盘的消息（ForceReply 会暂时隐藏自定义键盘）
    """
    sent_message = await message.answer(
        text,
        reply_to_message_id=chain_reply_id,
        reply_markup=ForceReply(force_reply=True, selective=True),
        parse_mode="HTML",
    )
    try:
        await message.answer("\u200b", reply_markup=keyboard)
    except Exception as e:
        logger.warning(f"⚠️ 恢复底部键盘失败: {e}")
    return sent_message


async def register_activity_anchor(
    chat_id: int, user_id: int, activity_message_id: int
) -> None:
    """活动卡片发出后：记录本次活动的机器人锚点（供回座/超时提醒引用）。"""
    await db.update_user_checkin_message(chat_id, user_id, activity_message_id)


async def register_back_chain_end(
    chat_id: int, user_id: int, back_message_id: int
) -> None:
    """回座确认发出后：清除活动锚点，链尾指向回座确认消息。"""
    await db.clear_user_checkin_message(chat_id, user_id)
    await db.update_pending_reply_message(chat_id, user_id, back_message_id)


async def register_bot_chain_message(
    chat_id: int, user_id: int, message_id: int
) -> None:
    """通用：机器人消息写入链尾（如上下班打卡确认）。"""
    await db.update_pending_reply_message(chat_id, user_id, message_id)


def resolve_bot_back_reply_target_id(
    snapshot: Optional[dict],
    user_trigger_message: Optional[types.Message],
    trigger_message: types.Message,
    *,
    from_inline: bool = False,
) -> Optional[int]:
    """
    回座确认引用规则：
    - 底部/指令回座：引用用户回座消息
    - 内联回座（无用户文本）：引用活动卡片机器人消息
    """
    if from_inline:
        if user_trigger_message:
            return user_trigger_message.message_id
        if snapshot:
            checkin_id = snapshot.get("checkin_message_id")
            if checkin_id:
                return int(checkin_id)
        return None
    # 底部 Reply Keyboard / 指令回座：机器人确认引用用户刚发的回座消息
    return trigger_message.message_id
