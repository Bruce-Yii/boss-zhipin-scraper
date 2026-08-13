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
"""消费端契约校验：与爬虫侧规格 08 v1.0 对齐的唯一校验实现。

供三处复用：CLI 自检（validate_export.py）、S3 每日报告（check_daily_export.py）、回归测试。
"""

import json

CONTRACT_FIELDS = ["job_id", "title", "location", "job_link", "company_name"]
SENSITIVE_KEYWORDS = ["cookie", "password", "session", "__zp_stoken", "acw_tc"]

# 校验器版本：契约变更时递增；双端比对此版本号即可检测 vendor 副本漂移
VALIDATOR_VERSION = "2.0.0"

# format_version 2 = B 增量扩展（mode/observed_jobs/exhausted 可选字段，docs/25 §3）；
# v1 文件兼容（扩展非破坏，必填字段不变）
EXPECTED_FORMAT_VERSIONS = {1, 2}


def validate_export(data: dict) -> dict:
    """校验导出数据，返回报告 dict（ok/errors/warnings/统计）。

    format_version 1/2 均接受；v2 可选扩展字段做类型校验（mode/observed_jobs/exhausted），
    缺失不报错（旧文件兼容）。
    """
    errors: list[str] = []
    warnings: list[str] = []
    fv = data.get("format_version")
    if fv not in EXPECTED_FORMAT_VERSIONS:
        errors.append(f"format_version={fv!r}（期望 1 或 2）")
    jobs = data.get("jobs")
    if not isinstance(jobs, list):
        errors.append("jobs 缺失或非数组")
        jobs = []
    if data.get("job_count") != len(jobs):
        warnings.append(f"job_count={data.get('job_count')} 与实际 {len(jobs)} 不一致")

    mode = data.get("mode")
    if mode is not None and not isinstance(mode, str):
        errors.append(f"mode 必须为字符串（got {type(mode).__name__}）")
    elif mode not in (None, "incremental"):
        warnings.append(f"mode={mode!r}（未知模式值，缺省按全量）")

    observed = data.get("observed_jobs")
    if observed is not None and (
        not isinstance(observed, list) or not all(isinstance(x, str) for x in observed)
    ):
        errors.append("observed_jobs 必须为字符串数组")
    elif mode == "incremental" and observed is None:
        warnings.append("mode=incremental 但缺 observed_jobs")

    exhausted = data.get("exhausted")
    if exhausted is not None and not isinstance(exhausted, bool):
        errors.append(f"exhausted 必须为布尔（got {type(exhausted).__name__}）")

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
        "format_version": fv,
    }

