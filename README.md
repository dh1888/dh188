# Telegram 打卡机器人（双班模式）

## 环境要求

- Python 3.12（Docker 部署）
- PostgreSQL（推荐 [Aiven](https://aiven.io) 免费版）
- Telegram Bot Token（@BotFather）

## 本地运行

1. 复制环境变量模板：

```bash
cp .env.example .env
```

2. 编辑 `.env`，填写 `BOT_TOKEN`、`DATABASE_URL`、`ADMINS`

3. 安装依赖并启动：

```bash
pip install -r requirements.txt
python main.py
```

## Render + Aiven 部署

1. 在 Aiven 创建 PostgreSQL，复制连接 URI（确保含 `sslmode=require`）
2. 将代码推送到 GitHub
3. 在 Render 创建 **Web Service**，Runtime 选 **Docker**
4. 配置环境变量：`BOT_TOKEN`、`DATABASE_URL`、`ADMINS`、`BOT_MODE=polling`
5. Health Check Path 设为 `/health`
6. 使用 UptimeRobot 等每 5–10 分钟 ping `/health`（免费实例防休眠）

也可使用仓库内 `render.yaml` Blueprint 一键部署（敏感变量在 Dashboard 填写）。

## 健康检查

```text
GET /health
```


# Telegram 打卡机器人 — 部署与管理员手册

---

## 一、部署步骤

### 1. 准备资源

| 项目 | 说明 |
|------|------|
| Telegram Bot | 向 [@BotFather](https://t.me/BotFather) 创建机器人，获取 `BOT_TOKEN` |
| PostgreSQL | 推荐 Aiven 免费版，连接串需含 `?sslmode=require` |
| 管理员 ID | 你的 Telegram 数字 ID（可用 [@userinfobot](https://t.me/userinfobot) 查询） |
| 服务器 | Render / VPS / 本地，需能访问 Telegram API 和 PostgreSQL |

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，至少填写 BOT_TOKEN、DATABASE_URL、ADMINS
```

### 3. 启动方式（三选一）

**本地：**

```bash
pip install -r requirements.txt
python main.py
```

**Docker：**

```bash
docker compose up -d --build
```

**Render：**

1. 推送代码到 GitHub
2. 创建 Web Service，Runtime 选 **Docker**
3. 在 Dashboard 填写环境变量（见下表）
4. Health Check Path 设为 `/health`
5. 可选：UptimeRobot 每 5–10 分钟 ping `https://你的域名/health`（防免费实例休眠）

也可使用仓库内 `render.yaml` Blueprint 一键部署（敏感变量在 Dashboard 填写）。

### 4. 部署后验证

1. 浏览器访问 `GET /health` 应返回正常
2. 在 Telegram 私聊或群里发送 `/start`
3. 用 `ADMINS` 中的账号发送 `/admin` 应出现管理员面板
4. 在目标工作群执行初始化命令（见「部署后必做」）

---

## 二、环境变量清单

### 必填（缺少则启动失败）

| 变量名 | 示例 | 说明 |
|--------|------|------|
| `BOT_TOKEN` | `7123456789:AAH...` | BotFather 发放的 Token |
| `DATABASE_URL` | `postgresql://user:pass@host:port/db?sslmode=require` | PostgreSQL 连接串 |
| `ADMINS` | `123456789,987654321` | 管理员 Telegram 用户 ID，逗号分隔，至少 1 个 |

> 管理员权限由 `ADMINS` 控制，与 Telegram 群管理员身份无关。

---

### 运行模式与网络

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `BOT_MODE` | `polling` | `polling`（长轮询）或 `webhook` |
| `WEBHOOK_URL` | 空 | Webhook 模式必填，完整 HTTPS 地址 |
| `PORT` | `10000` | HTTP 健康检查端口；Render 会自动注入 |
| `TZ` | `Asia/Shanghai` | 时区（Docker 建议显式设置） |

---

### 数据库连接池

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `DB_MIN_CONNECTIONS` | `3` | 最小连接数（Render 免费版建议 `2`） |
| `DB_MAX_CONNECTIONS` | `10` | 最大连接数（Render 免费版建议 `5`） |
| `DB_POOL_RECYCLE` | `1800` | 连接回收时间（秒） |
| `DB_CONNECTION_TIMEOUT` | `60` | 连接超时（秒） |
| `DB_HEALTH_CHECK_INTERVAL` | `30` | 健康检查间隔（秒） |

---

### 每日重置（全局默认，可被群内 `/setresettime` 覆盖）

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `DAILY_RESET_HOUR` | `0` | 新群默认重置时刻（小时 0–23） |
| `DAILY_RESET_MINUTE` | `0` | 新群默认重置时刻（分钟 0–59） |

> 实际硬重置执行时间 = 设定时间 **+ 2 小时**（±5 分钟）。例如设 `0:00` → 约 **02:00** 执行。

---

### 换班 / 班次（全局默认）

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `HANDOVER_ENABLED` | `true` | 是否启用换班功能 |
| `HANDOVER_NIGHT_START` | `21:00` | 换班日夜班开始时间 |
| `HANDOVER_DAY_START` | `09:00` | 换班日白班开始时间 |
| `HANDOVER_NIGHT_HOURS` | `18` | 换班日夜班总时长（小时） |
| `HANDOVER_DAY_HOURS` | `18` | 换班日白班总时长（小时） |
| `NORMAL_NIGHT_HOURS` | `12` | 正常夜班时长（小时） |
| `NORMAL_DAY_HOURS` | `12` | 正常白班时长（小时） |
| `HANDOVER_RESET_THRESHOLD_HOURS` | `12` | 换班日计数重置阈值（小时） |

---

### 数据保留与自动任务

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `DATA_RETENTION_DAYS` | `30` | 日常数据保留天数 |
| `MONTHLY_DATA_RETENTION_DAYS` | `40` | 月度数据保留天数 |
| `MONTHLY_EXPORT_ENABLED` | `true` | 是否启用每月自动导出 |
| `MONTHLY_EXPORT_HOUR` | `15` | 每月导出时刻（小时，北京时间） |
| `MONTHLY_EXPORT_MINUTE` | `0` | 每月导出时刻（分钟） |
| `AUTO_CLEANUP_ENABLED` | `true` | 是否自动清理过期数据 |
| `CLEANUP_HOUR` | `3` | 自动清理时刻（小时） |
| `CLEANUP_MINUTE` | `0` | 自动清理时刻（分钟） |
| `CLEANUP_INTERVAL` | `600` | 内存清理间隔（秒） |

---

### 日志、保活与界面

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `LOG_LEVEL` | `INFO` | 日志级别 |
| `ENABLE_PERFORMANCE_MONITOR` | `true` | 性能监控 |
| `MAX_MESSAGE_LENGTH` | `4096` | 单条消息最大长度 |
| `KEEPALIVE_ENABLED` | `true` | 保活循环（Render 防休眠） |
| `KEEPALIVE_INTERVAL` | `600` | 保活间隔（秒） |
| `EXTERNAL_MONITORING_URLS` | 空 | 额外监控 URL，逗号分隔 |
| `BOT_UI_LANG` | `both` | 界面语言：`both`（中越双语）\| `zh` \| `vi` |

---

### 平台自动注入（一般无需手动设置）

| 变量名 | 说明 |
|--------|------|
| `RENDER` | Render 平台标识 |
| `RENDER_EXTERNAL_URL` | Render 服务外网 URL |
| `RENDER_INSTANCE_INDEX` | Render 实例编号 |

---

### `.env` 最小示例

```env
BOT_TOKEN=你的Token
ADMINS=你的Telegram用户ID
DATABASE_URL=postgresql://user:pass@host:port/db?sslmode=require

BOT_MODE=polling
TZ=Asia/Shanghai
DB_MIN_CONNECTIONS=2
DB_MAX_CONNECTIONS=5
LOG_LEVEL=INFO
KEEPALIVE_ENABLED=true
```

---

## 三、部署后必做（管理员在**工作群**内执行）

建议按顺序执行：

```
1. /setdualmode on 09:00 21:00     # 开启双班（按实际班次改时间）
2. /setchannel -100xxxxxxxxxx      # 绑定数据推送频道（可选）
3. /setgroup -100xxxxxxxxxx        # 绑定通知群组（可选，通常填本群 ID）
4. /setresettime 0 0               # 设置每日重置时刻（实际执行 = 设定+2h）
5. /setshiftwindow off             # 若需任意时间上下班打卡（可选）
6. /showsettings                   # 核对当前配置
```

将 Bot 加入工作群，并赋予：**发送消息、删除消息**（用于打卡回复与清理）。

---

## 四、管理员指令大全

> 以下命令仅 `ADMINS` 环境变量中的用户可用。在群内发送 `/admin` 或点击「管理员面板」可查看快捷入口。

---

### 入口

| 命令 | 说明 |
|------|------|
| `/admin` | 打开管理员面板（键盘 + 命令列表） |

---

### 频道与推送

| 命令 | 用法 | 说明 |
|------|------|------|
| `/setchannel` | `/setchannel <频道ID>` | 绑定 CSV/报告推送频道 |
| `/setgroup` | `/setgroup <群组ID>` | 绑定通知群组 |
| `/addextraworkgroup` | `/addextraworkgroup <群组ID>` | 上下班额外推送群 |
| `/clearextraworkgroup` | 无参数 | 清除额外推送群 |
| `/setpush` | `/setpush <ch\|gr\|ad> <on\|off>` | 全局推送开关（频道/群/管理员） |
| `/showeverypush` | 无参数 | 查看所有推送配置 |

---

### 活动管理

| 命令 | 用法 | 说明 |
|------|------|------|
| `/addactivity` | `/addactivity <名> <次数> <分钟>` | 新增活动类型 |
| `/delactivity` | `/delactivity <名>` | 删除活动 |
| `/actnum` | `/actnum <名> <人数>` | 设置活动并发人数上限 |
| `/actstatus` | 无参数 | 查看各活动当前使用人数 |

默认活动：小厕、大厕、吃饭、抽烟或休息（可在库中配置）。

---

### 罚款管理

| 命令 | 用法 | 说明 |
|------|------|------|
| `/setfine` | `/setfine <活动名> <分钟段> <金额>` | 设置单个活动超时罚款 |
| `/setfines_all` | `/setfines_all <段1> <元1> [段2 元2 ...]` | 批量设置所有活动罚款 |
| `/setworkfine` | `/setworkfine <work_start\|work_end> <分> <元> ...` | 上下班迟到/早退罚款 |
| `/finesstatus` | 无参数 | 查看当前罚款规则 |

---

### 重置设置

| 命令 | 用法 | 说明 |
|------|------|------|
| `/setresettime` | `/setresettime <时> <分>` | 设置每日重置时刻（执行=设定+2h） |
| `/resettime` | 无参数 | 查看当前重置时间 |
| `/resetuser` | `/resetuser <用户ID> confirm` | 重置指定用户当日数据 |

---

### 上下班管理

| 命令 | 用法 | 说明 |
|------|------|------|
| `/setdualmode` | `/setdualmode on 09:00 21:00` | 开启双班模式 |
| | `/setdualmode off` | 关闭双班模式 |
| `/setworktime` | `/setworktime 09:00 18:00` | 设置单班上下班时间 |
| `/worktime` | 无参数 | 查看当前上下班时间 |
| `/setshiftgrace` | `/setshiftgrace <前分钟> <后分钟>` | 上班打卡宽容窗口 |
| `/setworkendgrace` | `/setworkendgrace <前分钟> <后分钟>` | 下班打卡宽容窗口 |
| `/setshiftwindow` | `/setshiftwindow off` | **关闭**时间窗口（任意时间可打卡） |
| | `/setshiftwindow on` | **恢复**时间窗口限制 |
| `/checkdual` | 无参数 | 检查双班配置是否正常 |
| `/delwork_clear` | 无参数 | 移除上下班功能并清除工作记录 |

**窗口默认值：** 上班前 120 分钟 / 上班后 360 分钟；下班前 120 分钟 / 下班后 360 分钟。

**放宽示例：**

```
/setshiftgrace 720 720
/setworkendgrace 720 720
```

---

### 换班管理

| 命令 | 用法 | 说明 |
|------|------|------|
| `/handover` | 无参数 | 查看当前换班状态 |
| `/handoverconfig` | 无参数 | 查看换班配置详情 |
| `/sethandoverday` | `/sethandoverday 15` | 每月 15 日换班 |
| | `/sethandoverday 31` | 每月末换班 |
| | `/sethandoverday 15 12` | 每年 12 月 15 日换班 |
| | `/sethandoverday off` | 关闭换班功能 |
| | `/sethandoverday status` | 查看换班日设置 |
| `/sethour` | `/sethour <类型> <小时>` | 设置班次时长 |

`/sethour` 类型：`handover_night` | `handover_day` | `normal_night` | `normal_day`

---

### 数据管理

| 命令 | 用法 | 说明 |
|------|------|------|
| `/export` | 无参数 | 手动导出当日 CSV 并推送 |
| `/exportmonthly` | `/exportmonthly [年] [月]` | 导出指定月份数据 |
| `/monthlyreport` | `/monthlyreport [年] [月]` | 生成月度报告 |
| `/cleanup_monthly` | `/cleanup_monthly [年] [月]` | 清理指定月份统计 |
| `/monthly_stats_status` | 无参数 | 查看月度统计状态 |
| `/cleanup_inactive` | `/cleanup_inactive [天]` | 清理长期不活跃用户 |
| `/fixmessages` | 无参数 | 清除所有消息引用记录 |

也可点击管理员键盘中的 **「导出数据」** 按钮，效果同 `/export`。

---

### 查看与调试

| 命令 | 用法 | 说明 |
|------|------|------|
| `/showsettings` | 无参数 | 查看群组全部当前设置 |
| `/checkdb` | 无参数 | 检查数据库连接状态 |
| `/testgroupaccess` | `/testgroupaccess [群组ID]` | 测试 Bot 能否访问群组 |
| `/checkperms` | 无参数 | 检查 Bot 在群内的权限 |

---

## 五、普通用户常用命令（参考）

| 命令 | 说明 |
|------|------|
| `/start` | 启动机器人 |
| `/menu` | 显示主菜单 |
| `/help` | 帮助 |
| `/workstart` | 上班打卡 |
| `/workend` | 下班打卡 |
| `/myinfo` | 我的信息 |
| `/ranking` | 排行榜 |
| `/myinfoday` / `/myinfonight` | 分班次查看记录 |
| `/rankingday` / `/rankingnight` | 分班次排行榜 |

活动打卡通过键盘按钮或 `/ci`、`/at` 等活动命令完成。

---

## 六、常见问题

| 问题 | 处理 |
|------|------|
| 启动报 `BOT_TOKEN 未设置` | 检查环境变量是否注入到运行环境 |
| 管理员命令无权限 | 确认 Telegram ID 在 `ADMINS` 中且已重启 Bot |
| 打卡提示不在窗口内 | `/setshiftwindow off` 或加大 grace 值 |
| 重置时间不对 | 记住实际执行 = `/setresettime` 设定 **+ 2 小时** |
| Render 实例休眠 | 开启 `KEEPALIVE_ENABLED`，外部 ping `/health` |

---

## 七、健康检查

```text
GET /health
```

Docker 与 Render 均使用该端点做存活探测。
