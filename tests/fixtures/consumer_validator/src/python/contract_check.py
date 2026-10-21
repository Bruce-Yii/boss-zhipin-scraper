# ---------------------------------------------------------------------------
# 双端契约校验器 vendor 副本（来源：ai-pm-job-intel 私有仓库）
#
# 来源 SHA: 规格侧 T2 契约 v2 交付（Issue 5278260355，docs/25 §3 顶层扩展字段表）
# 唯一口径: ai-pm-job-intel/src/python/contract_check.py（本文件 validate_export.py 同源）
#
# 同步约定（契约变更时）:
#   1. ai-pm-job-intel 递增 VALIDATOR_VERSION 并交付最新两份文件（contract_check.py + validate_export.py）
#   2. 同步替换本目录两份文件（tests/fixtures/consumer_validator/）
#   3. CI 断言 validator=v2.1.0 版本匹配，漂移即红（触发双端对齐）
#   4. 全量回归依据（私有仓不可直接引用，vendor 副本保证 CI 双端一致校验，无逻辑改动）
# ---------------------------------------------------------------------------
"""消费端契约校验：与爬虫侧规格 08 v1.0 对齐的唯一校验实现。

供三处复用：CLI 自检（validate_export.py）、S3 每日报告（check_daily_export.py）、回归测试。
"""

import re
from urllib.parse import parse_qsl, urlsplit

CONTRACT_FIELDS = ["job_id", "title", "location", "job_link", "company_name"]
_KEY_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_KEY_NON_ALNUM = re.compile(r"[^a-z0-9]+", re.IGNORECASE)
_SENSITIVE_TOKENS = frozenset({
    "cookie", "token", "password", "account", "auth", "secret", "session",
    "credential", "authorization", "wt2", "zp_stoken", "zp_token", "acw_tc",
})
_INTERNAL_TOKENS = frozenset({
    "security", "security_id", "security_map", "lid", "encrypt_job_id",
    "encrypt_boss_id", "encrypt_brand_id",
})


def _normalized_key(key):
    key = _KEY_CAMEL_BOUNDARY.sub("_", str(key))
    return _KEY_NON_ALNUM.sub("_", key).strip("_").lower()


def _is_sensitive_key(key):
    normalized = _normalized_key(key)
    return normalized in _SENSITIVE_TOKENS or bool(
        set(filter(None, normalized.split("_"))) & _SENSITIVE_TOKENS)


def _is_internal_key(key):
    normalized = _normalized_key(key)
    return (normalized in _INTERNAL_TOKENS
            or normalized.startswith("encrypt_")
            or normalized.startswith("security_"))


def _query_pairs(value):
    try:
        return parse_qsl((value or "").lstrip("?"), keep_blank_values=True)
    except (TypeError, ValueError):
        return []


def _url_sensitive_keys(value):
    """返回 URL query/fragment 中的敏感参数名；支持一层以上嵌套 URL。"""
    try:
        parsed = urlsplit(value)
    except (TypeError, ValueError):
        return set()
    hits = set()
    for key, nested in _query_pairs(parsed.query):
        if _is_sensitive_key(key) or _is_internal_key(key):
            hits.add(key)
        if isinstance(nested, str) and nested.startswith(("http://", "https://", "//")):
            hits.update(_url_sensitive_keys(nested))
    for key, _ in _query_pairs(parsed.fragment):
        if _is_sensitive_key(key) or _is_internal_key(key):
            hits.add(key)
    return hits


def _structured_sensitive_keys(value):
    """递归检查字段名；不把普通 JD 文本中的单词 token 当成凭据。"""
    hits = set()
    if isinstance(value, dict):
        for key, nested in value.items():
            if _is_sensitive_key(key) or _is_internal_key(key):
                hits.add(str(key))
            hits.update(_structured_sensitive_keys(nested))
    elif isinstance(value, list):
        for nested in value:
            hits.update(_structured_sensitive_keys(nested))
    elif isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("http://", "https://", "//")):
            hits.update(_url_sensitive_keys(stripped))
    return hits


# 校验器版本：契约变更时递增；双端比对此版本号即可检测 vendor 副本漂移
VALIDATOR_VERSION = "2.1.0"

# format_version 2 = B 增量扩展（mode/observed_jobs/exhausted 可选字段，docs/25 §3）；
# v1 文件兼容（扩展非破坏，必填字段不变）
EXPECTED_FORMAT_VERSIONS = {1, 2}


def validate_export(data) -> dict:
    """校验导出数据，返回报告 dict（ok/errors/warnings/统计）。

    format_version 1/2 均接受；v2 可选扩展字段做类型校验。所有结构错误都以
    报告返回，不把异常泄漏给 CLI 调用方。
    """
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(data, dict):
        return {
            "ok": False,
            "errors": ["顶层 JSON 必须是对象/dict"],
            "warnings": warnings,
            "job_count": 0,
            "format_version": None,
        }

    fv = data.get("format_version")
    if type(fv) is not int or fv not in EXPECTED_FORMAT_VERSIONS:
        errors.append(f"format_version={fv!r}（期望整数 1 或 2）")
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
    seen_ids = set()
    duplicate_ids = set()
    for index, job in enumerate(jobs):
        if not isinstance(job, dict):
            errors.append(f"jobs[{index}] 必须是对象/dict")
            missing["job_id"] += 1
            continue
        for field in CONTRACT_FIELDS:
            if not str(job.get(field) or "").strip():
                missing[field] += 1
        job_id = str(job.get("job_id") or "").strip()
        if job_id:
            if job_id in seen_ids:
                duplicate_ids.add(job_id)
            seen_ids.add(job_id)
    for field, count in missing.items():
        if count:
            errors.append(f"必填字段 {field} 缺失 {count}/{len(jobs)} 条")
    if duplicate_ids:
        errors.append(f"重复 job_id: {', '.join(sorted(duplicate_ids))}")

    for key in sorted(_structured_sensitive_keys(data)):
        errors.append(f"检出敏感字段或 URL 参数: {key}")

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "job_count": len(jobs),
        "format_version": fv,
    }

