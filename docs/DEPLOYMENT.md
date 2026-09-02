# ex-memory 部署指南

## 方式一：Docker Compose（推荐）

```bash
# 1. 配置环境变量
cp .env.example .env
# 编辑 .env 填入 API Key，或使用 Keychain（仅 macOS）

# 2. 启动
docker compose up -d

# 3. 验证
curl http://localhost:8000/health
curl http://localhost:8000/health/ready
```

服务端口：
- API: `http://localhost:8000`
- Gradio Web: `http://localhost:7860`
- API 文档: `http://localhost:8000/api/docs`

## 方式二：本地运行

### 前置条件

- Python 3.11+
- pip

### 安装

```bash
cd ex-memory
pip install -r requirements.txt

# 配置 API Key（二选一）
# A. 环境变量
cp .env.example .env
vim .env

# B. macOS Keychain（更安全）
python run.py
# 执行: /keychain set llm <your-key>
# 执行: /keychain set embedding <your-key>
```

### 运行

```bash
# Web API + 静态前端
python -m server.app

# 或 CLI 模式
python run.py
```

## 方式三：反向代理部署（生产环境）

### Nginx 配置示例

```nginx
server {
    listen 443 ssl;
    server_name your-domain.com;

    ssl_certificate /etc/letsencrypt/live/your-domain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/your-domain.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # 必须清掉浏览器可能自带的身份头，否则任何用户都能伪造身份越权访问他人镜像。
        # 空值会让 Nginx 不转发该头；SSO 模式下由下面的 auth_request 段重新写入可信值。
        proxy_set_header X-Ex-Memory-User-Id "";
        proxy_set_header X-Ex-Memory-Proxy-Token "";
    }

    # WebSocket 支持（SSE 流式对话需要）
    location /api/chat/stream {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_cache off;
    }
}
```

### Meteor Store 同源 SSO 模式

线上体验使用 `docker-compose.production.yml`，服务仅监听 `127.0.0.1:18000`，不能把
该端口直接暴露到公网。需要配置：

| 变量 | 值 |
|------|----|
| `METEOR_STORE_SSO_ENABLED` | `true` |
| `METEOR_STORE_PROXY_TOKEN` | 与 Meteor Store/Nginx 一致的随机令牌 |
| `PUBLIC_BASE_PATH` | `/ex-memory-runtime` |

代理模式下原生注册、登录和退出接口返回 404，所有业务 API 必须同时收到可信代理写入的
`X-Ex-Memory-User-Id` 与 `X-Ex-Memory-Proxy-Token`。Nginx 必须覆盖而不是透传浏览器提供的
同名请求头，并在转发前通过 Meteor Store `auth_request` 校验 session。

```bash
docker compose -f docker-compose.production.yml build
docker compose -f docker-compose.production.yml up -d
curl http://127.0.0.1:18000/health/ready
```

### HTTPS 配置

```bash
# 使用 Let's Encrypt
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d your-domain.com
```

## 环境变量完整清单

