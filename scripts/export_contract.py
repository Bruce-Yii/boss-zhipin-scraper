#!/usr/bin/env python3
"""导出契约与落盘（格式版本、脱敏、契约校验、口径一、原子写）。

从 ``scripts/boss_cdp_raw.py`` 抽出（2026-09-22 P1 架构重构）：
纯数据 / IO 逻辑、无 CDP 依赖，可独立测试；主文件对同名符号做 re-export，
保持 ``scripts.boss_cdp_raw.flush_jobs`` 等导入面不变。
"""

import json
import logging
import os
import re
import time

log = logging.getLogger("boss_cdp")  # 与主文件同名 logger，日志路由一致

FORMAT_VERSION = 2              # 导出文件契约版本（ai-pm-job-intel 规格 §3.2；v2 = B 增量扩展 mode/observed_jobs/exhausted，docs/25 §3，规格侧 T2 落档 5278260355）

_SENSITIVE_KEYS = ("cookie", "token", "wt2", "zp_stoken", "zp_token",
                   "password", "account", "auth", "secret")
# 日志/错误输出脱敏：key=value 形态的凭据值替换为 ***（凭证不进日志/异常/stderr）
_SECRET_KEY_PATTERN = re.compile(
    r"(__zp_stoken__|wt2|zp_token|zp_stoken|cookie|token|password|secret"
    r"|securityid|security_id)"
    r"=([^&\s\"'<>]+)",
    re.IGNORECASE,
)

# BOSS 内部标识字段（规格侧建议剔除：下游误读风险，非契约字段；
# 详情抓取用 job_link 即可导航，不依赖这些参数）
_INTERNAL_KEYS = ("security_id", "lid", "encrypt_job_id",
                  "encrypt_boss_id", "encrypt_brand_id")

# 契约必填字段（与消费端校验器 CONTRACT_FIELDS 一致）：写盘前 schema 校验，
# 缺失即 quarantine 剔除并记录原因——防页面结构漂移产出脏数据进下游
_CONTRACT_REQUIRED_FIELDS = ("job_id", "title", "location", "job_link", "company_name")


def _scrub_secrets(text):
    """日志/错误输出脱敏：cookie/token/__zp_stoken__ 等凭据值替换为 ***。"""
    if not isinstance(text, str):
        return text
    return _SECRET_KEY_PATTERN.sub(lambda m: f"{m.group(1)}=***", text)


def _sanitize_job(job):
    """导出前过滤敏感字段与 BOSS 内部标识（规格 NFR-6 + 联调建议）。

    外部数据（--merge/--input）可能夹带凭据字段；列表 API 字段均为公开
    职位信息，不受影响。
    """
    if not isinstance(job, dict):
        return job
    return {k: v for k, v in job.items()
            if not any(s in k.lower() for s in _SENSITIVE_KEYS)
            and k not in _INTERNAL_KEYS}


def _missing_required_fields(job):
    """返回 job 缺失的契约必填字段列表（非 dict / 空值都算缺失）。"""
    if not isinstance(job, dict):
        return list(_CONTRACT_REQUIRED_FIELDS)
    return [f for f in _CONTRACT_REQUIRED_FIELDS
            if not str(job.get(f) or "").strip()]


def merge_unique(existing, incoming, key="job_id", new_overrides=False):
    """按 key 合并去重。

    Args:
        existing: 已有记录列表
        incoming: 新记录列表
        key: 去重字段
        new_overrides: True 时新记录覆盖旧记录（同 key 保留新的）；False 时旧记录优先

    Returns:
        合并后的列表
    """
    if new_overrides:
        by_key = {}
        for item in existing:
            if isinstance(item, dict) and item.get(key):
                by_key[item.get(key)] = item
        for item in incoming:
            if isinstance(item, dict) and item.get(key):
                by_key[item.get(key)] = item
        return list(by_key.values())

    seen = {item.get(key, "") for item in existing if isinstance(item, dict)}
    merged = list(existing)
    for item in incoming:
        if isinstance(item, dict) and item.get(key, "") not in seen:
            seen.add(item.get(key, ""))
            merged.append(item)
    return merged


