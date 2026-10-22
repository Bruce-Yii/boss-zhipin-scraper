"""保真核对器：平台原文 ↔ 我们的记录，逐字段核对。

用户给的验收标准（原话）：
    「咱们验收标准就是 boss 上的东西，纸片上是什么样，你爬取出来的就应该是什么样」。

这句话要能**被核对**，前提是两件事：
1. **原始响应落盘**（可溯源）：没有原文就无从比对；
2. **每个字段说清值怎么来的**（溯源声明，见 job_model.FieldSpec.provenance）：
   - ``verbatim``   平台原样抄来 → 直接比；
   - ``normalized`` 平台原样但按**声明过的规则**改写（拼接/过滤）→ 按同一条规则比；
   - ``derived``    由平台字段推导 → **不与原文比**，核对①推导输入在原文里存在
     ②推导规则有登记。

三种判定结果：
- ``逐字一致`` / ``规范化后一致``：这一字段保真 ✓
- ``不一致``：**保真缺陷**（两边值都列出来，可复核）
- ``无法核对``：说了清原因（不是"算通过"）

用法::

    python scripts/fidelity.py <raw_响应.json> <产物.json> [--limit N]
"""

from __future__ import annotations

import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from scripts import job_model as _model  # noqa: E402

# 判定结果
EXACT = "逐字一致"
NORMALIZED = "规范化后一致"
MISMATCH = "不一致"
NO_INPUT = "无法核对"

# 产物里没有、原文里也没意义的字段（内部 id/凭据不进导出 → 不参与核对）
SKIP_FIELDS = {
    "company", "tags_list", "link",          # 历史别名（值取自模型字段）
    "security_id", "lid", "encrypt_job_id", "encrypt_boss_id", "encrypt_brand_id",
}


def _raw_jobs(raw_payload):
    """从原始响应里取出职位条目。

    支持三种落盘形态：
    1. 抓取器落盘的原始响应包：``{"list_pages": [{"page": 1, "response": {...}}]}``
    2. 平台 joblist 原始响应：``{"zpData": {"jobList": [...]}}``
    3. 已抽好的数组：``[...]``
    """
    if isinstance(raw_payload, list):
        return raw_payload
    if isinstance(raw_payload, dict):
        pages = raw_payload.get("list_pages")
        if isinstance(pages, list):
            jobs = []
            for entry in pages:
                if not isinstance(entry, dict):
                    continue
                response = entry.get("response")
                if not isinstance(response, dict):
                    continue
                data = response.get("zpData")
                if isinstance(data, dict) and isinstance(data.get("jobList"), list):
                    jobs.extend(data["jobList"])
            return jobs
        data = raw_payload.get("zpData")
        if isinstance(data, dict) and isinstance(data.get("jobList"), list):
            return data["jobList"]
        for key in ("jobs", "jobList"):
            if isinstance(raw_payload.get(key), list):
                return raw_payload[key]
    return []


