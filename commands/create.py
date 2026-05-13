"""/create — 创建新的记忆镜像。"""
from commands import register


def cmd_create(_=""):
    from pipeline.orchestrator import run_create_flow
    run_create_flow()


register("create", cmd_create)
