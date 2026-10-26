# -*- coding: utf-8 -*-
"""详情通道统计与供给×额度协调（从 boss_cdp_raw.py 增量拆分）。

含：验证码全停记账（EXPORT_FAIL + 列表 warnings + 告警）、
详情通道计数/归类/metadata、供给×额度上限计算、空详情提示。

约束：boss 侧全局经 _core() 取用（调用栈优先）；os/json 直引。
"""

import json
import logging
import os

log = logging.getLogger("boss_cdp")


def _core():
    import sys as _sys
    # 1) 调用栈优先：编排链路经某 boss 副本进入时，栈上必有它的帧 → 用那一份。
    #    （多测试文件以不同名字加载 boss_cdp_raw.py 时 sys.modules 里有多个
    #    副本，candidates[-1] 会命中别人的模块，导致调用方的直接赋值/patch 失效。）
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
    # 2) 直接调用（tests 直接调重导出函数，栈上无 boss 帧）：
    #    取最近一次 exec 的 boss 副本（序号由 boss_cdp_raw 底部登记）。
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
    # 3) 兜底：按路径加载 boss（子模块被独立 import 时）。
    try:
        from . import boss_cdp_raw as _m
        return _m
    except Exception:
        if "boss_cdp_raw" in _sys.modules:
            return _sys.modules["boss_cdp_raw"]
        import boss_cdp_raw as _m
        return _m


def _note_detail_risk_blocked(list_output_path=None, city_name="", keyword=""):
    """验证码命中 → 全停。输出 EXPORT_FAIL risk_blocked（08 规格 §3.6 语义，
    规格侧判定信号）+ 列表文件 warnings 追加原因 + 告警推送。

    注：列表阶段成功已输出 EXPORT_OK（时序事实不可撤回）；详情阶段全停
    追加 EXPORT_FAIL 行，规格侧以 EXPORT_FAIL 为当日异常判定信号。
    """
    core = _core()
    fail_line = (f"EXPORT_FAIL reason=risk_blocked city={city_name} "
                 f"keyword={keyword} detail=验证码命中已全部停止")
    print("⚠️ 验证码命中，已全部停止（保留已抓数据）")
    print(fail_line)
    core.send_alert("验证码全停", fail_line)
    if not list_output_path or not os.path.exists(list_output_path):
        return
    try:
        with open(list_output_path, encoding="utf-8") as f:
            meta = json.load(f)
        warnings = meta.setdefault("warnings", [])
        if not any("detail_risk_blocked" in w for w in warnings):
            warnings.append(
                "detail_risk_blocked: 验证码命中，已全部停止（保留已抓数据）")
            core._atomic_write_export_json(list_output_path, meta)
    except (OSError, json.JSONDecodeError, ValueError):
        pass


_DETAIL_CHANNELS = ("api", "encrypt", "dom", "panel")


def _record_detail_channel(channel_stats, result):
    """只按已完成岗位的实际结果累计详情通道，不把失败尝试计入。"""
    if not isinstance(channel_stats, dict) or not isinstance(result, dict):
        return
    if not result.get("ok"):
        return
    channel = result.get("channel")
    if channel in _DETAIL_CHANNELS:
        channel_stats[channel] = channel_stats.get(channel, 0) + 1


def _classify_detail_channels(channel_stats):
    """把实际通道计数归类为单一通道或 mixed。"""
    active = [channel for channel in _DETAIL_CHANNELS
              if isinstance(channel_stats, dict)
              and channel_stats.get(channel, 0) > 0]
    if not active:
        return "unknown"
    return active[0] if len(active) == 1 else "mixed"


def _detail_channel_metadata(list_data, requested_channel, channel_stats,
                             stats_supplied=True):
    """生成实际通道 metadata；未传 stats 的旧调用方走兼容推断。"""
    if stats_supplied and channel_stats:
        counts = {
            channel: int(channel_stats.get(channel, 0))
            for channel in _DETAIL_CHANNELS
            if isinstance(channel_stats, dict)
            and type(channel_stats.get(channel, 0)) is int
            and channel_stats.get(channel, 0) > 0
        }
        if counts:
            return {
                "detail_channel": _classify_detail_channels(counts),
                "detail_channel_counts": counts,
            }

    jobs = list_data.get("jobs", []) if isinstance(list_data, dict) else []
    if not isinstance(jobs, list):
        jobs = []
    security_map = (list_data.get("security_map") or {}
                    if isinstance(list_data, dict) else {})
    with_sid = sum(1 for job in jobs
                   if isinstance(job, dict) and job.get("job_id") in security_map)
    if requested_channel in ("encrypt", "dom", "panel"):
        channel = requested_channel
    elif security_map and with_sid == len(jobs):
        channel = "api"
    elif with_sid == 0:
        channel = "encrypt" if requested_channel == "auto" else "dom"
    else:
        channel = "mixed"
    metadata = {"detail_channel": channel}
    if stats_supplied:
        metadata["detail_channel_counts"] = {}
    return metadata


