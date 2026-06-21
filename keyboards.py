"""键盘与基础权限工具"""
import logging
import time

from config import Config
from database import db
from i18n import (
    WORK_BUTTONS_META,
    UI_BUTTONS_META,
    activity_label,
    get_lang_mode,
    input_placeholder,
    make_keyboard_button,
    ui_button_label,
    work_button_label,
)
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger("GroupCheckInBot")

_keyboard_cache: dict[tuple, tuple] = {}
_KEYBOARD_CACHE_TTL = 30
_ACTIVITY_COLS = 3


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


def _chunk_buttons(buttons: list, cols: int) -> list:
    return [buttons[i : i + cols] for i in range(0, len(buttons), cols)]


def build_activity_rows_with_back(
    activity_btns: list, back_btn_a: KeyboardButton, back_btn_b: KeyboardButton
) -> list:
    """
    活动按钮按行排列，末行固定 [最多1个活动, 回座, 回座]（回座占两格）。
    """
    back_slots = 2
    row_size = _ACTIVITY_COLS
    max_act_on_last_row = row_size - back_slots

    if not activity_btns:
        return [[back_btn_a, back_btn_b]]

    if len(activity_btns) <= max_act_on_last_row:
        return [activity_btns + [back_btn_a, back_btn_b]]

    main_activities = activity_btns[:-max_act_on_last_row]
    last_activities = activity_btns[-max_act_on_last_row:]
    rows = _chunk_buttons(main_activities, row_size)
    rows.append(last_activities + [back_btn_a, back_btn_b])
    return rows


def build_inline_back_keyboard(
    chat_id: int, uid: int, shift: str, lang=None
) -> InlineKeyboardMarkup:
    """打卡消息下方的内联回座按钮"""
    lang = lang or get_lang_mode(chat_id)
    label = ui_button_label("back", lang)
    style = UI_BUTTONS_META["back"].get("style")
    btn = InlineKeyboardButton(
        text=label,
        callback_data=f"quick_back:{chat_id}:{uid}:{shift}",
        **({"style": style} if style else {}),
    )
    return InlineKeyboardMarkup(inline_keyboard=[[btn]])


def make_back_reply_buttons(lang) -> tuple:
    """底部键盘回座按钮（两个独立实例，占两格）"""
    back_meta = UI_BUTTONS_META["back"]
    label = ui_button_label("back", lang)
    style = back_meta.get("style")
    return (
        make_keyboard_button(label, style),
        make_keyboard_button(label, style),
    )


# ========== 键盘生成 ==========
async def get_main_keyboard(
    chat_id: int = None, show_admin: bool = False
) -> ReplyKeyboardMarkup:
    """获取主回复键盘（彩色按钮 + 中越双语）"""
    lang = get_lang_mode(chat_id)
    cache_key = (chat_id, show_admin, lang)
    now = time.time()
    if chat_id is not None and cache_key in _keyboard_cache:
        markup, expiry = _keyboard_cache[cache_key]
        if now < expiry:
            return markup

    logger.debug(f"🔄 生成键盘 - chat_id={chat_id}, show_admin={show_admin}, lang={lang}")

    try:
        activity_limits = await db.get_activity_limits_cached()
    except Exception as e:
        logger.error(f"获取活动配置失败: {e}")
        activity_limits = await db.get_activity_limits_cached()

    activity_btns = [
        make_keyboard_button(activity_label(act, lang))
        for act in activity_limits.keys()
    ]

    back_btn_a, back_btn_b = make_back_reply_buttons(lang)

    activity_rows = build_activity_rows_with_back(
        activity_btns, back_btn_a, back_btn_b
    )

    work_row = []
    if chat_id:
        has_work = await db.has_work_hours_enabled(chat_id)
        logger.debug(f"📊 群组 {chat_id} 是否启用上下班: {has_work}")

        if has_work:
            logger.info("✅ 将添加上下班按钮（固定一行）")
            work_row = [
                [
                    make_keyboard_button(
                        work_button_label("work_start_day", lang),
                        WORK_BUTTONS_META["work_start_day"]["style"],
                    ),
                    make_keyboard_button(
                        work_button_label("work_start_night", lang),
                        WORK_BUTTONS_META["work_start_night"]["style"],
                    ),
                    make_keyboard_button(
                        work_button_label("work_end", lang),
                        WORK_BUTTONS_META["work_end"]["style"],
                    ),
                ]
            ]

    bottom_buttons = []
    if show_admin:
        bottom_buttons.append(
            [
                make_keyboard_button(ui_button_label("admin_panel", lang)),
                make_keyboard_button(ui_button_label("my_record", lang)),
                make_keyboard_button(ui_button_label("rank", lang)),
            ]
        )
    else:
        bottom_buttons.append(
            [
                make_keyboard_button(ui_button_label("my_record", lang)),
                make_keyboard_button(ui_button_label("rank", lang)),
            ]
        )

    keyboard = work_row + activity_rows + bottom_buttons

    markup = ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        one_time_keyboard=False,
        input_field_placeholder=input_placeholder(lang),
    )

    if chat_id is not None:
        _keyboard_cache[cache_key] = (markup, now + _KEYBOARD_CACHE_TTL)

    return markup


def invalidate_main_keyboard_cache(chat_id: int = None):
    """配置变更后清除键盘缓存"""
    if chat_id is None:
        _keyboard_cache.clear()
        return
    keys = [k for k in _keyboard_cache if k[0] == chat_id]
    for key in keys:
        _keyboard_cache.pop(key, None)


def get_admin_keyboard() -> ReplyKeyboardMarkup:
    """管理员专用键盘"""
    lang = get_lang_mode()
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [
                make_keyboard_button(ui_button_label("admin_panel", lang)),
                make_keyboard_button(ui_button_label("export_data", lang)),
            ],
            [make_keyboard_button(ui_button_label("back_to_main", lang))],
        ],
        resize_keyboard=True,
    )
    logger.debug("生成管理员键盘")
    return keyboard
