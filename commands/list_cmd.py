"""/list — 列出所有镜像。"""
import json
from config import EXES_DIR
from commands import register


def cmd_list(_=""):
    if not EXES_DIR.exists():
        print("还没有创建任何镜像。输入 /create 开始。")
        return

    exes = [d for d in EXES_DIR.iterdir() if d.is_dir() and (d / "meta.json").exists()]
    if not exes:
        print("还没有创建任何镜像。输入 /create 开始。")
        return

    print("\n[已创建的镜像]")
    for ex_dir in sorted(exes):
        meta_path = ex_dir / "meta.json"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            name = meta.get("name", ex_dir.name)
            state = meta.get("pipeline_state", "unknown")
            created = meta.get("created_at", "")[:10]
            print(f"  /{ex_dir.name:<15} {name}  ({state}, {created})")
        except Exception:
            print(f"  /{ex_dir.name:<15} (读取失败)")
    print()


register("list", cmd_list)
