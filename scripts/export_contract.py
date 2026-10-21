#!/usr/bin/env python3
"""导出契约与落盘（格式版本、脱敏、契约校验、口径一、原子写）。

从 ``scripts/boss_cdp_raw.py`` 抽出（2026-09-22 P1 架构重构）：
纯数据 / IO 逻辑、无 CDP 依赖，可独立测试；主文件对同名符号做 re-export，
保持 ``scripts.boss_cdp_raw.flush_jobs`` 等导入面不变。
"""

import json

try:
    from scripts import semantics as _semantics
except ImportError:  # pragma: no cover - 直接运行脚本时走此分支
    try:
        import semantics as _semantics
    except ImportError:  # pragma: no cover - 独立拷贝时语义层不可用则降级
        _semantics = None
import logging
import os
import re
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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
_INTERNAL_KEYS = ("security_id", "security_map", "lid", "encrypt_job_id",
                  "encrypt_boss_id", "encrypt_brand_id")

# 归一化后按完整 token 匹配，避免把 author/accounting 等正常字段误删。
_KEY_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_KEY_NON_ALNUM = re.compile(r"[^a-z0-9]+", re.IGNORECASE)
_SENSITIVE_KEY_TOKENS = frozenset({
    "cookie", "token", "password", "account", "auth", "secret", "session",
    "credential", "authorization", "wt2", "zp_stoken", "zp_token", "acw_tc",
})
_INTERNAL_KEY_TOKENS = frozenset({
    "security", "security_id", "security_map", "lid", "encrypt_job_id",
    "encrypt_boss_id", "encrypt_brand_id",
})
_CONTRACT_REQUIRED_FIELDS = ("job_id", "title", "location", "job_link", "company_name")
_URL_KEY_TOKENS = frozenset({
    "url", "urls", "link", "links", "job_link", "company_link", "href", "uri",
})


def _normalized_key(key):
    key = _KEY_CAMEL_BOUNDARY.sub("_", str(key))
    return _KEY_NON_ALNUM.sub("_", key).strip("_").lower()


def _is_sensitive_key(key):
    normalized = _normalized_key(key)
    tokens = set(filter(None, normalized.split("_")))
    # 复合凭据名（如 zp_stoken/acw_tc）不能只按 "_" 分词匹配；
    # 同时保留精确归一化名匹配，避免 token 交集规则漏掉这些历史字段。
    return normalized in _SENSITIVE_KEY_TOKENS or bool(tokens & _SENSITIVE_KEY_TOKENS)


def _is_internal_key(key):
    normalized = _normalized_key(key)
    return (normalized in _INTERNAL_KEY_TOKENS
            or normalized.startswith("encrypt_")
            or normalized == "security"
            or normalized.startswith("security_"))


def _is_url_key(key):
    if key is None:
        return False
    normalized = _normalized_key(key)
    return (normalized in _URL_KEY_TOKENS
            or normalized.endswith(("_url", "_link", "_uri", "_href"))
            or normalized.endswith(("_urls", "_links")))


def _looks_like_url(value):
    """判断字符串是否应进入 URL 清洗分支（不依赖字段名）。"""
    if not isinstance(value, str):
        return False
    text = value.strip()
    return text.startswith(("http://", "https://", "//"))


def _scrub_secrets(text):
    """日志/错误输出脱敏：cookie/token/__zp_stoken__ 等凭据值替换为 ***。"""
    if not isinstance(text, str):
        return text
    return _SECRET_KEY_PATTERN.sub(lambda m: f"{m.group(1)}=***", text)


def _query_pairs(value):
    """解析 query/fragment 参数；格式异常时返回空列表。"""
    try:
        return parse_qsl((value or "").lstrip("?"), keep_blank_values=True)
    except (TypeError, ValueError):
        return []


def _contains_sensitive_query(value):
    """递归检查 URL/query 值中的敏感参数（含嵌套 redirect URL）。"""
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    for key, nested in _query_pairs(parsed.query):
        if _is_sensitive_key(key) or _is_internal_key(key):
            return True
        if _looks_like_url(nested) and _contains_sensitive_query(nested):
            return True
        if _contains_sensitive_query(nested):
            return True
    return any(_is_sensitive_key(key) or _is_internal_key(key)
               for key, _ in _query_pairs(parsed.fragment))


