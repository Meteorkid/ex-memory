"""/export — 导出 ex-memory 对话记录。"""
import shutil
import shlex
from pathlib import Path

from commands import register


def cmd_export(arg: str):
    parts = shlex.split(arg)
    if not parts:
        print("用法: /export {镜像名称} [html|md|json|txt]")
        return

    slug = parts[0]
    fmt = parts[1] if len(parts) > 1 else "html"
    try:
        from core.exporters.conversation import export_ex_memory_conversation
        exported = export_ex_memory_conversation(slug, fmt)
    except Exception as e:
        print(f"导出失败: {e}")
        return

    target = Path(exported.filename)
    shutil.copyfile(exported.path, target)
    exported.path.unlink(missing_ok=True)
    print(f"导出完成: {target.resolve()}")


register("export", cmd_export)
