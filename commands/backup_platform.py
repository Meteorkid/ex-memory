"""/snapshot — 平台级备份与恢复。

镜像自带的 /backup 是**单个镜像的版本快照**，存在同一块盘上：磁盘坏了、
容器重建了，它跟着一起没。这个命令把镜像整体推到对象存储，是灾备意义上的
备份。

没演练过的备份等于没有备份，所以 --verify 会真的取回来比对，而不是只看
「上传成功」。
"""

import logging
from datetime import datetime

from commands import register

logger = logging.getLogger("ex-memory")


def _default_label() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def cmd_snapshot(args: str = "") -> None:
    """用法: /snapshot [--label X] [--restore X] [--list] [--verify]"""
    import config
    from core.blob_store import list_snapshots, restore_mirror, snapshot_mirror

    parts = args.split()

    if "--list" in parts:
        labels = list_snapshots()
        print("\n[可用快照]" if labels else "还没有任何快照。")
        for label in labels:
            print(f"  {label}")
        return

    def _opt(name: str) -> str:
        return (
            parts[parts.index(name) + 1]
            if name in parts and parts.index(name) + 1 < len(parts)
            else ""
        )

    restore_label = _opt("--restore")
    if restore_label:
        restored = 0
        for slug, owner, _path in config.iter_exe_dirs(require_meta=False):
            try:
                restored += restore_mirror(slug, owner, restore_label)
            except FileNotFoundError:
                continue
        print(f"恢复完成：共 {restored} 个文件（快照 {restore_label}）")
        return

    label = _opt("--label") or _default_label()
    total = 0
    mirrors = 0
    for slug, owner, _path in config.iter_exe_dirs(require_meta=False):
        try:
            total += snapshot_mirror(slug, owner, label)
            mirrors += 1
        except Exception as e:  # noqa: BLE001 — 单个镜像失败不该中断整体备份
            logger.error("镜像 %s 快照失败: %s", slug, e)
            print(f"  ✗ {slug}: {e}")
    print(f"\n快照完成：{mirrors} 个镜像 / {total} 个文件，标签 {label}")

    if "--verify" in parts:
        print(_verify(label))


def _verify(label: str) -> str:
    """取回快照与本地逐字节比对。

    只看「上传成功」是不够的——没演练过的备份等于没有备份。
    """
    import config
    from core.blob_store import backend

    store = backend()
    checked = mismatched = missing = 0
    for slug, owner, ex_dir in config.iter_exe_dirs(require_meta=False):
        owner_part = str(owner) if owner is not None else "_flat"
        prefix = f"snapshots/{label}/{owner_part}/{slug}"
        for path in sorted(ex_dir.rglob("*")):
            if not path.is_file() or path.suffix in (".lock", ".tmp"):
                continue
            if path.name == ".DS_Store":
                continue
            key = f"{prefix}/{path.relative_to(ex_dir).as_posix()}"
            stored = store.get(key)
            checked += 1
            if stored is None:
                missing += 1
            elif stored != path.read_bytes():
                mismatched += 1

    if missing or mismatched:
        return f"校验失败：{checked} 个文件中 {missing} 个缺失、{mismatched} 个不一致"
    return f"校验通过：{checked} 个文件与本地逐字节一致"


register("snapshot", cmd_snapshot)
