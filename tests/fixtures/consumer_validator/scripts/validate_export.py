#!/usr/bin/env python
# ---------------------------------------------------------------------------
# 消费端契约校验器（vendor 副本，来自 ai-pm-job-intel 仓库）
#
# 来源仓库: https://github.com/Bruce-Yii/ai-pm-job-intel（私有）
# 来源 SHA: ab8a958（feat: 校验器版本号（v1.0.0）用于双端漂移检测）
# 同步约定: 契约变更时由 ai-pm-job-intel 重新交付本文件与 src/python/contract_check.py，
#           爬虫侧替换副本后 CI 自动对齐（断言输出含 v1.0.0）；勿改逻辑
# ---------------------------------------------------------------------------
"""消费端契约校验器（爬虫侧可自检用）。

用法: python scripts/validate_export.py <export.json>
退出码: 0=合规 1=不合规 2=文件/参数错误
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "python"))
from contract_check import VALIDATOR_VERSION, validate_export


def main() -> int:
    if len(sys.argv) != 2:
        print("用法: python scripts/validate_export.py <export.json>")
        return 2
    path = Path(sys.argv[1])
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"读取失败: {e}")
        return 2
    report = validate_export(data)
    print(f"validator=v{VALIDATOR_VERSION} format_version={report['format_version']} "
          f"jobs={report['job_count']} ok={report['ok']}")
    for w in report["warnings"]:
        print(f"  WARN {w}")
    for e in report["errors"]:
        print(f"  FAIL {e}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
