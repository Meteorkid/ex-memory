"""/reflect — 关系反思分析。"""

from config import find_ex_dir_with_owner
from commands import register


def cmd_reflect(slug: str):
    if not slug:
        print("用法: /reflect {镜像名称}")
        return

    from pipeline.reflector import run_reflection

    try:
        ex_dir, owner = find_ex_dir_with_owner(slug)
    except ValueError as e:
        print(f"错误: {e}")
        return
    if ex_dir is None:
        print(f"镜像 [{slug}] 不存在。")
        return

    try:
        print("正在进行关系反思分析（可能需要 1-2 分钟）...")
        reflection = run_reflection(slug, owner=owner)
        print(f"\n{reflection}")
        print(f"\n已保存到 {ex_dir / 'reflections.md'}")
    except FileNotFoundError as e:
        print(f"错误: {e}")
    except RuntimeError as e:
        print(f"错误: {e}")


register("reflect", cmd_reflect)
