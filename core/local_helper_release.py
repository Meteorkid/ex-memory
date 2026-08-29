"""公共网站可展示的本地助手发布元数据。"""

from __future__ import annotations

import re
from urllib.parse import urlparse

import config


def get_local_helper_release() -> dict:
    downloads = []
    for architecture, url, digest in (
        (
            "arm64",
            config.LOCAL_WECHAT_HELPER_ARM64_URL,
            config.LOCAL_WECHAT_HELPER_ARM64_SHA256,
        ),
        (
            "x86_64",
            config.LOCAL_WECHAT_HELPER_X64_URL,
            config.LOCAL_WECHAT_HELPER_X64_SHA256,
        ),
    ):
        if _is_https_url(url) and re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            downloads.append(
                {
                    "architecture": architecture,
                    "url": url,
                    "sha256": digest.lower(),
                }
            )
    return {
        "enabled": bool(config.LOCAL_WECHAT_HELPER_ENABLED and downloads),
        "platform": "macos",
        "version": config.LOCAL_WECHAT_HELPER_VERSION,
        "min_api_version": config.LOCAL_WECHAT_HELPER_MIN_API_VERSION,
        "supported_wechat_versions": ["4.1.12"],
        "release_channel": "unsigned-open-source-beta",
        "downloads": downloads,
    }


def _is_https_url(value: str) -> bool:
    parsed = urlparse(value)
    return (
        parsed.scheme == "https"
        and bool(parsed.netloc)
        and not parsed.username
        and not parsed.password
    )
