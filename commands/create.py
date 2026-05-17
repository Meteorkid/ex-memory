"""/create — 创建新的记忆镜像。

用法：
  /create        — 全新创建
  /create <slug> — 从上次失败步骤继续
"""
from commands import register


def cmd_create(args=""):
    from pipeline.orchestrator import run_create_flow
    slug = args.strip() or None
    run_create_flow(slug=slug)


register("create", cmd_create)
