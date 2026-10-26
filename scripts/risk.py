# -*- coding: utf-8 -*-
"""风控页面探测与分类（从 boss_cdp_raw.py 增量拆分）。"""

import json
import re
import time
import random
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
def probe_risk_page(cdp, sid):
    """通过注入 JS 探测当前页面是否存在风控/验证码痕迹。

    Returns:
        dict: {"url", "title", "hasSlider", "hasLoginWall"}；探测失败返回 {}。
    """
    try:
        val = cdp.eval_js(_core().RISK_PROBE_JS, sid)
    except _core()._cdp_exception_types():
        _core().log.debug("风控页面探测失败", exc_info=True)
        return {}
    if not val:
        return {}
    try:
        probe = _core().json.loads(val) if isinstance(val, str) else val
    except (_core().json.JSONDecodeError, ValueError, TypeError):
        return {}
    return probe if isinstance(probe, dict) else {}


def _restore_minutes_from_now(restore_text):
    """把「将于 2026-10-21 20:14 恢复正常」解析成距今分钟数；无/解析失败 → 0。"""
    text = str(restore_text or "").strip()
    if not text:
        return 0
    match = re.search(r"(\d{4}-\d{2}-\d{2})\s+(\d{1,2}):(\d{2})", text)
    if not match:
        return 0
    try:
        stamp = _core().time.mktime(_core().time.strptime(
            f"{match.group(1)} {int(match.group(2)):02d}:{match.group(3)}",
            "%Y-%m-%d %H:%M"))
    except (ValueError, OverflowError):
        return 0
    delta = (stamp - _core().time.time()) / 60.0
    return int(delta) if delta > 0 else 0


def _classify_blocked(restore_text, login_required, page_text=""):
    """「访问受限」页分级：**账号级**（account_risk）/ **请求级**（env_risk）。

    账号级判据（任一）：页面要求重新登录；声明的恢复时间 > 30 分钟；
    正文提到「账户/账号存在异常行为」（平台自己的措辞）。
    请求级：平台只是拦了当前请求，冷却 5-10 分钟即可续（对标 BossHunter 的
    "BOSS 当前采集页连续检测到请求拦截（不代表账号封禁）"处置）。
    """
    minutes = _restore_minutes_from_now(restore_text)
    body = str(page_text or "")
    account_words = ("账户存在异常行为", "账号存在异常行为", "暂时被限制访问")
    if login_required or minutes > 30 or any(w in body for w in account_words):
        return "account_risk", minutes
    return "env_risk", minutes


def _risk_cooldown_seconds(category, restore_text=""):
    """按风控档位给冷却时长（秒）：账号级到声明的恢复时刻，请求级 5-10 分钟。"""
    if category == "account_risk":
        minutes = _restore_minutes_from_now(restore_text)
        return max(minutes * 60, 30 * 60) if minutes else 30 * 60
    if category == "env_risk":
        return random.randint(300, 600)
    return 0


def classify_risk_page(probe):
    """判断页面探测结果是否命中风控/验证码判据。

    Args:
        probe: probe_risk_page 返回的 dict

    Returns:
        (is_risk, reason, category): category 取值 ""（干净）/ "captcha" /
        "login"（登录墙）/ "rate_limit"（频率受限正文）。调用方据 category
        分流处置：captcha/login → 120s 人工等待；rate_limit → 跳过人工等待，
        直接冷却停手（频率限制人滑了也没用）。

    豁免（Ccelia）：``cardCount == 0`` 的空页面无内容证据 → 标题/登录墙文本
    命中不判（防空列表误停）；URL/滑块是强结构证据，不受豁免。真登录墙下游
    仍有 ``DetailLoginRequiredError`` 兜底。
    """
    if not isinstance(probe, dict):
        return False, "", ""
    url = str(probe.get("url") or "")
    title = str(probe.get("title") or "")
    if any(kw in url.lower() for kw in _core().RISK_URL_KEYWORDS):
        return True, "访问到验证/安全页面", "captcha"
    if probe.get("hasSlider"):
        return True, "检测到滑块验证元素", "captcha"
    # 「访问受限」页是**强结构证据**（无岗位卡片 + 正文是限制页），不受
    # cardCount==0 的空列表豁免影响——否则会被当成"空页面"放过。
    if probe.get("blocked"):
        category, minutes = _classify_blocked(
            probe.get("blockedRestore"), probe.get("blockedLoginRequired"))
        restore = probe.get("blockedRestore") or ""
        if category == "account_risk":
            return True, (f"页面提示账号/IP 被限制访问（恢复时间 {restore or '未声明'}），"
                          "需人工确认登录态后再继续"), category
        return True, "页面提示访问受限（请求级拦截），进入冷却", category
    card_count = probe.get("cardCount")
    if isinstance(card_count, int) and card_count == 0:
        return False, "", ""
    if probe.get("rateLimited"):
        return True, "页面提示访问频率受限（操作频繁），进入冷却", "rate_limit"
    if any(kw in title for kw in _core().RISK_TITLE_KEYWORDS):
        return True, f"页面标题含验证关键词「{title}」", "captcha"
    if probe.get("hasLoginWall"):
        return True, "页面出现登录墙", "login"
    return False, "", ""


def wait_for_risk_clear(cdp, sid, timeout=_core().DEFAULT_RISK_WAIT_TIMEOUT,
                        interval=_core().RISK_WAIT_INTERVAL):
    """页面命中风控时提示用户人工处理，轮询直到恢复或超时。

    Returns:
        True: 风控已解除（判据消失）；False: 等待超时。
    """
    deadline = _core().time.time() + timeout
    while _core().time.time() < deadline:
        probe = _core().probe_risk_page(cdp, sid)
        is_risk, reason, _category = _core().classify_risk_page(probe)
        if not is_risk:
            return True
        remaining = int(deadline - _core().time.time())
        print(f"⚠️ 检测到风控/验证码：{reason}。")
        print(f"   请在专用 Chrome 中完成人工验证（滑动/点选等），等待恢复（剩余 {remaining}s）...")
        _core().time.sleep(min(interval, remaining) if remaining > 0 else interval)
    print(f"❌ 等待人工处理超时（{timeout}s），已停止当前任务。")
    print("   可稍后重试；如果频繁出现，请检查网络环境（代理/VPN 出口 IP 可能触发风控）。")
    return False


