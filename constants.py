"""按钮常量、FSM 状态与 Bot 运行时上下文"""
import time
from typing import Dict

from aiogram.fsm.state import State, StatesGroup

from i18n import (
    all_special_button_texts,
    all_work_button_texts,
    resolve_button,
    work_button_label,
    ui_button_label,
)

# Bot 运行时上下文（避免循环导入）
bot = None
dp = None
start_time = time.time()
active_back_processing: Dict[str, float] = {}

# 双班上下班按钮（默认双语标签，兼容旧版 emoji 标签）
BTN_WORK_START_DAY = work_button_label("work_start_day")
BTN_WORK_START_NIGHT = work_button_label("work_start_night")
BTN_WORK_END = work_button_label("work_end")
WORK_BUTTONS = all_work_button_texts()

SPECIAL_BUTTONS = {
    ui_button_label("admin_panel"): "admin_panel",
    ui_button_label("back_to_main"): "back_to_main",
    ui_button_label("export_data"): "export_data",
    ui_button_label("my_record"): "my_record",
    ui_button_label("rank"): "rank",
    ui_button_label("back"): "back",
    BTN_WORK_START_DAY: "work_start_day",
    BTN_WORK_START_NIGHT: "work_start_night",
    BTN_WORK_END: "work_end",
}

# 所有可识别的特殊按钮文本（含中越双语与旧版 emoji）
for _text in all_special_button_texts() | all_work_button_texts():
    resolved = resolve_button(_text)
    if resolved in (
        "admin_panel",
        "back_to_main",
        "export_data",
        "my_record",
        "rank",
        "back",
        "work_start_day",
        "work_start_night",
        "work_end",
    ):
        SPECIAL_BUTTONS[_text] = resolved

ACTIVITY_MAP = {
    "wc_small": "小厕",
    "wc_large": "大厕",
    "smoke": "抽烟",
    "eat": "吃饭",
}


class AdminStates(StatesGroup):
    waiting_for_channel_id = State()
    waiting_for_group_id = State()
