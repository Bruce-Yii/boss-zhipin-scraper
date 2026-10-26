# -*- coding: utf-8 -*-
"""结果校验命令（从 boss_cdp_raw.py 增量拆分）。"""

import os
import json
import logging

log = logging.getLogger("boss_cdp")

def _core():
    import sys as _sys
    candidates = [m for m in _sys.modules.values()
                 if getattr(m, "__file__", None)
                 and str(m.__file__).endswith("boss_cdp_raw.py")]
    if candidates:
        return candidates[-1]
    try:
        from . import boss_cdp_raw as _m
        return _m
    except Exception:
        if "boss_cdp_raw" in _sys.modules:
            return _sys.modules["boss_cdp_raw"]
        import boss_cdp_raw as _m
        return _m
def verify_results(list_path, details_path=None):
    """校验已抓取结果文件完整性。

    检查：列表/详情 JSON 可解析性、job 必备字段（job_id/title）、重复
    job_id、详情 JD 完整度（短于 _core().MIN_DETAIL_TEXT_LENGTH 视为残缺）、
    列表-详情覆盖率。

    Args:
        list_path: boss_jobs_*.json 路径
        details_path: boss_details_*.json 路径；不传则自动查找
            （同时间戳优先，其次最新）

    Returns:
        dict: {"ok", "issues", "list": {"count"}, "details": {"count"},
               "detail_path", "coverage", "missing"}
    """
    issues = []

    list_count = 0
    list_ids = set()
    if not _core().os.path.exists(list_path):
        issues.append(f"列表文件不存在: {list_path}")
        list_data = None
    else:
        try:
            with open(list_path, encoding="utf-8") as f:
                list_data = _core().json.load(f)
        except (_core().json.JSONDecodeError, OSError, ValueError) as e:
            issues.append(f"列表文件无法解析: {e}")
            list_data = None
    if list_data is not None:
        jobs = list_data.get("jobs") if isinstance(list_data, dict) else None
        if not isinstance(jobs, list) or not jobs:
            issues.append("列表没有职位数据（jobs 为空或缺失）")
        else:
            list_count = len(jobs)
            seen = set()
            missing_fields = 0
            dup = set()
            for job in jobs:
                if not isinstance(job, dict):
                    continue
                jid = str(job.get("job_id") or "").strip()
                if not jid or not str(job.get("title") or "").strip():
                    missing_fields += 1
                if jid:
                    if jid in seen:
                        dup.add(jid)
                    seen.add(jid)
                    list_ids.add(jid)
            if missing_fields:
                issues.append(f"列表有 {missing_fields} 条记录缺少 job_id 或 title")
            if dup:
                issues.append(f"列表存在重复 job_id: {', '.join(sorted(dup)[:5])}")
            if not list_ids:
                issues.append("列表没有有效的 job_id，无法与详情匹配")

    if details_path is None:
        details_path = _core()._latest_details_path(list_path)
    detail_count = 0
    detail_ids = set()
    if details_path is None or not _core().os.path.exists(details_path):
        issues.append("未找到详情文件（可用 --detail-output 指定路径）")
        details = None
    else:
        try:
            with open(details_path, encoding="utf-8") as f:
                details = _core().json.load(f)
        except (_core().json.JSONDecodeError, OSError, ValueError) as e:
            issues.append(f"详情文件无法解析: {e}")
            details = None
    if details is not None:
        if not isinstance(details, list) or not details:
            issues.append("详情文件没有数据（空列表）")
        else:
            detail_count = len(details)
            seen = set()
            dup = set()
            short_jd = 0
            for d in details:
                if not isinstance(d, dict):
                    continue
                jid = str(d.get("job_id") or "").strip()
                if jid:
                    if jid in seen:
                        dup.add(jid)
                    seen.add(jid)
                    detail_ids.add(jid)
                if len(str(d.get("jd") or "")) < _core().MIN_DETAIL_TEXT_LENGTH:
                    short_jd += 1
            if dup:
                issues.append(f"详情存在重复 job_id: {', '.join(sorted(dup)[:5])}")
            if short_jd:
                issues.append(f"详情有 {short_jd} 条 JD 过短（<{_core().MIN_DETAIL_TEXT_LENGTH} 字），可能抓取残缺")

    missing = sorted(list_ids - detail_ids) if list_ids else []
    coverage = (len(list_ids) - len(missing)) / len(list_ids) if list_ids else 0.0
    if list_ids and coverage < 1.0:
        shown = ", ".join(missing[:5])
        more = f" 等 {len(missing)} 条" if len(missing) > 5 else ""
        issues.append(f"详情覆盖率 {coverage:.0%}，缺失 {len(missing)} 条: {shown}{more}")

    return {
        "ok": not issues,
        "issues": issues,
        "list": {"count": list_count},
        "details": {"count": detail_count},
        "detail_path": details_path,
        "coverage": coverage,
        "missing": missing,
    }


def run_verify(list_path, details_path=None):
    """打印 --verify 报告并返回退出码（0 全过 / 1 有问题）。"""
    print("\n=== 校验抓取结果文件 ===")
    print(f"列表: {list_path}")
    report = verify_results(list_path, details_path)
    print(f"详情: {report['detail_path'] or '未指定'}")
    print(f"列表 {report['list']['count']} 条 / 详情 {report['details']['count']} 条 "
          f"/ 覆盖率 {report['coverage']:.0%}")
    if not report["issues"]:
        print("✅ 校验通过：文件完整、字段齐全、详情覆盖列表")
    else:
        for i in report["issues"]:
            print(f"  ❌ {i}")
    print()
    return 0 if report["ok"] else 1



