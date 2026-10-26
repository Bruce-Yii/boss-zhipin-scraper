# -*- coding: utf-8 -*-
"""--batch 任务配置解析（从 boss_cdp_raw.py 增量拆分）。

纯函数：JSON 数组 → (tasks, errors)，非法任务跳过不中断。

约束：DEFAULT_CITY_INPUT / MAX_PAGES / FILTER_KEYS 经 _core() 取用。
"""

import json
import logging

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


def load_batch_config(path):
    """加载 --batch 任务配置（JSON 数组）；返回 (tasks, errors)。

    每个任务支持字段：keyword(必填)、city(默认上海)、pages(默认 3，
    自动限制在 1..MAX_PAGES)、sleep(任务间等待秒数，缺省随机 30-60)、
    detail(可选布尔，覆盖全局"默认抓完整"设置；false = 仅列表)、
    scale/stage/salary/experience/degree/industry（筛选）。非法任务
    跳过并记录错误，不中断其余任务。

    Args:
        path: 配置文件路径

    Returns:
        (list, list): 合法任务列表与错误信息列表
    """
    core = _core()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError) as e:
        return [], [f"配置文件无法读取: {e}"]
    if not isinstance(data, list):
        return [], ["配置必须是 JSON 数组（每个元素一个任务）"]

    tasks = []
    errors = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            errors.append(f"任务 {i + 1}: 不是对象，已跳过")
            continue
        keyword = str(item.get("keyword") or "").strip()
        if not keyword:
            errors.append(f"任务 {i + 1}: 缺少 keyword，已跳过")
            continue
        try:
            pages = int(item.get("pages") or 3)
        except (TypeError, ValueError):
            errors.append(f"任务 {i + 1}: pages 必须是整数，已跳过")
            continue
        task = {
            "keyword": keyword,
            "city": str(item.get("city") or core.DEFAULT_CITY_INPUT).strip(),
            "pages": min(max(pages, 1), core.MAX_PAGES),
        }
        if item.get("sleep") is not None:
            try:
                task["sleep"] = max(float(item["sleep"]), 0)
            except (TypeError, ValueError):
                errors.append(f"任务 {i + 1}: sleep 必须是数字，已忽略该字段")
        if item.get("detail") is not None:
            task["detail"] = bool(item["detail"])
        for key in core.FILTER_KEYS:
            if item.get(key):
                task[key] = item[key]
        tasks.append(task)
    return tasks, errors
