# -*- coding: utf-8 -*-
"""残留 tab 清扫与详情提取重试（从 boss_cdp_raw.py 增量拆分）。

含：停靠搜索页 tab 清扫、详情页 tab 清扫、详情 JS 提取重试、
列表→详情路径推导、列表文件详情风控降级标记检查。

约束：boss 侧全局经 _core() 取用（调用栈优先）；time/random 走 _core()；
EXTRACT_DETAIL_JS 等 JS 模板常量留在主文件（tests 直接读取）。
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


def close_orphan_dock_tabs(cdp_port=_core().DEFAULT_CDP_PORT, keep=2, threshold=8):
    """清扫残留的"停靠搜索页"tab（崩溃/异常退出遗留），防越积越多。

    只在**明显泄漏**（同页 target 数 > threshold）时才动手，并保留最新 keep 个，
    避免误关用户正常使用的页；只关 URL 匹配本工具搜索页
    （``/web/geek/jobs?query=``）的 page target。CDP 不可达/失败静默返回 0。
    """
    import urllib.request
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{cdp_port}/json/list", timeout=5) as resp:
            targets = json.load(resp)
    except (OSError, ValueError, json.JSONDecodeError):
        return 0
    docks = [t for t in targets
             if t.get("type") == "page"
             and "/web/geek/jobs?query=" in (t.get("url") or "")]
    if len(docks) <= threshold:
        return 0
    closed = 0
    for t in docks[keep:]:
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{cdp_port}/json/close/{t.get('id')}",
                timeout=5).read()
            closed += 1
        except (OSError, ValueError):
            pass
    return closed


def close_orphan_detail_tabs(cdp_port=_core().DEFAULT_CDP_PORT):
    """清扫残留的详情页 tab（#63：进程被杀时 finally 未执行，详情 tab 堆积）。

    正常结束的 run 不留详情 tab，故启动时见到的**全部是残留**，直接全关
    （与停靠页 keep 策略不同）；只关 URL 含 ``/job_detail/`` 的 page target，
    搜索页/新标签页/验证页（``_security_check``，用户可能正在手滑）不动。
    CDP 不可达/失败静默返回 0。
    """
    import urllib.request
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{cdp_port}/json/list", timeout=5) as resp:
            targets = json.load(resp)
    except (OSError, ValueError, json.JSONDecodeError):
        return 0
    orphans = [t for t in targets
               if t.get("type") == "page"
               and "/job_detail/" in (t.get("url") or "")
               and "_security_check" not in (t.get("url") or "")]
    closed = 0
    for t in orphans:
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{cdp_port}/json/close/{t.get('id')}",
                timeout=5).read()
            closed += 1
        except (OSError, ValueError):
            pass
    return closed


def eval_detail_with_retry(ws, sid, detail_url, retries=1):
    """提取详情页 JS；完全为空时刷新页面重试（凭证自愈）。

    Returns:
        解析后的 dict（{jd, page_text, tags, url}）；全部失败返回 {"jd": "", "tags": []}。
    """
    core = _core()

    def _extract_once():
        val = ws.eval_js(core.EXTRACT_DETAIL_JS, sid)
        try:
            return json.loads(val) if isinstance(val, str) else {"jd": "", "tags": []}
        except (json.JSONDecodeError, ValueError, TypeError):
            return {"jd": "", "tags": []}

    d = _extract_once()
    for _ in range(max(retries, 0)):
        if d.get("jd") or d.get("page_text"):
            break
        print("  ⚠️ 提取为空，刷新页面重试一次...")
        ws.send("Page.navigate", {"url": detail_url}, sid)
        core.time.sleep(core.random.uniform(2, 4))
        d = _extract_once()
    return d


def _detail_path_for_list(list_path):
    """由列表文件路径推导详情文件路径（同名 boss_details_*）；无法推导返回 None。"""
    if not list_path:
        return None
    base = os.path.basename(list_path)
    if not base.startswith("boss_jobs_"):
        return None
    return os.path.join(os.path.dirname(list_path),
                        "boss_details_" + base[len("boss_jobs_"):])


def _list_has_detail_risk(list_path):
    """列表文件 warnings 是否含详情风控降级标记（detail_risk_blocked）。"""
    try:
        with open(list_path, encoding="utf-8") as f:
            meta = json.load(f)
        return any("detail_risk_blocked" in str(w)
                   for w in (meta.get("warnings") or []))
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return False
