"""
Telegram 消息引用链 + Activity Context Map。
唯一真相源：user_activity_contexts（DB）。
user_message_sessions 仅作 write-through 缓存，不参与读取决策。
绝不以 Telegram reply_to_message 作为主引用来源。
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


async def _sync_session_cache(
    chat_id: int,
    user_id: int,
    bot_message_id: int,
    root_message_id: int,
) -> None:
    """session 仅作 write-through 缓存，供调试；读取一律走 activity context。"""
    from database import db

    await db.upsert_user_message_session(
        chat_id, user_id, bot_message_id, root_message_id
    )
    if Config.MESSAGE_CHAIN_DEBUG:
        logger.debug(
            f"🔗 [session-cache] chat={chat_id} user={user_id} "
            f"bot={bot_message_id} root={root_message_id}"
        )


async def get_root_message_id(
    chat_id: int,
    message_id: Optional[int],
    *,
    fallback_parent_id: Optional[int] = None,
) -> Optional[int]:
    if message_id is None:
        return None

    from database import db

    row = await db.get_message_relation(chat_id, message_id)
    if row:
        return int(row["root_message_id"])

    if fallback_parent_id and fallback_parent_id != message_id:
        parent_root = await get_root_message_id(chat_id, fallback_parent_id)
        if parent_root:
            return parent_root

    return int(message_id)


async def get_message_chain_path(
    chat_id: int, message_id: int, *, max_hops: int = 20
) -> List[Dict[str, Any]]:
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


async def resolve_context_reply_target(
    chat_id: int,
    user_id: int,
    *,
    context_id: Optional[int] = None,
) -> Optional[int]:
    """统一引用解析入口（仅 DB activity context，不读 Telegram）。"""
    from database import db

    target = await db.resolve_context_reply_target(
        chat_id, user_id, context_id=context_id
    )
    if Config.MESSAGE_CHAIN_DEBUG and target:
        ctx = await db.get_active_activity_context(chat_id, user_id)
        logger.info(
            f"🔗 [context] reply target chat={chat_id} user={user_id} "
            f"→ {target} active_ctx={ctx.get('id') if ctx else None}"
        )
    return target


async def get_user_reply_target(chat_id: int, user_id: int) -> Optional[int]:
    """兼容别名：等同于 resolve_context_reply_target。"""
    return await resolve_context_reply_target(chat_id, user_id)


async def message_belongs_to_user_context(
    chat_id: int, user_id: int, message_id: int
) -> bool:
    """判断 message_id 是否属于该用户自己的 activity context。"""
    from database import db

    ctx = await db.get_context_for_message(chat_id, message_id)
    return bool(ctx and int(ctx["user_id"]) == user_id)


async def open_message_context(
    chat_id: int,
    user_id: int,
    context_type: str,
    root_message_id: int,
    current_message_id: int,
    activity_name: Optional[str] = None,
) -> Dict[str, Any]:
    """开启新的 activity context（活动开始 / 上下班打卡）。"""
    from database import db

    ctx = await db.open_activity_context(
        chat_id,
        user_id,
        context_type,
        root_message_id,
        current_message_id,
        activity_name=activity_name,
    )
    if Config.MESSAGE_CHAIN_DEBUG:
        logger.info(
            f"🔗 [context] open id={ctx['id']} type={context_type} "
            f"chat={chat_id} user={user_id} root={root_message_id}"
        )
    return ctx


async def complete_message_context(
    chat_id: int,
    user_id: int,
    final_message_id: int,
    *,
    context_type: Optional[str] = "activity",
) -> Optional[Dict[str, Any]]:
    """结束 active context（回座 / 下班）。"""
    from database import db

    ctx = await db.complete_active_activity_context(
        chat_id,
        user_id,
        final_message_id,
        context_type=context_type,
    )
    if Config.MESSAGE_CHAIN_DEBUG and ctx:
        logger.info(
            f"🔗 [context] complete id={ctx['id']} type={ctx.get('context_type')} "
            f"final={final_message_id}"
        )
    return ctx


async def resolve_reply_to_id(
    chat_id: int,
    trigger: Union[types.Message, types.CallbackQuery],
    *,
    user_id: Optional[int] = None,
    context_id: Optional[int] = None,
    business_root_hint: Optional[int] = None,
) -> Optional[int]:
    """
    计算 bot 发送时应使用的 reply_to_message_id。
    唯一来源：activity context DB。Inline callback 额外校验消息归属。
    """
    uid = _message_user_id(trigger, user_id)
    if uid is None:
        return None

    if context_id is not None:
        return await resolve_context_reply_target(
            chat_id, uid, context_id=context_id
        )

    if isinstance(trigger, types.CallbackQuery):
        bot_msg = trigger.message
        ctx = await _get_context_for_callback(chat_id, uid, bot_msg.message_id)
        if ctx:
            return int(ctx["current_message_id"] or ctx["root_message_id"])

    target = await resolve_context_reply_target(chat_id, uid)
    if target:
        return target

    if business_root_hint:
        ctx = await _get_context_for_callback(
            chat_id, uid, business_root_hint
        )
        if ctx:
            return int(ctx["root_message_id"])

    return None


async def _get_context_for_callback(
    chat_id: int, user_id: int, message_id: int
) -> Optional[Dict[str, Any]]:
    from database import db

    ctx = await db.get_context_for_message(chat_id, message_id)
    if ctx and int(ctx["user_id"]) == user_id:
        return ctx
    return None


async def record_bot_outgoing(
    chat_id: int,
    sent_message_id: int,
    parent_message_id: Optional[int],
    *,
    user_id: Optional[int] = None,
    new_thread: bool = False,
    inherit_session_root: bool = False,
    context_type: Optional[str] = None,
    activity_name: Optional[str] = None,
) -> int:
    """
    bot 发出消息后登记 message_map 并同步 activity context。
    new_thread=True + context_type：open 新 context。
    否则：更新 active context 的 current_message_id。
    """
    from database import db

    if new_thread and user_id is not None and context_type:
        root_id = sent_message_id
        await save_message_relation(
            chat_id, sent_message_id, parent_message_id, root_id
        )
        await open_message_context(
            chat_id,
            user_id,
            context_type,
            root_message_id=sent_message_id,
            current_message_id=sent_message_id,
            activity_name=activity_name,
        )
        await _sync_session_cache(chat_id, user_id, sent_message_id, root_id)
        return root_id

    if new_thread:
        root_id = sent_message_id
    elif inherit_session_root and user_id is not None:
        active = await db.get_active_activity_context(chat_id, user_id)
        if active:
            root_id = int(active["root_message_id"])
        else:
            latest = await db.get_latest_context_anchor(chat_id, user_id)
            root_id = (
                int(latest["root_message_id"])
                if latest
                else sent_message_id
            )
    elif parent_message_id:
        parent_row = await db.get_message_relation(chat_id, parent_message_id)
        root_id = (
            int(parent_row["root_message_id"])
            if parent_row
            else sent_message_id
        )
    else:
        root_id = sent_message_id

    await save_message_relation(
        chat_id, sent_message_id, parent_message_id, root_id
    )

    if user_id is not None:
        updated = await db.update_active_context_message(
            chat_id, user_id, sent_message_id
        )
        if updated is None:
            latest = await db.get_latest_context_anchor(chat_id, user_id)
            if latest:
                await db.update_activity_context_message(
                    int(latest["id"]), sent_message_id
                )
        await _sync_session_cache(chat_id, user_id, sent_message_id, root_id)

    return root_id


async def answer_user_message(
    message: types.Message,
    text: str,
    *,
    user_id: Optional[int] = None,
    context_id: Optional[int] = None,
    business_root_hint: Optional[int] = None,
    record_parent_id: Optional[int] = None,
    reply_to_override: Optional[int] = None,
    new_thread: bool = False,
    inherit_session_root: bool = True,
    context_type: Optional[str] = None,
    activity_name: Optional[str] = None,
    **kwargs,
) -> types.Message:
    chat_id = message.chat.id
    uid = user_id or (message.from_user.id if message.from_user else None)

    reply_to_id = (
        reply_to_override
        if reply_to_override is not None
        else await resolve_reply_to_id(
            chat_id,
            message,
            user_id=uid,
            context_id=context_id,
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
                f"⚠️ [message_chain] 引用 target={reply_to_id} 发送失败，降级: {e}"
            )
            send_kwargs.pop("reply_to_message_id", None)
            sent = await message.answer(text, **send_kwargs)
        else:
            raise

    parent_id = (
        record_parent_id if record_parent_id is not None else message.message_id
    )
    root_id = await record_bot_outgoing(
        chat_id,
        sent.message_id,
        parent_id,
        user_id=uid,
        new_thread=new_thread,
        inherit_session_root=inherit_session_root and not new_thread,
        context_type=context_type,
        activity_name=activity_name,
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
    context_id: Optional[int] = None,
    business_root_hint: Optional[int] = None,
    reply_to_override: Optional[int] = None,
    user_id: Optional[int] = None,
    **kwargs,
) -> types.Message:
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
            context_id=context_id,
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
            f"⚠️ [message_chain] 引用 target={reply_to_id} 发送失败，降级: {e}"
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


# 兼容旧调用
async def clear_user_session(chat_id: int, user_id: int) -> None:
    from database import db

    await db.clear_user_message_session(chat_id, user_id)