| 变量 | 默认值 | 必需 | 说明 |
|------|--------|------|------|
| `LLM_API_KEY` | - | 是 | LLM API Key |
| `LLM_BASE_URL` | `https://api.deepseek.com` | 否 | LLM API 端点 |
| `LLM_MODEL` | `deepseek-chat` | 否 | 模型名称 |
| `LLM_TEMPERATURE` | `0.8` | 否 | 生成温度 |
| `LLM_MAX_TOKENS` | `4096` | 否 | 最大回复 token |
| `EMBEDDING_API_KEY` | - | RAG 需要 | Embedding API Key |
| `EMBEDDING_BASE_URL` | `https://api.siliconflow.cn/v1` | 否 | Embedding 端点 |
| `EMBEDDING_MODEL` | `BAAI/bge-m3` | 否 | Embedding 模型 |
| `CORS_ORIGINS` | `http://localhost:8000,http://localhost:7860` | 否 | 允许的来源 |
| `CONVERSATION_RETENTION_DAYS` | `90` | 否 | 对话留存天数，超期记录由 `/cleanup` 命令清理 |
| `SINGLE_USER_MODE` | `false` | 否 | 单人本机模式，跳过镜像归属校验 |
| `LOCAL_WECHAT_HELPER_ENABLED` | `false` | 否 | 启用 macOS 本地助手下载入口 |
| `LOCAL_WECHAT_HELPER_VERSION` | 空 | 否 | 本站发布的本地助手版本 |
| `LOCAL_WECHAT_HELPER_MIN_API_VERSION` | `1` | 否 | 网站接受的本地助手最低 API 协议版本 |
| `LOCAL_WECHAT_HELPER_ARM64_URL` | 空 | 否 | 阿里云 HTTPS DMG 地址 |
| `LOCAL_WECHAT_HELPER_ARM64_SHA256` | 空 | 否 | DMG SHA-256，用于网页校验展示 |
| `DISABLE_REGISTRATION` | `false` | 否 | 关闭开放注册 |
| `TRUSTED_PROXY` | `false` | 否 | 反向代理后信任 `X-Forwarded-For` |
| `TRUSTED_PROXY_IPS` | 空 | 启用 `TRUSTED_PROXY` 时必填 | 可信代理的直连 IP 白名单（逗号分隔）。留空时服务会忽略 `X-Forwarded-For` 并按直连 IP 限流 |
| `METEOR_STORE_SSO_ENABLED` | `false` | 否 | 启用 Meteor Store 代理身份模式 |
| `METEOR_STORE_PROXY_TOKEN` | 空 | SSO 模式必需 | Nginx 与服务之间的共享令牌 |
| `PUBLIC_BASE_PATH` | 空 | 否 | 同源子路径，例如 `/ex-memory-runtime` |
| `LOG_LEVEL` | `INFO` | 否 | 日志级别 |
| `LOG_FORMAT` | `text` | 否 | 日志格式 (text/json) |

## 多用户与安全部署

### 部署模式

| 场景 | 推荐配置 |
|------|----------|
| 本机单人使用 | `SINGLE_USER_MODE=true`，可选 `DISABLE_REGISTRATION=true` |
| 多用户共享服务器 | `SINGLE_USER_MODE=false`，配置 `CORS_ORIGINS` 为实际域名，Nginx 启用 HTTPS |

### 镜像隔离

- 每个镜像在 `meta.json` 中记录 `owner_user_id`，创建时自动绑定当前登录用户。
- API 对所有 `exes/{slug}` 操作校验归属；列表接口仅返回当前用户可访问的镜像。
- 旧镜像无 `owner_user_id` 时：多用户模式下不可访问；单人模式下首次访问会自动绑定。

### 其它安全项

- 上传聊天记录：文件名消毒 + 流式大小上限（100MB）。
- 版本备份/回滚：`version_name` 禁止路径穿越。
- 对话 `history`：仅接受 `user`/`assistant` 角色。
- Token：数据库中存储 SHA-256 哈希（非明文）。
- 自定义贴纸：按用户分目录 `web/static/stickers/custom/u{user_id}/`。
- 生产环境启用 `TRUSTED_PROXY=true` 时，**必须同时配置 `TRUSTED_PROXY_IPS`**。
  白名单为空会让服务无法判断 `X-Forwarded-For` 是否可信，此时它会 fail-closed
  地忽略该头并按直连 IP 限流——这意味着所有请求会被算作同一个来源。
- SSO 模式下 `X-Ex-Memory-User-Id` 是身份的唯一依据，Nginx 必须先清空再写入，
  否则浏览器可自带该头冒充任意用户（见上方 Nginx 示例）。

## 对话留存与隐私清理

