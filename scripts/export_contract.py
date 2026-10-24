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

try:
    from scripts import job_model as _job_model
except ImportError:  # pragma: no cover - 直接运行脚本时走此分支
    try:
        import job_model as _job_model
    except ImportError:  # pragma: no cover - 独立拷贝时模型不可用则降级
        _job_model = None
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


# 原始响应落盘用：与导出**不同**的一套规则（2026-10-22）。
#
# 为什么不同：导出产物是给用户看的，BOSS 内部标识不进产物；而**原始响应**是
# 保真核对的依据——没有岗位身份（encryptJobId）就没法把原文和记录对上，
# 原文落下来也等于没落。
#
# encryptJobId 不是凭据：它就是公开 URL 里的那一段
# （https://www.zhipin.com/job_detail/{encryptJobId}.html），
# **已经明文出现在导出的 job_link 里**，留着它不增加任何暴露面。
#
# 仍然必须剥掉的（真凭据 / 个人信息）：
#   securityId（详情接口凭据）、encryptBossId（HR 身份）、lid、
#   cookie / token / zp_stoken 等一切凭据。
_RAW_KEEP_KEYS = frozenset({"encrypt_job_id", "encrypt_brand_id"})
_RAW_DROP_KEYS = frozenset({
    "securityid", "security_id", "encryptbossid", "encrypt_boss_id", "lid",
})


def _sanitize_raw_payload(value, *, key=None):
    """原始响应落盘前的清理：保留岗位身份，剥掉凭据与个人信息。"""
    if isinstance(value, dict):
        cleaned = {}
        for raw_key, raw_value in value.items():
            normalized = _normalized_key(raw_key)
            if normalized in _RAW_DROP_KEYS:
                continue
            if normalized.startswith("encrypt") and normalized not in _RAW_KEEP_KEYS:
                continue
            if _is_sensitive_key(raw_key):
                continue
            cleaned[raw_key] = _sanitize_raw_payload(raw_value, key=raw_key)
        return cleaned
    if isinstance(value, list):
        return [_sanitize_raw_payload(item, key=key) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_raw_payload(item, key=key) for item in value)
    if isinstance(value, str):
        if _is_url_key(key) or _looks_like_url(value):
            return _sanitize_url(value)
        return _scrub_secrets(value)
    return value


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


def attach_acceptance_result(payload, source_path=None):
    """把**验收结论**写进产物 meta（``meta.acceptance``）——结论跟着产物走。

    独立审计 2026-10-22 指出：新验收器在生产代码里零引用，"没人被告知要跑，
    结论不进入任何决策"。修法不是提醒，是**让结论成为产物的一部分**：
    用户拿到文件就能看到它是否通过了验收、哪些性质没过。

    验收器不可用时**不能毁产物**（只记一条说明）→ 验收是附加信息，不是产物的前置条件。
    """
    if not isinstance(payload, dict):
        return payload
    try:
        try:
            from scripts import acceptance as _acc
        except ImportError:  # pragma: no cover - 直接运行脚本时走此分支
            import acceptance as _acc
        ok, findings = _acc.check_export(payload)
    except Exception as exc:  # pragma: no cover - 验收器不可用/异常时不能毁产物
        payload["acceptance"] = {"ok": None, "error": f"验收器不可用：{exc}"}
        return payload
    payload["acceptance"] = {
        "ok": ok,
        "source": source_path or "",
        "error_count": sum(1 for f in findings if f["level"] == "error"),
        "warn_count": sum(1 for f in findings if f["level"] == "warn"),
        "findings": [{"id": f["id"], "level": f["level"], "detail": f["detail"]}
                     for f in findings],
    }
    return payload


def _merge_dropped_items(prev, new, present_ids=()):
    """丢弃清单**累计去重**（按 job_id，保持先出现顺序）。

    增量合入同一文件时，本轮丢弃记录不覆盖历史（issue #167：dropped_junk
    115 条被本轮 4 条盖掉）。``present_ids`` 里的 job_id（= 已回到产物里
    的岗位）从清单剔除——清单始终只描述"不在产物里的"。
    """
    merged = {}
    for item in list(prev or []) + list(new or []):
        if not isinstance(item, dict):
            continue
        jid = str(item.get("job_id") or "")
        if jid and jid not in merged:
            merged[jid] = item
    for jid in present_ids:
        merged.pop(jid, None)
    return list(merged.values())


