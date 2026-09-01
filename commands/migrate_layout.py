"""/migrate-layout — 把存量扁平镜像迁到按账号隔离的目录布局。

D-03 之后新建镜像走 exes/<owner>/<slug>，存量镜像仍在 exes/<slug>。
两种布局并存时，扁平镜像的名字对所有账号依然是全局占用的：别人创建同名
镜像会收到 409，等于一个存在性预言机，而且那个名字谁也用不了。

本命令按各镜像 meta.json 里的 owner_user_id 归位。没有 owner_user_id 的
镜像不会被自动处理——归属未知时移动或删除都可能是错的，必须人工决定。

用法：
    /migrate-layout           # 只打印计划，不动文件
    /migrate-layout --apply   # 实际迁移
"""

import json
import logging
import shutil
from pathlib import Path
from typing import Optional

import config
from commands import register

logger = logging.getLogger("ex-memory")


def _plan() -> tuple[list[tuple[str, int, Path, Path]], list[tuple[str, str]]]:
    """扫描扁平镜像，返回 (可迁移列表, 需人工处理列表)。

    可迁移项为 (slug, owner, 源目录, 目标目录)；
    人工项为 (slug, 原因)。
    """
    movable: list[tuple[str, int, Path, Path]] = []
    manual: list[tuple[str, str]] = []

    if not config.EXES_DIR.exists():
        return movable, manual

    for slug, owner, path in config.iter_exe_dirs(require_meta=False):
        if owner is not None:
            continue  # 已在嵌套布局
        bound = _read_owner(path)
        if bound is None:
            manual.append((slug, "meta.json 缺少 owner_user_id，归属未知"))
            continue
        target = config.get_ex_dir_owned(slug, bound)
        if target.exists():
            manual.append((slug, f"目标 {target.relative_to(config.EXES_DIR)} 已存在"))
            continue
        movable.append((slug, bound, path, target))

    return movable, manual


def _read_owner(ex_dir: Path) -> Optional[int]:
    meta_path = ex_dir / "meta.json"
    if not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    bound = meta.get("owner_user_id")
    return int(bound) if bound is not None else None


def cmd_migrate_layout(args: str = "") -> None:
    """CLI 入口：/migrate-layout [--apply]"""
    apply = "--apply" in args.split()
    movable, manual = _plan()

    if not movable and not manual:
        print("没有需要迁移的扁平镜像。")
        return

    if movable:
        print(f"\n[可迁移 {len(movable)} 个]")
        for slug, owner, src, target in movable:
            rel_src = src.relative_to(config.EXES_DIR)
            rel_target = target.relative_to(config.EXES_DIR)
            print(f"  {rel_src}  →  {rel_target}   (owner={owner})")

    if manual:
        print(f"\n[需人工决定 {len(manual)} 个]")
        for slug, reason in manual:
            print(f"  {slug}: {reason}")

    if not apply:
        print("\n以上为计划，未改动任何文件。确认无误后执行：/migrate-layout --apply")
        return

    moved = 0
    for slug, owner, src, target in movable:
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            # 同一文件系统内的 rename，失败不会留下半个目录
            shutil.move(str(src), str(target))
        except OSError as e:
            logger.error("迁移镜像 %s 失败: %s", slug, e)
            print(f"  ✗ {slug}: {e}")
            continue
        moved += 1
        print(f"  ✓ {slug} → {target.relative_to(config.EXES_DIR)}")

    print(f"\n迁移完成：{moved}/{len(movable)} 个")
    if manual:
        print(f"仍有 {len(manual)} 个需人工处理，未做任何改动。")


register("migrate-layout", cmd_migrate_layout)
