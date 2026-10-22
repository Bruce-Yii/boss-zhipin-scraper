"""数据验收检查器：按**规格**（data/acceptance_spec.json）判定产物是否可用。

为什么要有这个模块（2026-10-22 的"源头"定位）
------------------------------------------------
上午连续漏掉三类缺陷的根因不是"不够仔细"，而是**验收与实现出自同一个心智模型**：
体检脚本是当场写的，它检查什么取决于我那一刻想到了什么 → 盲区必然存在，而且
**下一次体检还会用同样的盲区**（"抄了不用"这一族就是这么复发了好几轮）。

所以验收必须**独立于实现**：

1. **规格来自消费者的问题**（"这家公司是谁/这个岗是什么/多少钱/是不是猎头/是不是实习/
   还活着吗/是不是重复/链接能打开吗"），而不是来自"代码里有哪些字段"；
2. **按性质判定，不按字段清单**：P3（内容相同必须被标重复）、P4（链接必须归属本记录）、
   P5（JD 必须与岗位名对得上）这类性质**覆盖写规格时没想到的具体情形**；
3. **禁列检查**：平台内部 id/凭据不得出现在任何对外产物里（红线）。

用法::

    python scripts/acceptance.py <导出.json> [...]     # 检查一个或多个产物
    from acceptance import check_export                 # 供测试/脚本调用
"""

from __future__ import annotations

import json
import os
import re
import unicodedata

_HERE = os.path.dirname(os.path.abspath(__file__))
SPEC_PATH = os.path.join(os.path.dirname(_HERE), "data", "acceptance_spec.json")

# JD 尾部污染标记（页面模块文案混进正文）
_JUNK_TAIL_RE = re.compile(
    r"薪资详情|社保类型|提成方式|奖金补贴|刚刚活跃|今日活跃|本周活跃|"
    r"3日内活跃|2周内活跃|本月活跃|·招聘专员|·招聘主管|·HRBP")
_MIN_JD_LENGTH = 120


def load_spec(path=None):
    """读验收规格。"""
    with open(path or SPEC_PATH, encoding="utf-8") as handle:
        return json.load(handle)


