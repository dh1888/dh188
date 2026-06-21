"""按钮常量、FSM 状态与 Bot 运行时上下文"""
import time
from typing import Dict

from aiogram.fsm.state import State, StatesGroup

# Bot 运行时上下文（避免循环导入）
bot = None
dp = None
start_time = time.time()
active_back_processing: Dict[str, float] = {}

# 双班上下班按钮
BTN_WORK_START_DAY = "🟢 白班上班"
BTN_WORK_START_NIGHT = "⚫ 夜班上班"
BTN_WORK_END = "🔴 下班"
WORK_BUTTONS = {BTN_WORK_START_DAY, BTN_WORK_START_NIGHT, BTN_WORK_END}

SPECIAL_BUTTONS = {
    "👑 管理员面板": "admin_panel",
    "🔙 返回主菜单": "back_to_main",
    "📤 导出数据": "export_data",
    "📊 我的记录": "my_record",
    "🏆 排行榜": "rank",
    "✅ 回座": "back",
    BTN_WORK_START_DAY: "work_start_day",
    BTN_WORK_START_NIGHT: "work_start_night",
    BTN_WORK_END: "work_end",
}

ACTIVITY_MAP = {
    "wc_small": "小厕",
    "wc_large": "大厕",
    "smoke": "抽烟",
    "eat": "吃饭",
}


class AdminStates(StatesGroup):
    waiting_for_channel_id = State()
    waiting_for_group_id = State()
