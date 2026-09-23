#!/usr/bin/env python3
"""全局速率限制（并发详情抓取用）。

从 ``scripts/boss_cdp_raw.py`` 抽出（2026-09-22 P1 架构重构）：
纯逻辑、无 CDP 依赖，可独立测试；主文件对同名符号做 re-export，
保持 ``scripts.boss_cdp_raw.TokenBucket`` 等导入面不变。
"""

import os
import random
import threading
import time
from collections import deque

# 详情 API 通道节律参考值（秒）。
# **2026-09-22 同行研究修正**：全行安全区为 **~0.33–0.67 req/s（高斯 1.5–3.0s）**，
# 且**请求时刻串行化**；我方此前 `1.0s/worker × 并发 3 ≈ 3 req/s` **踩线**，
# 反复触发 `code 37 您的环境存在异常` → 全停 → 产出反而更少。
# 改由 BurstThrottle（见下）承担 API 通道节律；此常量保留为串行参考中心值。
DETAIL_API_PACE_SECONDS = 2.25

# BurstThrottle 实验旋钮（#61 / #31 档位实验）：环境变量覆盖构造参数。
# **默认值零变化**（节律红线：默认调整须实测边界 + 用户拍板）；仅供实验调用
# （如 0.44 档：BOSS_BURST_CENTER=2.25；0.33 档：CENTER/MIN/MAX=3.0；
#  0.20 档：CENTER/MIN/MAX=5.0；采集端 burst 阈值：SHORT=12/LONG=32）。
BURST_ENV_VARS = {
    "center": ("BOSS_BURST_CENTER", float),
    "min_delay": ("BOSS_BURST_MIN_DELAY", float),
    "max_delay": ("BOSS_BURST_MAX_DELAY", float),
    "short_threshold": ("BOSS_BURST_SHORT_THRESHOLD", int),
    "long_threshold": ("BOSS_BURST_LONG_THRESHOLD", int),
}


