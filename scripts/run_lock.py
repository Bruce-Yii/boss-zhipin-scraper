# -*- coding: utf-8 -*-
"""run 级锁与熔断冷却（从 boss_cdp_raw.py 增量拆分）。

含：pid 存活检查、锁文件读写（原子写 + guard 互斥防 RMW 竞态）、
CDP 熔断冷却标记（冷却截止 + 渐变恢复期）、批次冷却、acquire/release/risk。

约束：全部运行时全局经 _core() 动态取自主模块（含嵌套闭包），
tests 对 boss_cdp_raw 的 patch（SCRAPE_LOCK_PATH / _pid_is_running /
acquire_scrape_lock / check_cdp_cooldown 等）保持有效。
"""

import logging

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


def _pid_is_running(pid):
    """检查 pid 对应进程是否存活（跨平台）。"""
    core = _core()
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        if core.platform.system() == "Windows":
            r = core.subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"Get-Process -Id {pid} -ErrorAction SilentlyContinue"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=5)
            return bool(r.stdout.strip())
        core.os.kill(pid, 0)
        return True
    except (OSError, core.subprocess.TimeoutExpired):
        return False


def _read_scrape_lock():
    """读取锁文件，返回 (max_concurrent, 持有 pid 列表, risk 标志)；损坏返回 (1, [], False)。"""
    core = _core()
    try:
        with open(core.SCRAPE_LOCK_PATH, encoding="utf-8") as f:
            lines = [ln.strip() for ln in f.read().splitlines() if ln.strip()]
    except (OSError, UnicodeDecodeError, ValueError):
        return 1, [], False
    if not lines:
        return 1, [], False
    try:
        max_concurrent = int(lines[0])
    except (TypeError, ValueError):
        max_concurrent = 1
    risk = core.RISK_LINE in lines
    holders = [ln for ln in lines[1:] if ln != core.RISK_LINE]
    return max_concurrent, holders, risk


def _write_scrape_lock(max_concurrent, holders, risk=False):
    """原子写锁文件（tmp + os.replace + fsync，防并发撕裂/断电丢失）。"""
    core = _core()
    tmp = core.SCRAPE_LOCK_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(str(max_concurrent) + "\n")
        for pid in holders:
            f.write(str(pid) + "\n")
        if risk:
            f.write(core.RISK_LINE + "\n")
        f.flush()
        core.os.fsync(f.fileno())
    core.os.replace(tmp, core.SCRAPE_LOCK_PATH)


def _scrape_lock_guard_path():
    return _core().SCRAPE_LOCK_PATH + ".guard"


def _with_scrape_lock_guard(action, timeout=5.0):
    """用 O_CREAT|O_EXCL guard 文件对锁文件读-改-写做进程间互斥（防 RMW 竞态）。

    并发上限可配后（--max-concurrent>1）多进程同时 acquire 会 check-then-act：
    两进程同读空位 → 各自覆盖写 → 前者的 pid 丢失，实际并发超限。
    guard 原子创建保证同一时刻只有一个进程做 RMW；抢不到 spin + 超时。

    Args:
        action: 无参可调用，在持有 guard 期间执行（内部做读-改-写）。
        timeout: 最大等待秒数。
    """
    core = _core()
    guard = core._scrape_lock_guard_path()
    deadline = core.time.monotonic() + timeout
    while True:
        try:
            fd = core.os.open(guard, core.os.O_CREAT | core.os.O_EXCL | core.os.O_WRONLY)
            core.os.close(fd)
            break
        except FileExistsError:
            if core.time.monotonic() >= deadline:
                return False  # 超时：另一进程持 guard 过久（可能崩溃残留）
            core.time.sleep(0.05)
        except OSError:
            return False
    try:
        action()
        return True
    finally:
        try:
            core.os.remove(guard)
        except OSError:
            pass


# ============================================================
# CDP 熔断冷却（防"熔断→立即重启→再熔断"循环）
# ============================================================
def _cdp_cooldown_path():
    """冷却时间戳文件（与锁文件同目录，跨进程共享）。"""
    core = _core()
    return core.os.path.join(core.os.path.dirname(core.SCRAPE_LOCK_PATH), "cdp.cooldown")


