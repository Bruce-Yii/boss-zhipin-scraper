# -*- coding: utf-8 -*-
"""待重试详情清单（pending）与跨运行去重（从 boss_cdp_raw.py 增量拆分）。

pending 文件：`{详情输出}.pending.json`，[{job_id, attempts}]；
达到重试上限的 job 直接放弃，避免永久失败反复消耗请求。

约束：boss 侧全局（MAX_PENDING_RETRIES、_atomic_write_export_json、
pending_path_for）经 _core() 取用；os/json 直引。
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


def load_existing_detail_ids(output_path=None):
    """读取已有详情文件中的 job_id 集合，用于跨运行跳过已抓详情。

    Returns:
        set: 已有 job_id；文件不存在或损坏返回空集合。
    """
    if not output_path or not os.path.exists(output_path):
        return set()
    try:
        with open(output_path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError, ValueError):
        return set()
    items = data if isinstance(data, list) else []
    return {d.get("job_id", "") for d in items if isinstance(d, dict) and d.get("job_id")}


def pending_path_for(output_path):
    """待重试详情 job_id 清单文件路径（与输出文件同目录）。"""
    return f"{output_path}.pending.json"


def load_pending_ids(output_path, force_ids=None):
    """读取待重试详情 job_id → 已重试次数映射。

    兼容旧格式（纯字符串 job_id 列表，计数归零）；达到重试上限的
    job 直接放弃（不返回），避免永久失败的短 JD 反复消耗请求。

    Args:
        output_path: 详情输出路径（pending 文件与其同目录）
        force_ids: 用户强制重试白名单（iterable of str）；名单内 job_id
            无视重试次数上限，文件中未记录的也会加入返回结果

    Returns:
        dict: {job_id: attempts}
    """
    core = _core()
    force = {str(j).strip() for j in (force_ids or []) if str(j).strip()}
    path = core.pending_path_for(output_path)
    if not os.path.exists(path):
        return {jid: 0 for jid in force}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError, ValueError):
        return {jid: 0 for jid in force}
    if not isinstance(data, list):
        return {jid: 0 for jid in force}

    pending = {}
    for item in data:
        if isinstance(item, str):
            job_id, attempts = item.strip(), 0
        elif isinstance(item, dict):
            job_id = str(item.get("job_id") or "").strip()
            try:
                attempts = int(item.get("attempts") or 0)
            except (TypeError, ValueError):
                attempts = 0
        else:
            continue
        if job_id and (attempts < core.MAX_PENDING_RETRIES or job_id in force):
            pending[job_id] = attempts
    for jid in force:
        if jid not in pending:
            pending[jid] = 0
    return pending


def save_pending_ids(output_path, ids):
    """原子写回待重试详情（job_id → 重试次数）；空映射时删除文件。

    Args:
        ids: {job_id: attempts} 映射
    """
    core = _core()
    path = core.pending_path_for(output_path)
    if not ids:
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
        return
    core._atomic_write_export_json(path, [
        {"job_id": job_id, "attempts": attempts}
        for job_id, attempts in sorted(ids.items())
    ])
