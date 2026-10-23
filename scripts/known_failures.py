#!/usr/bin/env python3
"""已知坏页台账（跨 run、跨日持久化）。

为什么需要（2026-10-22）：实测每天固定有 3 个岗位的详情页抽不出 JD
（`invalid_detail`），工具**每天都重试**，每天白花 3 次详情页额度，而结果每次一样。

**但 2026-10-22 的独立审计证实这个前提是错的**：那 3 条绝大多数是**瞬时**失败
（SPA 还没渲染完 / 网络抖动），不是"这类页面的解析路径不适用"。实测形态：

- ``job description too short after validation: 38 < 120`` → 指纹 ``too_short``（瞬时）
- ``detail page rendered navigation chrome without a JD`` → 指纹 ``unknown``（瞬时）
- ``BOSS正在加载中`` → 指纹 ``shell_only``（瞬时）
- HTML 直取无响应 / HTTP 500 → 指纹 ``unknown``（瞬时）

把它们当"坏页"**永久**拉黑的后果：该岗位从此永远不出现在任何产出里
（不发请求、不进 pending、不计额度），控制台只有一句计数，**没有一行**说
"这条被永久拉黑了"。

修复后的口径（本文件）：

1. **结构性失败**（内容确实坏：结构/选择器不匹配、正文块取不到）→ 进**长期台账**，
   命中即跳过（不发请求、不消耗额度）；
2. **瞬时失败**（渲染没完成、网络抖动、登录/风控页）→ 只进**短期缓存**
   （run 作用域 + TTL）：**本轮内跳过、下一轮自动重试**；
3. 台账条目带 **TTL**（默认 30 天）与**最大跳过轮数**：超期/超次自动失效并清理，
   岗位重新出现——不会再"永远消失"；
4. ``--retry-job <job_id>`` **真正绕过台账**（此前是**假逃生口**：抓取路径上的
   ``is_known()`` 无条件命中，抓取根本到不了，pending 清单层面的白名单形同虚设）；
5. 跳过时控制台**打印一行**说明（job_id / 指纹 / 逃生口提示），不再只有计数。

台账文件在仓库外：``~/.boss-zhipin-scraper/known_bad_jobs.json``
（``BOSS_KNOWN_BAD_PATH`` 可覆盖）。

历史数据迁移：旧格式条目没有 ``nature`` 字段，**一律按瞬时处理**（等价于清空
历史黑名单）——旧台账里的条目正是被误判的瞬时失败，继续沿用等于继续伤害。
"""

import json
import os
import sys
import tempfile
import threading
import time
import uuid

try:
    from scripts import file_lock as _file_lock
except ImportError:  # pragma: no cover - 直接运行脚本时走此分支
    import file_lock as _file_lock

acquire_guard = _file_lock.acquire_guard
atomic_replace = _file_lock.atomic_replace

KNOWN_BAD_PATH_ENV = "BOSS_KNOWN_BAD_PATH"
STATE_DIR_ENV = "BOSS_STATE_DIR"
# 强制重试名单（除命令行 --retry-job 外的第二个入口，给批处理/子进程用）
RETRY_JOB_IDS_ENV = "BOSS_RETRY_JOB_IDS"

# ── 失败性质 ────────────────────────────────────────────────────────────────
STRUCTURAL = "structural"   # 结构性：内容确实坏 → 值得长期沉淀
TRANSIENT = "transient"     # 瞬时：渲染没完成 / 网络抖动 → 只本轮跳过

# 只有这些指纹算**结构性失败**。其余一律按**瞬时**处理（fail-open）：
# 认不出来的一律当瞬时——多试一次的代价（1 次详情额度）远小于岗位被永久拉黑的代价。
STRUCTURAL_FINGERPRINTS = frozenset({"no_section"})
# fail-open：只有明确的结构性指纹才进长期台账；认不出来的一律当瞬时。
# "too_short" 保持瞬时：页面可能没渲染完（瞬时），重试的代价（1 次详情额度）
# 远小于岗位被永久拉黑的代价。MAX_SKIP_RUNS=5 是安全阀。

# ── 台账时效 ────────────────────────────────────────────────────────────────
# 结构性条目的 TTL：**30 天**。
# 理由：结构性失败（结构/选择器不匹配）不会自己好，但平台改版与我们补兜底解析
# 都以"周"为单位；30 天足够覆盖"等平台修 / 等我们补解析"的窗口，同时保证一个
# 被误判成结构性的岗位**最迟 30 天后会被重新验证一次**（不会永远消失）。
KNOWN_BAD_TTL_DAYS = 30
KNOWN_BAD_TTL_SECONDS = KNOWN_BAD_TTL_DAYS * 24 * 3600

