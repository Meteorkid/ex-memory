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
| `SINGLE_USER_MODE` | `false` | 否 | 单人本机模式，跳过镜像归属校验 |
| `DISABLE_REGISTRATION` | `false` | 否 | 关闭开放注册 |
| `TRUSTED_PROXY` | `false` | 否 | 反向代理后信任 `X-Forwarded-For` |
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
- 生产环境请将 `TRUSTED_PROXY=true` 且仅在内层 Nginx 剥离不可信的 `X-Forwarded-For`。

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
