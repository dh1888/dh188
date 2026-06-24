"""
Telegram 消息引用链 + Reply Keyboard 用户 session。
bot 回复始终引用业务 root_message_id；Reply Keyboard 纯文本通过 user_session 恢复上下文。
"""
import logging
from typing import Any, Dict, List, Optional, Union

from aiogram import types

from config import Config

logger = logging.getLogger("GroupCheckInBot.MessageChain")


async def save_message_relation(
    chat_id: int,
    message_id: int,
    parent_message_id: Optional[int],
    root_message_id: int,
) -> None:
    """保存 bot/业务消息在链中的位置。"""
    from database import db

    await db.save_message_relation(
        chat_id, message_id, parent_message_id, root_message_id
    )
    if Config.MESSAGE_CHAIN_DEBUG:
        logger.info(
            f"🔗 [message_chain] save chat={chat_id} msg={message_id} "
            f"parent={parent_message_id} root={root_message_id}"
        )


async def get_user_session(
    chat_id: int, user_id: int
) -> Optional[Dict[str, Any]]:
    """获取用户 Reply Keyboard 上下文（含 TTL）。"""
    from database import db

    session = await db.get_user_message_session(chat_id, user_id)
    if session and Config.MESSAGE_CHAIN_DEBUG:
        logger.debug(
            f"🔗 [session] hit chat={chat_id} user={user_id} "
            f"root={session.get('last_root_message_id')} "
            f"bot={session.get('last_bot_message_id')}"
        )
    return session


async def update_user_session(
    chat_id: int,
    user_id: int,
    last_bot_message_id: int,
    last_root_message_id: int,
) -> None:
    """bot 发消息后更新用户 session。"""
    from database import db

    await db.upsert_user_message_session(
        chat_id, user_id, last_bot_message_id, last_root_message_id
    )
    if Config.MESSAGE_CHAIN_DEBUG:
        logger.info(
            f"🔗 [session] update chat={chat_id} user={user_id} "
            f"bot={last_bot_message_id} root={last_root_message_id}"
        )


async def clear_user_session(chat_id: int, user_id: int) -> None:
    from database import db

    await db.clear_user_message_session(chat_id, user_id)


async def get_root_message_id(
    chat_id: int,
    message_id: Optional[int],
    *,
    fallback_parent_id: Optional[int] = None,
) -> Optional[int]:
    """
    获取业务链 root message id。
    - 已在 map 中 → 返回 root_message_id
    - 不在 map 中 → 返回 message_id 自身
    - 提供 fallback_parent_id 时向上追溯（root 丢失回退 parent）
    """
    if message_id is None:
        return None

    from database import db

    row = await db.get_message_relation(chat_id, message_id)
    if row:
        root = int(row["root_message_id"])
        if Config.MESSAGE_CHAIN_DEBUG:
            logger.debug(
                f"🔗 [message_chain] root hit chat={chat_id} msg={message_id} → {root}"
            )
        return root

    if fallback_parent_id and fallback_parent_id != message_id:
        parent_root = await get_root_message_id(chat_id, fallback_parent_id)
        if parent_root:
            if Config.MESSAGE_CHAIN_DEBUG:
                logger.debug(
                    f"🔗 [message_chain] root fallback chat={chat_id} "
                    f"msg={message_id} via parent={fallback_parent_id} → {parent_root}"
                )
            return parent_root

    if Config.MESSAGE_CHAIN_DEBUG:
        logger.debug(
            f"🔗 [message_chain] root self chat={chat_id} msg={message_id}"
        )
    return int(message_id)


async def get_message_chain_path(
    chat_id: int, message_id: int, *, max_hops: int = 20
) -> List[Dict[str, Any]]:
    """Debug：从某 message 向上追溯 parent 路径。"""
    from database import db

    path: List[Dict[str, Any]] = []
    current = message_id
    for _ in range(max_hops):
        row = await db.get_message_relation(chat_id, current)
        if not row:
            path.append({"message_id": current, "known": False})
            break
        path.append(
            {
                "message_id": current,
                "parent_message_id": row.get("parent_message_id"),
                "root_message_id": row.get("root_message_id"),
                "known": True,
            }
        )
        parent = row.get("parent_message_id")
        if not parent or parent == current:
            break
        current = int(parent)
    return path


def _message_user_id(
    trigger: Union[types.Message, types.CallbackQuery],
    user_id: Optional[int] = None,
) -> Optional[int]:
    if user_id is not None:
        return user_id
    if isinstance(trigger, types.CallbackQuery):
        return trigger.from_user.id if trigger.from_user else None
    return trigger.from_user.id if trigger.from_user else None


