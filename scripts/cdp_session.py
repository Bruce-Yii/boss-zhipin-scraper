# -*- coding: utf-8 -*-
"""CDP 会话层（从 boss_cdp_raw.py 增量拆分）。

含：TargetCrashedError、CDPSession（WebSocket 连 CDP、心跳探活、事件缓冲）、
页面会话 helper（create_page_session，后台打开 + visibility override 防反爬）、
孤儿标签页登记/清理（record_opened_target / reap_orphan_targets）、
CDP 可预期异常解析（_cdp_exception_types / _ws_timeout_exception）。

 wreck 约束：全部运行时全局经 _core() 动态取自主模块，
 tests 对 boss_cdp_raw 的 patch（CDPSession / create_page_session /
 FOREGROUND_CAPTURE / OPEN_TARGETS_LOG）保持有效。
"""

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
    #    取最近一次 exec 的 boss 副本（setUp 现加载现测，必是调用方的那份；
    #    序号由 boss_cdp_raw 底部 _note_boss_exec() 登记）。
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


_BOSS_EXEC_SEQ_STATE = {"n": 0}


def _note_boss_exec():
    """boss_cdp_raw 每次 exec（import）登记一次，返回递增序号。

    多副本并存时子模块 _core() 兜底取序号最大的那份（最近 exec）。
    """
    _BOSS_EXEC_SEQ_STATE["n"] += 1
    return _BOSS_EXEC_SEQ_STATE["n"]


class TargetCrashedError(Exception):
    """CDP target 崩溃/会话 detached（如渲染进程 OOM）——立即失败，避免 evaluate 永久挂起。"""


