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

另外台账记录**今天已抓过的 job_id**：同一岗位当天第二次抓取默认**跳过且不消耗额度**
（2026-10-21 实测教训：多次实跑各写独立的 --detail-output 文件，跨 run 的
"跳过已抓详情"按输出文件去重因而完全失效，同一批 97 个岗位被抓了约 7.5 次，
700 的日额度只换来 97 个岗位）。有了台账，日额度必然换来**互不重复**的岗位。
DOM 渲染通道一次岗位 = 1 次页面导航；HTML 直取通道一次岗位 = 1 次 fetch；
直取失败回退渲染时同一岗位会发 2 次请求，两次都要计。

**2026-10-22 审计修复（并发/状态类）**：此前本模块只有**进程内** `threading.Lock`
+ 读改写，**没有跨进程文件锁**。实测 4 个进程各消费 60 次 → 3 个进程在
``os.replace`` 上抛 ``PermissionError: [WinError 5]``（Windows 上目标文件被
别的进程开着时 replace 会失败），落盘计数只有 **60/240**。后果链：

    PermissionError → 冒到 boss_cdp_raw._consume_detail_page_budget
                   → 被宽泛的 ``_cdp_exception_types()``（含 OSError）吞成
                     ``reason="cdp_session"``
                   → 连续 3 次就 ``mark_cdp_cooldown()`` + 停手（阈值 3，冷却 300s）

也就是说：**浏览器其实没问题，诊断信息是错的**；同时预算少算 → **真实详情请求数
可能超出平台日额度**。

本次修复：
1. 读改写套上**跨进程**锁（O_CREAT|O_EXCL guard 文件，与
   ``boss_cdp_raw._with_scrape_lock_guard`` 同思路；**不复用**那个函数是为了避免
   循环依赖——boss_cdp_raw 反过来 import 本模块）；
2. ``os.replace`` 的 PermissionError 当成**并发冲突** → 退避重试（不抛出去，
   抛出去会被 CDP 异常族误判成"浏览器故障"）；
3. 文件里维护**单调递增的 seq（版本号）**，检测到回退/计数倒退就告警
   （并发写覆盖导致计数丢失必须能被发现，不能静默）；
4. 进程内保留"最后一次成功快照"，读失败/写失败时**绝不让计数倒退**
   （计数倒退 = 额度被低估 = 可能超平台日额度）。