# 瞬时缓存的 TTL：**30 分钟**。
# 理由：SPA 渲染 / 网络抖动的恢复窗口是秒级~分钟级；30 分钟足够覆盖"同一轮里的
# 重复尝试"（HTML 直取失败回退渲染、批处理多任务），又不会把瞬时失败拖成长效黑名单。
# 真正的"本轮跳过、下一轮重试"由 run 作用域保证（见 _run_id）。
TRANSIENT_TTL_SECONDS = 30 * 60

# 单个条目最多被跳过多少**轮**：**5 轮**。
# 理由：这是"误判"的安全阀——即使指纹判断错了，最多连跳 5 轮就必须重新验证一次；
# 代价是每 5 轮多花 1 次详情请求，远小于"岗位永远消失"的代价。
MAX_SKIP_RUNS = 5

# 跨进程互斥（与 scripts/boss_cdp_raw.py 的 _with_scrape_lock_guard 同思路：
# O_CREAT|O_EXCL guard 文件原子创建，保证同一时刻只有一个进程做读-改-写）。
# 这里**不 import boss_cdp_raw**——它反过来 import 本模块，直接依赖会循环导入。
# 超时 30s / 陈旧阈值 20s：临界区只是读写一个小 JSON（含 fsync），20s 已是
# 正常耗时的几百倍；等待期间就能把崩溃残留的 guard 回收掉。
GUARD_TIMEOUT_SECONDS = 30.0
GUARD_STALE_SECONDS = 20.0
GUARD_POLL_SECONDS = 0.01

# os.replace 在 Windows 上会被并发写/读占用的目标文件顶掉（PermissionError
# [WinError 5]）——这是**并发冲突**不是错误，退避重试即可。
REPLACE_RETRIES = 8
REPLACE_BACKOFF_SECONDS = 0.02

_LOCK = threading.Lock()
_RUN_ID = None
_FORCED_IDS = set()
_ANNOUNCED = set()
_ARGV_CACHE = {"key": None, "ids": frozenset()}


def _state_dir():
    return os.environ.get(STATE_DIR_ENV) or os.path.expanduser(
        "~/.boss-zhipin-scraper")


def known_bad_path():
    """坏页台账文件路径。"""
    return os.environ.get(KNOWN_BAD_PATH_ENV) or os.path.join(
        _state_dir(), "known_bad_jobs.json")


def _run_id():
    """本次运行的标识（进程级）。

    同一进程内恒定、跨进程一定不同（带 uuid，避免 pid 复用造成的"下一轮误判成
    同一轮"）。瞬时失败只在**记录它的那一轮**内有效 → 下一轮自动重试。
    """
    global _RUN_ID
    if _RUN_ID is None:
        _RUN_ID = f"{os.getpid()}-{uuid.uuid4().hex[:12]}"
    return _RUN_ID


def _stamp(now=None):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(
        now if now is not None else time.time()))


def begin_new_run_scope():
    """切到新的"运行作用域"（瞬时失败只在本作用域内有效）。

    正常流程里"一个进程 = 一轮"；这个入口给测试用（模拟"下一轮"），
    也给将来"同进程内跑多轮"的调用方留个口子。
    """
    global _RUN_ID
    with _LOCK:
        _RUN_ID = None
    _ANNOUNCED.clear()
    return _run_id()


# ---------------------------------------------------------------------------
# 失败性质判定
# ---------------------------------------------------------------------------

def classify_failure_nature(fingerprint="", reason=""):
    """失败性质：**结构性**（内容确实坏）还是**瞬时**（渲染/网络抖动）。

    fail-open：只有**明确**的结构性指纹才进长期台账；认不出来的一律当瞬时。
    """
    fp = str(fingerprint or "").strip().lower()
    if fp in STRUCTURAL_FINGERPRINTS:
        return STRUCTURAL
    text = str(reason or "").lower()
    if "no_section" in fp or "no_section" in text:
        return STRUCTURAL
    return TRANSIENT


def _ttl_for(nature):
    return KNOWN_BAD_TTL_SECONDS if nature == STRUCTURAL else TRANSIENT_TTL_SECONDS


# ---------------------------------------------------------------------------
# 强制重试名单（--retry-job）：抓取路径真正绕过台账的逃生口
# ---------------------------------------------------------------------------

def set_forced_retry_ids(ids):
    """显式设置强制重试名单（程序内调用；测试用）。"""
    global _FORCED_IDS
    with _LOCK:
        _FORCED_IDS = {str(x).strip() for x in (ids or []) if str(x).strip()}
    _ARGV_CACHE["key"] = None