def _sanitize_url(value):
    """移除 URL query/fragment 中的凭据，并递归清理嵌套 URL。"""
    try:
        parsed = urlsplit(value)
    except (TypeError, ValueError):
        return _scrub_secrets(value)
    safe_pairs = []
    for key, nested in _query_pairs(parsed.query):
        if _is_sensitive_key(key) or _is_internal_key(key):
            continue
        if _looks_like_url(nested):
            nested = _sanitize_url(nested)
        elif _contains_sensitive_query(nested):
            # 无法安全证明的嵌套 query 参数宁可丢弃。
            continue
        safe_pairs.append((key, _scrub_secrets(nested)))
    # fragment 不属于发给服务端的必要职位信息，统一丢弃以覆盖隐式 token。
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path,
                       urlencode(safe_pairs), ""))


def _sanitize_payload(value, *, key=None):
    """递归复制并清理 dict/list/tuple 中的敏感字段和 URL 凭据。"""
    if isinstance(value, dict):
        cleaned = {}
        for raw_key, raw_value in value.items():
            if _is_sensitive_key(raw_key) or _is_internal_key(raw_key):
                continue
            cleaned[raw_key] = _sanitize_payload(raw_value, key=raw_key)
        return cleaned
    if isinstance(value, list):
        return [_sanitize_payload(item, key=key) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_payload(item, key=key) for item in value)
    if isinstance(value, str):
        if _is_url_key(key) or _looks_like_url(value):
            return _sanitize_url(value)
        return _scrub_secrets(value)
    return value


def _sanitize_job(job):
    """导出前递归过滤敏感字段与 BOSS 内部标识。"""
    return _sanitize_payload(job)


def _normalized_job_id(value):
    """返回可哈希的 job_id；容器/空值视为非法并进入 quarantine。"""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    text = str(value).strip()
    return text or None


def _missing_required_fields(job):
    """返回 job 缺失的契约必填字段列表（非 dict / 空值都算缺失）。"""
    if not isinstance(job, dict):
        return list(_CONTRACT_REQUIRED_FIELDS)
    missing = []
    for field in _CONTRACT_REQUIRED_FIELDS:
        value = job.get(field)
        if field == "job_id":
            if _normalized_job_id(value) is None:
                missing.append(field)
        elif not str(value or "").strip():
            missing.append(field)
    return missing


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
            if not isinstance(item, dict):
                continue
            normalized = _normalized_job_id(item.get(key)) if key == "job_id" else item.get(key)
            if normalized is not None and normalized != "":
                try:
                    by_key[normalized] = item
                except TypeError:
                    continue
        for item in incoming:
            if not isinstance(item, dict):
                continue
            normalized = _normalized_job_id(item.get(key)) if key == "job_id" else item.get(key)
            if normalized is not None and normalized != "":
                try:
                    by_key[normalized] = item
                except TypeError:
                    continue
        return list(by_key.values())

    seen = set()
    for item in existing:
        if not isinstance(item, dict):
            continue
        normalized = _normalized_job_id(item.get(key)) if key == "job_id" else item.get(key)
        if normalized is not None and normalized != "":
            try:
                seen.add(normalized)
            except TypeError:
                continue
    merged = list(existing)
    for item in incoming:
        if not isinstance(item, dict):
            continue
        normalized = _normalized_job_id(item.get(key)) if key == "job_id" else item.get(key)
        if normalized is None or normalized == "":
            continue
        try:
            if normalized not in seen:
                seen.add(normalized)
                merged.append(item)
        except TypeError:
            continue
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