文件位置：仓库外 ``~/.boss-zhipin-scraper/daily_budget.json``（与审计日志同目录），
``BOSS_DAILY_BUDGET_PATH`` 可覆盖（测试/多环境隔离）。
"""

import json
import logging
import os
import sys
import tempfile
import threading
import time

DEFAULT_DAILY_DETAIL_BUDGET = 700
BUDGET_PATH_ENV = "BOSS_DAILY_BUDGET_PATH"
STATE_DIR_ENV = "BOSS_STATE_DIR"
# 强制重试名单（与 --retry-job 同一份名单；见 _forced_retry_ids）
RETRY_JOB_IDS_ENV = "BOSS_RETRY_JOB_IDS"

# ── 跨进程互斥 ──────────────────────────────────────────────────────────────
# guard 文件用 O_CREAT|O_EXCL 原子创建（= 互斥），思路与
# scripts/boss_cdp_raw.py 的 _with_scrape_lock_guard 完全一致。
# 之所以在这里写一份**等价最小实现**而不 import boss_cdp_raw：boss_cdp_raw
# 反过来 import 本模块，直接依赖会形成循环依赖。
#
# 超时取值：临界区 = 读一个小 JSON + 写一个小 JSON（含 fsync）。实测本机
# 空闲时约 5-50ms、机器繁忙时可达数百毫秒。120s 的等待上限比"正常竞争"高
# 两三个数量级（多进程各消费几十次时完全够用），又不会让一次崩溃残留的
# guard 卡住太久（陈旧阈值 60s < 超时 → 等待期间就会被回收）。
LOCK_TIMEOUT_SECONDS = 120.0
LOCK_POLL_SECONDS = 0.01
LOCK_STALE_SECONDS = 60.0

# ── Windows 共享冲突重试 ────────────────────────────────────────────────────
# 目标文件正被别的进程打开时 os.replace 抛 PermissionError [WinError 5]：
# 这是**并发冲突**不是错误 → 退避重试即可。
REPLACE_RETRIES = 8
REPLACE_BACKOFF_SECONDS = 0.02

# 读文件同样可能被共享冲突顶掉 → 退避重试
READ_ATTEMPTS = 5

log = logging.getLogger(__name__)

# 最近的状态告警（排障/测试可见；不抛异常中断抓取）
WARNINGS = []

_LOCK = threading.Lock()
_LAST_GOOD = {}   # path → 最后一次成功读到的计数快照（绝不倒退用）
_LAST_SEQ = {}    # path → 见过的最大 seq（版本回退检测用）
_ARGV_CACHE = {"key": None, "ids": frozenset()}


def _warn(message):
    """记一条状态告警（计数丢失/文件不可读等）：写 WARNINGS + 日志，不中断抓取。"""
    text = str(message)
    WARNINGS.append(text)
    log.warning("%s", text)
    return text


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


# ---------------------------------------------------------------------------
# 强制重试名单（--retry-job）：让"强制重试"对日额度去重也真正生效
# ---------------------------------------------------------------------------

def _ids_from_argv(argv=None):
    """从命令行参数取 ``--retry-job <job_id>``（可重复 / 也支持 ``=`` 写法）。"""
    args = tuple(sys.argv[1:] if argv is None else argv)
    if _ARGV_CACHE["key"] == args:
        return _ARGV_CACHE["ids"]
    ids = set()
    index = 0
    while index < len(args):
        arg = str(args[index])
        if arg == "--retry-job":
            if index + 1 < len(args):
                value = str(args[index + 1]).strip()
                if value and not value.startswith("-"):
                    ids.add(value)
                    index += 1
        elif arg.startswith("--retry-job="):
            value = arg.split("=", 1)[1].strip()
            if value:
                ids.add(value)
        index += 1
    result = frozenset(ids)
    _ARGV_CACHE["key"] = args
    _ARGV_CACHE["ids"] = result
    return result


def _forced_retry_ids():
    """当前生效的强制重试名单（``--retry-job`` + 环境变量）。

    与 ``scripts/known_failures.py`` 的同名机制是**同一个真相来源**（同一个 CLI
    flag）。这里刻意不 import 那个模块：日预算与坏页台账是两个独立的纯逻辑模块，
    互相 import 会把"稳定纯逻辑"的边界搅在一起（bad_page 台账已经 import 语义层，
    日预算再 import 台账就是反向依赖）。
    """
    raw = os.environ.get(RETRY_JOB_IDS_ENV) or ""
    ids = {part.strip() for part in raw.replace(";", ",").split(",") if part.strip()}
    ids.update(_ids_from_argv())
    return ids


# ---------------------------------------------------------------------------
# 计数文件读写
# ---------------------------------------------------------------------------

def _load_raw(target):
    """读原始计数文件。

    返回 ``(data, error)``：``data`` 是 dict 或 None；``error`` 取值
    ``""``（成功）/ ``"missing"``（文件不存在）/ ``"corrupt"``（内容坏了）/
    ``"io"``（读不出来：Windows 共享冲突等）。
    """
    delay = 0.01
    for _ in range(READ_ATTEMPTS):
        try:
            with open(target, encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return None, "missing"
        except json.JSONDecodeError:
            return None, "corrupt"
        except (OSError, ValueError, TypeError):
            # 共享冲突/占用 → 退避重试（不是错误）
            time.sleep(delay)
            delay = min(delay * 2, 0.2)
            continue
        if not isinstance(data, dict):
            return None, "corrupt"
        return data, ""
    return None, "io"


def _read_state(path=None):
    """内部读：返回 ``(state, error)``。

    ``state`` = ``{"date", "detail", "jobs", "seq"}``；``seq`` 是**单调递增**的
    版本号（每次落盘 +1），用来发现"计数被并发写覆盖/丢失"。
    """
    target = path or budget_path()
    data, error = _load_raw(target)
    if data is None:
        snapshot = _LAST_GOOD.get(target)
        if snapshot is not None:
            _warn(f"日预算计数文件读取失败（{error}），沿用进程内最后快照"
                  f"（detail={snapshot.get('detail')}）")
            return dict(snapshot), error
        if error == "io":
            _warn(f"日预算计数文件读取失败（{error}），计数不可用")
        return {"date": today_key(), "detail": 0, "jobs": [], "seq": 0}, error

    date = str(data.get("date") or "")
    try:
        count = int(data.get("detail", 0))
    except (TypeError, ValueError):
        count = 0
    jobs = data.get("jobs")
    if not isinstance(jobs, list):
        jobs = []
    try:
        seq = int(data.get("seq") or 0)
    except (TypeError, ValueError):
        seq = 0

    local = _LAST_GOOD.get(target)
    if local is not None:
        local_seq = int(local.get("seq") or 0)
        if seq < local_seq:
            _warn(f"日预算计数文件版本回退（seq {seq} < 本地 {local_seq}）"
                  "：疑似并发写覆盖，计数可能已丢失")
        if date == str(local.get("date") or ""):
            if count < int(local.get("detail") or 0):
                _warn(f"日预算计数回退（{count} < 本地 {local.get('detail')}）"
                      "：疑似并发写覆盖，计数可能已丢失")
            # 本地快照是"至少已经记过"的量 → 取大者，**绝不让额度被低估**
            count = max(count, int(local.get("detail") or 0))
            merged = list(jobs)
            for job in local.get("jobs") or []:
                if job not in merged:
                    merged.append(job)
            jobs = merged
        seq = max(seq, local_seq)

    if date != today_key():
        # 跨自然日 → 计数归零（平台侧也是按日），seq 保持单调
        state = {"date": today_key(), "detail": 0, "jobs": [], "seq": seq}
    else:
        state = {"date": date, "detail": max(count, 0),
                 "jobs": [str(j) for j in jobs], "seq": seq}
    _LAST_GOOD[target] = dict(state)
    if seq > _LAST_SEQ.get(target, 0):
        _LAST_SEQ[target] = seq
    return state, ""


def read_state(path=None):
    """读取当前计数：``{"date": "2026-10-21", "detail": 128, "jobs": [...], "seq": 7}``。

    文件缺失/损坏/非当日 → 视为空计数（``detail`` 归零），**不因文件问题中断抓取**；
    读不出来（共享冲突等）→ 沿用进程内最后快照，**绝不返回 0**（返回 0 = 额度被
    低估 = 可能超平台日额度）。
    """
    state, _ = _read_state(path)
    return state


def _write_state(state, path=None):
    """原子写计数文件（tmp + fsync + os.replace；共享冲突退避重试）。

    写失败**不抛异常**：抛 OSError 会被 boss_cdp_raw 的 ``_cdp_exception_types()``
    吞成 ``reason="cdp_session"``（误判成浏览器故障，正是本次要修的坑）。
    写失败时把计数留在进程内快照里（读时取大者）→ 本进程额度口径仍然保守。
    """
    target = path or budget_path()
    directory = os.path.dirname(target)
    if directory:
        os.makedirs(directory, exist_ok=True)
    payload = dict(state)
    payload["seq"] = int(payload.get("seq") or 0) + 1
    data = json.dumps(payload, ensure_ascii=False)
    delay = REPLACE_BACKOFF_SECONDS
    last_error = None
    for _ in range(REPLACE_RETRIES):
        fd, tmp = tempfile.mkstemp(prefix=".daily_budget-", dir=directory or None)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, target)
            _LAST_GOOD[target] = dict(payload)
            if payload["seq"] > _LAST_SEQ.get(target, 0):
                _LAST_SEQ[target] = payload["seq"]
            return payload
        except PermissionError as exc:
            # 并发占用（别的进程正开着目标文件）→ 退避重试，不是错误
            last_error = exc
            try:
                os.remove(tmp)
            except OSError:
                pass
            time.sleep(delay)
            delay = min(delay * 2, 0.5)
            continue
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
    _warn(f"日预算计数写入失败（{last_error}）：本进程计数留在内存快照里，"
          "跨进程口径可能偏低（请检查状态目录权限/占用）")
    _LAST_GOOD[target] = dict(payload)
    return payload


# ---------------------------------------------------------------------------
# 跨进程互斥
# ---------------------------------------------------------------------------

def _lock_path(target):
    return target + ".lock"


def _with_file_lock(action, path=None, timeout=LOCK_TIMEOUT_SECONDS):
    """用 O_CREAT|O_EXCL guard 文件对计数文件的读-改-写做**进程间**互斥。

    与 ``boss_cdp_raw._with_scrape_lock_guard`` 同思路：guard 原子创建保证同一
    时刻只有一个进程做 RMW；抢不到就自旋等待（带微小抖动）。

    返回 ``True``（已执行 action）或 ``False``（抢不到锁——调用方必须**保守
    处理**，不能当作"计数为 0"继续）。action 的结果通过闭包回传。
    """
    target = path or budget_path()
    guard = _lock_path(target)
    directory = os.path.dirname(guard)
    if directory:
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError:
            return False
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(guard, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            break
        except FileExistsError:
            try:
                age = time.time() - os.path.getmtime(guard)
            except OSError:
                age = 0.0
            if age > LOCK_STALE_SECONDS:
                # 陈旧 guard（持锁进程崩溃残留）→ 清掉再抢
                try:
                    os.remove(guard)
                except OSError:
                    pass
                continue
            if time.monotonic() >= deadline:
                return False
            time.sleep(LOCK_POLL_SECONDS)
        except OSError:
            return False
    try:
        action()
        return True
    finally:
        try:
            os.remove(guard)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------

def already_fetched(job_id, path=None):
    """该岗位今天是否已经抓过（抓过就不再花额度重抓）。"""
    key = str(job_id or "").strip()
    if not key:
        return False
    return key in set(read_state(path)["jobs"])


def consume(n=1, limit=DEFAULT_DAILY_DETAIL_BUDGET, path=None, job_id=None):
    """记一次详情页请求；返回 ``(allowed, used, limit, reason)``。

    - ``reason == "duplicate"``：该岗位今天已抓过 → **不消耗额度**，调用方应跳过
      （``allowed`` 为 True，避免被当成"预算用尽"而停手）
    - ``reason == "exhausted"``：日预算用满 → ``allowed`` False，调用方应停止本轮
    - ``reason == "budget_lock_unavailable"``：抢不到跨进程锁 → 保守拒绝（宁可少抓，
      也不能让真实请求数超出平台日额度）
    - ``reason == "budget_state_unavailable"``：计数文件读不出来 → 保守拒绝
    - ``reason == ""``：正常计入

    计数按请求次数累加（直取 + 渲染回退各算一次）。
    ``--retry-job`` 指定的岗位**不受"当天已抓"限制**（强制重试要真能重抓）。
    """
    key = str(job_id or "").strip()
    forced = bool(key) and key in _forced_retry_ids()
    outcome = {"value": (False, 0, limit, "budget_lock_unavailable")}

    def _action():
        state, error = _read_state(path)
        if error == "io" and not state.get("jobs") and not state.get("detail"):
            # 读不到磁盘计数又没有可用快照 → 保守拒绝（额度不能靠猜）
            _warn("日预算计数不可读，本轮拒绝消耗额度（不冒险超平台日额度）")
            outcome["value"] = (False, 0, limit, "budget_state_unavailable")
            return
        jobs = list(state.get("jobs") or [])
        if key and not forced and key in set(jobs):
            outcome["value"] = (True, state["detail"], limit, "duplicate")
            return
        used = state["detail"] + max(int(n), 0)
        if key and key not in set(jobs):
            jobs.append(key)
        if limit is None or limit <= 0:
            _write_state({"date": state["date"], "detail": used, "jobs": jobs,
                          "seq": state.get("seq")}, path)
            outcome["value"] = (True, used, 0, "")
            return
        if used > limit:
            # 超额**不落盘**（避免把超限尝试也计入配额，放大误差）
            outcome["value"] = (False, state["detail"], limit, "exhausted")
            return
        _write_state({"date": state["date"], "detail": used, "jobs": jobs,
                      "seq": state.get("seq")}, path)
        outcome["value"] = (True, used, limit, "")

    with _LOCK:
        acquired = _with_file_lock(_action, path)
    if not acquired:
        _warn("日预算跨进程锁获取失败（另一个进程长时间持锁），本轮拒绝消耗额度")
        if limit is None or limit <= 0:
            return True, 0, 0, ""
        return False, 0, limit, "budget_lock_unavailable"
    return outcome["value"]


def remaining(limit=DEFAULT_DAILY_DETAIL_BUDGET, path=None):
    """今日剩余可用详情页请求数。"""
    if limit is None or limit <= 0:
        return -1  # 不限
    return max(limit - read_state(path)["detail"], 0)


def reset(path=None):
    """清零今日计数与台账（人工排障用；正常流程不需要）。"""
    target = path or budget_path()
    with _LOCK:
        def _action():
            state, _ = _read_state(target)
            _write_state({"date": today_key(), "detail": 0, "jobs": [],
                          "seq": state.get("seq")}, target)
            _LAST_GOOD.pop(target, None)
        _with_file_lock(_action, target)