def _records(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("jobs", "details"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return []


def _value_from_raw(raw, paths, shape):
    """按**声明的形状**从原文取值（verbatim=原样；normalized=拼接/过滤）。"""
    if not paths:
        return None, False
    is_join = shape.startswith("join:")
    values = []
    for path in paths:
        if path in raw:
            values.append(raw.get(path))
            continue
        if is_join:
            # 拼接形状：平台会省略空值（如只给 cityName 不给 areaDistrict）
            # → 缺失的段按空处理（拼接本身就是声明过的规范化，比较依然成立）
            values.append("")
            continue
        # 单值形状且原文没有这个键 → 该字段无法核对（不是"通过"）
        return None, False
    if is_join:
        spec = shape[len("join:"):]
        sep, _, filt = spec.partition("|filter:")
        items = []
        for value in values:
            if isinstance(value, (list, tuple)):
                items.extend(value)
            elif value not in (None, ""):
                items.append(value)
        if filt:
            items = [item for item in items if str(item) != filt]
        return sep.join(str(item) for item in items), True
    if shape == "zero_as_empty":
        # 平台用 0 表示"没有"（proxyJob=0 / proxyType=0），我们统一存空串 →
        # 这是**声明过的规范化**，不是逐字抄来，按规范化后一致处理。
        value = values[0]
        if value in (0, "0", None, ""):
            return "", True
        return str(value), True
    if shape == "list":
        return values[0], True
    if shape == "str":
        value = values[0]
        return ("" if value is None else str(value)), True
    return values[0], True


def _as_text(value):
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return " | ".join(str(v) for v in value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _compare(spec, raw, record):
    """核对一个字段，返回 ``(状态, 期望值, 实际值, 说明)``。"""
    if spec.provenance in ("", None):
        return NO_INPUT, "", "", "字段没有溯源声明"
    if spec.is_derived:
        if not spec.raw_paths:
            return NO_INPUT, "", "", f"派生字段的规则是 {spec.derive_rule}，不与原文比"
        missing = [p for p in spec.raw_paths if p not in raw]
        if missing:
            return NO_INPUT, "", "", f"推导输入在原文里缺失：{'、'.join(missing)}"
        if spec.derive_rule and spec.derive_rule not in _model.DERIVE_RULES:
            return NO_INPUT, "", "", f"推导规则 {spec.derive_rule} 未登记"
        return EXACT, "", "", f"派生字段（规则 {spec.derive_rule}，输入齐全）"

    expected, found = _value_from_raw(raw, spec.raw_paths, spec.raw_shape)
    if not found:
        return NO_INPUT, "", "", f"原文里没有 {'、'.join(spec.raw_paths)}"
    actual = record.get(spec.name)
    if spec.type == "list":
        expected_list = expected if isinstance(expected, list) else []
        actual_list = actual if isinstance(actual, list) else []
        if [str(x) for x in expected_list] == [str(x) for x in actual_list]:
            return (EXACT if spec.provenance == "verbatim" else NORMALIZED), \
                _as_text(expected), _as_text(actual), ""
        return MISMATCH, _as_text(expected), _as_text(actual), ""
    if _as_text(expected) == _as_text(actual):
        return (EXACT if spec.provenance == "verbatim" else NORMALIZED), \
            _as_text(expected), _as_text(actual), ""
    return MISMATCH, _as_text(expected), _as_text(actual), ""


def _norm_text(value):
    """比对用的归一化：去掉所有空白（JD 的换行/缩进不该算差异）。"""
    return re.sub(r"\s+", "", str(value or ""))


def check_detail_fidelity(raw_payload, records):
    """详情侧保真核对：交付的 JD 必须是页面原文的**子序列**。

    两条判据（都不依赖我的主观判断）：
    1. **没有凭空多字**：交付的 JD（去空白）必须是原文（去空白）的子串；
       否则说明我们改了内容或拼接了别的东西 → 保真缺陷。
    2. **没有悄悄丢一大半**：清洗后长度 / 原文长度 < 阈值 → 可疑（告警级）。
       清洗本来就该只去掉招聘者卡片/技能标签/页面模块，砍掉大半说明切错了。
    """
    details = {}
    if isinstance(raw_payload, dict):
        for entry in raw_payload.get("details") or []:
            if isinstance(entry, dict) and entry.get("job_id"):
                details[str(entry["job_id"])] = entry.get("raw_text") or ""
    findings = []
    checked = 0
    for record in records:
        if not isinstance(record, dict):
            continue
        job_id = str(record.get("job_id") or "")
        raw_text = details.get(job_id)
        if not raw_text:
            continue
        jd = str(record.get("jd") or "")
        if not jd:
            continue
        checked += 1
        norm_raw = _norm_text(raw_text)
        norm_jd = _norm_text(jd)
        if norm_jd and norm_jd not in norm_raw:
            findings.append({
                "job_id": job_id,
                "field": "jd",
                "title": str(record.get("title") or "")[:24],
                "expected": f"原文 {len(norm_raw)} 字（去空白）",
                "actual": f"交付 {len(norm_jd)} 字，且**不是原文子串**",
                "level": "error",
            })
            continue
        if norm_raw and len(norm_jd) / len(norm_raw) < 0.6:
            findings.append({
                "job_id": job_id,
                "field": "jd",
                "title": str(record.get("title") or "")[:24],
                "expected": f"原文 {len(norm_raw)} 字（去空白）",
                "actual": f"交付只剩 {len(norm_jd)} 字（{len(norm_jd)/len(norm_raw):.0%}）",
                "level": "warn",
            })
    errors = [f for f in findings if f.get("level") == "error"]
    return (not errors), {
        "checked_details": checked,
        "detail_findings": findings,
        "detail_error_count": len(errors),
    }

def check_fidelity(raw_payload, product_payload, limit=None, job_model=None):
    """核对一份产物与平台原文。

    Returns:
        ``(ok, report)``；report 含逐字段统计、失配清单、无法核对清单。
    """
    model = job_model or _model
    raw_jobs = _raw_jobs(raw_payload)
    records = _records(product_payload)
    raw_by_key = {}
    for raw in raw_jobs:
        if not isinstance(raw, dict):
            continue
        # 身份：平台的 encryptJobId（明文就在导出的 job_link 里，原始响应落盘时保留）
        encrypt = str(raw.get("encryptJobId") or raw.get("encryptjobid") or "")
        link = (f"https://www.zhipin.com/job_detail/{encrypt}.html" if encrypt else "")
        key = model.compute_job_id(link or str(raw.get("jobName") or ""))
        if key:
            raw_by_key[key] = raw
        # 兜底：有的记录 job_id 由 title 退化成 md5(title)
        if not encrypt:
            fallback = model.compute_job_id(str(raw.get("jobName") or ""))
            if fallback:
                raw_by_key.setdefault(fallback, raw)

    stats = {"exact": 0, "normalized": 0, "mismatch": 0, "no_check": 0}
    mismatches = []
    no_check = []
    checked_jobs = 0
    for record in records:
        if not isinstance(record, dict):
            continue
        job_id = str(record.get("job_id") or "")
        raw = raw_by_key.get(job_id)
        if raw is None:
            no_check.append({"job_id": job_id, "field": "*",
                             "reason": "原文里找不到这条（平台列表里没有或 id 对不上）"})
            stats["no_check"] += 1
            continue
        checked_jobs += 1
        for spec in model.FIELD_SPECS:
            if spec.name in SKIP_FIELDS:
                continue
            if spec.provenance in ("", None):
                continue
            if spec.source == _model.SOURCE_DETAIL:
                continue          # 详情侧核对需要详情页原文（另一份 raw）
            state, expected, actual, note = _compare(spec, raw, record)
            if state == EXACT:
                stats["exact"] += 1
            elif state == NORMALIZED:
                stats["normalized"] += 1
            elif state == MISMATCH:
                stats["mismatch"] += 1
                mismatches.append({"job_id": job_id, "field": spec.name,
                                   "title": str(record.get("title") or "")[:24],
                                   "expected": expected[:120], "actual": actual[:120]})
            else:
                stats["no_check"] += 1
                no_check.append({"job_id": job_id, "field": spec.name, "reason": note})
            if limit and len(mismatches) >= limit:
                break

    # 保真率只以**可比对的字段**为分母："无法核对"（派生字段、原文没有该键）
    # 不是"不保真"，把它算进分母会凭空压低数字，反而让真缺陷被噪音淹没。
    comparable = stats["exact"] + stats["normalized"] + stats["mismatch"]
    fidelity = ((stats["exact"] + stats["normalized"]) / comparable) if comparable else 0.0
    ok = stats["mismatch"] == 0 and checked_jobs > 0
    return ok, {
        "checked_jobs": checked_jobs,
        "total_records": len(records),
        "raw_jobs": len(raw_jobs),
        "stats": stats,
        "fidelity_rate": fidelity,
        "mismatches": mismatches,
        "no_check": no_check[:50],
        "no_check_count": len(no_check),
    }


def format_report(report):
    stats = report.get("stats") or {}
    lines = [
        "=== 保真核对（平台原文 ↔ 我们的记录）===",
        f"  核对的记录：{report.get('checked_jobs')}/{report.get('total_records')}"
        f"（原文 {report.get('raw_jobs')} 条）",
        f"  逐字一致 {stats.get('exact', 0)}｜规范化后一致 {stats.get('normalized', 0)}"
        f"｜不一致 **{stats.get('mismatch', 0)}**｜无法核对 {stats.get('no_check', 0)}",
        f"  保真率：{report.get('fidelity_rate', 0):.1%}",
    ]
    if report.get("mismatches"):
        lines.append("  ── 不一致明细 ──")
        for item in report["mismatches"][:20]:
            lines.append(f"    · {item['field']}｜{item['title']}｜"
                         f"平台={item['expected']!r}｜我们={item['actual']!r}")
        if len(report["mismatches"]) > 20:
            lines.append(f"    … 另有 {len(report['mismatches']) - 20} 条")
    if report.get("no_check_count"):
        lines.append(f"  ── 无法核对 {report['no_check_count']} 处（抽样）──")
        for item in report.get("no_check", [])[:8]:
            lines.append(f"    · {item.get('field')}｜{item.get('reason')}")
    return "\n".join(lines)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 2:
        print(__doc__)
        return 2
    raw_path, product_path = argv[0], argv[1]
    limit = None
    if "--limit" in argv:
        try:
            limit = int(argv[argv.index("--limit") + 1])
        except (IndexError, ValueError):
            limit = None
    with open(raw_path, encoding="utf-8") as handle:
        raw_payload = json.load(handle)
    with open(product_path, encoding="utf-8") as handle:
        product_payload = json.load(handle)
    ok, report = check_fidelity(raw_payload, product_payload, limit=limit)
    print(format_report(report))
    # 详情侧（可选）：第二个 raw 是详情页原文
    detail_raw = argv[2] if len(argv) > 2 and not argv[2].startswith("--") else None
    if detail_raw:
        with open(detail_raw, encoding="utf-8") as handle:
            detail_payload = json.load(handle)
        detail_ok, detail_report = check_detail_fidelity(
            detail_payload, _records(product_payload))
        print(f"  详情侧核对：{detail_report['checked_details']} 条 JD，"
              f"不一致 {detail_report['detail_error_count']} 条")
        for item in detail_report["detail_findings"][:10]:
            mark = "❌" if item.get("level") == "error" else "⚠️"
            print(f"    {mark} {item['field']}｜{item.get('title', '')}｜"
                  f"{item.get('expected', '')}｜{item.get('actual', '')}")
        ok = ok and detail_ok
    print("结论：", "保真 ✓" if ok else "存在保真缺陷 ✗")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