- **落库脱敏**：导入聊天记录与对话落库时，手机号 / 身份证 / 银行卡 / 邮箱四类敏感信息自动脱敏后才入库。这四类对语气还原没有价值，脱敏不影响拟真度。
- **留存天数**：由 `CONVERSATION_RETENTION_DAYS` 控制（默认 90 天）。
- **清理方式**：服务本身不会自动清理，需要配置 cron 定期执行 `/cleanup` 命令（清理所有镜像中超过留存期的对话记录）：

```bash
# 宿主机 cron：每天 04:00 清理过期对话（路径按实际部署调整）
0 4 * * * cd /path/to/ex-memory && python run.py /cleanup >> logs/cleanup.log 2>&1
```

Docker 部署时可改为在容器内执行：

```bash
0 4 * * * docker compose exec -T ex-memory python run.py /cleanup >> logs/cleanup.log 2>&1
```

## 数据备份

### 备份内容

| 数据 | 路径 | 说明 |
|------|------|------|
| 镜像数据 | `exes/{slug}/` | memory.md, persona.md, SKILL.md |
| 向量库 | `exes/{slug}/chroma_db/` | ChromaDB 持久化 |
| 用户数据 | `data/users.db` | SQLite 用户/Token |
| 审计日志 | `data/audit.log` | 登录/注册记录 |

### 自动备份

```bash
# CLI 中手动备份
/backup <slug>

# 回滚
/rollback <slug> <version>
```

### 手动备份脚本

```bash
#!/bin/bash
# backup.sh
BACKUP_DIR="/backup/ex-memory/$(date +%Y%m%d)"
mkdir -p "$BACKUP_DIR"
cp -r /app/data "$BACKUP_DIR/"
cp -r /app/exes "$BACKUP_DIR/"
echo "Backup complete: $BACKUP_DIR"
```

## 资源需求

| 环境 | CPU | 内存 | 磁盘 |
|------|-----|------|------|
| 开发 | 2 核 | 2 GB | 1 GB |
| 生产（低流量） | 2 核 | 4 GB | 20 GB |
| 生产（高流量） | 4 核 | 8 GB | 50 GB+ |

磁盘需求主要取决于导入的聊天记录大小和 ChromaDB 向量库。

## 健康检查

```bash
# Liveness（K8s liveness probe）
curl http://localhost:8000/health

# Readiness（依赖检查）
curl http://localhost:8000/health/ready
```

Docker Compose 已配置健康检查，每 30 秒检查一次。


## 多副本部署（M1 之后）

M1 把进程内状态、数据库、向量库都搬了出去，服务本身已经无状态，但**镜像
目录仍在文件系统上**。

### 必须配置

| 变量 | 说明 |
|---|---|
| `DATABASE_URL` | Postgres 连接串。不配则用 SQLite——多个副本共享一个 SQLite 文件会锁冲突甚至损坏 |
| `REDIS_URL` | 共享状态。不配则限流额度按副本数翻倍、验证码换台机器失效 |
| `VECTOR_BACKEND=pgvector` | 不配则用 ChromaDB 本地目录，各副本互相看不到对方的向量 |

### 仍需共享卷

`exes/` 目录（镜像的人格档案、对话归档、语料）目前仍走文件系统，多副本
部署**必须把它挂在共享卷上**（NFS / EFS / CSI）。

这是一个已知的、有意保留的限制：这些文件在 29 个模块里有 74 处直接访问，
把它们全部改成对象存储 API 是独立的一次重构，风险高于收益。对象存储当前
承担的是**灾备**职责而非运行时访问。

### 备份

```bash
python run.py
> /snapshot --label daily-20260902 --verify   # 备份并逐字节校验
> /snapshot --list                            # 列出快照
> /snapshot --restore daily-20260902          # 恢复
```

配置 `BLOB_BUCKET` 后快照推到 S3 兼容存储；不配则落在 `data/blobs/`
（仅适合单机，磁盘坏了就一起没了）。

`--verify` 会真的把快照取回来与本地逐字节比对，而不是只看「上传成功」——
没演练过的备份等于没有备份。建议纳入定期演练。
