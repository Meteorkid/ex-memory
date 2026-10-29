# 记忆闭环验收

使用现有隔离 runner，每次指定新的仓库外目录。不要直接对个人运行目录执行脚本。

```bash
.venv/bin/python -B scripts/validation/2026-10-29-astrbot-hardening/run_checks.py \
  --workspace "/tmp/exmemory-loop-$(date +%Y%m%d-%H%M%S)" \
  pytest -q -ra -p pytest_cov --cov --cov-report=term --cov-report=xml --cov-fail-under=70
```

协议级闭环：把最后一行改为
`script scripts/validation/2026-10-29-memory-loop/protocol_acceptance.py`。
它运行真实 FastAPI 与 stdio MCP 子进程，父子进程共享该次隔离数据库/镜像，
分别验证热缓存刷新、权威历史、会话隔离、引用校验、自动归档和重启恢复。
模型、固定检索结果与摘要生成为替身；这些结果不证明真实模型质量或平台送达。

浏览器验收：使用新的工作目录和
`script scripts/validation/2026-10-29-memory-loop/serve_fixture.py 8877`。
服务只监听127.0.0.1，合成账号为 `loopcheck` / `offline-only-pass`。
这不是生产凭证。可验证IM种子会话、来源展开、显式纠正、下一轮采用、刷新恢复、
独立网页会话，以及375px窄屏布局。完成后停止该次本地服务。

前端回归：`node --test tests/test_web_*.cjs`。
源码按当前静态脚本模式交付，无新依赖、构建步骤或数据库迁移。