def _check_supply_vs_budget(list_data, detail_budget=None):
    """比对「今天还没抓过的岗位数」与「今日剩余详情额度」，把**真实产出上限**算出来。

    为什么需要（2026-10-22 根因定位）：平台单个「关键词×城市」组合的搜索深度上限约
    10 页，且第 11 页起内容重复（实测请求 20 页 → "第 11 页与之前页内容重复（翻页失效）"
    → 239 条），单组合最多约 240-300 个岗位；日额度 700 装不下。此前**详情额度（按次数）
    与列表供给（按页数×组合）之间没有任何协调机制** → "额度没花完但没岗可抓"是结构性缺口。

    这里把缺口算出来并给出补法（多关键词/多城市由 --keyword/--city 逗号分隔内建支持，
    列表侧按 job_id 自动合并去重）。
    """
    core = _core()
    budget = core.DAILY_DETAIL_BUDGET if detail_budget is None else detail_budget
    jobs = (list_data or {}).get("jobs") or []
    state = core._daily_budget.read_state()
    ledger = set(state["jobs"])
    todo = [j for j in jobs
            if isinstance(j, dict) and j.get("job_id") not in ledger]
    if not budget:
        info = {"list_jobs": len(jobs), "todo_today": len(todo),
                "budget_remaining": None, "supply_gap": 0, "need_combos": 0,
                "ceiling": len(todo)}
        print(f"\n[供给 × 额度] 列表 {len(jobs)} 岗｜今天还没抓过 {len(todo)} 个｜未设额度上限")
        return info
    remaining = max(int(budget) - int(state["detail"]), 0)
    gap, need_combos = core.supply_gap(remaining, len(todo))
    ceiling = min(len(todo), remaining)
    print(f"\n[供给 × 额度] 列表 {len(jobs)} 岗｜今天还没抓过 {len(todo)} 个｜额度剩余 {remaining}")
    if gap > 0:
        print(f"  ⚠️ 供给比额度少 {gap} 个岗位 → **今天实际产出上限 {ceiling} 条**"
              f"（不是 {remaining} 条）")
        print(f"     按单组合 ≈{core._semantics.COMBO_CAPACITY} 岗算，还需 {need_combos} 个"
              "「关键词×城市」组合，例如：")
        print('     --keyword "AI产品经理,AIGC产品经理,大模型产品经理" --city "杭州"')
        print("     （列表侧按 job_id 自动合并去重；列表请求不占详情页额度）")
    else:
        print(f"  ✅ 供给充足（{len(todo)} ≥ {remaining}），可以把额度跑满")
    return {"list_jobs": len(jobs), "todo_today": len(todo),
            "budget_remaining": remaining, "supply_gap": gap,
            "need_combos": need_combos, "ceiling": ceiling}


def _print_empty_details_notice(output_path, done=0, skipped_today=0):
    """issue #154：0 条新增且无可续历史时，不写空详情文件，改为明确告警。

    空 [] 详情文件会把今天早些时候的真实详情**遮蔽成"最新"**（后续
    find_latest_detail_file 命中它），控制台还打印"详情已保存"，让人
    误以为有产出。改为：不落空文件 + 说清数据在哪、怎么续用。
    """
    print("\n⚠️ 详情 0 条新增且无可续历史 → 未写空详情文件"
          "（空文件会遮蔽早先的真实详情，还显得像有产出）。")
    if skipped_today:
        print(f"   其中 {skipped_today}/{done or skipped_today} 条「今天已抓过」跳过"
              "（不重复耗额度）：数据在早先的详情文件里，重跑时把 --detail-output "
              "指向该文件即可续用；或开 --db 跨 run 续抓。")
    else:
        print("   失败/跳过原因见上方统计；处理后重跑即可。")