def _burst_env_override(name, default):
    """读环境变量覆盖 BurstThrottle 构造参数；缺省/非法/越界时回退默认。"""
    env_name, cast = BURST_ENV_VARS[name]
    raw = os.environ.get(env_name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = cast(raw)
    except (TypeError, ValueError):
        return default
    if name in ("center", "min_delay", "max_delay"):
        if not (value > 0):
            return default
    else:
        if not (value >= 1):
            return default
    return value


class BurstThrottle:
    """**串行化 + burst-aware** 请求节律（对标同行 boss-agent-cli throttle 模型）。

    - 请求时刻**全局串行**（持锁跨 sleep）：并发 worker 也不再同时发请求
    - 间隔 ~高斯(center, sigma)，裁剪到 [min_delay, max_delay]
    - ~5% 概率叠加一次长暂停（2–5s）
    - burst 惩罚：近 15s ≥3 次加 1.2–2.8s；近 45s ≥6 次加 4–7s
    - slow_factor（熔断恢复期=2.0）整体放大间隔

    目标全局速率 ≈ 1/center（默认 ~0.44 req/s），落在全行安全区。

    #61 实验旋钮：``BOSS_BURST_*`` 环境变量可覆盖 center/min_delay/max_delay
    与 short/long 阈值（缺省/非法/越界 → 回退传入值；默认零变化）。
    """

    def __init__(self, center=2.25, sigma=0.4, min_delay=1.5, max_delay=3.0,
                 long_pause_prob=0.05, long_pause=(2.0, 5.0),
                 short_window=15.0, short_threshold=3, short_penalty=(1.2, 2.8),
                 long_window=45.0, long_threshold=6, long_penalty=(4.0, 7.0)):
        # #61 实验旋钮：环境变量覆盖（缺省/非法/越界 → 回退传入值；默认零变化）
        dfl_min, dfl_max = min_delay, max_delay
        center = _burst_env_override("center", center)
        min_delay = _burst_env_override("min_delay", min_delay)
        max_delay = _burst_env_override("max_delay", max_delay)
        short_threshold = _burst_env_override("short_threshold", short_threshold)
        long_threshold = _burst_env_override("long_threshold", long_threshold)
        if min_delay > max_delay:
            # 环境配错时回退默认（静默交换会误导实验读数）
            min_delay, max_delay = dfl_min, dfl_max
        self.center = center
        self.sigma = sigma
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.long_pause_prob = long_pause_prob
        self.long_pause = long_pause
        self.short_window = short_window
        self.short_threshold = short_threshold
        self.short_penalty = short_penalty
        self.long_window = long_window
        self.long_threshold = long_threshold
        self.long_penalty = long_penalty
        self.slow_factor = 1.0
        self._lock = threading.Lock()
        self._last = 0.0
        self._times = deque()

    def _base_delay(self):
        d = random.gauss(self.center, self.sigma)
        d = min(max(d, self.min_delay), self.max_delay)
        if random.random() < self.long_pause_prob:
            d += random.uniform(*self.long_pause)
        return d

    def acquire(self):
        with self._lock:
            now = time.monotonic()
            horizon = max(self.short_window, self.long_window)
            while self._times and now - self._times[0] > horizon:
                self._times.popleft()
            n_short = sum(1 for t in self._times if now - t <= self.short_window)
            n_long = sum(1 for t in self._times if now - t <= self.long_window)
            delay = self._base_delay()
            if n_long >= self.long_threshold:
                delay += random.uniform(*self.long_penalty)
            elif n_short >= self.short_threshold:
                delay += random.uniform(*self.short_penalty)
            delay *= self.slow_factor
            wait = delay - (now - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            self._times.append(self._last)

    # 与 AdaptiveRateLimiter 接口兼容（并行路径会调用；burst 模型用不到成功率）
    def record_success(self):
        pass

    def record_failure(self):
        pass


class TokenBucket:
    """线程安全的全局速率限制令牌桶。

    容量 capacity 内可突发；令牌按 rate（个/秒）持续补充，
    耗尽时 acquire 阻塞直到令牌补充（并发详情抓取的全局限速用）。
    """
    def __init__(self, rate, capacity):
        self.rate = rate
        self.capacity = capacity
        self._tokens = float(capacity)
        self._last_refill = time.monotonic()  # monotonic：NTP 回拨不导致桶爆满/负数
        self._lock = threading.Lock()

    def _refill(self, now):
        elapsed = now - self._last_refill
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            self._last_refill = now

    def acquire(self):
        # 循环取令牌：仅在锁内判定/扣减，锁外睡眠等待补充后**重新复核**。
        # 修复（2026-09-22 审计）：原实现锁外 sleep 后不再复核令牌，
        # 并发下 N 个线程会同sleep 同一时长、醒来各自扣减 → 突发超速。
        while True:
            with self._lock:
                self._refill(time.monotonic())
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                deficit = (1.0 - self._tokens) / self.rate
            time.sleep(deficit)


class AdaptiveRateLimiter:
    """全局限速 + 错误率自适应（Scrapy AutoThrottle 思想）。

    - 滑动窗口（默认 60s）内失败率 > failure_threshold → 速率降半
    - 连续 2 个坏窗口 → acquire 暂停 pause_seconds（等风控窗口过去）
    - 健康窗口 → 速率恢复基线
    """
    def __init__(self, base_rate, capacity=None, window=60.0,
                 failure_threshold=0.3, pause_seconds=60.0):
        self.base_rate = base_rate
        self.capacity = capacity or max(int(base_rate), 1)
        self.window = window
        self.failure_threshold = failure_threshold
        self.pause_seconds = pause_seconds
        self._halved = False
        self._consecutive_bad = 0
        self._window_start = time.monotonic()  # monotonic：窗口判断不受时钟回拨影响
        self._window_total = 0
        self._window_failures = 0
        self._bucket = TokenBucket(base_rate, self.capacity)
        self._lock = threading.Lock()
        self._pause_lock = threading.Lock()  # 保证"连续坏窗口暂停"只由单线程执行一次

    def current_rate(self):
        """当前生效速率（降半后的值）。"""
        return self.base_rate / 2.0 if self._halved else self.base_rate

    def record_success(self):
        """记录一次成功请求。"""
        self._record(failed=False)

    def record_failure(self):
        """记录一次失败请求。"""
        self._record(failed=True)

    def _record(self, failed):
        with self._lock:
            now = time.monotonic()
            if now - self._window_start >= self.window:
                self._roll_window()
            self._window_total += 1
            if failed:
                self._window_failures += 1

    def _roll_window(self):
        """窗口推进：按失败率调整速率与连续坏窗口计数。"""
        if self._window_total > 0:
            failure_rate = self._window_failures / self._window_total
            if failure_rate > self.failure_threshold:
                self._halved = True
                self._consecutive_bad += 1
            else:
                self._halved = False
                self._consecutive_bad = 0
        self._window_start = time.monotonic()
        self._window_total = 0
        self._window_failures = 0

    def acquire(self):
        """申请一个请求配额；连续坏窗口时先暂停，再走令牌桶。"""
        self._pause_if_needed()
        self._bucket.rate = self.current_rate()
        self._bucket.acquire()

    def _pause_if_needed(self):
        """连续坏窗口触发一次全局暂停（单飞）。

        修复（2026-09-22 审计）：原实现每个线程各自 sleep(pause_seconds)，
        并发下形成"N 个线程同时长睡"的暂停风暴；改用独立锁保证只暂停一次，
        其余线程等待该暂停结束后继续（读 _consecutive_bad 的竞态可容忍）。
        """
        with self._pause_lock:
            if self._consecutive_bad >= 2:
                time.sleep(self.pause_seconds)
                self._consecutive_bad = 0