def _records(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("jobs", "details"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def _norm(text):
    return unicodedata.normalize("NFKC", str(text or "")).strip().lower()


def _title_terms(title):
    """岗位名里的 2 字以上词（用于 P5：JD 必须与岗位名对得上）。"""
    text = _norm(title)
    terms = set()
    for chunk in re.split(r"[\s\-—_|/·、,，()（）【】\[\]]+", text):
        chunk = chunk.strip()
        if len(chunk) < 2:
            continue
        terms.add(chunk)
        # 中文长词再切 2 字滑窗
        if re.search(r"[\u4e00-\u9fff]", chunk) and len(chunk) > 3:
            for i in range(len(chunk) - 1):
                terms.add(chunk[i:i + 2])
    return {t for t in terms if len(t) >= 2}


def _fingerprint(job, head=180):
    """内容指纹 —— **必须与打标器同源**（models.content_fingerprint）。

    验收器与打标器各写一份指纹时，会出现"检查器说重复、打标器说不重复"（实测踩过：
    4 组真重复没被标上，因为两边指纹的"城市"归一化方式不同）。
    """
    try:
        from scripts import job_model as _model
    except ImportError:  # pragma: no cover - 直接运行脚本时走此分支
        import job_model as _model
    return _model.content_fingerprint(job, head=head)


def _registered_fields(spec):
    """登记字段集合：模型契约（权威注册表）+ 规格点名的字段。

    模型负责"有哪些字段"，规格负责"消费者需要哪些字段"——两者都要登记才算合法。
    """
    fields = set()
    model = None
    try:
        from scripts import job_model as model  # noqa: F401
    except ImportError:  # pragma: no cover
        try:
            import job_model as model  # noqa: F811
        except ImportError:
            model = None
    if model is not None:
        fields.update(model.FIELD_INDEX)
    for question in spec.get("questions") or []:
        fields.update(question.get("requires") or [])
    fields.update(spec.get("required_always") or [])
    return fields


def check_export(payload, spec=None, sample_title_check=True):
    """按规格检查一个产物（列表 / 导出 / 详情记录皆可）。

    Returns:
        ``(ok, findings)``；findings 是 ``[{"id","level","detail","count"}]``。
        level: ``error``（产物不可用）/ ``warn``（可用但需人判断）。
    """
    spec = spec or load_spec()
    records = _records(payload)
    findings = []

    def add(fid, level, detail, count=None):
        findings.append({"id": fid, "level": level, "detail": detail, "count": count})

    if not records:
        add("P0_empty", "error", "产物里没有记录")
        return False, findings

    required = list(spec.get("required_always") or [])
    value_rules = spec.get("value_rules") or {}
    registered = _registered_fields(spec)

    # P1 必填非空
    empty_counts = {}
    for field in required:
        n = sum(1 for r in records if not str(r.get(field) or "").strip())
        if n:
            empty_counts[field] = n
    if empty_counts:
        add("P1_required_non_empty", "error",
            "必填字段为空：" + "，".join(f"{k}({v} 条)" for k, v in empty_counts.items()),
            sum(empty_counts.values()))

    # P2 未登记字段（除了规格里没写但确实存在的元数据键）
    meta_allowed = {"job_id", "jd"}
    unknown = set()
    for record in records:
        for key in record:
            if key in registered or key in meta_allowed:
                continue
            unknown.add(key)
    if unknown:
        add("P2_no_unregistered_field", "warn",
            "出现规格未登记的字段：" + "，".join(sorted(unknown)[:12]),
            len(unknown))

    # P3 内容相同必须被标重复
    groups = {}
    for record in records:
        groups.setdefault(_fingerprint(record), []).append(record)
    unflagged = 0
    for members in groups.values():
        if len(members) < 2:
            continue
        keys = {str(m.get("dup_group") or "") for m in members}
        if len(keys) != 1 or "" in keys:
            unflagged += len(members)
    if unflagged:
        add("P3_duplicates_are_flagged", "error",
            f"有 {unflagged} 条内容相同但未归入同一 dup_group", unflagged)

    # P4 链接归属：同一链接出现在多个 job_id 上 = 链接串用错
    link_owner = {}
    bad_link = 0
    for record in records:
        link = str(record.get("job_link") or "")
        jid = str(record.get("job_id") or "")
        if not link:
            continue
        owner = link_owner.setdefault(link, jid)
        if owner != jid:
            bad_link += 1
    if bad_link:
        add("P4_link_belongs_to_record", "error",
            f"有 {bad_link} 条记录的 job_link 指向别的 job_id", bad_link)

    # P5 JD 与岗位名对得上
    if sample_title_check:
        mismatch = []
        for record in records:
            title = str(record.get("title") or "")
            jd = str(record.get("jd") or "")
            terms = _title_terms(title)
            if not terms or not jd:
                continue
            jd_norm = _norm(jd)
            if not any(term in jd_norm for term in terms):
                mismatch.append(record)
        if mismatch:
            add("P5_jd_matches_its_title", "warn",
                f"有 {len(mismatch)} 条 JD 与岗位名对不上（疑似张冠李戴）", len(mismatch))

    # P6 链接校验时刻
    missing_stamp = sum(1 for r in records if not str(r.get("link_verified_at") or "").strip())
    if missing_stamp:
        add("P6_link_verified", "warn",
            f"有 {missing_stamp} 条缺 link_verified_at（链接是短时效凭证）", missing_stamp)

    # P7 三个语义判定齐全且取值合法
    for field, allowed in value_rules.items():
        missing = sum(1 for r in records if not str(r.get(field) or "").strip())
        if missing:
            add("P7_semantic_labels_present", "warn",
                f"{field} 缺失 {missing} 条", missing)
            continue
        illegal = sum(1 for r in records if str(r.get(field)) not in allowed)
        if illegal:
            add("P7_semantic_labels_present", "error",
                f"{field} 有 {illegal} 条取值不在 {allowed}", illegal)

    # P8 薪资来源一致
    bad_salary = sum(1 for r in records
                     if str(r.get("salary_source") or "") == "api"
                     and not str(r.get("salary") or "").strip())
    if bad_salary:
        add("P8_salary_source_consistent", "error",
            f"有 {bad_salary} 条标了 salary_source=api 但薪资为空", bad_salary)

    # P9 JD 长度
    short = sum(1 for r in records if len(str(r.get("jd") or "")) < _MIN_JD_LENGTH)
    if short:
        add("P9_jd_not_too_short", "error",
            f"有 {short} 条 JD 短于 {_MIN_JD_LENGTH} 字", short)

    # P10 尾部污染
    junk = sum(1 for r in records if _JUNK_TAIL_RE.search(str(r.get("jd") or "")))
    if junk:
        add("P10_no_junk_tail", "error",
            f"有 {junk} 条 JD 尾部混进页面模块文案", junk)

    # 禁列（红线）
    forbidden = set(spec.get("forbidden_fields") or [])
    leaked = set()
    for record in records:
        for key in record:
            if key in forbidden:
                leaked.add(key)
    if leaked:
        add("P0_forbidden_fields", "error",
            "产物里出现平台内部字段（红线）：" + "，".join(sorted(leaked)), len(leaked))

    ok = not any(f["level"] == "error" for f in findings)
    return ok, findings


def format_findings(findings, limit=20):
    lines = []
    for finding in findings[:limit]:
        mark = "❌" if finding["level"] == "error" else "⚠️"
        lines.append(f"{mark} {finding['id']}: {finding['detail']}")
    if len(findings) > limit:
        lines.append(f"… 另有 {len(findings) - limit} 项")
    return "\n".join(lines) if lines else "✅ 全部通过"


def main(argv=None):
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__)
        return 2
    spec = load_spec()
    all_ok = True
    for path in argv:
        try:
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError) as exc:
            print(f"❌ {os.path.basename(path)}: 读取失败 {exc}")
            all_ok = False
            continue
        ok, findings = check_export(payload, spec)
        records = _records(payload)
        print(f"\n=== {os.path.basename(path)}（{len(records)} 条）===")
        print(format_findings(findings))
        all_ok = all_ok and ok
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
