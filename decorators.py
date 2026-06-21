"""共享装饰器"""
from functools import wraps

from aiogram import types

from config import Config
from keyboards import get_main_keyboard, is_admin


def admin_required(func):
    """管理员权限检查装饰器"""

    @wraps(func)
    async def wrapper(message: types.Message, *args, **kwargs):
        if not await is_admin(message.from_user.id):
            await message.answer(
                Config.MESSAGES["no_permission"],
                reply_markup=await get_main_keyboard(
                    message.chat.id, await is_admin(message.from_user.id)
                ),
            )
            return
        return await func(message, *args, **kwargs)

    return wrapper