def _trigger_reply_to_id(trigger: types.Message) -> Optional[int]:
    if trigger.reply_to_message:
        return trigger.reply_to_message.message_id
    return None


async def message_belongs_to_user_context(
    chat_id: int, user_id: int, message_id: int
) -> bool:
    """判断 message_id 是否属于该用户自己的业务引用链（非他人打卡消息）。"""
    from database import db

    session = await get_user_session(chat_id, user_id)
    if session:
        own_ids = {
            int(x)
            for x in (
                session.get("last_bot_message_id"),
                session.get("last_root_message_id"),
            )
            if x
        }
        if message_id in own_ids:
            return True

    checkin_id = await db.get_user_checkin_message_id(chat_id, user_id)
    if checkin_id and message_id == int(checkin_id):
        return True

    if session and session.get("last_root_message_id"):
        row = await db.get_message_relation(chat_id, message_id)
        if row and int(row["root_message_id"]) == int(session["last_root_message_id"]):
            return True

    return False


async def get_user_reply_target(chat_id: int, user_id: int) -> Optional[int]:
    """
    该用户 Reply Keyboard 应引用的 bot 消息。
    优先最近一条发给该用户的 bot 消息，再回退 root / checkin。
    """
    from database import db

    session = await get_user_session(chat_id, user_id)
    if session:
        last_bot = session.get("last_bot_message_id")
        if last_bot:
            return int(last_bot)
        last_root = session.get("last_root_message_id")
        if last_root:
            return int(last_root)

    checkin_id = await db.get_user_checkin_message_id(chat_id, user_id)
    if checkin_id:
        return int(checkin_id)

    return None


async def resolve_reply_to_id(
    chat_id: int,
    trigger: Union[types.Message, types.CallbackQuery],
    *,
    user_id: Optional[int] = None,
    business_root_hint: Optional[int] = None,
) -> Optional[int]:
    """
    计算 bot 发送时应使用的 reply_to_message_id。
    Reply Keyboard：仅引用本用户 session / checkin，忽略他人打卡消息。
    """
    uid = _message_user_id(trigger, user_id)

    if isinstance(trigger, types.CallbackQuery):
        bot_msg = trigger.message
        if uid and await message_belongs_to_user_context(
            chat_id, uid, bot_msg.message_id
        ):
            root = await get_root_message_id(
                chat_id,
                bot_msg.message_id,
                fallback_parent_id=(
                    bot_msg.reply_to_message.message_id
                    if bot_msg.reply_to_message
                    else None
                ),
            )
            if root:
                return root
        if uid:
            own = await get_user_reply_target(chat_id, uid)
            if own:
                return own
        root = await get_root_message_id(
            chat_id,
            bot_msg.message_id,
            fallback_parent_id=(
                bot_msg.reply_to_message.message_id
                if bot_msg.reply_to_message
                else None
            ),
        )
        if root:
            return root
        if business_root_hint:
            return await get_root_message_id(chat_id, business_root_hint)
        return bot_msg.message_id

    # Reply Keyboard / 纯文本：session 优先，避免引用群里他人打卡的 bot 消息
    if uid:
        own_target = await get_user_reply_target(chat_id, uid)
        if own_target:
            if Config.MESSAGE_CHAIN_DEBUG:
                logger.info(
                    f"🔗 [session] own target chat={chat_id} user={uid} → {own_target}"
                )
            return own_target

    reply_to = _trigger_reply_to_id(trigger)
    if reply_to and uid and await message_belongs_to_user_context(
        chat_id, uid, reply_to
    ):
        root = await get_root_message_id(chat_id, reply_to)
        if root:
            return root

    if business_root_hint and uid:
        checkin = await get_user_reply_target(chat_id, uid)
        if checkin:
            return checkin
        hinted = await get_root_message_id(chat_id, business_root_hint)
        if hinted:
            return hinted

    return None


