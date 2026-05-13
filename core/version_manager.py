"""版本管理：备份、回滚、列出版本。"""

import shutil
import json
from datetime import datetime
from pathlib import Path
from config import get_ex_dir


def backup(slug: str, version_name: str = "", include_chroma: bool = True) -> str:
    """备份当前镜像版本。

    Args:
        slug: 前任代号
        version_name: 自定义版本名（默认用时间戳）
        include_chroma: 是否同时备份向量库（大型库可跳过）

    Returns:
        版本名称
    """
    ex_dir = get_ex_dir(slug)
    if not ex_dir.exists():
        raise FileNotFoundError(f"镜像不存在: {slug}")

    versions_dir = ex_dir / "versions"
    versions_dir.mkdir(parents=True, exist_ok=True)

    if not version_name:
        version_name = datetime.now().strftime("v%Y%m%d_%H%M%S")

    version_path = versions_dir / version_name
    version_path.mkdir(parents=True, exist_ok=True)

    # 备份关键文件
    for filename in ["SKILL.md", "memory.md", "persona.md", "corrections.md", "meta.json"]:
        src = ex_dir / filename
        if src.exists():
            shutil.copy2(src, version_path / filename)

    # 备份向量库
    if include_chroma:
        chroma_src = ex_dir / "chroma_db"
        if chroma_src.exists() and any(chroma_src.iterdir()):
            shutil.copytree(chroma_src, version_path / "chroma_db", dirs_exist_ok=True)

    # 写入版本元信息
    version_meta = {
        "version": version_name,
        "created_at": datetime.now().isoformat(),
        "slug": slug,
    }
    (version_path / "version_meta.json").write_text(
        json.dumps(version_meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return version_name


def rollback(slug: str, version_name: str):
    """回滚到指定版本。

    回滚前自动创建安全备份，防止回滚出错后无法恢复。
    """
    ex_dir = get_ex_dir(slug)
    version_path = ex_dir / "versions" / version_name

    if not version_path.exists():
        raise FileNotFoundError(f"版本不存在: {version_name}")

    # 回滚前自动备份当前状态
    safety_backup = datetime.now().strftime("v%Y%m%d_%H%M%S") + "_pre_rollback"
    try:
        backup(slug, safety_backup)
    except Exception:
        pass  # 备份失败不阻塞回滚

    # 恢复关键文件
    for filename in ["SKILL.md", "memory.md", "persona.md", "corrections.md", "meta.json"]:
        src = version_path / filename
        if src.exists():
            shutil.copy2(src, ex_dir / filename)

    # 恢复向量库
    chroma_src = version_path / "chroma_db"
    if chroma_src.exists():
        current_chroma = ex_dir / "chroma_db"
        if current_chroma.exists():
            shutil.rmtree(current_chroma)
        shutil.copytree(chroma_src, current_chroma)


def list_versions(slug: str) -> list[str]:
    """列出所有版本。

    Returns:
        版本名称列表
    """
    ex_dir = get_ex_dir(slug)
    versions_dir = ex_dir / "versions"

    if not versions_dir.exists():
        return []

    versions = []
    for d in sorted(versions_dir.iterdir()):
        if d.is_dir():
            meta_path = d / "version_meta.json"
            if meta_path.exists():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                versions.append(f"  {meta['version']}  ({meta['created_at'][:10]})")
            else:
                versions.append(f"  {d.name}")

    return versions
