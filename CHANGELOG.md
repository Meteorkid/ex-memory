# Changelog

## v0.2.0 (2026-05-13)

### 新增功能

- **QQ 聊天记录解析器**：支持 3 种 TXT 格式（时间戳+昵称+QQ、时间戳+昵称、昵称+QQ+时间戳）和 MHT 格式（beta），自动检测格式，支持部分名称匹配
- **Session LLM 语义摘要**：对话归档时调用 LLM 生成结构化摘要，下次启动优先加载摘要而非原始归档，快速恢复对话上下文
- **ChromaDB 备份/恢复**：版本备份自动包含向量数据库，回滚时文本与向量数据一致恢复
- **API Token 累计计数**：新增 `GET /api/exes/{slug}/usage` 端点，返回 session 级累计 token 消耗
- **关系反思模块提取**：新建 `pipeline/reflector.py`，CLI 和 API 统一调用
- **朋友圈生成器提取**：新建 `pipeline/moment_generator.py`

### 代码质量优化

- **run.py 拆分**：从 414 行缩减到 79 行（-81%），`commands/` 包 + 注册表模式
- **Gradio 多用户支持**：`gr.State(AppState)` 替代全局单例，新增流式对话、贴纸显示、token 状态栏
- **API 异步化**：对话端点改为 `async def`，阻塞 LLM 调用通过 `run_in_threadpool` 包装
- **引擎缓存**：`_engine_cache` 避免重复实例化，SKILL.md 变更时自动刷新
- **Embedding 优化**：`embed_one()` 直接 API 调用，无需切片
- **增量合并重构**：提取 `_merge_one()` 消除 memory/persona 重复逻辑
- **Collection 名称限制**：`get_collection_name()` 截断到 ChromaDB 63 字符限制

### Bug 修复

- 修复 `_build_system_prompt()` 截断摘要时意外修改原始 `session_summaries` 的问题
- 修复 `correction_handler.py` 中 LLM 返回 None 时的类型错误
- 修复 QQ 解析器格式 C 被格式 A 误匹配的检测顺序问题
- 修复 QQ 空文件返回一条空消息而非空列表的问题

### 文档

- 新增完整 `README.md`：快速开始、命令参考、架构说明、API 端点、配置说明、隐私提示
- 新增 `CHANGELOG.md` 版本更新日志

### 测试

- 测试用例增至 57 个，全部通过
- QQ 解析器新增 22 个测试覆盖多格式 + 边界情况

---

## v0.1.0 (2026-05-12)

初始版本发布。

### 核心功能

- **三层记忆架构**：潜意识层（向量库原话） > 记忆层（memory.md） > 人格层（persona.md）
- **RAG 检索增强**：ChromaDB 向量库 + BAAI/bge-m3 embedding，每轮对话前自动检索 ta 的原话作为语气锚点
- **微信聊天记录解析**：支持 WeFlow JSON/JSONL、WeChatMsg TXT、留痕 JSON
- **全自动蒸馏流水线**：解析 → 切片 → 入库 → LLM 生成 persona.md + memory.md → 合并 SKILL.md
- **对话引擎**：SKILL.md + RAG 动态注入 + 重试 + Token 预算控制
- **关系反思**：7 维度深度分析，生成 reflections.md
- **纠正机制**：对话中说"ta不会这样"即时修正
- **版本管理**：backup/rollback/let-go
- **微信模拟器 Web 界面**：仿微信 UI，支持贴纸、红包、转账、朋友圈
- **FastAPI 服务端**：REST API + 用户认证 + 静态文件服务
- **CLI 交互**：prompt_toolkit 驱动的命令行界面
- **macOS Keychain 集成**：安全存储 API Key
