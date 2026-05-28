"""WechatExporter 外部二进制适配器。

源码以 git submodule 形式保留在 third_party/WechatExporter；运行时仍要求
用户显式配置编译后的独立二进制路径。
"""

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

CONFIG_HINT = (
    "请设置 WECHAT_EXPORTER_BIN 或传入二进制路径；如需使用随仓库声明的源码子模块，"
    "先执行 git submodule update --init --recursive，并按上游说明编译 WechatExporter。"
)
SUPPORTED_ASYNC_LOADING = {"sync", "oninit", "onscroll"}


class WechatExporterNotConfigured(RuntimeError):
    """未配置 WechatExporter 二进制。"""


@dataclass(frozen=True)
class WechatExportOptions:
    backup_dir: Path
    output_dir: Path
    account: str
    sessions: tuple[str, ...] = ()
    async_loading: str = "onscroll"
    enable_filter: bool = False
    binary_path: Optional[Path] = None


def run_wechat_exporter(options: WechatExportOptions) -> subprocess.CompletedProcess:
    """调用外部 WechatExporter 命令行导出 iTunes 备份。"""
    binary = resolve_wechat_exporter_binary(options.binary_path)
    if not options.backup_dir.exists() or not options.backup_dir.is_dir():
        raise ValueError("iTunes 备份目录不存在")
    options.output_dir.mkdir(parents=True, exist_ok=True)
    if not options.account.strip():
        raise ValueError("微信账号不能为空")
    if options.async_loading not in SUPPORTED_ASYNC_LOADING:
        raise ValueError("asyncloading 仅支持 sync、oninit、onscroll")

    cmd = [
        str(binary),
        f"--backup={options.backup_dir}",
        f"--output={options.output_dir}",
        f"--account={options.account}",
        f"--asyncloading={options.async_loading}",
        f"--filter={'yes' if options.enable_filter else 'no'}",
    ]
    for session in options.sessions:
        if session.strip():
            cmd.append(f"--session={session}")

    return subprocess.run(
        cmd,
        check=True,
        capture_output=True,
        text=True,
    )


def resolve_wechat_exporter_binary(binary_path: Optional[Path] = None) -> Path:
    """解析并校验 WechatExporter 二进制路径。"""
    raw = binary_path or os.getenv("WECHAT_EXPORTER_BIN")
    if not raw:
        raise WechatExporterNotConfigured(f"未配置 WechatExporter 二进制。{CONFIG_HINT}")
    path = Path(raw).expanduser()
    if not path.exists() or not path.is_file():
        raise WechatExporterNotConfigured(f"WechatExporter 二进制不存在: {path}。{CONFIG_HINT}")
    if not os.access(path, os.X_OK):
        raise WechatExporterNotConfigured(f"WechatExporter 不可执行: {path}。请执行 chmod +x 或重新编译。")
    return path