class CDPSession:
    def __init__(self, cdp_port=_core().DEFAULT_CDP_PORT, heartbeat_interval=30):
        core = _core()
        if not core.require_runtime_dependencies("requests", "websocket"):
            raise RuntimeError("缺少 CDP 运行依赖")
        self.cdp_port = cdp_port
        resp = core.requests.get(f"http://127.0.0.1:{cdp_port}/json/version", timeout=10)
        ws_url = resp.json()["webSocketDebuggerUrl"]
        self.ws = core.websocket.create_connection(ws_url, timeout=60)
        self.mid = 0
        # CDP 事件缓冲：send() 等待命令响应期间到达的事件通知都会存这里，
        # 供 Network 域被动捕获使用（见 NetworkJoblistCapture）。
        self.events = []
        self._dead = False
        if heartbeat_interval and heartbeat_interval > 0:
            self._start_heartbeat(heartbeat_interval)

    def _start_heartbeat(self, interval):
        """后台线程 HTTP 探活：NAT/代理静默断连时尽早标记 _dead（防僵尸连接）。

        CDP 无内置保活，空闲连接可能被静默掐断且收不到任何事件；
        用同端口 /json/version HTTP 探活（不干扰 ws 消息循环），
        连续失败即标记连接死亡，send 快速失败而非挂起 30s 超时。
        """
        import threading

        def _beat():
            core = _core()
            while not self._dead:
                try:
                    requests_mod = core.requests
                    if requests_mod is None:
                        return
                    requests_mod.get(
                        f"http://127.0.0.1:{self.cdp_port}/json/version",
                        timeout=5,
                    )
                except OSError:
                    # requests.RequestException 继承 IOError(=OSError)；网络层
                    # 失败即判死。用 OSError 而不用 requests_mod.RequestException
                    # ——后者在 requests 未初始化时是 None.属性访问，直接崩线程
                    # （2026-10-27 实测 traceback）。
                    self._dead = True
                    try:
                        self.ws.close()
                    except Exception:
                        pass
                    return
                core.time.sleep(interval)

        threading.Thread(target=_beat, daemon=True,
                         name="cdp-heartbeat").start()

    def send(self, method, params=None, sid=None, timeout=30):
        """发送 CDP 命令并等待匹配的响应。

        Args:
            method: CDP 方法名
            params: 参数字典
            sid: Target session ID
            timeout: 等待响应的超时秒数，默认 30s

        Returns:
            CDP 响应字典

        Raises:
            ConnectionError: 连接已死亡/WebSocket 异常断开（快速失败，不挂起）
            TargetCrashedError: 渲染进程崩溃/会话 detached（Inspector.detached/targetCrashed）
            TimeoutError: 超过 timeout 仍未收到匹配响应
        """
        core = _core()
        if self._dead:
            raise ConnectionError(
                f"CDP 连接已断开（心跳探活失败），method={method}"
            )
        self.mid += 1
        msg = {"id": self.mid, "method": method, "params": params or {}}
        if sid:
            msg["sessionId"] = sid
        self.ws.send(core.json.dumps(msg))

        start_time = core.time.time()
        max_retries = 1000

        for attempt in range(max_retries):
            # 检查超时
            elapsed = core.time.time() - start_time
            if elapsed > timeout:
                raise TimeoutError(
                    f"CDP send({method}) 超时 ({timeout}s), "
                    f"已跳过 {attempt} 条不匹配消息"
                )

            try:
                raw = self.ws.recv()
            except core.websocket.WebSocketTimeoutException:
                raise TimeoutError(
                    f"CDP WebSocket recv 超时, method={method}") from None
            except core.websocket.WebSocketException:
                # 连接被对端关闭（NAT 掐断/Chrome 退出）——快速失败，不等到超时
                self._dead = True
                raise ConnectionError(
                    f"CDP WebSocket 连接异常断开, method={method}"
                ) from None

            try:
                r = core.json.loads(raw)
            except (core.json.JSONDecodeError, ValueError):
                core.log.debug(f"跳过非 JSON 消息: {raw[:100]}")
                continue

            if r.get("method") in ("Inspector.detached", "Target.targetCrashed"):
                # OOM 时只发 Inspector.detached（reason="Render process gone."）且
                # pending evaluate 会永久挂起（社区已知 bug）——必须立即失败
                raise TargetCrashedError(
                    f"CDP target 崩溃/会话断开: {r.get('method')} "
                    f"{core.json.dumps(r.get('params', {}), ensure_ascii=False)[:200]}"
                )

            if r.get("id") == self.mid:
                return r

            # 不匹配的消息：事件通知，存入缓冲供被动捕获，避免丢失
            event_name = r.get("method", "unknown")
            core.log.debug(f"缓冲事件消息 (id={r.get('id')}, event={event_name})")
            self.events.append(r)

        raise TimeoutError(
            f"CDP send({method}) 在 {max_retries} 条消息内未找到匹配响应"
        )

    def drain_events(self, duration):
        """在 duration 秒内持续接收并缓冲 CDP 事件，超时或期间无消息则返回。

        用于等待页面自身发起的请求完成（Network 域事件），不发送任何命令。
        """
        core = _core()
        deadline = core.time.time() + duration
        timeout_exc = core._ws_timeout_exception()
        try:
            while True:
                remaining = deadline - core.time.time()
                if remaining <= 0:
                    return
                self.ws.settimeout(min(0.5, remaining))
                try:
                    raw = self.ws.recv()
                except timeout_exc:
                    continue
                except core._cdp_exception_types():
                    # 连接异常：标记死亡并返回（best-effort，不抛）
                    self._dead = True
                    return
                try:
                    r = core.json.loads(raw)
                except (core.json.JSONDecodeError, ValueError):
                    continue
                if "method" in r:
                    self.events.append(r)
        finally:
            # 恢复默认超时，避免影响后续 send() 的等待
            self.ws.settimeout(60)

    def eval_js(self, js, sid):
        r = self.send("Runtime.evaluate", {"expression": js, "returnByValue": True}, sid)
        return r.get("result", {}).get("result", {}).get("value", None)

    def eval_js_async(self, js, sid, timeout=20):
        """在页面内 await 一个 Promise，**一次 CDP 往返**取回结果。

        用于「等 DOM 就绪」这类轮询：页内自己等，CDP 侧只发一次命令，
        省掉每轮一次往返（就绪 3s / 轮询 0.4s ≈ 8 次往返）。
        """
        r = self.send(
            "Runtime.evaluate",
            {"expression": js, "returnByValue": True, "awaitPromise": True},
            sid, timeout=timeout)
        return r.get("result", {}).get("result", {}).get("value", None)

    def close(self):
        self._dead = True
        try:
            self.ws.close()
        except _core().websocket.WebSocketException:
            pass


BACKGROUND_VISIBILITY_SCRIPT = (
    "Object.defineProperty(document, 'hidden', {get: () => false});"
    "Object.defineProperty(document, 'visibilityState', {get: () => 'visible'});"
    "Object.defineProperty(document, 'webkitHidden', {get: () => false});"
    "Object.defineProperty(document, 'webkitVisibilityState', {get: () => 'visible'});"
)


# --foreground-capture（§1.3）：Chrome 151 在 Linux/Xvfb 下后台 Target 可能捕获不到
# 搜索响应（上游 #67/#68）。默认保持后台（避免抢焦点、避免 document.hidden 触发
# BOSS visibility 反爬，issue #18）；该开关为问题环境提供逃生口。
FOREGROUND_CAPTURE = False


def page_background_default():
    """自动化页面默认后台；`--foreground-capture` 打开时改为前台（逃生口）。"""
    return not _core().FOREGROUND_CAPTURE


try:
    OPEN_TARGETS_LOG = os.path.join(_core().STATE_DIR, "open_targets.jsonl")
except Exception:
    OPEN_TARGETS_LOG = os.path.join(
        os.path.expanduser("~/.boss-zhipin-scraper"), "open_targets.jsonl")


