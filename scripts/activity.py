"""岗位活跃度判定与导出过滤（从 boss_cdp_raw.py 增量拆分）。"""

import logging
import os
import re

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
def map_list_boss_active_status(job):
    """Map list-API job fields to ``boss_active_status``.

    BOSS ``/wapi/zpgeek/search/joblist.json`` typically exposes ``bossOnline``
    but not ``activeTimeDesc``. Prefer ``activeTimeDesc`` when present;
    otherwise map ``bossOnline=True`` to 「在线」. Detailed labels such as
    「刚刚活跃」still come from the detail path.
    """
    if not isinstance(job, dict):
        return ""
    desc = str(job.get("activeTimeDesc") or "").strip()
    if desc:
        return desc
    if job.get("bossOnline"):
        return "在线"
    return ""


def resolve_boss_active_status(list_status="", detail_status=""):
    """Prefer detail activity text; fall back to list mapping result."""
    detail = str(detail_status or "").strip()
    if detail:
        return detail
    return str(list_status or "").strip()


# 僵尸岗信号：HR「周/月/年前活跃」（实测标签有 半年前活跃 等）。
# 保守规则——**要求出现"前活跃"**，避免误杀实测的近期标签「本周活跃」「2周内活跃」
# （2026-09-22 本地详情聚合：本周活跃 8、2周内活跃 5 均属近期活跃；照抄 [周月年] 会误杀）。
_ZOMBIE_ACTIVITY_RE = re.compile(r"(?:\d+\s*)?(?:个?月|周|年)\s*前活跃")


def is_inactive_activity(status):
    """True 当 HR 活跃度显示为"周/月/年前活跃"（僵尸岗信号）。"""
    return bool(_ZOMBIE_ACTIVITY_RE.search(str(status or "")))


def inactive_job_ids(details=None, jobs=None):
    """汇总 job_id → HR 活跃度（详情优先、列表兜底），返回僵尸岗 job_id 集合。"""
    status = {}
    for rec in jobs or []:
        if isinstance(rec, dict) and rec.get("job_id"):
            status[str(rec["job_id"])] = rec.get("boss_active_status", "")
    for rec in details or []:
        if isinstance(rec, dict) and rec.get("job_id"):
            status[str(rec["job_id"])] = rec.get("boss_active_status", "")
    return {jid for jid, st in status.items() if is_inactive_activity(st)}


def filter_inactive_in_export(path, inactive_ids):
    """从导出的列表文件中剔除给定 job_id（--filter-inactive）；返回 (kept, dropped)。

    在口径一（jd 并入）之后对最终导出生效，保证下游拿到的是过滤后的岗位集；
    meta 记 `inactive_filtered` 与 warning 便于追溯。
    """
    inactive_ids = {str(j) for j in (inactive_ids or set())}
    if not inactive_ids or not path or not os.path.exists(path):
        return 0, 0
    try:
        with open(path, encoding="utf-8") as f:
            data = _core().json.load(f)
    except (OSError, _core().json.JSONDecodeError, ValueError):
        return 0, 0
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        return 0, 0
    kept, dropped = [], 0
    for job in data["jobs"]:
        if isinstance(job, dict) and str(job.get("job_id") or "") in inactive_ids:
            dropped += 1
        else:
            kept.append(job)
    if dropped:
        data["jobs"] = kept
        data["job_count"] = len(kept)
        data["total"] = len(kept)
        data["inactive_filtered"] = dropped
        warnings = list(data.get("warnings") or [])
        warnings.append(f"--filter-inactive：已剔除 {dropped} 条 HR 长期未活跃岗位")
        data["warnings"] = warnings
        _core()._atomic_write_export_json(path, data)
    return len(kept), dropped



