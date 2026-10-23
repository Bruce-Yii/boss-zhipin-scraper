#!/usr/bin/env python3
"""跨进程文件锁与原子替换的公共工具。

从 ``scripts/daily_budget.py`` 与 ``scripts/known_failures.py`` 中抽取的重复逻辑：

- ``acquire_guard(path, timeout, stale_threshold)``：基于 ``O_CREAT|O_EXCL`` 原子创建的
  guard 文件互斥锁，返回上下文管理器；
- ``atomic_replace(src, dst, attempts, base_delay)``：``os.replace`` 的并发冲突
  （Windows 上的 ``PermissionError [WinError 5]``）退避重试，返回是否成功。

本模块**只依赖标准库**，不会导入 ``daily_budget`` / ``known_failures`` /
``boss_cdp_raw``，因此可在三者之间安全共享，不会形成循环依赖。
"""

import contextlib
import os
import time

__all__ = ["acquire_guard", "atomic_replace", "GuardAcquisitionError"]

# guard 自旋间隔（秒）。临界区只是读写一个小 JSON，10ms 已足够细。
GUARD_POLL_SECONDS = 0.01


class GuardAcquisitionError(TimeoutError):
    """无法在超时内获取 guard 锁时抛出。"""


def acquire_guard(path, timeout=30.0, stale_threshold=20.0):
    """以 ``path`` 为 guard 文件，获取跨进程互斥锁（上下文管理器）。

    语义与原 ``_with_*_lock`` / ``_with_guard`` 一致：用 ``O_CREAT|O_EXCL`` 原子创建
    guard 文件保证同一时刻只有一个进程进入临界区；抢不到就自旋等待，超过 ``timeout``
    秒或 guard 文件陈旧（年龄超过 ``stale_threshold``）时回收残留 guard 后继续抢；
    超时仍抢不到则抛出 :class:`GuardAcquisitionError`（``TimeoutError`` 子类）。

    用法::

        with acquire_guard(guard_path, timeout=10.0, stale_threshold=5.0):
            ...  # 临界区
    """
    guard = str(path)
    directory = os.path.dirname(guard)
    if directory:
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as err:
            raise GuardAcquisitionError(
                f"无法创建 guard 所在目录：{directory}") from err

    @contextlib.contextmanager
    def _manager():
        deadline = time.monotonic() + float(timeout)
        acquired = False
        try:
            while True:
                try:
                    fd = os.open(guard, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    os.close(fd)
                    acquired = True
                    break
                except FileExistsError:
                    try:
                        age = time.time() - os.path.getmtime(guard)
                    except OSError:
                        age = 0.0
                    if age > float(stale_threshold):
                        # 陈旧 guard（持锁进程崩溃残留）→ 清掉再抢
                        try:
                            os.remove(guard)
                        except OSError:
                            pass
                        continue
                    if time.monotonic() >= deadline:
                        raise GuardAcquisitionError(
                            f"无法在 {timeout}s 内获取 guard：{guard}") from None
                    time.sleep(GUARD_POLL_SECONDS)
                except OSError as err:
                    raise GuardAcquisitionError(
                        f"获取 guard 失败：{guard}") from err
            yield
        finally:
            if acquired:
                try:
                    os.remove(guard)
                except OSError:
                    pass

    return _manager()


def atomic_replace(src, dst, attempts=8, base_delay=0.02):
    """原子替换 ``src -> dst``，``PermissionError`` 退避重试。

    Windows 上目标文件被别的进程/杀软打开时 ``os.replace`` 抛
    ``PermissionError [WinError 5]``——这是**并发冲突**不是错误，退避重试即可。

    返回 ``True`` 表示替换成功；重试耗尽仍失败返回 ``False``（不抛异常，由调用方决定
    如何处理）。其它非 ``PermissionError`` 的异常也按失败处理（返回 ``False``）。
    """
    src = str(src)
    dst = str(dst)
    delay = float(base_delay)
    for _ in range(int(attempts)):
        try:
            os.replace(src, dst)
            return True
        except PermissionError:
            time.sleep(delay)
            delay = min(delay * 2, 0.5)
            continue
        except FileNotFoundError:
            # 源文件不存在：无法重试，直接失败
            return False
        except OSError:
            time.sleep(delay)
            delay = min(delay * 2, 0.5)
            continue
    return False