"""列表阶段纯辅助函数（从 boss_cdp_raw.py 增量拆分）。"""

import logging

log = logging.getLogger("boss_cdp")

try:
    from . import export_contract as _ec
except Exception:
    import export_contract as _ec

merge_unique = _ec.merge_unique

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
def split_keywords(raw):
    """把 ``--keyword`` 拆成关键词列表（逗号分隔；去空白、去空项、保序去重）。

    单关键词返回 ``[raw]``；全空返回 ``[]``。
    """
    seen = set()
    out = []
    for part in str(raw or "").split(","):
        key = part.strip()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def merge_list_data(base, incoming):
    """按 job_id 合并两次列表抓取结果（并集，先到优先），并合并 security_map。

    多关键词（§4.1）依次抓取后调用；``keyword`` 字段以逗号连接作为统一标签。
    """
    if not isinstance(incoming, dict):
        return base
    if base is None:
        return incoming
    merged_jobs = merge_unique(base.get("jobs") or [], incoming.get("jobs") or [])
    merged_map = dict(base.get("security_map") or {})
    merged_map.update(incoming.get("security_map") or {})
    base["jobs"] = merged_jobs
    base["total"] = len(merged_jobs)
    base["security_map"] = merged_map
    labels = [x for x in (base.get("keyword", ""), incoming.get("keyword", "")) if x]
    base["keyword"] = ",".join(labels)
    return base


def page_fingerprint(jobs):
    """整页 job 指纹（job_link/标题 的 frozenset）——用于"翻页失效=内容重复"判停。"""
    keys = set()
    for j in jobs or []:
        if isinstance(j, dict):
            key = j.get("job_link") or j.get("title") or ""
            if key:
                keys.add(key)
    return frozenset(keys)


