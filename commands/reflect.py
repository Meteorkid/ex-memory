"""/reflect — 关系反思分析。"""
from config import get_ex_dir
from commands import register


def cmd_reflect(slug: str):
    if not slug:
        print("用法: /reflect {镜像名称}")
        return

    from pipeline.reflector import run_reflection
    try:
        print("正在进行关系反思分析（可能需要 1-2 分钟）...")
        reflection = run_reflection(slug)
        print(f"\n{reflection}")
        print(f"\n已保存到 {get_ex_dir(slug) / 'reflections.md'}")
    except FileNotFoundError as e:
        print(f"错误: {e}")
    except RuntimeError as e:
        print(f"错误: {e}")


register("reflect", cmd_reflect)
