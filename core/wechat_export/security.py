"""本机微信导出的安全边界。"""

from typing import Any

import config
from core.exporters.wechat_adapter import (
    WechatExporterNotConfigured,
    resolve_wechat_exporter_binary,
)
from core.wechat_export.backups import backup_root

_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}


def is_local_request(request: Any) -> bool:
    client_host = getattr(getattr(request, "client", None), "host", "") or ""
    url_host = getattr(getattr(request, "url", None), "hostname", "") or ""
    return client_host in _LOCAL_HOSTS or url_host in _LOCAL_HOSTS


def require_local_wechat_export(request: Any):
    if not config.LOCAL_WECHAT_EXPORT_ENABLED:
        raise PermissionError("本机微信导出未启用，请设置 LOCAL_WECHAT_EXPORT_ENABLED=true")
    if not is_local_request(request):
        raise PermissionError("微信导出仅允许从本机 localhost 访问")


def get_local_wechat_export_status(request: Any) -> dict:
    binary = _binary_status()
    root = backup_root()
    enabled = bool(config.LOCAL_WECHAT_EXPORT_ENABLED)
    local_request = is_local_request(request)
    ready = enabled and local_request and binary["configured"] and root.exists() and root.is_dir()
    return {
        "enabled": enabled,
        "local_request": local_request,
        "ready": ready,
        "backup_root": str(root),
        "backup_root_exists": root.exists() and root.is_dir(),
        "output_dir": str(config.WECHAT_EXPORT_OUTPUT_DIR.expanduser()),
        "binary": binary,
    }


def _binary_status() -> dict:
    try:
        path = resolve_wechat_exporter_binary()
        return {"configured": True, "path": str(path), "error": ""}
    except WechatExporterNotConfigured as e:
        return {"configured": False, "path": "", "error": str(e)}
