# -*- coding: utf-8 -*-
"""进度/耗时辅助函数（从 boss_cdp_raw.py 增量拆分）。"""

import math
import threading
import logging

log = logging.getLogger("boss_cdp")

_DOM_STAGE_LOCK = threading.Lock()
_DOM_STAGE_SECONDS = {}

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

def progress_step(total):
    """阶段进度汇报粒度：总量 ≤200 条每 10 条一报，超 200 后按 5%（取整）。

    Args:
        total: 待处理总数

    Returns:
        int: 汇报间隔（条数，至少 10）
    """
    return max(10, int(math.ceil(total * 0.05)))


def resume_hint(pending, output_path):
    """生成断点续抓提示；pending 为空返回空串。

    详情输出路径不变时，pending 文件会被自动加载，重跑原命令即自动
    跳过已抓、只补失败详情，无需拼写任何参数。

    Args:
        pending: {job_id: attempts} 待重试映射
        output_path: 详情输出路径

    Returns:
        str: 提示文本（可能为空串）
    """
    if not pending:
        return ""
    path = _core().pending_path_for(output_path)
    return (f"ℹ️  {len(pending)} 个详情待重试（已记录到 {path}）。"
            f"续抓：重跑刚才的命令即可——输出路径不变时自动跳过已抓、"
            f"只补这 {len(pending)} 条")


def _format_elapsed(seconds):
    """耗时格式化：≥60 秒显示"X 分 Y 秒"，否则"Y 秒"。"""
    seconds = int(seconds)
    if seconds >= 60:
        return f"{seconds // 60} 分 {seconds % 60} 秒"
    return f"{seconds} 秒"


def run_summary(elapsed_sec, total, ok_count, reason_counts):
    """生成详情抓取结束统计行；无任务返回空串。

    Args:
        elapsed_sec: 已耗时（秒）
        total: 处理总数
        ok_count: 成功数
        reason_counts: 失败原因分类 {reason: count}（不含成功）

    Returns:
        str: 统计行（可能为空串）
    """
    if total <= 0:
        return ""
    failed = total - ok_count
    reason_txt = "，".join(
        f"{k}:{v}" for k, v in sorted(reason_counts.items())) if reason_counts else "—"
    rate = math.ceil(elapsed_sec / total)
    line = (f"  ✅ 完成 {total} 条：成功 {ok_count}，失败 {failed}（{reason_txt}）"
            f"| 耗时 {_format_elapsed(elapsed_sec)}，平均 {rate}s/条")
    # 平台漂移信号：列表正常但详情大面积 CDP 会话失败 → 提示检查页面结构/风控，
    # 不要在同一轮里反复重试（平台侧结构性变化，重试纯浪费）
    cdp_fails = reason_counts.get("cdp_session", 0)
    if failed >= 5 and cdp_fails / max(failed, 1) > 0.5:
        line += ("\n  ⚠️ 平台漂移信号：详情大面积 CDP 会话失败（列表正常但详情异常），"
                 "建议检查页面结构/风控状态后再重试详情")
    return line


def progress_line(completed, total, ok_count):
    """生成阶段进度汇总行；未到汇报点返回 None（避免刷屏）。

    每 progress_step(total) 条汇报一次，完成时（completed == total）
    即使不整除也汇报，保证任务结束有最终汇总。

    Args:
        completed: 已处理条数
        total: 总数
        ok_count: 成功条数

    Returns:
        str|None: 汇总行文本
    """
    if total <= 0:
        return None
    if completed % progress_step(total) != 0 and completed != total:
        return None
    pct = completed / total * 100
    return f"  [进度 {completed}/{total} {pct:.0f}%] 成功 {ok_count}，失败 {completed - ok_count}"


# TokenBucket / AdaptiveRateLimiter 已抽出到 scripts/ratelimit.py，
# 由文件顶部导入兼容层 re-export（见 P1 架构重构 2026-09-22）。


def _dom_stage(name, seconds):
    """累计一个 DOM 阶段的耗时（--detail-timing 开时生效）。"""
    if not _core().DOM_TIMING_ENABLED:
        return
    with _DOM_STAGE_LOCK:
        acc = _DOM_STAGE_SECONDS.setdefault(name, {"n": 0, "total": 0.0})
        acc["n"] += 1
        acc["total"] += seconds


def _dom_stage_reset():
    with _DOM_STAGE_LOCK:
        _DOM_STAGE_SECONDS.clear()


def print_dom_stage_timings(count):
    """打印 DOM 阶段耗时分解（供提速定位）。"""
    with _DOM_STAGE_LOCK:
        stages = dict(_DOM_STAGE_SECONDS)
    if not stages:
        return
    print(f"\n⏱️  DOM 阶段耗时分解（{count} 岗）：")
    total = sum(s["total"] for s in stages.values())
    for name, s in sorted(stages.items(), key=lambda kv: -kv[1]["total"]):
        avg = s["total"] / s["n"] if s["n"] else 0.0
        share = (s["total"] / total * 100) if total else 0.0
        print(f"   {name:<12} 合计 {s['total']:7.1f}s  均 {avg:5.2f}s/岗  占比 {share:4.1f}%")


