# 首次体验的离线复现

这组脚本用完全虚构的聊天记录验证导入、生成文件、权限、失败恢复和首条对话。HTTP、SQLite、ChromaDB 和业务流程是真实实现；LLM 使用确定性替身，Embedding 使用字符 bigram 伪向量。因此结果只证明流程可运行，不能用于人物还原度、检索质量、费用或真实模型效果的宣传。

## 安全边界

- 指定仓库外的任务专用空目录；脚本只在该目录生成测试账号、同意记录、聊天和数据库。
- 按源码清单复制，排除 `.env`、真实 `data/`、`exes/`、`logs/` 和自定义贴纸；`evals/` 取 Git HEAD 的代码及公开合成语料，避免混入未完成的评测工作。
- 导入 `config` 前禁用 dotenv 和 Keychain 读取，清除继承的凭证、数据库及代理配置；连接只允许 loopback，阻断外部 DNS、模型和遥测请求。
- 服务只监听 `127.0.0.1`。测试代理注入的是合成身份，仍走真实 owner 和独立同意检查；不会替真实用户同意协议。
- API 脚本只连接 `127.0.0.1`，不支持生产 URL。不要将验收服务部署或反向代理到公网。

## 运行

先使用项目已有 Python 环境，不需新增依赖。以下命令从仓库根目录运行；`--repo` 可覆盖默认仓库路径。

```sh
.venv/bin/python scripts/validation/2026-10-23-first-run/sandbox.py \
  --workspace /tmp/ex-memory-first-run-check sync

.venv/bin/python scripts/validation/2026-10-23-first-run/sandbox.py \
  --workspace /tmp/ex-memory-first-run-check pytest -q \
  tests/test_first_import.py tests/test_import_route.py tests/test_routes_auth.py \
  tests/test_engine_cache_isolation.py tests/test_runtime_shell.py

.venv/bin/python scripts/validation/2026-10-23-first-run/sandbox.py \
  --workspace /tmp/ex-memory-first-run-check serve --port 8765
```

另开终端执行：

```sh
.venv/bin/python scripts/validation/2026-10-23-first-run/http_acceptance.py \
  --workspace /tmp/ex-memory-first-run-check --port 8765
```

浏览器打开 `http://127.0.0.1:8765/`，使用页面提供的虚构示例，或临时目录 `samples/` 中的样例。浏览器使用独立合成账号，不会与 HTTP 验收脚本的随机测试账号混用。请实际执行未勾选确认、错误文件、修改后重试、完成后开始对话；确认只发生于隔离合成账号。

## 结果与清理

- `http-acceptance.json`：每项检查及证据，包含零模型调用的无效文件拒绝、正确目标说话人、实际生成文件、重传去重、跨用户隔离和首条 SSE。
- `model-calls.jsonl`：只记录替身调用类型、消息数量/字符数，不记录聊天全文。
- `runtime/`、`source/data/`：本次合成账号、镜像、任务和暂存数据。
- `samples/`：确定性生成的虚构聊天与坏文件。
- `sync` 更新源码快照；后端变更后重启本机服务才能生效，静态资源变更可刷新浏览器加载。不要在任务正在导入时同步源码。
- `Ctrl+C` 停止服务。保留结果后，可删除自己指定的整份临时目录；不要将临时账号或数据复制回仓库。

2026-10-23 的全量隔离检查排除了两项已有研究工作边界：未跟踪 `tests/test_error_analysis.py` 依赖未跟踪的 `evals.error_analysis`；HEAD 中 `test_eval_holdout.py::TestRunHoldoutEvalOffline::test_structure` 要求实现尚未返回的逐查询字段。这两项不应被描述为已通过。相关研究完成后，应取消排除并单独重跑。
