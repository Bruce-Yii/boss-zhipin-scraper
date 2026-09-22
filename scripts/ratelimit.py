#!/usr/bin/env python3
"""全局速率限制（并发详情抓取用）。

从 ``scripts/boss_cdp_raw.py`` 抽出（2026-09-22 P1 架构重构）：
纯逻辑、无 CDP 依赖，可独立测试；主文件对同名符号做 re-export，
保持 ``scripts.boss_cdp_raw.TokenBucket`` 等导入面不变。
"""

import threading
import time

# 详情 API 通道每 worker 最小间隔（秒）：API 单次约 1s，
# 无渲染等待，限速器是唯一刹车 → 并发 N 时全局基线 N/15 次/秒
# （避免旧公式 concurrency*0.5/秒 对详情接口过快触发风控）
DETAIL_API_PACE_SECONDS = 15.0


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
