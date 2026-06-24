# Telegram 打卡机器人（双班模式）

群组考勤与活动打卡 Bot，支持双班上下班、活动计时、超时罚款、每日重置、月度导出与换班管理。

---

## 环境要求

| 项目 | 说明 |

|------|------|

| Python | 3.12（Docker 部署） |

| 数据库 | PostgreSQL（推荐 [Aiven]([https://aiven.io](https://aiven.io))） |

| Bot Token | 通过 [@BotFather]([https://t.me/BotFather](https://t.me/BotFather)) 创建 |

| 管理员 ID | Telegram 数字 ID（可用 [@userinfobot]([https://t.me/userinfobot](https://t.me/userinfobot)) 查询） |

---

## 部署流程

### 第一步：准备资源

1. **创建 Telegram Bot**  

   向 @BotFather 发送 `/newbot`，保存 `BOT_TOKEN`。

2. **创建 PostgreSQL**  

   - 推荐 Aiven 免费版  

   - 连接串使用 `postgresql://` 格式，并带上 `?sslmode=require`  

   - 示例：  

     `postgresql://user:pass@host:25060/defaultdb?sslmode=require`

3. **获取管理员 ID**  

   你的 Telegram 用户 ID 填入 `ADMINS`（逗号分隔，可多个）。

---

### 第二步：配置环境变量

```bash

cp .env.example .env

# 编辑 .env，至少填写 BOT_TOKEN、DATABASE_URL、ADMINS

```

**必填变量：**

| 变量 | 说明 |

|------|------|

| `BOT_TOKEN` | BotFather 发放的 Token |

| `DATABASE_URL` | PostgreSQL 连接串（含 `sslmode=require`） |

| `ADMINS` | 管理员 Telegram ID，逗号分隔 |

**推荐配置（Render + Aiven）：**

```env

BOT_TOKEN=你的Token

ADMINS=你的Telegram用户ID

DATABASE_URL=postgresql://user:pass@host:port/db?sslmode=require

BOT_MODE=polling

TZ=Asia/Shanghai

DB_MIN_CONNECTIONS=1

DB_MAX_CONNECTIONS=5

LOG_LEVEL=INFO

KEEPALIVE_ENABLED=true

DB_DISK_QUOTA_GB=8

```

> **连接池说明：** Aiven 小套餐连接数有限（约 20 个，部分保留给 superuser）。请保持 `DB_MAX_CONNECTIONS=5`，且 **只运行 1 个 Bot 实例**。若出现 `remaining connection slots are reserved`，在 Aiven 重启数据库或释放空闲连接后重新部署。

---

### 第三步：启动服务（三选一）

#### 方式 A：本地运行

```bash

pip install -r requirements.txt

python [main.py](http://main.py)

```

#### 方式 B：Docker

```bash

docker compose up -d --build

```

#### 方式 C：Render 部署（推荐）

1. 将代码推送到 GitHub  

2. Render 创建 **Web Service**，Runtime 选 **Docker**  

3. 在 Dashboard 填写环境变量`BOT_TOKENDATABASE_URLADMINS` 等）  

4. **Health Check Path** 设为 `/health`  

5. 确认 **实例数量为 1**（不要多副本连同一数据库）  

6. 可选：使用仓库内 `render.yaml` Blueprint 一键部署  

7. 可选：UptimeRobot 每 5–10 分钟 ping `https://你的域名/health`[（防免费实例休眠）](https://你的域名/health`（防免费实例休眠）)

---

### 第四步：部署后验证

1. 浏览器访问 `GET /health`，应返回正常  

2. Telegram 发送 `/start`，Bot 有响应  

3. 管理员账号发送 `/admin`，出现管理员面板  

4. 日志中应出现：  

   - `PostgreSQL连接池创建成功 (min=1, max=5)`  

   - `Bootstrap 完成，系统进入 BOT_READY`

---

### 第五步：工作群初始化（管理员在群内执行）

按顺序执行（时间按实际班次修改）：

```

/setdualmode on 09:00 21:00    # 开启双班

/setchannel -100xxxxxxxxxx     # 绑定导出推送频道（可选）

/setgroup -100xxxxxxxxxx       # 绑定通知群（可选，通常填本群 ID）

/setresettime 0 0             # 每日重置时刻（实际执行 ≈ 设定 + 2 小时）

/setshiftwindow off            # 如需任意时间打卡（可选）

/showsettings                  # 核对配置

```

**Bot 入群权限：** 发送消息、删除消息（用于打卡回复与清理）。

---

## 管理员指令

> 仅 `ADMINS` 环境变量中的用户可用，与 Telegram 群管理员身份无关。  

> 发送 `/admin` 打开面板，或点击键盘「管理员面板」。

### 入口

| 命令 | 说明 |

|------|------|

| `/admin` | 打开管理员面板（分类目录 + 快捷按钮） |

---

### 频道与推送

| 命令 | 用法 | 说明 |

|------|------|------|

| `/setchannel` | `/setchannel <频道ID>` | 绑定 XLSX/报告推送频道 |

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

默认活动：小厕、大厕、吃饭、抽烟或休息。

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

| `/setresettime` | `/setresettime <时> <分>` | 设置每日重置时刻（执行 ≈ 设定 + 2h） |

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

| `/setshiftwindow` | `/setshiftwindow off` | 关闭时间窗口（任意时间可打卡） |

| | `/setshiftwindow on` | 恢复时间窗口限制 |

| `/checkdual` | 无参数 | 检查双班配置 |

| `/delwork_clear` | 无参数 | 移除上下班功能并清除工作记录 |

---

### 换班管理

| 命令 | 用法 | 说明 |

|------|------|------|

| `/handover` | 无参数 | 查看当前换班状态 |

| `/handoverconfig` | 无参数 | 查看换班配置详情 |

| `/sethandoverday` | `/sethandoverday 15` | 每月 15 日换班 |

| | `/sethandoverday 31` | 每月末换班 |

| | `/sethandoverday 15 12` | 每年 12 月 15 日换班 |

| | `/sethandoverday off` | 关闭换班 |

| | `/sethandoverday status` | 查看换班日设置 |

| `/sethour` | `/sethour <类型> <小时>` | 设置班次时长 |

`/sethour` 类型`handover_night` | `handover_day` | `normal_night` | `normal_day`

---

### 数据管理

| 命令 | 用法 | 说明 |

|------|------|------|

| `/export` | 无参数 | 手动导出当前业务日数据（XLSX） |

| `/exportmonthly` | `/exportmonthly [年] [月]` | 导出指定月份数据（XLSX） |

| `/monthlyreport` | `/monthlyreport [年] [月]` | 生成月度报告并导出 |

| `/cleanup_monthly` | `/cleanup_monthly [年] [月]` | 清理指定月份统计 |

| `/monthly_stats_status` | 无参数 | 查看月度统计状态 |

| `/cleanup_inactive` | `/cleanup_inactive [天]` | 清理长期不活跃用户 |

| `/fixmessages` | 无参数 | 清除消息引用 / 会话上下文 |

也可点击管理员键盘 **「导出数据」**，效果同 `/export`。

---

### 系统与调试

| 命令 | 用法 | 说明 |

|------|------|------|

| `/showsettings` | 无参数 | 查看群组全部配置 |

| `/checkdb` | 无参数 | 检查数据库连接状态 |

| `/chatid` | 无参数 | 查看本群 ID |

| `/testgroupaccess` | `/testgroupaccess [群组ID]` | 测试 Bot 能否访问群组 |

| `/checkperms` | 无参数 | 检查 Bot 在群内的权限 |

---

## 普通用户常用命令

| 命令 | 说明 |

|------|------|

| `/start` | 启动机器人 |

| `/menu` | 显示主菜单 |

| `/help` | 帮助 |

| `/workstart` | 上班打卡 |

| `/workend` | 下班打卡 |

| `/ci [活动]` | 开始活动 |

| `/at` | 回座 |

| `/myinfo` | 我的信息 |

| `/myinfoday` / `/myinfonight` | 分班次查看记录 |

| `/ranking` | 排行榜 |

| `/rankingday` / `/rankingnight` | 分班次排行榜 |

活动也可通过 Reply Keyboard 按钮或动态活动命令（由 `/addactivity` 自动生成）完成。

---

## 自动任务说明

| 任务 | 默认时刻 | 说明 |

|------|----------|------|

| 每日硬重置 | 群内 `/setresettime` + 2h | 导出日表 → 归档月表 → 清理日表 |

| 过期数据清理 | 每天 03:00 | 保留日常 30 天、月度 40 天 |

| 月度自动导出 | 每月 1 日 15:00 | 导出上月 XLSX |

| 磁盘兜底清理 | 磁盘 ≥ 85% | 自动缩短保留期并清理历史数据 |

相关环境变量见 [[DEPLOY.md](http://DEPLOY.md)](./[DEPLOY.md](http://DEPLOY.md)) 完整清单。

---

## 常见问题

| 问题 | 处理 |

|------|------|

| 启动报 `BOT_TOKEN 未设置` | 检查环境变量是否注入到运行环境 |

| 管理员命令无权限 | 确认 Telegram ID 在 `ADMINS` 中，重启 Bot |

| 打卡提示不在窗口内 | `/setshiftwindow off` 或加大 grace 值 |

| 重置时间不对 | 实际执行 = `/setresettime` 设定 **+ 约 2 小时** |

| Render 实例休眠 | 开启 `KEEPALIVE_ENABLED`，外部 ping `/health` |

| 数据库连接槽满 | `DB_MAX_CONNECTIONS=5`，单实例运行，Aiven 重启 DB |

| 月度导出无数据 | 确认日表已正常重置归档；用 `/exportmonthly 年 月` 手动测试 |

| 导出文件格式 | 均为 **XLSX**（Excel），不是 CSV |

---

## 健康检查

```text

GET /health

```

Docker 与 Render 均使用该端点做存活探测。

---

## 更多文档

- [[DEPLOY.md](http://DEPLOY.md)](./[DEPLOY.md](http://DEPLOY.md)) — 完整环境变量与运维细节