def record_opened_target(target_id):
    """登记"本工具打开的标签页"（崩溃安全：append-only 日志）。

    用途：进程被**强杀**时（执行器超时 / 手动 kill）``finally`` 不会执行 → 标签页
    泄漏（用户 2026-10-22 反馈：昨晚限流后窗口没关）。下一次运行启动时按这份日志
    把仍然开着的那些关掉（只关本工具开的，不动用户自己的标签页）。
    """
    core = _core()
    if not target_id:
        return
    try:
        core.os.makedirs(core.STATE_DIR, exist_ok=True)
        with open(core.OPEN_TARGETS_LOG, "a", encoding="utf-8") as handle:
            handle.write(core.json.dumps({"target_id": str(target_id),
                                          "ts": core.time.time()}) + "\n")
    except OSError:
        core.log.debug("登记标签页失败", exc_info=True)


def reap_orphan_targets(cdp_port=_core().DEFAULT_CDP_PORT):
    """关掉上一次运行遗留的标签页；返回关掉的数量。

    只关**本工具登记过**且**现在还开着**的 target → 不碰用户自己的标签页。
    """
    core = _core()
    if not core.os.path.exists(core.OPEN_TARGETS_LOG):
        return 0
    recorded = []
    try:
        with open(core.OPEN_TARGETS_LOG, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    recorded.append(core.json.loads(line)["target_id"])
                except (ValueError, KeyError):
                    continue
    except OSError:
        return 0
    if not recorded:
        core._truncate_open_targets_log()
        return 0

    closed = 0
    try:
        cdp = core.CDPSession(cdp_port)
    except Exception:
        core.log.debug("孤儿标签页清理：CDP 不可达", exc_info=True)
        return 0
    try:
        targets = cdp.send("Target.getTargets").get("result", {}).get(
            "targetInfos", [])
        open_ids = {str(t.get("targetId")) for t in targets}
        for target_id in dict.fromkeys(recorded):
            if target_id not in open_ids:
                continue
            try:
                cdp.send("Target.closeTarget", {"targetId": target_id})
                closed += 1
            except Exception:
                core.log.debug("关闭孤儿标签页失败 %s", target_id, exc_info=True)
    except Exception:
        core.log.debug("孤儿标签页清理失败", exc_info=True)
    finally:
        try:
            cdp.close()
        except Exception:
            pass
    core._truncate_open_targets_log()
    if closed:
        print(f"🧹 清理上一次运行遗留的标签页：{closed} 个")
    return closed


def _truncate_open_targets_log():
    core = _core()
    try:
        with open(core.OPEN_TARGETS_LOG, "w", encoding="utf-8"):
            pass
    except OSError:
        pass


def create_page_session(cdp, background=True):
    """Create and attach an about:blank target without stealing focus by default.

    Background pages report themselves as hidden, which prevents BOSS detail
    pages from rendering reliably. Register the existing visibility override
    before callers navigate. Interactive callers such as the login flow must
    opt into a foreground target explicitly.
    """
    core = _core()
    target = cdp.send(
        "Target.createTarget",
        {"url": "about:blank", "background": background},
    )
    target_id = target["result"]["targetId"]
    core.record_opened_target(target_id)
    attached = cdp.send(
        "Target.attachToTarget",
        {"targetId": target_id, "flatten": True},
    )
    session_id = attached["result"]["sessionId"]
    if background:
        cdp.send(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": core.BACKGROUND_VISIBILITY_SCRIPT},
            session_id,
        )
    return target_id, session_id


def _cdp_exception_types():
    """返回 CDP 层可预期异常的元组。

    websocket-client 是 lazy 导入（模块顶部 websocket=None），且测试环境中是
    Mock（其属性不是异常类）。这里动态解析真实异常类，避免 except 元组里出现
    非异常类型导致 TypeError。
    """
    # TargetCrashedError（渲染进程崩溃 OOM 等）必须在内：它虽是 Exception 子类、
    # 不在 RuntimeError 族，若漏掉则异常会逃逸 → 清理被跳过、停靠 tab 泄漏
    # （2026-09-22 实测：`Render process gone` 逃逸到 run_cli 兜底并遗留 ~18 个 tab）。
    core = _core()
    types = (RuntimeError, TimeoutError, KeyError, OSError, TargetCrashedError)
    ws_exc = getattr(core.websocket, "WebSocketException", None) if core.websocket is not None else None
    if isinstance(ws_exc, type) and issubclass(ws_exc, BaseException) and ws_exc not in types:
        types += (ws_exc,)
    return types


def _ws_timeout_exception():
    """websocket 层"接收超时"异常类（websocket-client 的 WebSocketTimeoutException）。

    依赖未加载 / 测试 mock 时回退 ``TimeoutError``，避免 except 元组出现非异常类型。
    """
    core = _core()
    exc = getattr(core.websocket, "WebSocketTimeoutException", None) if core.websocket is not None else None
    if isinstance(exc, type) and issubclass(exc, BaseException):
        return exc
    return TimeoutError
