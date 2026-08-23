"""System Integrity Protection 状态检测。"""

from __future__ import annotations

import subprocess
from enum import Enum
from typing import Callable


class SIPStatus(str, Enum):
    ENABLED = "enabled"
    DISABLED = "disabled"
    UNKNOWN = "unknown"


def parse_sip_status(output: str) -> SIPStatus:
    normalized = output.strip().lower()
    if "status: enabled" in normalized:
        return SIPStatus.ENABLED
    if "status: disabled" in normalized:
        return SIPStatus.DISABLED
    return SIPStatus.UNKNOWN


def get_sip_status(
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> SIPStatus:
    try:
        result = runner(
            ["/usr/bin/csrutil", "status"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return SIPStatus.UNKNOWN
    return parse_sip_status(f"{result.stdout}\n{result.stderr}")
