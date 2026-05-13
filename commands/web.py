"""/web — 启动 Gradio Web 界面。"""
from commands import register


def cmd_web(_=""):
    from web.app import run_web
    print("正在启动 Web 界面...")
    run_web()


register("web", cmd_web)
