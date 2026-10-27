# -*- coding: utf-8 -*-
"""列表多页抓取 workers（从 boss_cdp_raw.py 增量拆分）。

含：并发 XHR 抓取（_fetch_pages_parallel）、被动 Network 旁听抓取
（_fetch_pages_passive）。任一页失败 → 返回 None，调用方回退串行。

约束：boss 侧函数/常量（_open_api_tab、parse_api_eval_value、
NetworkJoblistCapture、CDPSession 等）经 _core() 取用——tests 会 patch
boss 上的这些名字后再调本模块函数（含线程闭包内调用）。
"""

import logging
import queue
import threading

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


def _fetch_pages_parallel(cdp_port, keyword, city_code, pages, filters,
                          workers=3, raw_sink=None):
    """并发抓取多页列表：并发开 `workers` 个停靠 tab，再跨 tab 同发搜索 XHR。

    实测（2026-09-22 E5）：3 页同发一次取全（无风控）；配合并发开 tab，
    列表阶段从"串行 ~50s（含 12-22s 页间等待）"降到约 6-7s。
    任一页无数据/异常 → 返回 None（调用方回退串行，保证稳健）。
    返回：[[page1 jobs], [page2 jobs], ...]（长度 = pages）。
    """
    core = _core()
    workers = max(1, min(int(workers), pages))
    sessions = [None] * workers
    open_errs = []

    def _open(i):
        try:
            sessions[i] = core._open_api_tab(cdp_port, keyword, city_code)
        except core._cdp_exception_types():
            open_errs.append(i)

    ots = [threading.Thread(target=_open, args=(i,)) for i in range(workers)]
    for t in ots:
        t.start()
    for t in ots:
        t.join()
    if open_errs or any(s is None for s in sessions):
        for s in sessions:
            core._close_api_tab(s)
        return None

    page_q = queue.Queue()
    for p in range(1, pages + 1):
        page_q.put(p)
    out = {}
    failed = []
    lock = threading.Lock()

    def _run(sess):
        while True:
            try:
                pg = page_q.get_nowait()
            except queue.Empty:
                return
            params = {"scene": "1", "query": keyword, "city": city_code,
                      "page": pg, "pageSize": core.PAGE_SIZE}
            for k, v in (filters or {}).items():
                if v:
                    params[k] = v
            api_url = f"{core.API_JOB_LIST_PATH}?{core.urlencode(params)}"
            js = core.FETCH_API_JS_TEMPLATE.replace("__API_URL__", api_url)
            try:
                val = sess[0].eval_js(js, sess[2])
                jobs, raw_page = core.parse_api_eval_value(val)
                if raw_page and raw_sink is not None:
                    with lock:
                        raw_sink.append({"page": pg, "response": raw_page})
            except core._cdp_exception_types():
                with lock:
                    failed.append(pg)
                return
            if not jobs:
                with lock:
                    failed.append(pg)
                return
            with lock:
                out[pg] = jobs

    ts = [threading.Thread(target=_run, args=(s,)) for s in sessions]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    for s in sessions:
        core._close_api_tab(s)
    if failed or len(out) != pages:
        return None
    return [out[p] for p in range(1, pages + 1)]


def _fetch_pages_passive(cdp_port, keyword, city_code, pages, filters,
                           raw_sink=None):
    """被动捕获多页列表（上游 #55 思路）：导航真实搜索页 + 滚动触发无限滚动，
    用 Network 域旁听页面自己发出的 joblist 响应，**零注入 XHR**，从而消除注入
    请求特征触发的 `code 37`。

    返回：[[page1 jobs], [page2 jobs], ...]（长度 = pages；已 map_api_jobs）。
    任一页未捕获/登录风控异常 → 返回 None（调用方回退串行 XHR，保证稳健）。
    raw_sink 非空时同步追加 {"page", "response"}（保真核对用，与并行版同口径；
    缺了它保真核对可比对 0 条 → 按 fail-safe 判失败）。
    """
    core = _core()
    ws = None
    tid = None
    try:
        ws = core.CDPSession(cdp_port)
        tid, sid = core.create_page_session(ws, background=core.page_background_default())
        capture = core.NetworkJoblistCapture(ws, sid)
        capture.enable()

        def navigate():
            ws.send("Page.navigate",
                    {"url": core.build_search_url(keyword, city_code, 1, filters)}, sid)
            core.time.sleep(core.random.uniform(3, 5))

        first = capture.wait_next_response(timeout=core.PROBE_CAPTURE_TIMEOUT, trigger=navigate)
        if not first:
            core.log.warning("被动捕获：未捕获到页面自身首个搜索响应")
            return None
        login = core.classify_login_probe_response(first)
        if login.status is core.LoginProbeStatus.UNAUTHENTICATED:
            print("❌ 被动捕获：未检测到 BOSS直聘登录状态。请先在 Chrome 中登录 zhipin.com。")
            return None
        if login.status is core.LoginProbeStatus.RESTRICTED:
            core.set_scrape_lock_risk()
            print(f"❌ 被动捕获：{core.describe_login_probe_result(login)}，已停止抓取。")
            return core._PASSIVE_RISK

        out = [core.map_api_jobs(first)]
        if raw_sink is not None:
            raw_sink.append({"page": 1, "response": first})
        for _ in range(2, pages + 1):
            # 滚到底触发无限滚动加载下一页，继续旁听页面自身请求
            ws.eval_js(core.SCROLL_BOTTOM_JS, sid)
            page_data = capture.wait_next_response(timeout=core.PROBE_CAPTURE_TIMEOUT)
            if not page_data:
                core.log.warning("被动捕获：翻页未捕获到响应")
                return None
            out.append(core.map_api_jobs(page_data))
            if raw_sink is not None:
                raw_sink.append({"page": len(out), "response": page_data})
        return out
    except core._cdp_exception_types():
        core.log.warning("被动捕获列表失败，回退串行 XHR", exc_info=True)
        return None
    finally:
        if ws is not None:
            try:
                if tid is not None:
                    ws.send("Target.closeTarget", {"targetId": tid})
                ws.close()
            except core._cdp_exception_types():
                core.log.debug("关闭被动捕获 tab 失败", exc_info=True)