def _atomic_write_export_json(path, payload):
    """用户可见导出统一先递归脱敏；sidecar 等内部通道不调用此 helper。"""
    _atomic_write_json(path, _sanitize_payload(payload))


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
    if not isinstance(meta, dict):
        meta = {}
    existing_jobs = []
    old_counts = {"new": 0, "duplicate": 0, "quarantine": 0}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                old = json.load(f)
            if isinstance(old, dict):
                raw_existing = old.get("jobs")
                if isinstance(raw_existing, list):
                    existing_jobs = raw_existing
                raw_counts = old.get("record_counts")
                if isinstance(raw_counts, dict):
                    for name in old_counts:
                        value = raw_counts.get(name)
                        if type(value) is int and value >= 0:
                            old_counts[name] = value
        except (json.JSONDecodeError, OSError, ValueError, TypeError):
            pass

    existing_clean = [_sanitize_job(job) for job in existing_jobs]
    existing_ids = set()
    existing_payloads = {}
    for job in existing_clean:
        if not isinstance(job, dict):
            continue
        jid = _normalized_job_id(job.get("job_id"))
        if jid is not None:
            existing_ids.add(jid)
            existing_payloads[jid] = job

    if isinstance(jobs, (list, tuple)):
        raw_jobs = list(jobs)
    elif jobs is None:
        raw_jobs = []
    else:
        raw_jobs = [jobs]
    invalid_incoming = [job for job in raw_jobs if not isinstance(job, dict)]
    incoming_dicts = [job for job in raw_jobs if isinstance(job, dict)]
    sanitized_incoming = [_sanitize_job(job) for job in incoming_dicts]

    # 先按规范化 ID 对本次 incoming 有序去重；重复重发计入 duplicate，
    # 不同 payload 记录为显式 key_conflict。
    unique_incoming = []
    incoming_by_id = {}
    invalid_id_count = 0
    batch_conflicts = set()
    valid_incoming_count = 0
    for job in sanitized_incoming:
        jid = (_normalized_job_id(job.get("job_id"))
               if isinstance(job, dict) else None)
        if jid is None:
            invalid_id_count += 1
            continue
        valid_incoming_count += 1
        if jid in incoming_by_id:
            if incoming_by_id[jid] != job:
                batch_conflicts.add(jid)
            continue
        incoming_by_id[jid] = job
        unique_incoming.append(job)

    incoming_ids = list(incoming_by_id)
    new_count = sum(1 for jid in incoming_ids if jid not in existing_ids)
    dup_count = valid_incoming_count - new_count

    merged = merge_unique(existing_clean, unique_incoming)
    sanitized = [_sanitize_job(job) for job in merged]
    quarantine = [{
        "job_id": None,
        "reason": "record_not_object",
    } for _ in invalid_incoming]
    if invalid_id_count:
        quarantine.append({
            "job_id": None,
            "reason": f"invalid_job_id={invalid_id_count}",
        })

    valid = []
    for job in sanitized:
        missing = _missing_required_fields(job)
        if missing:
            quarantine.append({
                "job_id": job.get("job_id") if isinstance(job, dict) else None,
                "reason": "missing_required_fields=" + ",".join(missing),
            })
        else:
            valid.append(job)

    key_conflicts = {
        jid for jid, old_job in existing_payloads.items()
        if jid in incoming_by_id and old_job != incoming_by_id[jid]
    }
    key_conflicts.update(batch_conflicts)
    if key_conflicts:
        log.warning("job_id 键冲突（同 ID 不同 payload，保留首条/旧版本）: %s",
                    sorted(key_conflicts)[:5])
        quarantine.extend({"job_id": jid, "reason": "key_conflict"}
                           for jid in sorted(key_conflicts))

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
    _atomic_write_json(path, _sanitize_payload(meta))


# 详情页的 HR 活跃状态比列表**更精细**：列表侧只有「在线」/空，详情侧有
# 「刚刚活跃 / 今日活跃 / 本周活跃 / 3日内活跃 / 2周内活跃 / 本月活跃」。
# 2026-10-21 实测：合并只并 `jd`，把详情侧更精细的值丢掉了（75 条被列表的粗值
# 覆盖或直接留空）——「HR 还活跃吗」正是求职端最需要的信号，不能在合并时降级。
_ACTIVITY_RANK = {
    "": 0,
    "在线": 1,
    "本月活跃": 2,
    "2周内活跃": 3,
    "本周活跃": 4,
    "3日内活跃": 5,
    "今日活跃": 6,
    "刚刚活跃": 7,
}


