"""/list — 列出所有镜像。"""

import json
from config import EXES_DIR, iter_exe_dirs
from commands import register


def cmd_list(_=""):
    if not EXES_DIR.exists():
        print("还没有创建任何镜像。输入 /create 开始。")
        return

    # 走 iter_exe_dirs 才能同时看到扁平与按账号隔离两种布局的镜像
    exes = [(slug, owner, path) for slug, owner, path in iter_exe_dirs()]
    if not exes:
        print("还没有创建任何镜像。输入 /create 开始。")
        return

    print("\n[已创建的镜像]")
    for slug, owner, ex_dir in sorted(exes):
        label = f"{slug}@{owner}" if owner is not None else slug
        meta_path = ex_dir / "meta.json"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            name = meta.get("name", slug)
            state = meta.get("pipeline_state", "unknown")
            created = meta.get("created_at", "")[:10]
            print(f"  /{label:<15} {name}  ({state}, {created})")
        except Exception:
            print(f"  /{label:<15} (读取失败)")
    print()


register("list", cmd_list)
