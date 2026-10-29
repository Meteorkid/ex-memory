"""把 AstrBot 插件打包成可独立发布的仓库目录。

插件源码放在 ex-memory 仓库的 integrations/ 下（便于跟着主项目一起改），
但 AstrBot 插件市场要求它自己是一个 GitHub 仓库。这个脚本负责把插件目录
复制成一份干净的、可直接 push 的仓库内容。

用法：
    python scripts/package_astrbot_plugin.py [输出目录]

输出目录默认是 ./dist/astrbot_plugin_exmemory。
"""

import shutil
import sys
from pathlib import Path

REPO_NAME = "astrbot_plugin_exmemory"
SOURCE = Path(__file__).resolve().parent.parent / "integrations" / "astrbot-plugin"

# 插件本体只需要这几个文件，其余（.gitignore 等）由本脚本补齐
REQUIRED = (
    "main.py",
    "metadata.yaml",
    "_conf_schema.json",
    "requirements.txt",
    "README.md",
)


def build(target: Path) -> int:
    if not SOURCE.exists():
        print(f"找不到插件源码目录: {SOURCE}")
        return 1

    missing = [f for f in REQUIRED if not (SOURCE / f).exists()]
    if missing:
        print(f"插件缺少必需文件: {', '.join(missing)}")
        return 1

    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)

    for name in REQUIRED:
        shutil.copy2(SOURCE / name, target / name)
    shutil.copy2(SOURCE / ".gitignore", target / ".gitignore")

    # 插件市场的 Logo 是可选的，但缺了在卡片上很难看；
    # 这里不自动放一个占位图，避免误把无关素材当成官方图标提交上去。
    if not (target / "logo.png").exists():
        print("提示：可放一张 256x256 的 logo.png 到插件目录，市场卡片会展示。")

    size = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
    print(f"已生成 {target}（{size / 1024:.0f} KB）")
    if size > 16 * 1024 * 1024:
        print("警告：超过插件市场 16MB 的压缩包上限，发布会被 CI 拒绝。")
        return 1
    return 0


def main() -> int:
    if len(sys.argv) > 1:
        target = Path(sys.argv[1])
    else:
        target = Path.cwd() / "dist" / REPO_NAME
    return build(target)


if __name__ == "__main__":
    sys.exit(main())
