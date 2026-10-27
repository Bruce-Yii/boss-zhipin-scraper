"""列表/详情过滤入口（从 boss_cdp_raw.py 增量拆分）。"""

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
def prefilter_detail_queue(list_data, keep_junk=False):
    """详情前筛（用户 2026-10-23：筛完再爬，不浪费额度）。

    scrape_details 之前对列表 jobs 跑一遍删库规则。列表阶段无 JD →
    只走标题分支（+FDE 例外）；JD 疑似只预警。**直接改 list_data**
    （后面详情队列与合并都读同一份）；丢掉的逐条打印，不断链。
    默认开，``--keep-junk`` 跳过。返回 (kept, dropped)。
    """
    if not isinstance(list_data, dict):
        return 0, 0
    jobs = list_data.get("jobs")
    if not jobs:
        return 0, 0
    if keep_junk or _core()._semantics is None or not hasattr(_core()._semantics, "drop_junk_jobs"):
        return len(jobs), 0
    try:
        kept, dropped = _core()._semantics.drop_junk_jobs(jobs)
    except Exception as exc:  # pragma: no cover - 预筛失败不得阻塞抓取
        print(f"⚠️ 详情前筛异常（已跳过预筛）：{exc}")
        return len(jobs), 0
    items = dropped.get("items", [])
    if items:
        list_data["jobs"] = kept
        print(f"🧹 详情前筛：剔除 {len(items)} 条标题垃圾（不进详情、不耗额度）")
        for it in items[:8]:
            print(f"   - {it.get('title', '')[:24]}｜{it.get('company', '')[:16]}｜{it.get('reason', '')}")
        if len(items) > 8:
            print(f"   …等共 {len(items)} 条（见合并产物 dropped_junk）")
    return len(kept), len(items)


def apply_run_filters(list_data, args=None, keyword=None, label="相关度"):
    """**唯一的过滤入口**（独立审计 F2/F3/F11 的根因修法）。

    审计结论：过滤此前只在 main() 的一个分支里改了一份**内存副本** → 控制台说
    "丢弃 N 条"，落盘 JSON/CSV 仍是全量；``--merge`` 又会把被丢掉的岗位从旧文件
    复活并**再花详情额度**；``--input`` / ``--batch`` 两条入口干脆忽略参数。

    这里把过滤收敛到一处，并**直接改 list_data**（后面所有写盘点都读同一份）→
    控制台口径与落盘口径必然一致；调用点放在**合并之后**（旧数据也一起过滤）；
    所有入口（含 ``--input`` / ``--batch``）都走这里。

    Returns: (kept_count, dropped_count, dropped_detail)
    """
    if not isinstance(list_data, dict):
        return 0, 0, {}
    jobs = list_data.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        return 0, 0, {}

    def _opt(name, default=None):
        """选项读取：args 可以是 argparse 命名空间，也可以是 dict。"""
        if args is None:
            return default
        if isinstance(args, dict):
            return args.get(name, default)
        return getattr(args, name, default)

    def _as_list(value):
        """选项值可能是逗号分隔的字符串，也已经是列表 → 统一成列表。"""
        if value is None:
            return []
        if isinstance(value, (list, tuple, set)):
            return [str(v).strip() for v in value if str(v).strip()]
        return [part.strip() for part in str(value).split(",") if part.strip()]

    recruit_types = (_as_list(_opt("recruit_type", "")) or None)
    raw_allow = _opt("industry_allow", None)
    industry_allow = (_as_list(raw_allow) or None) if raw_allow is not None else None
    industry_block = (_as_list(_opt("industry_block", "")) or None)
    only_title = bool(_opt("only_relevant_title", False))

    if not (recruit_types or industry_allow or industry_block or only_title):
        return len(jobs), 0, {}

    before = len(jobs)
    kept, dropped = _core()._semantics.filter_by_relevance(
        jobs, recruit_types=recruit_types, industry_allow=industry_allow,
        industry_block=industry_block, only_relevant_title=only_title)
    list_data["jobs"] = kept
    list_data["total"] = len(kept)
    list_data["job_count"] = len(kept)
    dropped_total = before - len(kept)
    print(f"🔎 {label}过滤：{before} → {len(kept)} 岗（丢弃 {dropped_total}）")
    for key, label_text in (("recruit_type", "招聘类型不符"),
                             ("industry_block", "行业在排除名单"),
                             ("industry_allow", "行业不在白名单"),
                             ("title_relevance", "标题不是产品/AI 方向")):
        if dropped.get(key):
            print(f"   {label_text}：{dropped[key]} 条")
    if dropped.get("no_industry"):
        print(f"   ⚠️ 行业字段缺失（无法判定）：{dropped['no_industry']} 条（已保留）")
    for item in (dropped.get("items") or [])[:20]:
        print(f"     · 丢弃：{str(item.get('title'))[:22]}｜{str(item.get('company'))[:16]}"
              f"｜{item.get('reason')}")
    if len(dropped.get("items") or []) > 20:
        print(f"     … 另有 {len(dropped['items']) - 20} 条同类丢弃")
    if dropped_total:
        print("   → 这些岗不抓详情，**不消耗详情页额度**")
    return len(kept), dropped_total, dropped


def _filter_options(args):
    """把 CLI 的过滤参数收成一份 dict（供 scrape_list 与 apply_run_filters 共用）。

    同一处定义 → 各条入口（live 列表 / --input / --batch）行为必然一致
    （独立审计 F11：此前 --input 与 --batch 完全忽略这些参数且无任何提示）。
    """
    if args is None:
        return {}
    if isinstance(args, dict):
        return dict(args)
    return {
        "recruit_type": getattr(args, "recruit_type", None),
        "industry_allow": getattr(args, "industry_allow", None),
        "industry_block": getattr(args, "industry_block", None),
        "only_relevant_title": bool(getattr(args, "only_relevant_title", False)),
    }



