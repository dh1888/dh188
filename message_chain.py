"""
Event-driven Context Graph：消息引用唯一真相源。
- user_activity_contexts（按 scope_id 隔离）
- message_map（绑定 context_id）
- event_log（审计）
禁止：global latest / session 读取 / Telegram reply_to 主路径。
"""
import logging
from typing import Any, Dict, List, Optional, Union

from aiogram import types

from config import Config

logger = logging.getLogger("GroupCheckInBot.MessageChain")

SCOPE_WORK = "work"
SCOPE_ACTIVITY = "activity"


async def save_message_relation(
    chat_id: int,
    message_id: int,
    parent_message_id: Optional[int],
    root_message_id: int,
    *,
    user_id: Optional[int] = None,
    context_id: Optional[int] = None,
    role: str = "bot",
) -> None:
    from database import db

    await db.save_message_relation(
        chat_id,
        message_id,
        parent_message_id,
        root_message_id,
        user_id=user_id,
        context_id=context_id,
        role=role,
    )
    if Config.MESSAGE_CHAIN_DEBUG:
        logger.info(
            f"🔗 [message_map] chat={chat_id} msg={message_id} ctx={context_id} "
            f"parent={parent_message_id} root={root_message_id}"
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
                "context_id": row.get("context_id"),
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
    scope_id: str = SCOPE_ACTIVITY,
    context_id: Optional[int] = None,
    prefer_root: bool = False,
) -> Optional[int]:
    """统一引用解析（仅 DB context graph，按 scope 隔离）。"""
    from database import db

    target = await db.resolve_context_reply_target(
        chat_id,
        user_id,
        scope_id=scope_id,
        context_id=context_id,
        prefer_root=prefer_root,
    )
    if Config.MESSAGE_CHAIN_DEBUG:
        active = await db.get_active_context_by_scope(
            chat_id, user_id, scope_id
        )
        logger.info(
            f"🔗 [context] scope={scope_id} user={user_id} chat={chat_id} "
            f"→ {target} prefer_root={prefer_root} "
            f"active_ctx={active.get('id') if active else None}"
        )
    return target


async def get_user_reply_target(chat_id: int, user_id: int) -> Optional[int]:
    return await resolve_context_reply_target(
        chat_id, user_id, scope_id=SCOPE_ACTIVITY
    )


async def message_belongs_to_user_context(
    chat_id: int, user_id: int, message_id: int
) -> bool:
    from database import db

    ctx = await db.get_context_for_message(
        chat_id, message_id, user_id=user_id
    )
    return bool(ctx and int(ctx["user_id"]) == user_id)


async def open_message_context(
    chat_id: int,
    user_id: int,
    context_type: str,
    root_message_id: int,
    current_message_id: int,
    activity_name: Optional[str] = None,
) -> Dict[str, Any]:
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
            f"🔗 [context] open id={ctx['id']} scope={ctx.get('scope_id')} "
            f"type={context_type} user={user_id} root={root_message_id}"
        )
    return ctx


async def complete_message_context(
    chat_id: int,
    user_id: int,
    final_message_id: int,
    *,
    context_type: Optional[str] = "activity",
) -> Optional[Dict[str, Any]]:
    from database import db

    ctx = await db.complete_active_activity_context(
        chat_id,
        user_id,
        final_message_id,
        context_type=context_type,
    )
    if ctx:
        await db.append_event_log(
            chat_id,
            user_id,
            "BOT_MESSAGE_BOUND",
            context_id=int(ctx["id"]),
            message_id=final_message_id,
            payload={"action": "context_complete"},
        )
    return ctx


async def resolve_reply_to_id(
    chat_id: int,
    trigger: Union[types.Message, types.CallbackQuery],
    *,
    user_id: Optional[int] = None,
    scope_id: str = SCOPE_ACTIVITY,
    context_id: Optional[int] = None,
    prefer_root: bool = False,
) -> Optional[int]:
    uid = _message_user_id(trigger, user_id)
    if uid is None:
        return None

    if isinstance(trigger, types.CallbackQuery):
        bot_msg = trigger.message
        ctx = await _get_context_for_callback(
            chat_id, uid, bot_msg.message_id
        )
        if ctx and ctx.get("scope_id") == scope_id:
            if prefer_root:
                return int(ctx["root_message_id"])
            return int(ctx["current_message_id"] or ctx["root_message_id"])

    return await resolve_context_reply_target(
        chat_id,
        uid,
        scope_id=scope_id,
        context_id=context_id,
        prefer_root=prefer_root,
    )


