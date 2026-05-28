"""/export-wechat — 调用外部 WechatExporter 导出微信备份。"""

import shlex
from pathlib import Path

from commands import register


def cmd_export_wechat(arg: str):
    parts = shlex.split(arg)
    if len(parts) < 3:
        print("用法: /export-wechat {iTunes备份目录} {输出目录} {微信账号} [会话名...]")
        print("提示: 需先设置 WECHAT_EXPORTER_BIN 指向 WechatExporter 二进制。")
        return

    backup_dir = Path(parts[0]).expanduser()
    output_dir = Path(parts[1]).expanduser()
    account = parts[2]
    sessions = tuple(parts[3:])

    try:
        from core.exporters.wechat_adapter import WechatExportOptions, run_wechat_exporter
        result = run_wechat_exporter(WechatExportOptions(
            backup_dir=backup_dir,
            output_dir=output_dir,
            account=account,
            sessions=sessions,
        ))
    except Exception as e:
        print(f"WechatExporter 导出失败: {e}")
        return

    if result.stdout.strip():
        print(result.stdout.strip())
    print(f"微信聊天记录导出完成: {output_dir}")


register("export-wechat", cmd_export_wechat)