def _restart_scraper_chrome(cdp_port=_core().DEFAULT_CDP_PORT):
    """冷却期重启专用 Chrome（清内存防性能衰减，#78）。best-effort。"""
    core = _core()
    try:
        for d in (core.chrome_user_data_dirs_for_cdp_port(cdp_port) or []):
            core.stop_cdp_chrome(d)
    except Exception:
        core.log.warning("冷却期停止 Chrome 失败", exc_info=True)
    core.time.sleep(3)
    try:
        return core.run_setup_chrome(cdp_port=cdp_port, wait_login=False) == 0
    except Exception:
        core.log.warning("冷却期重启 Chrome 失败", exc_info=True)
        return False


def _batch_cooldown_if_needed(done_count):
    """每 ``COOL_EVERY`` 条主动批次冷却（可选重启 Chrome，#78）。

    返回: bool —— True 表示刚冷却过（调用方可能需要重建失效会话）。
    """
    core = _core()
    if not core.COOL_EVERY or core.COOL_EVERY <= 0 or done_count <= 0:
        return False
    if done_count % core.COOL_EVERY != 0:
        return False
    print(f"\n🧊 批次冷却：已完成 {done_count} 条，暂停 {core.COOL_SECONDS}s"
          + ("（含 Chrome 重启）" if core.COOL_RESTART_CHROME else "") + "...")
    restarted = False
    if core.COOL_RESTART_CHROME:
        restarted = core._restart_scraper_chrome()
        print(f"🧊 Chrome 重启：{'成功' if restarted else '失败（继续用旧会话）'}")
    core.time.sleep(core.COOL_SECONDS)
    print("🧊 冷却结束，继续\n")
    return restarted


def _atomic_replace_with_retry(src, dst, attempts=8, base_delay=0.02):
    """原子替换，带 PermissionError 退避重试（Windows 上目标文件可能被占用）。

    独立审计 2026-10-22：os.replace 的 PermissionError 被 `except OSError: pass`
    吞掉 → 冷却标记静默丢失 = **熔断冷却失效**；导出原子写也有同型风险。
    退避重试后仍失败则抛出（让上层决定怎么办），不再静默。
    """
    core = _core()
    last = None
    for attempt in range(attempts):
        try:
            core.os.replace(src, dst)
            return True
        except PermissionError as exc:
            last = exc
            core.time.sleep(base_delay * (2 ** attempt))
        except FileNotFoundError:
            raise
    raise last if last else OSError(f"替换失败：{src} → {dst}")


def mark_cdp_cooldown(seconds=_core().CDP_COOLDOWN_SECONDS):
    """熔断时记录冷却截止时间戳（原子写）。

    文件两行：首行冷却截止，次行恢复期截止（冷却结束后仍限速减半，
    渐变恢复而非跳回全速，防恢复瞬间再触发风控）。

    P4e：同时落一条审计事件（冷却进入）。
    """
    core = _core()
    core.record_audit_event("cooldown", seconds=seconds)
    try:
        core.os.makedirs(core.os.path.dirname(core.SCRAPE_LOCK_PATH), exist_ok=True)
        tmp = core._cdp_cooldown_path() + ".tmp"
        now = core.time.time()
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(str(now + seconds) + "\n")
            f.write(str(now + seconds + core.CDP_RECOVERY_SECONDS) + "\n")
            f.flush()
            core.os.fsync(f.fileno())
        core._atomic_replace_with_retry(tmp, core._cdp_cooldown_path())
    except OSError as exc:
        # 冷却标记写不进去 = 熔断冷却失效（本该停手，结果照跑）→ 必须让调用方知道
        core.record_audit_event("cooldown_write_failed", error=str(exc))
        print(f"⚠️ 熔断冷却标记写入失败（{exc}）——冷却可能不生效，请手动等待后再试")
        raise


def check_cdp_recovery():
    """熔断冷却结束后进入渐变恢复期：返回剩余恢复秒数（0=无恢复期）。

    调用方（如详情限速器）在恢复期内降低速率，冷却结束不直接跳回全速。
    冷却与恢复期都结束后清理残留标记文件。
    """
    core = _core()
    try:
        with open(core._cdp_cooldown_path(), encoding="utf-8") as f:
            lines = f.read().splitlines()
        deadline = float(lines[0])
        recovery_until = float(lines[1]) if len(lines) > 1 else deadline
        remaining = recovery_until - core.time.time()
        if remaining > 0:
            return remaining
        if core.time.time() > deadline:
            try:
                core.os.remove(core._cdp_cooldown_path())
            except OSError:
                pass
        return 0.0
    except (OSError, ValueError, IndexError):
        return 0.0


