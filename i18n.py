"""界面多语言与按钮样式（Telegram Bot API 9.4+ style 字段）"""
import os
from typing import Dict, Literal, Optional, Set

from aiogram.types import KeyboardButton

LangMode = Literal["both", "zh", "vi"]

STYLE_SUCCESS = "success"  # 绿色
STYLE_DANGER = "danger"  # 红色
STYLE_PRIMARY = "primary"  # 蓝色

DEFAULT_LANG_MODE: LangMode = os.getenv("BOT_UI_LANG", "both")  # type: ignore[assignment]

# 活动名（内部仍用中文）-> 越南语显示
ACTIVITY_VI: Dict[str, str] = {
    "吃饭": "Đi ăn",
    "大厕": "Nhà vệ sinh lớn",
    "小厕": "Nhà vệ sinh nhỏ",
    "抽烟或休息": "Nghỉ hoặc hút thuốc",
}

WORK_BUTTONS_META = {
    "work_start_day": {
        "zh": "白班上班",
        "vi": "Ca ngày Đi làm",
        "style": STYLE_SUCCESS,
        "legacy": ("🟢 白班上班",),
    },
    "work_start_night": {
        "zh": "夜班上班",
        "vi": "Ca đêm Đi làm",
        "style": STYLE_PRIMARY,
        "legacy": ("⚫ 夜班上班",),
    },
    "work_end": {
        "zh": "下班",
        "vi": "Tan ca",
        "style": STYLE_DANGER,
        "legacy": ("🔴 下班",),
    },
}

UI_BUTTONS_META = {
    "back": {
        "zh": "回座",
        "vi": "Trở lại chỗ ngồi",
        "style": STYLE_PRIMARY,
        "legacy": ("✅ 回座",),
    },
    "admin_panel": {"zh": "管理员面板", "vi": "Bảng quản trị", "style": None, "legacy": ("👑 管理员面板",)},
    "my_record": {"zh": "我的记录", "vi": "Lịch sử của tôi", "style": None, "legacy": ("📊 我的记录",)},
    "rank": {"zh": "排行榜", "vi": "Bảng xếp hạng", "style": None, "legacy": ("🏆 排行榜",)},
    "export_data": {"zh": "导出数据", "vi": "Xuất dữ liệu", "style": None, "legacy": ("📤 导出数据",)},
    "back_to_main": {"zh": "返回主菜单", "vi": "Về menu chính", "style": None, "legacy": ("🔙 返回主菜单",)},
}


def get_lang_mode(chat_id: int = None) -> LangMode:
    """群组界面语言；后续可扩展为按群配置"""
    _ = chat_id
    mode = DEFAULT_LANG_MODE
    if mode in ("both", "zh", "vi"):
        return mode
    return "both"


def format_label(zh: str, vi: Optional[str] = None, mode: LangMode = "both") -> str:
    if mode == "zh":
        return zh
    if mode == "vi":
        return vi or zh
    if vi and vi != zh:
        return f"{zh} / {vi}"
    return zh


def activity_label(activity: str, mode: LangMode = "both") -> str:
    vi = ACTIVITY_VI.get(activity)
    return format_label(activity, vi, mode)


def _labels_for_meta(meta: dict, mode: LangMode) -> str:
    return format_label(meta["zh"], meta.get("vi"), mode)


def make_keyboard_button(label: str, style: Optional[str] = None) -> KeyboardButton:
    if style:
        return KeyboardButton(text=label, style=style)
    return KeyboardButton(text=label)


def work_button_label(key: str, mode: LangMode = "both") -> str:
    meta = WORK_BUTTONS_META[key]
    return _labels_for_meta(meta, mode)


def ui_button_label(key: str, mode: LangMode = "both") -> str:
    meta = UI_BUTTONS_META[key]
    return _labels_for_meta(meta, mode)


def _register_lookup(lookup: Dict[str, str], canonical: str, *texts: str):
    for text in texts:
        if text:
            lookup[text] = canonical


def _build_button_lookup() -> Dict[str, str]:
    lookup: Dict[str, str] = {}

    for key, meta in WORK_BUTTONS_META.items():
        zh, vi = meta["zh"], meta.get("vi")
        _register_lookup(lookup, key, zh, vi, format_label(zh, vi, "both"))
        for legacy in meta.get("legacy", ()):
            _register_lookup(lookup, key, legacy)

    for key, meta in UI_BUTTONS_META.items():
        zh, vi = meta["zh"], meta.get("vi")
        _register_lookup(lookup, key, zh, vi, format_label(zh, vi, "both"))
        for legacy in meta.get("legacy", ()):
            _register_lookup(lookup, key, legacy)

    for activity, vi in ACTIVITY_VI.items():
        _register_lookup(lookup, activity, activity, vi, format_label(activity, vi, "both"))

    lookup["回座"] = "back"
    lookup["back"] = "back"

    return lookup


BUTTON_LOOKUP: Dict[str, str] = _build_button_lookup()


def resolve_button(text: str) -> str:
    """将按钮显示文本解析为内部 canonical key 或活动名"""
    return BUTTON_LOOKUP.get(text.strip(), text.strip())


def resolve_activity_name(text: str, activity_limits: Dict) -> Optional[str]:
    """将按钮/输入文本匹配到数据库中的活动名"""
    text = text.strip()
    if not text or not activity_limits:
        return None
    if text in activity_limits:
        return text
    resolved = resolve_button(text)
    if resolved in activity_limits:
        return resolved
    for act in activity_limits:
        for mode in ("both", "zh", "vi"):
            if activity_label(act, mode) == text:  # type: ignore[arg-type]
                return act
    return None


def is_work_button_action(text: str) -> bool:
    return resolve_button(text.strip()) in (
        "work_start_day",
        "work_start_night",
        "work_end",
    )


def all_work_button_texts() -> Set[str]:
    texts: Set[str] = set()
    for key in WORK_BUTTONS_META:
        for mode in ("both", "zh", "vi"):
            texts.add(work_button_label(key, mode))  # type: ignore[arg-type]
        for legacy in WORK_BUTTONS_META[key].get("legacy", ()):
            texts.add(legacy)
    return texts


def all_back_button_texts() -> Set[str]:
    texts: Set[str] = set()
    for mode in ("both", "zh", "vi"):
        texts.add(ui_button_label("back", mode))  # type: ignore[arg-type]
    for legacy in UI_BUTTONS_META["back"].get("legacy", ()):
        texts.add(legacy)
    texts.add("回座")
    return texts


def all_special_button_texts() -> Set[str]:
    texts: Set[str] = set()
    for key in UI_BUTTONS_META:
        for mode in ("both", "zh", "vi"):
            texts.add(ui_button_label(key, mode))  # type: ignore[arg-type]
        for legacy in UI_BUTTONS_META[key].get("legacy", ()):
            texts.add(legacy)
    return texts


def input_placeholder(mode: LangMode = "both") -> str:
    if mode == "vi":
        return "Chọn thao tác hoặc nhập tên hoạt động..."
    if mode == "zh":
        return "请选择操作或输入活动名称..."
    return "请选择操作 / Chọn thao tác..."
