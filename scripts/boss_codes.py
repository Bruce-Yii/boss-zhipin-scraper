"""BOSS 业务码处置表与限流退避（从 boss_cdp_raw.py 增量拆分）。"""

import logging

log = logging.getLogger("boss_cdp")

BOSS_CODE_TABLE = {
    9: ("rate_limited", "限流（冷却后重试）"),
    17: ("invalid_params", "参数错误（缺少必填参数），应报错退出"),
    19: ("invalid_params", "参数错误（参数非法），应报错退出"),
    31: ("env_risk", "环境风控（停手冷却）"),
    35: ("env_risk", "环境风控（停手冷却）"),
    36: ("account_risk", "账号风险（停手）"),
    38: ("env_risk", "环境风控（停手冷却）"),
    121: ("security_block", "安全拦截（直接停，提示走网页端人工处理）"),
    122: ("security_block", "安全拦截（直接停，提示走网页端人工处理）"),
}

# 处置类别 -> 是否"可直接重试"（仅 token_expired 值得刷新会话后重试一次）
RETRYABLE_CATEGORIES = {"token_expired"}

# code 9（限流 rate_limited）指数退避
RATE_LIMIT_BACKOFF_BASE_SECONDS = 10
RATE_LIMIT_BACKOFF_MAX_SECONDS = 60
RATE_LIMIT_MAX_RETRIES = 3


def rate_limit_backoff(attempt):
    """code9 限流的指数退避秒数：min(MAX, BASE x 2^attempt)。"""
    try:
        attempt = max(int(attempt), 0)
    except (TypeError, ValueError):
        attempt = 0
    return min(RATE_LIMIT_BACKOFF_MAX_SECONDS,
               RATE_LIMIT_BACKOFF_BASE_SECONDS * (2 ** attempt))


def classify_boss_code(code, msg=""):
    """把 BOSS 业务码归为处置类别并给出人话说明。"""
    try:
        code = int(code or 0)
    except (TypeError, ValueError):
        return "unknown", "业务码非整数（保守停手）"
    if code == 0:
        return "ok", "正常"
    if code == 37:
        text = str(msg or "")
        low = text.lower()
        if (any(k in low for k in ("token", "session", "expired"))
                or any(k in text for k in ("会话", "登录", "过期", "令牌"))):
            return "token_expired", "会话/令牌过期（刷新会话后重试一次）"
        return "env_risk", "环境风控（换 tab 无用，停手冷却）"
    entry = BOSS_CODE_TABLE.get(code)
    if entry:
        return entry
    return "unknown", "未知业务码（保守停手）"

DEFAULT_LOGIN_TIMEOUT = 300

