# ---------------------------------------------------------------------------
# 双端契约校验器 vendor 副本（来源：ai-pm-job-intel 私有仓库）
#
# 来源 SHA: 规格侧 T2 契约 v2 交付（Issue 5278260355，docs/25 §3 顶层扩展字段表）
# 唯一口径: ai-pm-job-intel/src/python/contract_check.py（本文件 validate_export.py 同源）
#
# 同步约定（契约变更时）:
#   1. ai-pm-job-intel 递增 VALIDATOR_VERSION 并交付最新两份文件（contract_check.py + validate_export.py）
#   2. 同步替换本目录两份文件（tests/fixtures/consumer_validator/）
#   3. CI 断言 validator=v2.0.0 版本匹配，漂移即红（触发双端对齐）
#   4. 全量回归依据（私有仓不可直接引用，vendor 副本保证 CI 双端一致校验，无逻辑改动）
# ---------------------------------------------------------------------------
#!/usr/bin/env python
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

