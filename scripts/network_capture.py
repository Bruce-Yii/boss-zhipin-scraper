# -*- coding: utf-8 -*-
"""被动捕获页面自身发出的 joblist API 响应（从 boss_cdp_raw.py 增量拆分）。"""

import base64
import json
import time
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
class NetworkJoblistCapture:
    """监听并捕获页面自身发出的 joblist API 响应。

    依赖 ``CDPSession.events`` 事件缓冲：send() 执行其它命令期间到达的 Network
    事件已自动入缓冲；wait_next_response() 再补充等待窗口。
    """

    def __init__(self, cdp, sid):
        self.cdp = cdp
        self.sid = sid
        self._consumed = set()   # 已返回给调用方的 requestId

    def enable(self):
        self.cdp.send("Network.enable", {}, self.sid)

    @staticmethod
    def _is_joblist_url(url):
        return _core().API_JOB_LIST_PATH in str(url or "")

    def _next_completed(self):
        """扫描事件缓冲，返回下一个已完成且未消费的 joblist 响应 requestId。"""
        requests = {}
        finished = set()
        for ev in self.cdp.events:
            method = ev.get("method", "")
            params = ev.get("params", {})
            if method == "Network.requestWillBeSent":
                if self._is_joblist_url((params.get("request") or {}).get("url", "")):
                    requests[params.get("requestId")] = True
            elif method == "Network.loadingFinished":
                finished.add(params.get("requestId"))
        for request_id in requests:
            if request_id in finished and request_id not in self._consumed:
                return request_id
        return None

    def wait_next_response(self, timeout, trigger=None, poll=0.5):
        """等待下一个未消费的 joblist 响应并解析 JSON。

        Args:
            timeout: 最长等待秒数
            trigger: 等待前执行一次的触发动作（导航/滚动），页面将自行发请求
            poll: 每轮事件等待窗口

        Returns:
            dict: 解析后的响应 JSON；超时未捕获返回 None
        """
        if trigger is not None:
            trigger()
        deadline = _core().time.time() + timeout
        while _core().time.time() < deadline:
            request_id = self._next_completed()
            if request_id is None:
                self.cdp.drain_events(min(poll, max(deadline - _core().time.time(), 0)))
                continue
            self._consumed.add(request_id)
            body = self._fetch_body(request_id)
            if body is None:
                continue
            try:
                return _core().json.loads(body)
            except (_core().json.JSONDecodeError, ValueError) as e:
                _core().log.warning(f"joblist 响应不是有效 JSON: {e}")
        return None

    def _fetch_body(self, request_id):
        try:
            result = self.cdp.send(
                "Network.getResponseBody", {"requestId": request_id}, self.sid)
        except _core()._cdp_exception_types() as e:
            _core().log.debug(f"读取响应体失败 request={request_id}: {e}")
            return None
        payload = result.get("result") or {}
        body = payload.get("body", "")
        if payload.get("base64Encoded"):
            try:
                body = base64.b64decode(body).decode("utf-8", errors="replace")
            except (ValueError, TypeError) as e:
                _core().log.debug(f"响应体 base64 解码失败: {e}")
                return None
        return body



