"""详情页就绪等待与失败截图（从 boss_cdp_raw.py 增量拆分）。

含：JD 区渲染就绪等待（页面内 awaitPromise 优先，宿主轮询兜底）、
遮挡弹窗清理、失败截图目录与 CDP 截屏（best-effort）。

注意：DETAIL_READY_WAIT_JS / DETAIL_READY_JS / REMOVE_DIALOG_JS 等 JS
模板常量留在主文件（tests 直接读 `module.REMOVE_DIALOG_JS` 等）；
DEBUG_SHOT_ENABLED 由 CLI 经 `global` 赋值，运行时经 _core() 读取。

约束：timeout 默认用 None 哨兵（tests 会 patch boss.DETAIL_DOM_READY_TIMEOUT
后不传参调用；def 时烘焙会让补丁失效）。
"""

import base64
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


def _wait_for_detail_ready(ws, sid, timeout=None, poll=0.4):
    """等待详情页 JD 区渲染就绪（就绪即返回 True；超时返回 False）。

    DOM 详情提速核心：替代原来"导航后固定 sleep 5-10s"——绝大多数页面 1-3s 就绪。

    优先走**页面内等待**（awaitPromise，一次 CDP 往返）；CDP 不支持 awaitPromise
    或页面报错时回退到原来的宿主侧轮询（行为不变，只是多几次往返）。
    """
    core = _core()
    if timeout is None:
        timeout = core.DETAIL_DOM_READY_TIMEOUT
    try:
        js = core.DETAIL_READY_WAIT_JS.replace(
            "__LIMIT_MS__", str(int(timeout * 1000)))
        value = ws.eval_js_async(js, sid, timeout=timeout + 8)
        # 只认真正的布尔结果：非 bool（None / 异常包装 / 桩对象）一律回退轮询，
        # 避免把"拿不到答案"误判成"已就绪"。
        if isinstance(value, bool):
            return value
    except core._cdp_exception_types():
        core.log.debug("页面内就绪等待不可用，回退宿主侧轮询", exc_info=True)
    deadline = core.time.time() + timeout
    while core.time.time() < deadline:
        try:
            if ws.eval_js(core.DETAIL_READY_JS, sid):
                return True
        except core._cdp_exception_types():
            return False
        core.time.sleep(poll)
    return False


def _dismiss_dialogs(ws, sid):
    """移除可能遮挡 JD 的弹窗/遮罩（dialog-wrap/boss-layer/boss-popup，Snseam 手法）。"""
    core = _core()
    try:
        ws.eval_js(core.REMOVE_DIALOG_JS, sid)
    except core._cdp_exception_types():
        core.log.debug("dialog 清理失败", exc_info=True)


def debug_screenshot_dir():
    """失败截图目录（`BOSS_DEBUG_DIR` 可覆盖，供测试/多环境）；仓库外。"""
    core = _core()
    path = core.os.environ.get(core.DEBUG_DIR_ENV) or core.DEFAULT_DEBUG_DIR
    try:
        core.os.makedirs(path, exist_ok=True)
    except OSError:
        pass
    return path


def capture_debug_screenshot(ws, sid, tag):
    """CDP 截屏存本地（排障用，best-effort，绝不抛异常）。

    仅当 `DEBUG_SHOT_ENABLED` 为真（CLI `--debug-screenshots`）时执行。
    返回保存路径或 None。
    """
    core = _core()
    if not core.DEBUG_SHOT_ENABLED or ws is None:
        return None
    try:
        r = ws.send("Page.captureScreenshot", {"format": "png"}, sid)
        data = (r.get("result") or {}).get("data")
        if not data:
            return None
        name = f"err_{tag}_{core.datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        path = os.path.join(core.debug_screenshot_dir(), name)
        with open(path, "wb") as f:
            f.write(base64.b64decode(data))
        print(f"  📷 已保存失败截图: {path}")
        return path
    except (OSError, ValueError, TypeError, *core._cdp_exception_types()):
        core.log.debug("失败截图保存失败", exc_info=True)
        return None
