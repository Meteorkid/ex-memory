#!/usr/bin/env python3
"""覆盖率分层门禁（NFR-075）。

全局 70% 的单一门槛掩盖了结构性空洞：审计时发现 append_turn、import_data、
文件上传三条关键路径是零覆盖，而全局数字看着很健康。

核心域要求更高：认证与隔离、计费、安全闸门出问题的后果远大于一般代码。
"""

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# 模块路径后缀 → 最低行覆盖率
TIERS = {
    "server/auth.py": 85,
    "server/safety_gate.py": 85,
    "server/billing_gate.py": 85,
    "core/billing.py": 80,
    "core/safety/crisis.py": 85,
    "core/exe_access.py": 85,
    "server/account_lifecycle.py": 80,
}
GLOBAL_MIN = 70


def main() -> int:
    report = Path("coverage.xml")
    if not report.exists():
        print("找不到 coverage.xml，请先运行 pytest --cov --cov-report=xml")
        return 1

    root = ET.parse(report).getroot()
    overall = float(root.get("line-rate", 0)) * 100
    failures = []
    if overall < GLOBAL_MIN:
        failures.append(f"全局 {overall:.1f}% < {GLOBAL_MIN}%")

    seen: dict[str, float] = {}
    for cls in root.iter("class"):
        filename = cls.get("filename", "")
        rate = float(cls.get("line-rate", 0)) * 100
        for prefix, minimum in TIERS.items():
            if filename.endswith(prefix):
                seen[prefix] = rate
                if rate < minimum:
                    failures.append(f"{prefix} {rate:.1f}% < {minimum}%")

    for prefix in TIERS:
        if prefix not in seen:
            failures.append(f"{prefix} 未出现在覆盖率报告中")

    print(f"全局行覆盖率: {overall:.1f}%")
    for prefix, rate in sorted(seen.items()):
        status = "✓" if rate >= TIERS[prefix] else "✗"
        print(f"  {status} {prefix}: {rate:.1f}% (要求 {TIERS[prefix]}%)")

    if failures:
        print("\n核心域覆盖率未达标：")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\n覆盖率分层门禁通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
