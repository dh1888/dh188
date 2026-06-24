"""个人消息引用链：每位用户独立闭环，互不引用他人消息。"""
from __future__ import annotations

from typing import Optional

from aiogram import types

from database import db


async def get_bot_chain_reply_id(chat_id: int, user_id: int) -> Optional[int]:
    """机器人下一条消息应引用的链内上一条机器人消息；首次无引用。"""
    pending = await db.get_pending_reply_message(chat_id, user_id)
    return int(pending) if pending else None


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
