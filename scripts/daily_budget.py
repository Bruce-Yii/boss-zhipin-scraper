#!/usr/bin/env python3
"""详情页访问日预算（跨 run 持久化的计数器）。

为什么需要（2026-10-21 实测）：
- 平台侧对**详情页访问次数**有日累计预算。09-23 实测在累计 ~700 处撞第一道墙
  （滑块验证码，可人工解开）、~994 处撞第二道；同日 20:14 前后的实跑在累计
  **693** 处撞到「访问受限：账户存在异常行为，已暂时被限制访问」这一**更严的档**。
- 此前工具只有**单次 run** 的 `MAX_API_REQUESTS=500` 上限，**没有日预算**：
  一天里跑 8 次实跑（每次各自独立计数）累计 693 次，全程没有任何一处拦得住。

参照的第三方校准值（shengjidaguai-china/BossHunter 的 config.example.yaml）：
``daily_detail_page_limit: 900``、``daily_search_page_limit: 60``、
``daily_platform_page_limit: 1100``、风险信号后随机暂停 5-10 分钟。
我方取更保守的 **700**（我们自己撞到的第一道墙位置）。

计数口径：**详情页请求次数**（不是岗位数）——平台数的是请求。
DOM 渲染通道一次岗位 = 1 次页面导航；HTML 直取通道一次岗位 = 1 次 fetch；
直取失败回退渲染时同一岗位会发 2 次请求，两次都要计。

文件位置：仓库外 ``~/.boss-zhipin-scraper/daily_budget.json``（与审计日志同目录），
``BOSS_DAILY_BUDGET_PATH`` 可覆盖（测试/多环境隔离）。
"""

import json
import os
import tempfile
import threading
import time

DEFAULT_DAILY_DETAIL_BUDGET = 700
BUDGET_PATH_ENV = "BOSS_DAILY_BUDGET_PATH"
STATE_DIR_ENV = "BOSS_STATE_DIR"

_LOCK = threading.Lock()


def _state_dir():
    return os.environ.get(STATE_DIR_ENV) or os.path.expanduser(
        "~/.boss-zhipin-scraper")


def budget_path():
    """日预算计数文件路径（``BOSS_DAILY_BUDGET_PATH`` 可覆盖）。"""
    return os.environ.get(BUDGET_PATH_ENV) or os.path.join(
        _state_dir(), "daily_budget.json")


def today_key(now=None):
    """当前自然日（本地时区）键，形如 ``2026-10-21``。跨零点自动滚动。"""
    stamp = time.localtime(now if now is not None else time.time())
    return time.strftime("%Y-%m-%d", stamp)


def read_state(path=None):
    """读取当前计数：``{"date": "2026-10-21", "detail": 128}``。

    文件缺失/损坏/非当日 → 视为空计数（``detail`` 归零），**不因文件问题中断抓取**。
    """
    target = path or budget_path()
    try:
        with open(target, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError, TypeError):
        return {"date": today_key(), "detail": 0}
    if not isinstance(data, dict):
        return {"date": today_key(), "detail": 0}
    if data.get("date") != today_key():
        return {"date": today_key(), "detail": 0}
    try:
        count = int(data.get("detail", 0))
    except (TypeError, ValueError):
        count = 0
    return {"date": today_key(), "detail": max(count, 0)}


def _write_state(state, path=None):
    """原子写计数文件（tmp + fsync + os.replace）。"""
    target = path or budget_path()
    directory = os.path.dirname(target)
    if directory:
        os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".daily_budget-", dir=directory or None)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def consume(n=1, limit=DEFAULT_DAILY_DETAIL_BUDGET, path=None):
    """记一次详情页请求；返回 ``(allowed, used, limit)``。

    ``allowed`` 为 False 表示**已用满或超出日预算**，调用方应停止本轮。
    计数按请求次数累加（直取 + 渲染回退各算一次）。
    """
    if limit is None or limit <= 0:
        return True, read_state(path)["detail"], 0
    with _LOCK:
        state = read_state(path)
        used = state["detail"] + max(int(n), 0)
        if used > limit:
            # 超额**不落盘**（避免把超限尝试也计入配额，放大误差）
            return False, state["detail"], limit
        _write_state({"date": state["date"], "detail": used}, path)
        return True, used, limit


def remaining(limit=DEFAULT_DAILY_DETAIL_BUDGET, path=None):
    """今日剩余可用详情页请求数。"""
    if limit is None or limit <= 0:
        return -1  # 不限
    return max(limit - read_state(path)["detail"], 0)


def reset(path=None):
    """清零今日计数（人工排障用；正常流程不需要）。"""
    with _LOCK:
        _write_state({"date": today_key(), "detail": 0}, path)
