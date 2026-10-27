# -*- coding: utf-8 -*-
"""跑后战报（从 boss_cdp_raw.py 增量拆分计划内新模块）。

每次抓取结束只看这一张表：关键词/城市/页数、列表→删库→详情→导出的
整条漏斗、新增 JD 数、失败原因、产物路径、预算余额。数据全部来自
落盘产物（列表 meta、详情文件、failures 文件、日预算），不猜。

CLI: ``--report [LIST_FILE]``（缺省取最新列表）。
"""

import glob
import json
import logging
import os

log = logging.getLogger("boss_cdp")


def _core():
    import sys as _sys
    try:
        _frame = _sys._getframe(1)
    except ValueError:
        _frame = None
    _depth = 0
    while _frame is not None and _depth < 64:
        _depth += 1
        try:
            _g = _frame.f_globals
            _fpath = _g.get("__file__", "") if isinstance(_g, dict) else ""
        except Exception:
            _fpath = ""
        if _fpath and str(_fpath).endswith("boss_cdp_raw.py"):
            for _m in list(_sys.modules.values()):
                try:
                    if getattr(_m, "__dict__", None) is _g:
                        return _m
                except Exception:
                    continue
            break
        try:
            _frame = _frame.f_back
        except Exception:
            break
    _candidates = [m for m in _sys.modules.values()
                   if getattr(m, "__file__", None)
                   and str(m.__file__).endswith("boss_cdp_raw.py")]
    if _candidates:
        _best = _candidates[-1]
        try:
            _best_seq = getattr(_best, "_BOSS_EXEC_SEQ", -1)
            for _m in _candidates[:-1]:
                _seq = getattr(_m, "_BOSS_EXEC_SEQ", -1)
                if _seq is not None and _best_seq is not None and _seq > _best_seq:
                    _best, _best_seq = _m, _seq
        except Exception:
            pass
        return _best
    try:
        from . import boss_cdp_raw as _m
        return _m
    except Exception:
        if "boss_cdp_raw" in _sys.modules:
            return _sys.modules["boss_cdp_raw"]
        import boss_cdp_raw as _m
        return _m


def _load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _fmt_duration(seconds):
    try:
        seconds = int(seconds or 0)
    except (TypeError, ValueError):
        return "--"
    if seconds < 60:
        return f"{seconds} 秒"
    return f"{seconds // 60} 分 {seconds % 60} 秒"


def collect_report(list_path=None, result_dir=None):
    """从落盘产物组装战报数据；缺什么就空什么，不抛异常。"""
    core = _core()
    result_dir = result_dir or core.DEFAULT_RESULT_DIR
    if list_path is None:
        files = sorted(
            p for p in glob.glob(os.path.join(result_dir, "boss_jobs_*.json"))
            if os.path.isfile(p))
        list_path = files[-1] if files else None
    report = {"list_path": list_path or "", "ok": False}
    if not list_path or not os.path.exists(list_path):
        report["error"] = "找不到列表文件"
        return report
    meta = _load_json(list_path)
    jobs = meta.get("jobs") if isinstance(meta.get("jobs"), list) else []
    report.update({
        "ok": True,
        "keyword": meta.get("keyword", ""),
        "city": meta.get("city", ""),
        "list_jobs": len(jobs),
        "record_counts": meta.get("record_counts") or {},
        "warnings": meta.get("warnings") or [],
        "started_at": meta.get("started_at", ""),
        "ended_at": meta.get("ended_at", meta.get("scraped_at", "")),
    })
    # 详情文件：同时间戳优先（与 load_existing_details 同口径），
    # 但排除 failures/pending 这类 sidecar（它们也以前缀开头）。
    detail_path = core._latest_details_path(list_path)
    if detail_path and detail_path.endswith((".failures.json", ".pending.json")):
        detail_path = None
    if detail_path is None:
        base = os.path.dirname(list_path) or "."
        cands = []
        try:
            for name in os.listdir(base):
                if (name.startswith("boss_details_") and name.endswith(".json")
                        and not name.endswith(".pending.json")
                        and not name.endswith(".failures.json")):
                    path = os.path.join(base, name)
                    cands.append((os.path.getmtime(path), path))
        except OSError:
            cands = []
        detail_path = max(cands)[1] if cands else None
    details, failures = [], []
    if detail_path and os.path.exists(detail_path):
        try:
            with open(detail_path, encoding="utf-8") as f:
                data = json.load(f)
            details = data if isinstance(data, list) else []
        except (OSError, ValueError):
            details = []
    report["detail_path"] = detail_path or ""
    report["detail_count"] = len(details)
    report["detail_with_jd"] = sum(
        1 for r in details
        if isinstance(r, dict) and str(r.get("jd") or "").strip())
    # 失败明细（failures sidecar）
    if detail_path:
        fpath = detail_path + ".failures.json"
        if os.path.exists(fpath):
            try:
                with open(fpath, encoding="utf-8") as f:
                    data = json.load(f)
                failures = data if isinstance(data, list) else []
            except (OSError, ValueError):
                failures = []
    report["failures_path"] = (
        detail_path + ".failures.json" if detail_path else "")
    report["failures"] = failures
    # 日预算余额
    try:
        state = core._daily_budget.read_state()
        limit = core.DAILY_DETAIL_BUDGET or 0
        used = int(state.get("detail") or 0)
        report["budget"] = {"used": used, "limit": limit,
                            "left": max(limit - used, 0) if limit else None}
    except Exception:
        report["budget"] = {}
    return report


def render_report(report):
    """战报可视化：一张表，结论先行。"""
    if not report.get("ok"):
        return f"无战报：{report.get('error', '未知错误')}"
    lines = []
    bar = "━" * 46
    lines.append(bar)
    kw = str(report.get("keyword") or "?")
    if len(kw) > 30:
        kw = kw[:30] + "…"
    lines.append(f" 抓取战报 · {report.get('city', '?')} · {kw}")
    rc = report.get("record_counts") or {}
    parts = []
    if rc.get("new") is not None:
        parts.append(f"新增{rc.get('new')}")
    if rc.get("duplicate"):
        parts.append(f"去重{rc.get('duplicate')}")
    list_line = f" 列表 {report.get('list_jobs', 0)} 条"
    if parts:
        list_line += "（" + "｜".join(parts) + "）"
    lines.append(list_line)
    lines.append(f" 详情 {report.get('detail_count', 0)} 条"
                 f"（有JD {report.get('detail_with_jd', 0)}）")
    failures = report.get("failures") or []
    if failures:
        from collections import Counter
        reasons = Counter(
            str(f.get("reason") or "?") for f in failures if isinstance(f, dict))
        lines.append(" 失败 {} 条（{}）".format(
            len(failures),
            "、".join(f"{k}×{v}" for k, v in reasons.most_common(3))))
    else:
        lines.append(" 失败 0 条")
    budget = report.get("budget") or {}
    if budget.get("limit"):
        lines.append(f" 预算 今日已用 {budget['used']}/{budget['limit']}"
                     f"（剩 {budget['left']}）")
    if report.get("detail_path"):
        lines.append(f" 产物 {os.path.basename(report['detail_path'])}")
    for w in (report.get("warnings") or [])[:3]:
        lines.append(f" ⚠ {str(w)[:60]}")
    lines.append(bar)
    return "\n".join(lines)


def run_report(list_path=None, result_dir=None):
    """打印 --report 战报并返回退出码。"""
    report = collect_report(list_path, result_dir)
    print()
    print(render_report(report))
    print()
    return 0