def clear_forced_retry_ids():
    """清空显式设置的强制重试名单。"""
    set_forced_retry_ids([])


def _ids_from_argv(argv=None):
    """从命令行参数里取 ``--retry-job <job_id>``（可重复 / 也支持 ``=`` 写法）。

    台账在抓取路径上被**无条件**询问，而 ``--retry-job`` 只作用在 pending 清单
    层面 → 抓取根本到不了，"强制重试"是假的。这里直接从 argv 读同一份名单，
    让两条路径用同一个真相来源。
    """
    args = tuple(sys.argv[1:] if argv is None else argv)
    cached = _ARGV_CACHE
    if cached["key"] == args:
        return cached["ids"]
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


def _ids_from_env():
    raw = os.environ.get(RETRY_JOB_IDS_ENV) or ""
    return {part.strip() for part in raw.replace(";", ",").split(",") if part.strip()}


def forced_retry_ids():
    """当前生效的强制重试名单：显式设置 + ``--retry-job`` 命令行 + 环境变量。"""
    with _LOCK:
        ids = set(_FORCED_IDS)
    ids.update(_ids_from_env())
    ids.update(_ids_from_argv())
    return ids


# ---------------------------------------------------------------------------
# 读写（跨进程互斥 + Windows 共享冲突重试）
# ---------------------------------------------------------------------------

def _guard_path(path=None):
    target = path or known_bad_path()
    return target + ".guard"


def _with_guard(action, path=None, timeout=GUARD_TIMEOUT_SECONDS):
    """用 guard 文件对台账读-改-写做**进程间**互斥（委托给 file_lock）。"""
    guard = _guard_path(path)
    directory = os.path.dirname(guard)
    if directory:
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError:
            return False
    try:
        with acquire_guard(guard, timeout=timeout,
                           stale_threshold=GUARD_STALE_SECONDS):
            action()
            return True
    except _file_lock.GuardAcquisitionError:
        return False


