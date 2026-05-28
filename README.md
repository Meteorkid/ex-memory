# ex-memory — 前任记忆智能体

> 把一段记忆，变成可以对话的人。

[![Version](https://img.shields.io/badge/version-v0.2.0-blue.svg)](CHANGELOG.md)
[![Python](https://img.shields.io/badge/python-3.10+-green.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-lightgrey.svg)](LICENSE)

ex-memory 是一个开源的数字镜像系统，通过分析你和前任的聊天记录，生成一个能还原 ta 语气、性格和表达习惯的 AI 对话体。支持微信、QQ 聊天记录导入，内置 RAG 检索增强、三层记忆架构、关系反思分析，以及一个仿微信 Web 界面。

## 核心特性

- **语气还原**：原话优先于描述 —— 从聊天记录中提取 ta 的真实表达作为语气锚点
- **RAG 检索增强**：每轮对话前自动检索向量库，找到 ta 在类似场景下说过的话
- **三层记忆架构**：潜意识层（向量库原话） > 记忆层（memory.md） > 人格层（persona.md）
- **关系反思**：7 维度深度分析，生成 reflections.md
- **纠正机制**：对话中说"ta不会这样"即可即时修正
- **微信模拟器**：仿微信 Web 界面，支持贴纸、红包、转账、朋友圈
- **纯 Python CLI**：不依赖 Claude，可独立运行

## 快速开始

### 环境要求

- Python 3.10+
- macOS / Linux / Windows

### 安装

```bash
git clone https://github.com/yourname/ex-memory.git
cd ex-memory
pip install -r requirements.txt
```

### 配置 API

复制示例配置并填入你的 API Key：

```bash
cp .env.example .env
```

编辑 `.env`：

```env
# LLM（必填）
LLM_API_KEY=your_api_key_here
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-chat

# Embedding（可选，不配则 RAG 不可用）
EMBEDDING_API_KEY=your_api_key_here
EMBEDDING_BASE_URL=https://api.siliconflow.cn/v1
EMBEDDING_MODEL=BAAI/bge-m3
```

支持任意 OpenAI 兼容端点（硅基流动、DeepSeek、OpenAI 等）。也可将 Key 存入 macOS Keychain：

```bash
python run.py
> /keychain set llm sk-xxx
```

### 启动

**CLI 模式：**
```bash
python run.py
```

**Web API 服务：**
```bash
python -m server.app
```
默认启动在 `http://localhost:8000`，提供 REST API 和静态 Web 前端。

**Gradio Web 界面：**
```bash
python run.py
> /web
```

## 使用指南

### 创建镜像

```
> /create
```

按提示输入：
1. 镜像代号（如 `xiaoming`）
2. 基本信息（在一起多久、分手多久、ta 的职业等）
3. 选择数据源（微信/QQ 聊天记录、口述、截图）

系统自动完成：解析 → 切片 → 入库 → LLM 生成 persona.md + memory.md → 合并 SKILL.md

### 对话

```
> /xiaoming
```

进入对话模式。每轮对话前系统会自动检索向量库，找到 ta 在类似场景下的原话作为语气参考。

### 追加素材

```
> /update xiaoming
```

向已有镜像追加新的聊天记录或口述内容，自动增量合并。

### 关系反思

```
> /reflect xiaoming
```

从 7 个维度分析这段关系，生成反思报告。

### 版本管理

```
> /backup xiaoming          # 备份当前版本
> /rollback xiaoming v1     # 回滚到指定版本
> /let-go xiaoming          # 删除镜像（不可逆）
```

## 命令参考

| 命令 | 说明 |
|------|------|
| `/create` | 创建新的记忆镜像 |
| `/{名称}` | 进入已有镜像的对话模式 |
| `/list` | 列出所有镜像 |
| `/update {名称}` | 向已有镜像追加新素材 |
| `/reflect {名称}` | 关系反思分析 |
| `/backup {名称}` | 备份镜像版本 |
| `/rollback {名称} {版本}` | 回滚到指定版本 |
| `/let-go {名称}` | 删除镜像（不可逆） |
| `/keychain` | 管理 API Key（macOS Keychain） |
| `/web` | 启动 Gradio Web 界面 |
| `/help` | 显示帮助 |
| `/exit` | 退出 |

## 项目结构

```
ex-memory/
├── run.py                    # CLI 主入口
├── config.py                 # 全局配置
├── requirements.txt
├── .env.example
│
├── core/
│   ├── engine.py             # ChatEngine：SKILL.md + RAG → API 调用
│   ├── session.py            # ChatSession：CLI 循环、指令分发
│   ├── token_counter.py      # Token 统计
│   ├── sticker_selector.py   # 情绪贴纸选择器
│   ├── validation.py         # 输入校验 + 注入检测
│   ├── version_manager.py    # 版本备份/回滚
│   ├── wallet_manager.py     # 红包/转账系统
│   └── keychain.py           # macOS Keychain 集成
│
├── memory/
│   ├── vector_store.py       # ChromaDB 封装
│   ├── chunker.py            # 聊天记录切片
│   ├── embedder.py           # Embedding API 封装
│   └── ingest.py             # 数据摄入入口
│
├── pipeline/
│   ├── orchestrator.py       # 创建/更新流程总调度
│   ├── persona_builder.py    # LLM 生成 persona.md
│   ├── memory_builder.py     # LLM 生成 memory.md
│   ├── skill_combiner.py     # 合并 SKILL.md
│   ├── correction_handler.py # 对话纠正
│   ├── merger.py             # 增量合并
│   └── reflector.py          # 关系反思
│
├── parsers/
│   ├── wechat_parser.py      # 微信聊天记录解析
│   └── qq_parser.py          # QQ 聊天记录解析
│
├── commands/                 # CLI 命令模块
│   ├── create.py / chat.py / update.py / ...
│   └── __init__.py           # 命令注册表
│
├── server/
│   ├── app.py                # FastAPI 应用
│   ├── routes.py             # API 路由
│   ├── auth.py               # 用户认证
│   └── middleware.py         # 中间件
│
├── web/
│   ├── app.py                # Gradio Web 界面
│   └── static/               # 微信模拟器 SPA
│
├── prompts/                  # LLM prompt 模板
├── tests/                    # 测试套件
│
└── exes/{slug}/              # 运行时数据
    ├── SKILL.md              # 合并版运行 prompt
    ├── memory.md             # 记忆层
    ├── persona.md            # 人格层（含原话语料库）
    ├── reflections.md        # 关系反思
    ├── corrections.md        # 纠正记录
    ├── meta.json             # 元信息
    ├── chroma_db/            # ChromaDB 持久化
    ├── sessions/             # 对话归档
    └── versions/             # 版本快照
```

## API 端点

Web API 服务提供以下端点（`http://localhost:8000/api`）：

### 认证
| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/auth/register` | 注册 |
| POST | `/auth/login` | 登录 |
| POST | `/auth/logout` | 登出 |

### 镜像管理
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/exes` | 列出所有镜像 |
| POST | `/exes` | 创建镜像 |
| GET | `/exes/{slug}/export` | 导出镜像数据 zip |
| DELETE | `/exes/{slug}` | 彻底删除镜像数据 |
| GET | `/exes/{slug}/conversations/export?format=html` | 导出 ex-memory 对话记录（html/md/json/txt） |
| POST | `/exes/{slug}/import` | 导入聊天记录（自动检测微信/QQ） |
| POST | `/exes/{slug}/update` | 追加素材 |
| POST | `/exes/{slug}/reflect` | 关系反思 |

### 对话
| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/chat` | 普通对话（非流式） |
| POST | `/chat/stream` | 流式对话（SSE） |
| GET | `/exes/{slug}/usage` | 获取 token 累计消耗 |

### 钱包 / 红包 / 转账
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/exes/{slug}/wallet` | 钱包信息 |
| POST | `/exes/{slug}/redpacket/send` | 发红包 |
| POST | `/exes/{slug}/redpacket/{id}/open` | 开红包 |
| POST | `/exes/{slug}/transfer/send` | 转账 |
| POST | `/exes/{slug}/transfer/{id}/confirm` | 确认收款 |

### 朋友圈
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/exes/{slug}/moments` | 朋友圈动态 |

### 本机微信导出
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/wechat-export/status` | 查看本机微信导出环境 |
| GET | `/wechat-export/backups` | 扫描固定 iTunes/iOS 备份目录 |
| POST | `/wechat-export/tasks` | 创建 WechatExporter 后台导出任务 |
| GET | `/wechat-export/tasks/{task_id}` | 查询导出任务状态 |
| GET | `/wechat-export/tasks/{task_id}/files/{path}` | 下载任务输出文件 |

## 配置说明

通过 `.env` 文件配置，支持以下变量：

| 变量 | 必填 | 说明 | 默认值 |
|------|------|------|--------|
| `LLM_API_KEY` | 是 | LLM API Key | — |
| `LLM_BASE_URL` | 否 | API 端点 | `https://api.deepseek.com` |
| `LLM_MODEL` | 否 | 模型名称 | `deepseek-chat` |
| `LLM_TEMPERATURE` | 否 | 生成温度 | `0.8` |
| `EMBEDDING_API_KEY` | 否 | Embedding API Key | — |
| `EMBEDDING_BASE_URL` | 否 | Embedding 端点 | `https://api.siliconflow.cn/v1` |
| `EMBEDDING_MODEL` | 否 | Embedding 模型 | `BAAI/bge-m3` |
| `WECHAT_EXPORTER_BIN` | 否 | 外部 WechatExporter 二进制路径，用于 `/export-wechat` | — |
| `LOCAL_WECHAT_EXPORT_ENABLED` | 否 | 启用网页本机微信导出向导 | `false` |
| `WECHAT_EXPORT_BACKUP_ROOT` | 否 | iTunes/iOS 备份扫描根目录 | `~/Library/Application Support/MobileSync/Backup` |
| `WECHAT_EXPORT_OUTPUT_DIR` | 否 | 微信导出任务输出目录 | `data/wechat_exports` |

未配置 Embedding 时 RAG 检索不可用，但对话仍可正常进行（退化为纯文本模式）。

## 支持的聊天记录格式

### 微信
- WeFlow 导出（JSON / JSONL）
- WeChatMsg 导出（TXT）
- 留痕导出（JSON）

### QQ
- QQ 导出 TXT（时间戳 + 昵称 + QQ 号格式）
- MHT 格式（beta）

### WechatExporter 外部导出

WechatExporter 源码以 git submodule 形式记录在 `third_party/WechatExporter`，授权为 GPL-2.0-or-later，详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。运行时仍通过独立二进制调用，不和 `ex-memory` 主服务链接或混合分发。首次拉取源码可执行：

```bash
git submodule update --init --recursive
```

可选配置 `WECHAT_EXPORTER_BIN` 后，可通过 CLI 调用外部 WechatExporter 从未加密 iTunes/iOS 备份导出微信聊天 HTML：

```bash
export WECHAT_EXPORTER_BIN=/path/to/WechatExporter
python run.py
> /export-wechat ~/Library/Application\ Support/MobileSync/Backup/xxx ~/Desktop/wx-export wxid_xxx 好友名
```

该能力适合本机 CLI 使用，不建议在远程 Web 服务中开放任意本机路径读取。`WECHAT_EXPORTER_BIN` 必须指向已存在且可执行的 WechatExporter 二进制。

网页端提供“发现 → 微信聊天导出”本机向导。该入口还需要 `LOCAL_WECHAT_EXPORT_ENABLED=true`，且仅允许从 localhost 访问；页面只能选择固定根目录下扫描到的 iTunes/iOS 备份，输出固定写入 `data/wechat_exports/{task_id}/`。

## 隐私说明

- 镜像数据存储在本地 `exes/` 目录，自定义贴纸存储在 `data/stickers/custom/`
- 聊天记录仅用于生成记忆镜像，不会用于其他目的
- 用户可通过 `GET /api/exes/{slug}/export` 导出单个镜像数据，通过 `DELETE /api/exes/{slug}` 彻底删除镜像目录
- Web/API 对话会归档到 `exes/{slug}/conversations/conversation.jsonl`，可通过对话导出接口生成 HTML/Markdown/JSON/TXT
- 微信导出任务输出存放在 `data/wechat_exports/`，只通过鉴权和本机限制的 API 下载
- API Key 可存储在 macOS Keychain 中，避免明文写入配置文件
- 建议将 `exes/`、`data/`、`.coverage` 等运行时数据加入 `.gitignore`

## 更新日志

详见 [CHANGELOG.md](CHANGELOG.md)。

### v0.2.0 (2026-05-13)

- QQ 聊天记录解析器（3 种 TXT 格式 + MHT beta）
- Session LLM 语义摘要 + ChromaDB 备份/恢复
- API Token 累计计数（`GET /api/exes/{slug}/usage`）
- run.py 拆分为 `commands/` 注册表模式（-81%）
- Gradio 多用户状态 + 流式对话 + 贴纸/Token 显示
- API 异步化 + 引擎缓存 + 多项 bug 修复

### v0.1.0 (2026-05-12)

- 初始版本：三层记忆架构、RAG 检索增强、微信解析、全自动蒸馏流水线
- 对话引擎、关系反思、纠正机制、版本管理
- 微信模拟器 Web 界面 + FastAPI 服务端 + CLI

## 许可证

MIT License
