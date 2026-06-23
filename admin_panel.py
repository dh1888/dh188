"""管理员面板指令目录（单一数据源）"""

from typing import Dict, List

_HEADER = "👑 <b>管理员面板</b>\n━━━━━━━━━━━━━━━━\n"

_SECTIONS: Dict[str, str] = {
    "push": (
        "📢 <b>频道与推送</b>\n"
        "├ <code>/setchannel [频道ID]</code> - 绑定导出频道\n"
        "├ <code>/setgroup [群组ID]</code> - 绑定通知群组\n"
        "├ <code>/addextraworkgroup [ID]</code> - 上下班额外推送群\n"
        "├ <code>/clearextraworkgroup</code> - 清除额外推送群\n"
        "├ <code>/setpush [目标] [开关]</code>\n"
        "│  ├ 目标: <code>ch</code> 频道 | <code>gr</code> 群 | <code>ad</code> 管理员\n"
        "│  └ 开关: <code>on</code> | <code>off</code>\n"
        "└ <code>/showeverypush</code> - 查看推送开关\n"
    ),
    "activity": (
        "🎯 <b>活动管理</b>\n"
        "├ <code>/addactivity [名] [次数] [分钟]</code>\n"
        "├ <code>/delactivity [名]</code>\n"
        "├ <code>/actnum [名] [人数]</code>\n"
        "└ <code>/actstatus</code> - 活跃活动统计\n"
    ),
    "fines": (
        "💰 <b>罚款管理</b>\n"
        "├ <code>/setfine [活动] [分钟段] [金额]</code>\n"
        "├ <code>/setfines_all [段1] [元1] ...</code>\n"
        "├ <code>/setworkfine [类型] [分钟] [金额]</code>\n"
        "│  └ 类型: <code>start</code> 上班 | <code>end</code> 下班\n"
        "└ <code>/finesstatus</code> - 查看费率\n"
    ),
    "reset": (
        "🔄 <b>重置设置</b>\n"
        "├ <code>/setresettime [时] [分]</code> - 设定重置时刻（执行=设定+2h）\n"
        "├ <code>/resettime</code> - 查看当前重置时间\n"
        "└ <code>/resetuser [用户ID]</code> - 重置单个用户当日数据\n"
    ),
    "work": (
        "⏰ <b>上下班管理</b>\n"
        "├ <code>/setdualmode on [白班始] [白班终]</code> - 例: on 09:00 21:00\n"
        "├ <code>/setdualmode off</code>\n"
        "├ <code>/setworktime [上班] [下班]</code> - 单班模式\n"
        "├ <code>/setshiftgrace [前分钟] [后分钟]</code> - 上班宽容窗口\n"
        "├ <code>/setworkendgrace [前分钟] [后分钟]</code> - 下班宽容窗口\n"
        "├ <code>/setshiftwindow on|off</code> - 开关打卡时间窗\n"
        "├ <code>/worktime</code> - 查看考勤时间\n"
        "├ <code>/checkdual</code> - 双班配置诊断\n"
        "└ <code>/delwork_clear</code> - 关闭上下班并清记录\n"
    ),
    "handover": (
        "🔁 <b>换班管理</b>\n"
        "├ <code>/handover</code> - 当前换班状态\n"
        "├ <code>/handoverconfig</code> - 查看换班配置\n"
        "├ <code>/handover on|off</code> - 开关换班\n"
        "├ <code>/handover set_night_start HH:MM</code> - 换班夜班开始\n"
        "├ <code>/handover set_handover_day_start HH:MM</code> - 换班白班开始\n"
        "├ <code>/handover set_day_start HH:MM</code> - 换班白班结束/次日白班起点\n"
        "├ <code>/handover set_hours [类型] [小时]</code>\n"
        "├ <code>/sethandoverday [日] [月]</code> - 设置换班锚点日\n"
        "│  ├ <code>/sethandoverday status</code>\n"
        "│  ├ <code>/sethandoverday off</code>\n"
        "│  └ 例: <code>1</code> 每月1号 | <code>31</code> 月末 | <code>15 12</code>\n"
        "└ <code>/sethour [类型] [小时]</code>\n"
        "   └ 类型: handover_night | handover_day | normal_night | normal_day\n"
    ),
    "data": (
        "📊 <b>数据管理</b>\n"
        "├ <code>/export</code> - 导出当前业务日数据\n"
        "├ <code>/exportmonthly [年] [月]</code>\n"
        "├ <code>/monthlyreport [年] [月]</code>\n"
        "├ <code>/cleanup_monthly [年] [月]</code>\n"
        "├ <code>/monthly_stats_status</code>\n"
        "├ <code>/cleanup_inactive [天]</code>\n"
        "└ <code>/fixmessages</code> - 修复消息引用\n"
    ),
    "settings": (
        "💾 <b>系统配置</b>\n"
        "├ <code>/showsettings</code> - 查看群组全部配置\n"
        "├ <code>/checkdb</code> - 数据库健康检查\n"
        "├ <code>/chatid</code> - 本群 ID\n"
        "└ <code>/admin</code> - 打开本面板\n"
    ),
    "debug": (
        "🔧 <b>调试工具</b>\n"
        "├ <code>/testgroupaccess [群组ID]</code>\n"
        "└ <code>/checkperms</code> - 检查机器人权限\n"
    ),
    "query": (
        "📋 <b>查询命令</b>（全员可用）\n"
        "├ <code>/myinfo</code> | <code>/myinfoday</code> | <code>/myinfonight</code>\n"
        "├ <code>/ranking</code> | <code>/rankingday</code> | <code>/rankingnight</code>\n"
        "├ <code>/workstart</code> | <code>/workend</code>\n"
        "├ <code>/ci [活动]</code> | <code>/at</code> 回座\n"
        "├ <code>/menu</code> | <code>/help</code> | <code>/start</code>\n"
        "└ 动态活动命令（由活动配置自动生成）\n"
    ),
}