def _read(path=None):
    target = path or known_bad_path()
    try:
        with open(target, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(data, path=None):
    """原子写台账（tmp + fsync + os.replace；Windows 共享冲突退避重试）。"""
    target = path or known_bad_path()
    directory = os.path.dirname(target)
    if directory:
        os.makedirs(directory, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False)
    fd, tmp = tempfile.mkstemp(prefix=".known_bad-", dir=directory or None)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if atomic_replace(tmp, target):
            return True
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    try:
        os.remove(tmp)
    except OSError:
        pass
    raise OSError("台账写入失败")


def _is_expired(entry, now=None):
    """条目是否过了 TTL（缺 expires_at 的旧条目一律算过期 → 顺带清掉历史黑名单）。"""
    now = time.time() if now is None else now
    try:
        expires_at = float(entry.get("expires_at"))
    except (TypeError, ValueError):
        return True
    return now >= expires_at


def _nature_of(entry):
    """旧条目（无 nature）按**瞬时**处理 —— 等价于清空历史黑名单。"""
    nature = str(entry.get("nature") or "").strip().lower()
    return nature if nature in (STRUCTURAL, TRANSIENT) else TRANSIENT


def _is_active(entry, now=None):
    """条目此刻是否还"生效"（→ 该跳过这个岗位）。

    - 结构性：未过期，且跳过轮数未达上限；
    - 瞬时：未过期，**且是本轮记录的**（别的轮次一律当没失败过 → 下一轮重试）。
    """
    now = time.time() if now is None else now
    if _is_expired(entry, now):
        return False
    nature = _nature_of(entry)
    if nature == STRUCTURAL:
        return int(entry.get("skip_runs") or 0) < MAX_SKIP_RUNS
    return str(entry.get("run_id") or "") == _run_id()


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------

def record(job_id, fingerprint="", reason="", path=None):
    """记一条抽取失败。

    结构性 → 长期台账（TTL ``KNOWN_BAD_TTL_DAYS`` 天，最多跳 ``MAX_SKIP_RUNS`` 轮）；
    瞬时   → 短期缓存（run 作用域 + ``TRANSIENT_TTL_SECONDS``），下一轮自动重试。

    返回写入的条目（失败时 None）。
    """
    key = str(job_id or "").strip()
    if not key:
        return None
    fp = str(fingerprint or "").strip()
    nature = classify_failure_nature(fp, reason)
    written = {"entry": None}

    def _action():
        data = _read(path)
        # 顺手清掉过期条目（TTL 到期的瞬时缓存别一直堆在文件里）
        now = time.time()
        for stale in [k for k, v in data.items()
                      if isinstance(v, dict) and _is_expired(v, now)]:
            data.pop(stale, None)
        entry = data.get(key)
        if not isinstance(entry, dict):
            entry = {}
        if _nature_of(entry) != nature:
            # 性质变了（瞬时 → 结构性，或反过来）→ 跳过计数重来
            entry["skip_runs"] = 0
            entry["skips"] = 0
        entry["fingerprint"] = fp or str(entry.get("fingerprint") or "")
        entry["reason"] = str(reason or entry.get("reason") or "")[:300]
        entry["nature"] = nature
        entry["first_seen"] = entry.get("first_seen") or _stamp(now)
        entry["last_seen"] = _stamp(now)
        entry["hits"] = int(entry.get("hits") or 0) + 1
        entry["expires_at"] = now + _ttl_for(nature)
        entry["skip_runs"] = int(entry.get("skip_runs") or 0)
        entry["skips"] = int(entry.get("skips") or 0)
        if nature == TRANSIENT:
            entry["run_id"] = _run_id()
        else:
            entry.pop("run_id", None)
        data[key] = entry
        _write(data, path)
        written["entry"] = entry

    with _LOCK:
        _with_guard(_action, path)
    return written["entry"]


def is_known(job_id, path=None):
    """该岗位是否应当跳过（结构性台账命中 / 本轮已瞬时失败）。

    ``--retry-job`` 指定的 job_id **直接放行**（真正的强制重试逃生口）。
    """
    key = str(job_id or "").strip()
    if not key:
        return False
    if key in forced_retry_ids():
        return False
    decision = {"skip": False, "entry": None}

    def _action():
        data = _read(path)
        entry = data.get(key)
        if not isinstance(entry, dict):
            return
        now = time.time()
        expired = _is_expired(entry, now)
        active = (not expired) and _is_active(entry, now)
        if not active:
            # 过期 / 跳过轮数用尽 → 清理条目：岗位重新出现（不再永远消失）
            if expired or _nature_of(entry) == STRUCTURAL:
                data.pop(key, None)
                _write(data, path)
            return
        run = _run_id()
        if str(entry.get("last_skip_run") or "") != run:
            entry["skip_runs"] = int(entry.get("skip_runs") or 0) + 1
            entry["last_skip_run"] = run
        entry["skips"] = int(entry.get("skips") or 0) + 1
        entry["last_skip_at"] = _stamp(now)
        data[key] = entry
        _write(data, path)
        decision["skip"] = True
        decision["entry"] = entry

    with _LOCK:
        _with_guard(_action, path)
    if decision["skip"]:
        _announce_skip(key, decision["entry"])
    return decision["skip"]


def _announce_skip(job_id, entry):
    """跳过时打印**一行**说明（不能只有计数）。"""
    entry = entry or {}
    marker = (_run_id(), job_id)
    if marker in _ANNOUNCED:
        return
    _ANNOUNCED.add(marker)
    fingerprint = str(entry.get("fingerprint") or "unknown")
    nature = _nature_of(entry)
    label = "结构性坏页" if nature == STRUCTURAL else "瞬时失败"
    print(f"⏭️  坏页跳过：{job_id} 该页此前被判为坏页（{label} / 指纹 {fingerprint}），"
          f"已跳过；如需重试用 --retry-job {job_id}")


def get(job_id, path=None):
    """取坏页记录（不存在返回 None）。"""
    return _read(path).get(str(job_id or "").strip())


def fingerprint_counts(path=None):
    """按失败指纹统计（用于判断"哪一类页面结构需要补兜底解析"）。"""
    counts = {}
    for entry in _read(path).values():
        if not isinstance(entry, dict):
            continue
        if not _is_active(entry):
            continue
        key = str(entry.get("fingerprint") or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return counts


def summary(path=None):
    """台账概览：生效条目数 + 按指纹分布 + 按性质分布。"""
    data = _read(path)
    active = [entry for entry in data.values()
              if isinstance(entry, dict) and _is_active(entry)]
    counts = {}
    for entry in active:
        key = str(entry.get("fingerprint") or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return {
        "total": len(active),
        "by_fingerprint": counts,
        "structural": sum(1 for e in active if _nature_of(e) == STRUCTURAL),
        "transient": sum(1 for e in active if _nature_of(e) == TRANSIENT),
    }


def clear(path=None):
    """清空台账（解析兜底补上后用）。"""
    with _LOCK:
        _with_guard(lambda: _write({}, path), path)
