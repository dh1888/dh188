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