def _more_specific_activity(list_value, detail_value):
    """取更精细的 HR 活跃状态（详情侧优先，列表侧兜底）。"""
    list_text = str(list_value or "").strip()
    detail_text = str(detail_value or "").strip()
    if not detail_text:
        return list_text
    if not list_text:
        return detail_text
    if _ACTIVITY_RANK.get(detail_text, 0) > _ACTIVITY_RANK.get(list_text, 0):
        return detail_text
    return list_text


def _merge_jd_into_export(target_path, details, base=None, keep_without_jd=False,
                          extra_meta=None):
    """把详情 jd 并入列表导出（口径一：默认只保留有 JD 的岗位）。

    - 每条 job 追加 `jd` 字段（有详情时）
    - `boss_active_status` 取**更精细**的一侧（详情页的「刚刚活跃/今日活跃…」优先于
      列表的「在线」/空），不因合并而降级
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
            with open(target_path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError, ValueError):
            data = None
    if not isinstance(data, dict):
        data = _sanitize_payload(base)
    else:
        data = _sanitize_payload(data)
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        return None

    jd_map = {}
    activity_map = {}
    semantic_map = {}
    for d in details or []:
        if isinstance(d, dict) and d.get("job_id") and str(d.get("jd") or "").strip():
            jid = str(d["job_id"])
            jd_map[jid] = d["jd"]
            activity_map[jid] = d.get("boss_active_status")
            semantic_map[jid] = {
                key: d.get(key) for key in
                ("post_type", "title_key", "link_verified_at")
                if d.get(key)
            }

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
            activity = _more_specific_activity(
                job.get("boss_active_status"), activity_map.get(jid))
            if activity:
                job["boss_active_status"] = activity
            for key, value in (semantic_map.get(jid) or {}).items():
                job[key] = value
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
    # ── 语义层统计与自检（2026-10-22）──────────────────────────────
    # 把"逐条读数据才能发现的问题"变成机器检查：发布主体类型分布、同源重复投放、
    # JD 污染/长度、关键字段空值率、链接时效标注。任一项不达标 → 写进 warnings。
    if _semantics is not None:
        try:
            counts = {}
            for job in kept:
                key = str(job.get("post_type") or "unlabeled")
                counts[key] = counts.get(key, 0) + 1
            data["post_type_counts"] = counts
            fingerprint_by_id, sizes = _semantics.group_duplicates(kept)
            dup_groups = {fp: n for fp, n in sizes.items() if n > 1}
            data["duplicate_groups"] = {
                "group_count": len(dup_groups),
                "records_in_groups": sum(dup_groups.values()),
                "max_group": max(dup_groups.values()) if dup_groups else 0,
            }
            # 同源重复**标记**到每条记录（dup_group/dup_size/is_primary）：
            # 平台允许同一岗位多条发布，job_id 去重抓不到 → 只能内容指纹识别。
            # 标记而不删除（猎头岗有时是唯一渠道），消费端可自行折叠。
            try:
                _semantics.mark_duplicate_groups(kept)
            except Exception:  # pragma: no cover - 标记失败不影响导出
                pass
            # 标题归一化分组：平台原样回传（AI产品经理 67 / ai产品经理 55 /
            # AI 产品经理 6）→ 归一化键把它们并成一组，聚合时才不会漏。
            try:
                data["title_groups"] = _semantics.title_group_counts(kept)
            except Exception:  # pragma: no cover
                pass
            # 活跃度三态：**"平台没给"不等于"不活跃"**（实测 14 条两边都没有活跃
            # 标签）→ 显式给 active/inactive/unknown，消费端才分得清。
            for job in kept:
                try:
                    job["active_state"] = _semantics.active_state(
                        job.get("boss_active_status"))
                except Exception:  # pragma: no cover
                    pass
            invariants = _semantics.check_invariants(kept, data)
            data["invariants"] = invariants
            failures = _semantics.invariant_failures(invariants)
            if failures:
                warns = list(data.get("warnings") or [])
                warns.append("数据完整性自检未通过：" + "；".join(failures))
                data["warnings"] = warns
        except Exception as exc:  # pragma: no cover - 自检失败不得影响导出
            data.setdefault("warnings", []).append(f"数据完整性自检异常：{exc}")
    if extra_meta:
        data.update(_sanitize_payload(extra_meta))
    _atomic_write_json(target_path, _sanitize_payload(data))
    return len(kept), len(dropped_ids)
