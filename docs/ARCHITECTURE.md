# ex-memory 架构文档

## 系统概览

```
用户 ─── CLI (run.py) ───┐
                          ├── ChatEngine ──┬── LLM API (DeepSeek/OpenAI)
用户 ─── Web UI (SPA) ───┤                ├── ChromaDB (向量检索)
                          ├── FastAPI ─────┤
用户 ─── Gradio ──────────┘                └── Embedding API (SiliconFlow)
```

## 三层记忆模型

ex-memory 模仿人脑的分层记忆结构：

| 层 | 存储 | 检索方式 | 用途 |
|----|------|---------|------|
| **潜意识层** | ChromaDB 向量库 | 语义检索 (RAG) | ta 真实的原话，语气锚点 |
| **记忆层** | memory.md | 全文加载到 system prompt | 关系事实、关键事件 |
| **人格层** | persona.md | 全文加载到 system prompt | 性格画像、说话风格 |

### RAG 检索机制

每轮对话：
1. 用户消息 → Embedding → ChromaDB `search_target_only()` → 检索 ta 最相似原话
2. 原话注入 system prompt 作为「语气锚点」
3. LLM 生成回复时参考原话的标点、空格、断句习惯

**降级策略**：连续 3 次 RAG 失败后进入纯文本模式，每 5 轮尝试恢复一次。

## 数据流

### 创建流程 (/create)

```
用户摄入 (3 问题)
  → 数据源 (微信/QQ/口述)
    → Parser 解析 (list[dict])
      → Chunker 切片 (重叠窗口, dominant_speaker 判定)
        → ChromaDB 入库 (分批写入, 检查点)
  → LLM 蒸馏
    → memory.md (关系记忆)
    → persona.md (性格画像 + 9 场景原话)
  → Skill Combiner
    → SKILL.md (运行 prompt)
```

### 对话流程 (/{slug})

```
用户输入
  → validate_user_input (注入检测, 长度限制)
  → RAG 检索 (search_target_only)
  → _build_system_prompt (SKILL.md + session 摘要 + corrections + RAG)
  → chat() / chat_stream()
  → 贴纸选择 + 红包检测
  → 纠正检测 (可选)
  → 轮次计数 → 20 轮触发归档询问
```

## 目录结构

```
ex-memory/
├── core/              # 核心引擎与工具
│   ├── engine.py      # ChatEngine: RAG + prompt 构造 + API 调用
│   ├── session.py     # ChatSession: CLI 循环与归档
│   ├── validation.py  # 输入校验与注入检测
│   ├── retry.py       # API 重试装饰器
│   ├── token_counter.py  # Token 统计
│   ├── keychain.py    # macOS Keychain 密钥管理
│   ├── file_utils.py  # 原子写入与文件锁
│   ├── factory.py     # Engine/Store 创建工厂
│   └── version_manager.py  # 版本备份/回滚
│
├── server/            # Web API 层
│   ├── app.py         # FastAPI 应用
│   ├── routes.py      # REST API 路由
│   ├── auth.py        # 用户认证 (SQLite + Token)
│   └── middleware.py   # CORS, 限流, 审计日志
│
├── memory/            # 向量数据库层
│   ├── vector_store.py  # ChromaDB 封装
│   ├── embedder.py    # Embedding API 封装
│   ├── importer.py    # 聊天记录导入编排
│   └── chunker.py     # 聊天记录切片
│
├── pipeline/          # 自动化蒸馏管线
│   ├── orchestrator.py   # 总调度
│   ├── persona_builder.py  # persona.md 生成
│   ├── memory_builder.py   # memory.md 生成
│   └── skill_combiner.py   # SKILL.md 合并
│
├── parsers/           # 数据源解析器
├── commands/          # CLI 指令模块
├── prompts/           # LLM prompt 模板
├── tests/             # 测试
├── web/               # 前端 (Vanilla JS SPA)
└── migrations/        # 数据库迁移
```

## 关键设计决策

### 为什么用 ChromaDB 而非 Milvus/Qdrant？

- **零运维**：`pip install chromadb` 即用，无需 Docker
- **原生 where 过滤**：`dominant_speaker == "target"` 直接支持
- **内置持久化**：SQLite 后端，自动持久化
- **性能充足**：万级数据量内响应 < 100ms

### 为什么用 Vanilla JS 而非 React/Vue？

- **零构建步骤**：不需要 webpack/vite，直接部署
- **体积小**：~50KB 单文件 vs 数百 KB 框架
- **PWA 友好**：配合 Service Worker 实现离线可用
- **维护简单**：一人项目不需要框架复杂性

### 为什么用三层记忆而非单层？

- **原话优先**：潜意识层（向量库）提供 ta 的真实语气，LLM 描述是不够的
- **描述兜底**：当没有匹配的原话时，persona 描述作为补充
- **上下文不爆炸**：session 摘要比完整对话短得多，节省 token

## 安全架构

| 层 | 机制 |
|----|------|
| 传输 | Bearer Token (7 天过期) |
| 存储 | 密码 PBKDF2-SHA256 (200K 迭代), API Key → macOS Keychain |
| 输入 | 注入检测 (中英文 regex), 8000 字符限制 |
| 限流 | 全局 120/min, 登录 5/min/用户名 |
| 审计 | JSON Line 格式审计日志 (login/register/delete) |
| CORS | 环境变量可配来源 |

## 数据生命周期

- **导入**：API 层只负责认证、文件名消毒、大小限制和临时文件落盘；`memory/importer.py` 负责根据文件类型选择解析器、创建 Embedding/VectorStore，并执行入库。
- **导出**：`GET /api/exes/{slug}/export` 打包当前用户拥有的 `exes/{slug}/`，包含 `export_manifest.json`，跳过 `.lock`、临时文件和符号链接。
- **对话记录导出**：Web/API 聊天会写入 `exes/{slug}/conversations/conversation.jsonl`；`GET /api/exes/{slug}/conversations/export?format=html|md|json|txt` 可导出服务端归档和 CLI session 归档。
- **本机微信导出**：`core/wechat_export/` 负责扫描固定 iTunes/iOS 备份根目录、创建后台任务、写入 `data/wechat_exports/{task_id}/` 并提供受限下载；API 层只做认证、本机模式和 localhost 边界校验。
- **彻底删除**：`DELETE /api/exes/{slug}` 删除整个镜像目录，包括 `sessions/`、`versions/`、钱包文件和 `chroma_db/`，同时清理服务端内存中的 engine 缓存与 session token 计数。
- **私有资源**：自定义贴纸存储在 `data/stickers/custom/u{user_id}/`，只能通过鉴权 API 读取，不挂载到公开静态目录。

## 外部导出器

WechatExporter 源码以 git submodule 形式记录在 `third_party/WechatExporter`，授权边界见 `THIRD_PARTY_NOTICES.md`。运行时通过 `core/exporters/wechat_adapter.py` 以外部二进制方式接入，本机 CLI 可通过 `/export-wechat` 调用用户配置的 `WECHAT_EXPORTER_BIN`，用于从未加密 iTunes/iOS 备份导出微信 HTML 记录。
