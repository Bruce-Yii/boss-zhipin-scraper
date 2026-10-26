# -*- coding: utf-8 -*-
"""登录态探测（从 boss_cdp_raw.py 增量拆分）。"""

import json
import time
import logging
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlencode

log = logging.getLogger("boss_cdp")

_IMPORTER_NAMES = []

def _record_importer():
    try:
        import sys as _sys
        frame = _sys._getframe(1)
        while frame is not None:
            g = frame.f_globals
            name = g.get("__name__")
            fpath = g.get("__file__", "")
            if name and fpath and str(fpath).endswith("boss_cdp_raw.py"):
                if name not in _IMPORTER_NAMES:
                    _IMPORTER_NAMES.append(name)
                break
            frame = frame.f_back
    except Exception:
        pass

_record_importer()

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



# 登录探测缓存挂在 core 模块（每进程一份），避免跨 reload 共享状态
class LoginProbeStatus(Enum):
    """Outcome of one login probe request."""

    AVAILABLE = "available"
    UNAUTHENTICATED = "unauthenticated"
    RESTRICTED = "restricted"
    EMPTY = "empty"
    RESPONSE_ERROR = "response_error"


@dataclass(frozen=True)
class LoginProbeResult:
    """Structured login probe result with the original failure context."""

    status: LoginProbeStatus
    code: int | None = None
    message: str = ""
    retryable: bool = False


def classify_login_probe_response(data, http_status=200):
    """Classify a BOSS search response without collapsing failures to bool."""
    if http_status == 401:
        return LoginProbeResult(
            LoginProbeStatus.UNAUTHENTICATED,
            message="HTTP 401",
        )
    if http_status in (403, 429):
        return LoginProbeResult(
            LoginProbeStatus.RESTRICTED,
            message=f"HTTP {http_status}",
        )
    if http_status != 200:
        return LoginProbeResult(
            LoginProbeStatus.RESPONSE_ERROR,
            message=f"HTTP {http_status}",
            retryable=http_status == 0 or http_status >= 500,
        )
    if not isinstance(data, dict):
        return LoginProbeResult(
            LoginProbeStatus.RESPONSE_ERROR,
            message="响应不是 JSON 对象",
            retryable=True,
        )

    raw_code = data.get("code")
    try:
        code = int(raw_code) if raw_code is not None else None
    except (TypeError, ValueError):
        code = None
    message = str(data.get("message") or data.get("msg") or "")

    if code in _core().LOGIN_RESTRICTED_CODES:
        # 账号级风控码（BOSS 明确的风控码）→ 立即判定，不再试其它探测目标
        return LoginProbeResult(LoginProbeStatus.RESTRICTED, code=code, message=message)
    if code != 0:
        # 未知非零 code = **请求级**受限（某个 query/城市被拒、瞬时抖动…），
        # 不是账号级风控。按"降速语义"处理：退避后重试一次同一个目标，都失败才判受限
        # ——不因一次瞬时返回把整次抓取判死（2026-10-22 08:25 实测因此白跑两次列表拉取）。
        return LoginProbeResult(LoginProbeStatus.RESTRICTED, code=code, message=message)

    zp_data = data.get("zpData")
    if not isinstance(zp_data, dict):
        return LoginProbeResult(
            LoginProbeStatus.RESPONSE_ERROR,
            code=code,
            message="响应缺少 zpData",
            retryable=True,
        )
    job_list = zp_data.get("jobList")
    if not isinstance(job_list, list):
        return LoginProbeResult(
            LoginProbeStatus.RESPONSE_ERROR,
            code=code,
            message="响应缺少 jobList",
            retryable=True,
        )
    if not job_list:
        return LoginProbeResult(LoginProbeStatus.EMPTY, code=code)
    if any(
        (job.get("salaryDesc") or "").strip()
        for job in job_list
        if isinstance(job, dict)
    ):
        return LoginProbeResult(LoginProbeStatus.AVAILABLE, code=code)
    return LoginProbeResult(LoginProbeStatus.UNAUTHENTICATED, code=code)


def is_logged_in_search_response(data):
    """Return True only when BOSS returns jobs with plaintext salary."""
    result = classify_login_probe_response(data)
    return result.status is LoginProbeStatus.AVAILABLE


def build_login_probe_url(query, city_code):
    params = {
        "scene": 1,
        "query": query,
        "city": city_code,
        "page": 1,
        "pageSize": _core().LOGIN_PROBE_PAGE_SIZE,
    }
    return f"{_core().API_JOB_LIST_PATH}?{urlencode(params)}"


def probe_login_state(cdp, sid, query=None, city_code=None):
    """Run exactly one budgeted search probe and return its structured state."""
    core = _core()
    if query is None:
        query = core.LOGIN_PROBE_QUERY
    if city_code is None:
        city_code = core.LOGIN_PROBE_CITY
    probe_url = build_login_probe_url(query, city_code)
    js = f"""
    (function(){{
        var xhr = new XMLHttpRequest();
        xhr.open('GET', '{probe_url}', false);
        xhr.send();
        return JSON.stringify({{
            httpStatus: xhr.status,
            body: xhr.responseText
        }});
    }})()
    """
    _core().incr_request("probe")
    val = cdp.eval_js(js, sid)
    if not val:
        return LoginProbeResult(
            LoginProbeStatus.RESPONSE_ERROR,
            message="探测响应为空",
            retryable=True,
        )
    try:
        envelope = json.loads(val) if isinstance(val, str) else val
    except (json.JSONDecodeError, ValueError):
        return LoginProbeResult(
            LoginProbeStatus.RESPONSE_ERROR,
            message="探测响应不是有效 JSON",
            retryable=True,
        )
    if not isinstance(envelope, dict):
        return LoginProbeResult(
            LoginProbeStatus.RESPONSE_ERROR,
            message="探测响应格式异常",
            retryable=True,
        )

    raw_http_status = envelope.get("httpStatus", 200)
    try:
        http_status = int(raw_http_status)
    except (TypeError, ValueError):
        http_status = 0
    body = envelope.get("body", envelope)
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            return LoginProbeResult(
                LoginProbeStatus.RESPONSE_ERROR,
                message="搜索接口响应不是有效 JSON",
                retryable=True,
            )
    return classify_login_probe_response(body, http_status=http_status)