async def record_bot_outgoing(
    chat_id: int,
    sent_message_id: int,
    parent_message_id: Optional[int],
    *,
    user_id: Optional[int] = None,
    new_thread: bool = False,
    inherit_session_root: bool = False,
) -> int:
    """
    bot 发出消息后登记引用链并更新 user_session。
    new_thread=True：本条作为新的业务 root（上下班打卡/开始活动）。
    inherit_session_root=True：沿用该用户 session 内已有 root（错误提示等）。
    """
    from database import db

    if new_thread:
        root_id = sent_message_id
    elif inherit_session_root and user_id is not None:
        session = await get_user_session(chat_id, user_id)
        if session and session.get("last_root_message_id"):
            root_id = int(session["last_root_message_id"])
        else:
            root_id = sent_message_id
    elif parent_message_id:
        parent_row = await db.get_message_relation(chat_id, parent_message_id)
        if parent_row:
            root_id = int(parent_row["root_message_id"])
        else:
            root_id = sent_message_id
    else:
        root_id = sent_message_id

    await save_message_relation(
        chat_id, sent_message_id, parent_message_id, root_id
    )

    if user_id is not None:
        await update_user_session(
            chat_id, user_id, sent_message_id, root_id
        )

    return root_id


async def answer_user_message(
    message: types.Message,
    text: str,
    *,
    user_id: Optional[int] = None,
    business_root_hint: Optional[int] = None,
    record_parent_id: Optional[int] = None,
    reply_to_override: Optional[int] = None,
    new_thread: bool = False,
    inherit_session_root: bool = True,
    **kwargs,
) -> types.Message:
    """
    Reply Keyboard / 用户文本触发的 bot 回复统一入口。
    自动引用 session root，禁止 reply_to=用户本条 message_id。
    """
    chat_id = message.chat.id
    uid = user_id or (message.from_user.id if message.from_user else None)

    reply_to_id = (
        reply_to_override
        if reply_to_override is not None
        else await resolve_reply_to_id(
            chat_id,
            message,
            user_id=uid,
            business_root_hint=business_root_hint,
        )
    )

    send_kwargs = dict(kwargs)
    if reply_to_id is not None:
        send_kwargs["reply_to_message_id"] = reply_to_id

    try:
        sent = await message.answer(text, **send_kwargs)
    except Exception as e:
        if reply_to_id is not None:
            logger.warning(
                f"⚠️ [message_chain] 引用 root={reply_to_id} 发送失败，降级: {e}"
            )
            send_kwargs.pop("reply_to_message_id", None)
            sent = await message.answer(text, **send_kwargs)
        else:
            raise

    parent_id = record_parent_id if record_parent_id is not None else message.message_id
    root_id = await record_bot_outgoing(
        chat_id,
        sent.message_id,
        parent_id,
        user_id=uid,
        new_thread=new_thread,
        inherit_session_root=inherit_session_root and not new_thread,
    )

    if Config.MESSAGE_CHAIN_DEBUG:
        path = await get_message_chain_path(chat_id, sent.message_id)
        logger.info(
            f"🔗 [answer_user] chat={chat_id} user={uid} msg={sent.message_id} "
            f"reply_to={reply_to_id} root={root_id} path={path}"
        )

    return sent


async def answer_with_chain(
    trigger: Union[types.Message, types.CallbackQuery],
    text: str,
    *,
    parent_for_record: Optional[int] = None,
    business_root_hint: Optional[int] = None,
    reply_to_override: Optional[int] = None,
    user_id: Optional[int] = None,
    **kwargs,
) -> types.Message:
    """发送 bot 回复并维护 message_map + user_session。"""
    if isinstance(trigger, types.CallbackQuery):
        message = trigger.message
        chat_id = message.chat.id
        uid = _message_user_id(trigger, user_id)
        if parent_for_record is None:
            parent_for_record = message.message_id
    else:
        message = trigger
        chat_id = message.chat.id
        uid = _message_user_id(trigger, user_id)
        if parent_for_record is None:
            parent_for_record = message.message_id

    reply_to_id = (
        reply_to_override
        if reply_to_override is not None
        else await resolve_reply_to_id(
            chat_id,
            trigger,
            user_id=uid,
            business_root_hint=business_root_hint,
        )
    )

    send_kwargs = dict(kwargs)
    if reply_to_id is not None:
        send_kwargs["reply_to_message_id"] = reply_to_id

    try:
        sent = await message.answer(text, **send_kwargs)
    except Exception as e:
        logger.warning(
            f"⚠️ [message_chain] 引用 root={reply_to_id} 发送失败，降级: {e}"
        )
        send_kwargs.pop("reply_to_message_id", None)
        sent = await message.answer(text, **send_kwargs)

    root_id = await record_bot_outgoing(
        chat_id, sent.message_id, parent_for_record, user_id=uid
    )

    if Config.MESSAGE_CHAIN_DEBUG:
        path = await get_message_chain_path(chat_id, sent.message_id)
        logger.info(
            f"🔗 [message_chain] sent chat={chat_id} user={uid} msg={sent.message_id} "
            f"reply_to={reply_to_id} root={root_id} path={path}"
        )

    return sent
