"""键盘与基础权限工具"""
import logging
from config import Config
from database import db
from constants import (
    BTN_WORK_START_DAY, BTN_WORK_START_NIGHT, BTN_WORK_END,
    WORK_BUTTONS, SPECIAL_BUTTONS,
)
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

logger = logging.getLogger("GroupCheckInBot")

async def is_admin(uid: int) -> bool:
    """检查用户是否为管理员"""
    return uid in Config.ADMINS


async def calculate_work_fine(checkin_type: str, late_minutes: float) -> int:
    """根据分钟阈值动态计算上下班罚款金额"""
    work_fine_rates = await db.get_work_fine_rates_for_type(checkin_type)
    if not work_fine_rates:
        return 0

    thresholds = sorted([int(k) for k in work_fine_rates.keys() if str(k).isdigit()])
    late_minutes_abs = abs(late_minutes)

    applicable_fine = 0
    for threshold in thresholds:
        if late_minutes_abs >= threshold:
            applicable_fine = work_fine_rates[str(threshold)]
        else:
            break

    return applicable_fine
# ========== 键盘生成 ==========
async def get_main_keyboard(
    chat_id: int = None, show_admin: bool = False
) -> ReplyKeyboardMarkup:
    """获取主回复键盘"""
    logger.debug(f"🔄 生成键盘 - chat_id={chat_id}, show_admin={show_admin}")

    try:
        activity_limits = await db.get_activity_limits_cached()
    except Exception as e:
        logger.error(f"获取活动配置失败: {e}")
        activity_limits = await db.get_activity_limits_cached()

    dynamic_buttons = []
    current_row = []

    for act in activity_limits.keys():
        current_row.append(KeyboardButton(text=act))
        if len(current_row) >= 3:
            dynamic_buttons.append(current_row)
            current_row = []

    # 添加详细日志
    if chat_id:
        work_hours = await db.get_group_work_time(chat_id)
        has_work = await db.has_work_hours_enabled(chat_id)
        logger.debug(f"📊 群组 {chat_id} 工作时间: {work_hours}, 是否启用: {has_work}")

        if has_work:
            if current_row:
                dynamic_buttons.append(current_row)
                current_row = []
            logger.info("✅ 将添加双班上下班按钮到键盘")
            dynamic_buttons.append(
                [
                    KeyboardButton(text=BTN_WORK_START_NIGHT),
                    KeyboardButton(text=BTN_WORK_START_DAY),
                    KeyboardButton(text=BTN_WORK_END),
                ]
            )
        else:
            logger.debug("❌ 不添加上班/下班按钮")

    if current_row:
        dynamic_buttons.append(current_row)

    fixed_buttons = []
    fixed_buttons.append([KeyboardButton(text="✅ 回座")])

    bottom_buttons = []
    if show_admin:
        bottom_buttons.append(
            [
                KeyboardButton(text="👑 管理员面板"),
                KeyboardButton(text="📊 我的记录"),
                KeyboardButton(text="🏆 排行榜"),
            ]
        )
    else:
        bottom_buttons.append(
            [KeyboardButton(text="📊 我的记录"), KeyboardButton(text="🏆 排行榜")]
        )

    keyboard = dynamic_buttons + fixed_buttons + bottom_buttons

    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        one_time_keyboard=False,
        input_field_placeholder="请选择操作或输入活动名称...",
    )


def get_admin_keyboard() -> ReplyKeyboardMarkup:
    """管理员专用键盘"""
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="👑 管理员面板"),
                KeyboardButton(text="📤 导出数据"),
            ],
            [KeyboardButton(text="🔙 返回主菜单")],
        ],
        resize_keyboard=True,
    )
    logger.debug("生成管理员键盘")
    return keyboard