def _merge_jd_into_export(target_path, details, base=None, keep_without_jd=False,
                          extra_meta=None, keep_junk=False):
    """把详情 jd 并入列表导出（口径一：默认只保留有 JD 的岗位）。

    - 每条 job 追加 `jd` 字段（有详情时）
    - `boss_active_status` 取**更精细**的一侧（详情页的「刚刚活跃/今日活跃…」优先于
      列表的「在线」/空），不因合并而降级
    - `keep_without_jd=False`（默认）：**剔除无 JD 的岗位**——用户口径"没 JD 的
      岗位毫无意义"，避免下游再筛
    - `keep_junk=False`（默认）：**删库**——销售/技术/噪声岗直接剔除（用户
      2026-10-23 口径，旧"摘要层过滤"作废）；`--keep-junk` 可保留
    - meta 记录 `jd_coverage`（with_jd/total_before/dropped_no_jd）与 `dropped_no_jd`
      （被剔除 job_id 列表，可追溯；**跨轮累计去重**，见 #167）；`job_count`/`total`
      同步为保留数

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

    # 详情记录按 job_id 归拢；合并**由岗位模型负责**（字段白名单、覆盖规则、
    # 派生语义重算都在模型里，不再在这里手工搬字段——那正是"抄了/丢了一半"的来源）。
    detail_by_id = {}
    for d in details or []:
        if isinstance(d, dict) and d.get("job_id") and str(d.get("jd") or "").strip():
            detail_by_id[str(d["job_id"])] = d

    jobs = data["jobs"]
    kept, dropped_ids = [], []
    with_jd = 0
    for job in jobs:
        if not isinstance(job, dict):
            continue
        jid = str(job.get("job_id") or "")
        detail = detail_by_id.get(jid)
        if detail:
            if _job_model is not None:
                record = _job_model.JobRecord(job).merge_detail(detail)
                activity = _more_specific_activity(
                    job.get("boss_active_status"), detail.get("boss_active_status"))
                if activity:
                    record.data["boss_active_status"] = activity
                record.apply_derived(
                    link_verified_at=record.data.get("link_verified_at", ""))
                job = record.to_dict()
            else:  # pragma: no cover - 模型不可用时的降级路径
                job = {**job, "jd": detail.get("jd")}
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
    # ── 删库（2026-10-23 用户口径）──────────────────────────────
    # 销售/技术/噪声岗直接剔除，不保留（旧"摘要层过滤"作废）。
    # --keep-junk 可保留（排查用）。删库在自检/验收之前，结果写进
    # data["dropped_junk"]（job_id/title/company/reason，可追溯）。
    # 极端情况（全删光）会走到下面的空导出保护，保留列表原数据并告警。
    junk = {}
    if not keep_junk and _semantics is not None and hasattr(_semantics, "drop_junk_jobs"):
        try:
            kept2, junk = _semantics.drop_junk_jobs(kept)
            if junk.get("items"):
                data["jobs"] = kept2
                data["job_count"] = len(kept2)
                data["total"] = len(kept2)
                # 累计去重而非覆盖：增量合入时历史删库记录不丢（issue #167）
                data["dropped_junk"] = _merge_dropped_items(
                    data.get("dropped_junk"), junk["items"])
                data.setdefault("warnings", []).append(
                    f"删库：本次剔除 {len(junk['items'])} 条销售/技术/噪声岗"
                    f"（累计 {len(data['dropped_junk'])} 条，"
                    "用户 2026-10-23 口径，见 dropped_junk）")
                print(f"🗑️ 删库：剔除 {len(junk['items'])} 条销售/技术/噪声岗"
                      f"（{', '.join(sorted({it['reason'].split('（')[0] for it in junk['items']})[:4])}）")
                kept = kept2
            if junk.get("suspect_items"):
                data["suspect_sales_jd"] = junk["suspect_items"]
                data.setdefault("warnings", []).append(
                    f"销售疑似（JD）：{len(junk['suspect_items'])} 条 JD 命中销售强特征词，"
                    "未删库，请人工复核（见 suspect_sales_jd）")
        except Exception as exc:  # pragma: no cover - 删库失败不得影响导出
            data.setdefault("warnings", []).append(f"删库规则异常（已跳过删库）：{exc}")
    # 验收结论**跟着产物走**（独立审计：新验收器在生产代码里零引用 → 没人跑、
    # 结论进不了决策）。挂在最终产物出口，不是靠人记得手动跑。
    # 顺序修正（2026-10-23）：验收必须跑在**全部派生字段算完之后**——
    # 重复组打标（dup_group/dup_size）、active_state、invariants、warnings 都
    # 在下面的语义块里产生；此前验收跑在它们之前，合并已有 dup 标记的旧产物时
    # 会读到陈旧 dup_size → P13 误报"组内条数不符"、P3 误报"重复未标记"。
    # 现在挪到写盘前最后一步，验收看到的就是落盘的同一份状态。
    # 漏斗 meta（issue #167）：默认口径一（剔无 JD）下按**累计口径**对账——
    # dropped 清单是跨轮累计的，with_jd/total_before 必须与之一致才对得上账；
    # 全量重建（正常路径）时累计值 == 单轮值，与旧行为逐字一致。
    # keep_without_jd 模式 kept 可含无 JD 记录，累计口径不成立 → 维持本轮口径。
    job_ids_now = {str(j.get("job_id") or "")
                   for j in data["jobs"] if isinstance(j, dict)}
    prev_junk = data.get("dropped_junk") or []
    junk_items = _merge_dropped_items(prev_junk, [], present_ids=job_ids_now)
    if prev_junk:
        data["dropped_junk"] = junk_items
    junk_total = len(junk_items)
    prev_nojd = [str(x) for x in (data.get("dropped_no_jd") or []) if str(x)]
    cumulative_nojd = [jid for jid in dict.fromkeys(prev_nojd + dropped_ids)
                       if jid not in job_ids_now]
    if keep_without_jd:
        data["jd_coverage"] = {
            "with_jd": with_jd,
            "total_before": total_before,
            "dropped_no_jd": len(dropped_ids),
        }
        cumulative_nojd = list(dropped_ids)
    else:
        with_jd_total = len(data["jobs"]) + junk_total
        data["jd_coverage"] = {
            "with_jd": with_jd_total,
            "total_before": with_jd_total + len(cumulative_nojd),
            "dropped_no_jd": len(cumulative_nojd),
        }
    if cumulative_nojd:
        data["dropped_no_jd"] = cumulative_nojd
        warnings = list(data.get("warnings") or [])
        warnings.append(
            f"口径一：已剔除 {len(cumulative_nojd)} 条无 JD 岗位（见 meta.dropped_no_jd）")
        data["warnings"] = warnings
    elif "dropped_no_jd" in data:
        del data["dropped_no_jd"]  # 全部回炉 → 旧清单不残留
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
    # ── 空导出不覆盖原文件（独立审计 F8）─────────────────────────
    # 详情全失败（登录失效/风控/坏页台账）时 kept 会是 0 → 旧行为把列表文件里
    # 已有的 title/薪资/公司数据整个覆盖成空数组，**退出码还是 0** → 下游按
    # "成功"处理就拿到空表。这里改成：**不写**（保留原文件），并把失败原因
    # 写进 meta.warnings，让调用方能给出非零退出码。
    if total_before > 0 and not kept:
        data["jobs"] = list(jobs)          # 保留原来的列表数据
        data["job_count"] = len(jobs)
        data["total"] = len(jobs)
        data["empty_export_blocked"] = True
        warns = list(data.get("warnings") or [])
        warns.append(
            f"导出为空（详情 {total_before} 条全部未拿到 JD）→ **未覆盖原文件**，"
            "保留列表侧数据；请检查登录状态/风控/坏页台账")
        data["warnings"] = warns
    # 验收：**最后一步**，看到的就是即将落盘的同一份状态（见上方顺序修正说明）。
    attach_acceptance_result(data, source_path=target_path)
    _atomic_write_json(target_path, _sanitize_payload(data))
    return len(kept), len(dropped_ids)


def cleanup_corrupted_json(directory):
    """启动时检测并隔离损坏的 JSON 文件（不可解析或零字节）。

    根因：进程被杀/断电/旧版本非原子写可能留下损坏文件。损坏文件不可解析，
    但占空间且让溯源/校验失败。隔离（移入 quarantine/）而非删除，便于恢复。
    返回隔离的文件数。
    """
    import json
    import os
    import shutil
    if not directory or not os.path.isdir(directory):
        return 0
    quarantined = 0
    quarantine_dir = os.path.join(directory, "quarantine")
    for name in os.listdir(directory):
        if not name.endswith(".json"):
            continue
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            continue
        try:
            size = os.path.getsize(path)
            if size == 0:
                raise ValueError("zero-byte file")
            with open(path, encoding="utf-8") as handle:
                json.load(handle)
        except (json.JSONDecodeError, OSError, ValueError, UnicodeDecodeError):
            os.makedirs(quarantine_dir, exist_ok=True)
            try:
                shutil.move(path, os.path.join(quarantine_dir, name))
                quarantined += 1
            except OSError:
                pass
    return quarantined