def _atomic_write_json(path, payload):
    """先写临时文件再原子替换，避免进程中断留下半截 JSON 覆盖旧数据。
    写盘前 flush+fsync（断电不丢数据），再 os.replace 原子替换。"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp_path = f"{path}.tmp{os.getpid()}"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def cleanup_stale_tmp_files(result_dir, max_age_seconds=300):
    """启动时清扫残留 .tmp 文件（崩溃/断电遗留）。

    只删修改时间超过 max_age_seconds 的 tmp，避免误删并发进程中
    正在写入的 tmp（其文件名也是 *.tmp* 形态）。
    """
    try:
        for name in os.listdir(result_dir):
            if ".tmp" not in name:
                continue
            path = os.path.join(result_dir, name)
            try:
                if time.time() - os.path.getmtime(path) > max_age_seconds:
                    os.remove(path)
            except OSError:
                pass
    except OSError:
        pass


def flush_jobs(path, meta, jobs):
    """每次有新数据就全量刷写（jobs 去重后），保证异常退出也能保留。

    血缘字段（record_counts）跨次累积：渐进写盘时每次 flush 只算当次
    new/duplicate/quarantine 增量，与旧值相加。
    """
    existing_jobs = []
    old_counts = {"new": 0, "duplicate": 0, "quarantine": 0}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                old = json.load(f)
            existing_jobs = old.get("jobs", [])
            old_counts = old.get("record_counts") or old_counts
        except (json.JSONDecodeError, OSError, ValueError):
            pass
    existing_ids = {j.get("job_id") for j in existing_jobs
                    if isinstance(j, dict) and j.get("job_id")}
    existing_payloads = {j.get("job_id"): j for j in existing_jobs
                         if isinstance(j, dict) and j.get("job_id")}
    sanitized_incoming = [_sanitize_job(j) for j in jobs]

    # 键冲突检测：incoming 与已有同 job_id 但 payload 不同 → quarantine 记录
    # （merge_unique 本身旧版本优先，这里把"静默覆盖"变成显式信号；
    #  比较在 sanitize 后进行，跨 run 续抓不因敏感键差异误报）
    incoming_ids = [j.get("job_id") for j in sanitized_incoming
                    if j.get("job_id")]
    new_count = sum(1 for jid in incoming_ids if jid not in existing_ids)
    dup_count = len(incoming_ids) - new_count

    merged = merge_unique(existing_jobs, jobs)
    sanitized = [_sanitize_job(j) for j in merged]
    quarantine = []
    valid = []
    for j in sanitized:
        missing = _missing_required_fields(j)
        if missing:
            quarantine.append({
                "job_id": j.get("job_id"),
                "reason": "missing_required_fields=" + ",".join(missing),
            })
        else:
            valid.append(j)
    key_conflicts = [jid for jid in existing_payloads
                     if jid in incoming_ids
                     and existing_payloads[jid] != next(
                         (x for x in sanitized_incoming
                          if x.get("job_id") == jid), {})]
    if key_conflicts:
        log.warning("job_id 键冲突（同 ID 不同 payload，保留旧版本）: %s",
                    key_conflicts[:5])
        quarantine.extend({"job_id": jid, "reason": "key_conflict"}
                           for jid in key_conflicts)

    counts = dict(old_counts)
    counts["new"] += new_count
    # 注：duplicate 语义为"逐次写盘的重复重发量"（write amplification 诊断，
    # 由 test_flush_jobs_accumulates_record_counts_across_writes 锁定），
    # 2026-09-22 审计曾疑其失真，经复核认定为**刻意设计**，保持不变。
    counts["duplicate"] += dup_count
    counts["quarantine"] = counts.get("quarantine", 0) + len(quarantine)
    meta["record_counts"] = counts
    meta["format_version"] = FORMAT_VERSION
    meta["total"] = len(valid)
    meta["job_count"] = len(valid)
    if quarantine:
        meta["quarantine"] = quarantine
    meta["jobs"] = valid
    _atomic_write_json(path, meta)


def _merge_jd_into_export(target_path, details, base=None, keep_without_jd=False,
                          extra_meta=None):
    """把详情 jd 并入列表导出（口径一：默认只保留有 JD 的岗位）。

    - 每条 job 追加 `jd` 字段（有详情时）
    - `keep_without_jd=False`（默认）：**剔除无 JD 的岗位**——用户口径"没 JD 的
      岗位毫无意义"，避免下游再筛
    - meta 记录 `jd_coverage`（with_jd/total_before/dropped_no_jd）与 `dropped_no_jd`
      （被剔除 job_id 列表，可追溯）；`job_count`/`total` 同步为保留数

    Args:
        target_path: 列表文件路径（存在则以其为准，否则用 base）
        details: 详情记录列表（每条含 job_id/jd）
        base: target 不存在时的基础列表 dict（如 --input 模式）
        keep_without_jd: True 时保留无 JD 岗位（仅标注）

    Returns:
        (kept, dropped) 或 None（无法处理时）
    """
    data = None
    if target_path and os.path.exists(target_path):
        try:
            with open(target_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError, ValueError):
            data = None
    if not isinstance(data, dict):
        data = base
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        return None

    jd_map = {}
    for d in details or []:
        if isinstance(d, dict) and d.get("job_id") and str(d.get("jd") or "").strip():
            jd_map[str(d["job_id"])] = d["jd"]

    jobs = data["jobs"]
    kept, dropped_ids = [], []
    with_jd = 0
    for job in jobs:
        if not isinstance(job, dict):
            continue
        jid = str(job.get("job_id") or "")
        jd = jd_map.get(jid)
        if jd:
            job = {**job, "jd": jd}
            kept.append(job)
            with_jd += 1
        elif keep_without_jd:
            kept.append(job)
        else:
            dropped_ids.append(jid)

    total_before = len(jobs)
    data["jobs"] = kept
    data["job_count"] = len(kept)
    data["total"] = len(kept)
    data["jd_coverage"] = {
        "with_jd": with_jd,
        "total_before": total_before,
        "dropped_no_jd": len(dropped_ids),
    }
    if dropped_ids:
        data["dropped_no_jd"] = dropped_ids
        warnings = list(data.get("warnings") or [])
        warnings.append(
            f"口径一：已剔除 {len(dropped_ids)} 条无 JD 岗位（见 meta.dropped_no_jd）")
        data["warnings"] = warnings
    if extra_meta:
        data.update(extra_meta)
    _atomic_write_json(target_path, data)
    return len(kept), len(dropped_ids)