def describe_login_probe_result(result):
    """Return a concise user-facing explanation for a non-available state."""
    context = []
    if result.code is not None:
        context.append(f"code: {result.code}")
    if result.message:
        context.append(result.message)
    suffix = f"（{'; '.join(context)}）" if context else ""

    if result.status is LoginProbeStatus.UNAUTHENTICATED:
        return f"未检测到可用登录态{suffix}"
    if result.status is LoginProbeStatus.RESTRICTED:
        return f"BOSS 接口返回限制状态{suffix}"
    if result.status is LoginProbeStatus.EMPTY:
        return "探测样本没有职位，暂时无法确认登录态"
    return f"登录探测响应异常{suffix}"


# ============================================================
# 登录状态检测
# ============================================================
# 登录探测缓存挂在 core 模块（每进程一份），避免跨 reload 共享状态


def check_login_state(cdp_port=None, use_cache=True):
    """通过 CDP 检测 BOSS直聘登录状态（会话内结果缓存）。

    同一进程内 TTL 内重复调用（如 --check 与抓取前的登录检测）直接复用
    上次探测结果，避免重复开 tab、导航与请求；`--login-timeout` 等待循环
    不受影响（走 wait_for_login，不经过本缓存）。

    Args:
        cdp_port: CDP 端口
        use_cache: False 时强制重新探测（绕过缓存）

    Returns:
        LoginProbeResult: 登录探测的结构化状态
    """
    core = _core()
    if cdp_port is None:
        cdp_port = core.DEFAULT_CDP_PORT
    cached = core._LOGIN_PROBE_CACHE
    if use_cache and cached["result"] is not None \
            and _core().time.time() - cached["ts"] < _core().LOGIN_PROBE_CACHE_TTL:
        return cached["result"]
    result = _probe_login_state_uncached(cdp_port)
    cached["ts"] = _core().time.time()
    cached["result"] = result
    return result


def _probe_login_state_uncached(cdp_port=None):
    """实际探测逻辑（无缓存）：多组关键词/城市轮换探测。

    单次探测可能因关键词恰好 0 结果而误判（EMPTY），轮换可降低误判；
    未登录（UNAUTHENTICATED）与风控（RESTRICTED）是确定状态，命中直接
    返回，不继续轮换。

    Returns:
        LoginProbeResult: 登录探测的结构化状态
    """
    core = _core()
    if cdp_port is None:
        cdp_port = core.DEFAULT_CDP_PORT
    cdp = None
    tid = None
    try:
        cdp = _core().CDPSession(cdp_port)
        tid, sid = _core().create_page_session(cdp, background=_core().page_background_default())

        # 先导航到 BOSS直聘，确保 cookie 域名正确
        cdp.send("Page.navigate", {"url": "https://www.zhipin.com/"}, sid)
        _core().time.sleep(4)

        last_result = None
        restricted_result = None
        for query, city_code in _core().LOGIN_PROBE_TARGETS:
            result = _core().probe_login_state(cdp, sid, query=query, city_code=city_code)
            if result.status in (
                LoginProbeStatus.AVAILABLE,
                LoginProbeStatus.UNAUTHENTICATED,
            ):
                return result
            if result.status is LoginProbeStatus.RESTRICTED:
                if result.code in _core().LOGIN_RESTRICTED_CODES:
                    # 账号级风控码 → 立即判定，不再试其它目标
                    return result
                # 请求级受限（未知非零 code）：退避后**重试一次同一个目标**。
                # 这是把代码里写明的"降速语义"真正落地——不是加压，而是别让一次
                # 瞬时返回把整次抓取判死。
                _core().time.sleep(_core().LOGIN_PROBE_BACKOFF_SECONDS)
                retry = _core().probe_login_state(cdp, sid, query=query, city_code=city_code)
                if retry.status in (
                    LoginProbeStatus.AVAILABLE,
                    LoginProbeStatus.UNAUTHENTICATED,
                ):
                    return retry
                if retry.status is LoginProbeStatus.RESTRICTED:
                    restricted_result = retry
                else:
                    last_result = retry
                continue
            last_result = result
        if restricted_result is not None:
            return restricted_result
        return last_result if last_result is not None else LoginProbeResult(
            LoginProbeStatus.RESPONSE_ERROR,
            message="全部探测均无结果",
        )
    except (_core().requests.ConnectionError, _core().requests.Timeout, KeyError,
            json.JSONDecodeError, _core().websocket.WebSocketException,
            TimeoutError, RuntimeError) as e:
        _core().log.error(f"登录状态检测失败: {e}")
        return LoginProbeResult(
            LoginProbeStatus.RESPONSE_ERROR,
            message=str(e),
        )
    finally:
        if cdp is not None:
            if tid is not None:
                try:
                    cdp.send("Target.closeTarget", {"targetId": tid})
                except (KeyError, _core().websocket.WebSocketException, TimeoutError):
                    _core().log.debug("关闭登录探测 target 失败", exc_info=True)
            try:
                cdp.close()
            except _core().websocket.WebSocketException:
                _core().log.debug("关闭登录探测 CDP 连接失败", exc_info=True)