def check_cdp_cooldown():
    """检查冷却期：未冷却返回 None；冷却中返回剩余秒数。

    冷却期过后返回 None（不再删除标记——恢复期信息保留在文件第二行，
    由 check_cdp_recovery 在恢复期结束后清理）。
    文件首行为冷却截止（第二行为恢复期截止，冷却判断只看首行）。
    """
    core = _core()
    try:
        with open(core._cdp_cooldown_path(), encoding="utf-8") as f:
            deadline = float(f.read().splitlines()[0])
    except (OSError, UnicodeDecodeError, ValueError, IndexError):
        return None
    remaining = deadline - core.time.time()
    if remaining > 0:
        return int(remaining)
    return None


def acquire_scrape_lock(max_concurrent=1):
    """获取并发锁；熔断中或存活持有者已达上限时返回 False。

    max_concurrent=1 时行为与旧版单 pid 互斥一致（硬防线，默认）。
    持锁进程崩溃（pid 已死）自动清理其条目并接管。
    熔断标志（risk）一旦置位即拒绝新任务（挂起等人工确认；
    人工处理后 --reset-lock 清除，再重开）。
    """
    core = _core()
    if max_concurrent < 1:
        max_concurrent = 1
    try:
        core.os.makedirs(core.os.path.dirname(core.SCRAPE_LOCK_PATH), exist_ok=True)
    except OSError:
        return False
    # guard 内 RMW：读-存活过滤-写 全程互斥，杜绝 check-then-act 竞态
    result = {"acquired": False}

    def _do_acquire():
        max_c, holders, risk = core._read_scrape_lock()
        if risk:
            return  # 熔断中：必须人工确认（--reset-lock）后才能重开
        if core.os.path.exists(core.SCRAPE_LOCK_PATH):
            alive = [p for p in holders if core._pid_is_running(p)]
            if len(alive) >= max(max_c, max_concurrent):
                return
        else:
            alive = []
        core._write_scrape_lock(max_concurrent, alive + [str(core.os.getpid())])
        result["acquired"] = True

    if not core._with_scrape_lock_guard(_do_acquire):
        return False
    return result["acquired"]


def set_scrape_lock_risk():
    """置熔断标志：任一并发任务遇风控时广播全停（其他任务在页间检查并停止）。"""
    core = _core()

    def _do_set_risk():
        if not core.os.path.exists(core.SCRAPE_LOCK_PATH):
            return
        max_c, holders, risk = core._read_scrape_lock()
        if risk:
            return
        core._write_scrape_lock(max_c, holders, risk=True)

    try:
        core._with_scrape_lock_guard(_do_set_risk)
    except OSError:
        pass


def is_scrape_lock_risk():
    """读取当前锁文件是否已熔断（并发任务全停广播）。"""
    core = _core()
    try:
        if not core.os.path.exists(core.SCRAPE_LOCK_PATH):
            return False
        _, _, risk = core._read_scrape_lock()
        return risk
    except OSError:
        return False


def release_scrape_lock():
    """释放本进程持有的锁；只移除自己的 pid（他人 pid 保留）。

    熔断中（risk 已置位）保留锁文件（挂起等人工确认），不因持有者退出而删除；
    无 risk 且无剩余持有者时删除锁文件。
    """
    core = _core()
    try:
        if not core.os.path.exists(core.SCRAPE_LOCK_PATH):
            return
        max_c, holders, risk = core._read_scrape_lock()
        mine = str(core.os.getpid())
        rest = [p for p in holders if p != mine]
        if len(rest) == len(holders):
            return  # 锁里没有自己（他人/已清理），不动
        if rest or risk:
            # 仍有持有者，或熔断中需保留风险状态
            core._write_scrape_lock(max_c, rest, risk=risk)
        else:
            core.os.remove(core.SCRAPE_LOCK_PATH)
    except OSError:
        pass


def clear_scrape_lock_risk():
    """人工确认后清除熔断状态（--reset-lock）。

    仅清除 risk 标志；若锁文件内仍有存活持有者则保留（他人任务在跑不动）。
    """
    core = _core()
    try:
        if not core.os.path.exists(core.SCRAPE_LOCK_PATH):
            return True
        max_c, holders, risk = core._read_scrape_lock()
        if not risk:
            return True
        if holders:
            core._write_scrape_lock(max_c, holders, risk=False)
        else:
            core.os.remove(core.SCRAPE_LOCK_PATH)
        return True
    except OSError:
        return False