_SECTION_ORDER: List[str] = [
    "push",
    "activity",
    "fines",
    "reset",
    "work",
    "handover",
    "data",
    "settings",
    "debug",
    "query",
]

_ADMIN_SECTION_BUTTON_KEYS = frozenset(
    {
        "admin_sec_push",
        "admin_sec_activity",
        "admin_sec_fines",
        "admin_sec_reset",
        "admin_sec_work",
        "admin_sec_handover",
        "admin_sec_data",
        "admin_sec_settings",
        "admin_sec_debug",
        "admin_sec_query",
    }
)

_BUTTON_TO_SECTION = {
    "admin_sec_push": "push",
    "admin_sec_activity": "activity",
    "admin_sec_fines": "fines",
    "admin_sec_reset": "reset",
    "admin_sec_work": "work",
    "admin_sec_handover": "handover",
    "admin_sec_data": "data",
    "admin_sec_settings": "settings",
    "admin_sec_debug": "debug",
    "admin_sec_query": "query",
}

_FOOTER = (
    "\n━━━━━━━━━━━━━━━━\n"
    "<i>💡 点击下方分类按钮查看单项；发送 /help 命令名 查看详情</i>"
)


def build_admin_panel_text(section: str = "full") -> str:
    """生成管理员面板 HTML 文本。section=full 为完整目录。"""
    if section == "full":
        body = "\n".join(_SECTIONS[key] for key in _SECTION_ORDER)
        return f"{_HEADER}{body}{_FOOTER}"

    if section in _SECTIONS:
        return f"{_HEADER}{_SECTIONS[section]}{_FOOTER}"

    return build_admin_panel_text("full")


def is_admin_section_button(canonical_key: str) -> bool:
    return canonical_key in _ADMIN_SECTION_BUTTON_KEYS


_ADMIN_UI_BUTTON_KEYS = _ADMIN_SECTION_BUTTON_KEYS | {
    "admin_panel",
    "export_data",
    "back_to_main",
}


def is_admin_ui_button(canonical_key: str) -> bool:
    return canonical_key in _ADMIN_UI_BUTTON_KEYS
