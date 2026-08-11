# ---------------------------------------------------------------------------
# 消费端契约校验器（vendor 副本，来自 ai-pm-job-intel 仓库）
#
# 来源仓库: https://github.com/Bruce-Yii/ai-pm-job-intel（私有）
# 来源 SHA: ab8a958（feat: 校验器版本号（v1.0.0）用于双端漂移检测）
# 唯一口径: ai-pm-job-intel/src/python/contract_check.py（本文件与 scripts/validate_export.py 同源）
#
# 同步约定（契约变更时）:
#   1. ai-pm-job-intel 升级 VALIDATOR_VERSION → 交付新文件（含新版本号）
#   2. 爬虫侧替换本目录两个文件（contract_check.py + validate_export.py）
#   3. CI 断言输出含 v1.0.0（版本不匹配即红，触发双端对齐）
#   4. 全程无凭据、无私有仓访问；本副本仅供 CI 双端一致校验，勿改逻辑
# ---------------------------------------------------------------------------
"""消费端契约校验：与爬虫侧规格 08 v1.0 对齐的唯一校验实现。

供三处复用：CLI 自检（validate_export.py）、S3 每日报告（check_daily_export.py）、回归测试。
"""

import json

CONTRACT_FIELDS = ["job_id", "title", "location", "job_link", "company_name"]
SENSITIVE_KEYWORDS = ["cookie", "password", "session", "__zp_stoken", "acw_tc"]

# 校验器版本：契约变更时递增；双端比对此版本号即可检测 vendor 副本漂移
VALIDATOR_VERSION = "1.0.0"


def validate_export(data: dict) -> dict:
    """校验导出数据，返回报告 dict（ok/errors/warnings/统计）。"""
    errors: list[str] = []
    warnings: list[str] = []
    if data.get("format_version") != 1:
        errors.append(f"format_version={data.get('format_version')!r}（期望 1）")
    jobs = data.get("jobs")
    if not isinstance(jobs, list):
        errors.append("jobs 缺失或非数组")
        jobs = []
    if data.get("job_count") != len(jobs):
        warnings.append(f"job_count={data.get('job_count')} 与实际 {len(jobs)} 不一致")

    missing = {f: 0 for f in CONTRACT_FIELDS}
    for j in jobs:
        for f in CONTRACT_FIELDS:
            if not j.get(f):
                missing[f] += 1
    for f, n in missing.items():
        if n:
            errors.append(f"必填字段 {f} 缺失 {n}/{len(jobs)} 条")

    blob = json.dumps(data, ensure_ascii=False).lower()
    for kw in SENSITIVE_KEYWORDS:
        if kw in blob:
            errors.append(f"检出敏感关键字: {kw}")

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "job_count": len(jobs),
        "format_version": data.get("format_version"),
    }
