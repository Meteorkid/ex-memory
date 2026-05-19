# AGENTS.md

本文件是 `ex-memory` 仓库内的代理协作说明。若与全局说明冲突，以本文件为准；若用户在当前对话中给出更具体要求，以用户要求为准。

## 项目概览

`ex-memory` 是 Python 3.10+ 项目，包含 CLI、FastAPI 服务、Gradio 界面和原生 JS Web 前端。系统通过聊天记录解析、向量检索、LLM 蒸馏和运行时对话，生成可交互的记忆镜像。

核心入口：

- `run.py`：CLI 主入口。
- `server/app.py`：FastAPI 应用入口。
- `server/routes.py`：REST API 路由。
- `web/app.py`：Gradio Web 界面。
- `web/static/`：原生 JS SPA 与静态资源。

核心目录：

- `core/`：对话引擎、校验、文件工具、版本管理、钱包与贴纸等核心逻辑。
- `memory/`：ChromaDB、Embedding、切片和摄入。
- `pipeline/`：创建、更新、蒸馏、合并、纠正和反思流程。
- `parsers/`：微信、QQ、口述等数据解析器。
- `commands/`：CLI 命令。
- `tests/`：pytest 测试。
- `docs/`：架构和部署文档。

## 本地运行

安装依赖：

```bash
pip install -r requirements.txt -r requirements-dev.txt
```

运行 CLI：

```bash
python run.py
```

运行 API：

```bash
python -m server.app
```

开发模式：

```bash
uvicorn server.app:app --reload --host 0.0.0.0 --port 8000
```

常用 Make 目标：

```bash
make test
make lint
make dev
```

## 测试与验证

- 默认使用 `pytest`。
- 修改核心逻辑、API 路由、认证、文件读写、解析器、RAG、钱包、贴纸、版本管理时，必须新增或更新测试。
- 小范围验证可运行相关测试文件；提交前优先运行：

```bash
pytest -q
```

- 覆盖率检查可运行：

```bash
pytest --cov --cov-report=term
```

- 静态检查：

```bash
ruff check .
```

如无法运行测试或检查，最终回复必须说明原因、已做的替代验证和建议下一步。

## 代码风格

- 遵循现有 Python 风格：类型提示优先，函数职责清晰，异常具体捕获。
- 不做无关重构；每一行改动都应能对应当前任务。
- 注释只解释业务规则、隐私边界、并发不变量或复杂流程，避免重复代码含义。
- 复用现有工具函数，例如：
  - `core.validation`
  - `core.path_safety`
  - `core.file_utils`
  - `core.exe_access`
  - `core.factory`
- 文件写入优先使用 `atomic_write` / `atomic_write_json`。涉及读-改-写共享 JSON 时，必须考虑文件锁或进程内互斥。
- 路径拼接必须经过 slug 校验或 `resolve_under` 等路径安全工具；不要直接信任用户输入路径。

## 安全与隐私

本项目处理聊天记录、人物画像、关系记忆和用户自定义资源，默认按高隐私数据处理。

- 不要提交 `.env`、API Key、Token、私钥、生产连接串或真实聊天记录。
- 不要把 `exes/`、`data/`、`logs/`、`htmlcov/`、`.coverage` 纳入提交，除非用户明确要求且已确认脱敏。
- API Key 优先使用环境变量或 macOS Keychain。
- 新增 API 时必须确认认证、owner 校验和越权访问测试。
- 静态文件暴露前要确认是否包含用户私有内容；私有资源应走鉴权路由。
- 日志中不得打印完整密钥、聊天原文大段内容或隐私字段。

## API 与多用户边界

- 受保护路由应使用 `Depends(require_auth)`。
- 访问镜像数据前应调用 `_check_exe_access(slug, user_id)` 或等价逻辑。
- 新增与镜像相关的路由时，至少覆盖：
  - 未登录返回 401。
  - 非 owner 访问返回 403。
  - 不存在的 slug 返回 404。
  - 非法 slug 返回 400。
- `SINGLE_USER_MODE` 是特殊兼容模式，不应成为多用户安全逻辑的默认假设。

## 文件与数据目录约定

- `exes/{slug}/` 是运行时镜像数据目录，包含 `SKILL.md`、`memory.md`、`persona.md`、`corrections.md`、`meta.json`、`chroma_db/`、`sessions/`、`versions/` 等。
- `data/` 存放认证数据库和服务运行数据。
- `web/static/stickers/` 包含内置和自定义贴纸。自定义贴纸涉及用户私有资源，改动时必须检查访问控制。
- 数据库 schema 变更放在 `migrations/`，并保持幂等迁移。

## 前端约定

- `web/static/app.js` 是原生 JS SPA，不引入构建步骤，除非用户明确要求。
- 前端新增 API 调用必须处理 401、错误提示和加载状态。
- 不要把服务端安全校验依赖前端实现；前端校验只是体验优化。
- 样式改动保持微信模拟器的现有视觉语言，不做大范围重设。

## 依赖与环境

- 生产依赖维护在 `requirements.txt` 和 `requirements.lock`。
- 开发依赖维护在 `requirements-dev.txt`。
- 新增依赖前先确认确有必要，并说明用途、替代方案和影响面。
- Docker 相关改动需同步检查 `Dockerfile`、`docker-compose.yml`、`.dockerignore` 和部署文档。

## 已知审查重点

后续修改时优先关注这些风险点：

- `/api/exes/{slug}/import` 这类上传导入路径需要端到端测试。
- 自定义贴纸、上传文件和静态资源必须确认是否绕过认证。
- 钱包、红包、转账等 JSON 读-改-写流程需要并发保护。
- Engine 缓存失效要覆盖会改变 `SKILL.md`、`persona.md`、`memory.md`、`corrections.md` 或向量库的操作。

## Git 与提交

- 不要自动提交。完成修改后先汇报做了什么、如何验证、风险点，再询问用户是否提交。
- commit 信息默认使用中文，简洁描述实际改动。
- 不要回退用户已有改动；遇到无关脏文件直接忽略。