async def _get_context_for_callback(
    chat_id: int, user_id: int, message_id: int
) -> Optional[Dict[str, Any]]:
    from database import db

    return await db.get_context_for_message(
        chat_id, message_id, user_id=user_id
    )


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
    context_id: Optional[int] = None,
    scope_id: str = SCOPE_ACTIVITY,
) -> int:
    from database import db

    bound_context_id = context_id

    if new_thread and user_id is not None and context_type:
        root_id = sent_message_id
        ctx = await open_message_context(
            chat_id,
            user_id,
            context_type,
            root_message_id=sent_message_id,
            current_message_id=sent_message_id,
            activity_name=activity_name,
        )
        bound_context_id = int(ctx["id"])
        await save_message_relation(
            chat_id,
            sent_message_id,
            parent_message_id,
            root_id,
            user_id=user_id,
            context_id=bound_context_id,
        )
        await db.append_event_log(
            chat_id,
            user_id,
            "BOT_MESSAGE_SENT",
            context_id=bound_context_id,
            message_id=sent_message_id,
            payload={"new_thread": True, "context_type": context_type},
        )
        return root_id

    if new_thread:
        root_id = sent_message_id
    elif inherit_session_root and user_id is not None:
        active = await db.get_active_context_by_scope(
            chat_id, user_id, scope_id
        )
        if active:
            root_id = int(active["root_message_id"])
        elif bound_context_id:
            ctx = await db.get_activity_context(bound_context_id)
            root_id = (
                int(ctx["root_message_id"])
                if ctx
                else sent_message_id
            )
        else:
            completed = await db.get_last_completed_context_by_scope(
                chat_id, user_id, scope_id
            )
            root_id = (
                int(completed["root_message_id"])
                if completed
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
        chat_id,
        sent_message_id,
        parent_message_id,
        root_id,
        user_id=user_id,
        context_id=bound_context_id,
    )

    if user_id is not None:
        if bound_context_id:
            await db.update_activity_context_message(
                bound_context_id, sent_message_id
            )
        else:
            await db.update_active_context_message(
                chat_id,
                user_id,
                sent_message_id,
                scope_id=scope_id,
            )
        await db.append_event_log(
            chat_id,
            user_id,
            "BOT_MESSAGE_SENT",
            context_id=bound_context_id,
            message_id=sent_message_id,
            payload={"inherit_root": inherit_session_root},
        )

    return root_id


async def answer_user_message(
    message: types.Message,
    text: str,
    *,
    user_id: Optional[int] = None,
    scope_id: str = SCOPE_ACTIVITY,
    context_id: Optional[int] = None,
    prefer_root: bool = False,
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
            scope_id=scope_id,
            context_id=context_id,
            prefer_root=prefer_root,
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
        context_id=context_id,
        scope_id=scope_id,
    )

    if Config.MESSAGE_CHAIN_DEBUG:
        path = await get_message_chain_path(chat_id, sent.message_id)
        logger.info(
            f"🔗 [answer_user] chat={chat_id} user={uid} scope={scope_id} "
            f"msg={sent.message_id} reply_to={reply_to_id} root={root_id} "
            f"path={path}"
        )

    return sent


async def answer_with_chain(
    trigger: Union[types.Message, types.CallbackQuery],
    text: str,
    *,
    parent_for_record: Optional[int] = None,
    scope_id: str = SCOPE_ACTIVITY,
    context_id: Optional[int] = None,
    prefer_root: bool = False,
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
            scope_id=scope_id,
            context_id=context_id,
            prefer_root=prefer_root,
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
        chat_id,
        sent.message_id,
        parent_for_record,
        user_id=uid,
        scope_id=scope_id,
        context_id=context_id,
    )

    if Config.MESSAGE_CHAIN_DEBUG:
        path = await get_message_chain_path(chat_id, sent.message_id)
        logger.info(
            f"🔗 [message_chain] sent chat={chat_id} user={uid} "
            f"scope={scope_id} msg={sent.message_id} reply_to={reply_to_id} "
            f"root={root_id} path={path}"
        )

    return sent


async def clear_user_session(chat_id: int, user_id: int) -> None:
    from database import db

    await db.clear_user_message_session(chat_id, user_id)
