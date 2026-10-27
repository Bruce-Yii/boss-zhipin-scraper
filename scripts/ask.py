#!/usr/bin/env python3
"""交互层（L3）：agent 会话里的只读问答 API。

用户**只在对话里提问**（"今天怎么样""这条为什么没上榜""跟昨天比呢"），由 agent
调这里的函数拿结构化答案，再翻译成人话。因此这里**不是用户 CLI**：不产出
xlsx/固定报告文件/Web 页面；``human`` 只是给 agent 引用的一屏 markdown，不是
交付物。C（CLI 编排层）只通过 ``run_ask`` 转发 ``--ask``，不承担解释逻辑。

契约
----
所有命令返回同一个信封::

    {"ok": bool, "command": str, "data": dict, "human": str, "errors": [str]}

- ``ok=True``：命令给出了回答（可能带"暂无数据/降级"说明）；
- ``ok=False``：命令无法回答（未知命令/缺参数/查无此岗/核心数据源完全缺失），
  ``errors`` 说明原因；**任何情况下都不抛异常**；
- ``errors`` 同时收集所有降级项（缺文件/缺库/旧 schema），即使 ``ok=True``；
- ``human`` 是一屏 markdown（给 agent 读，可摘要转述），不是给用户敲命令的说明。

只读保证
--------
不抓取、不发 BOSS 请求、不写产物/清单/DB：``artifacts.build_index`` 只扫描磁盘
（不落 ``artifacts.json``），SQLite 用 ``mode=ro`` 只读 URI 打开，预算/运行状态
只读文件。默认只对本机 CDP 端口做一次 HTTP 探测（``probe_cdp=False`` 可关）；
**登录态不做探测**（那会发 BOSS 请求，交给 ``--check``），避免用户只是在问问题时
触发平台风控。

命令
----
- ``status``   单屏状态：运行/预算/锁/冷却/CDP + 今日新增/详情 + 最新交付物（较上版）
               + 异常 + 待拍板（口径边界）；
- ``today``    今日简报（复用 ``brief.collect`` 的聚合，结构化返回）；
- ``why``      某个 job_id 为什么留/删：所在交付物、剔除原因、人工剔除名单、
               规则版本、来源批次（DB run lineage）、证据词；拿不到就列出查过哪些
               来源，不编造原因；
- ``diff``     最新交付物 vs 同口径上一版：逐条 +新增/−消失/保持 + 原因；
- ``templates``日常模板：名称/别名/口径/交付物/上次运行；
- ``batches``  批次健康度（``batch_health.jsonl``，可能尚未生成）。

降级策略（旧库/缺文件）
----------------------
SQLite 只读打开后先看 ``sqlite_master`` 与 ``PRAGMA table_info``：没有
``schema_meta``/``keyword``/``city``/``jd`` 等新旧列时只查存在的列，绝不让
"旧 schema"变成异常；产物目录不存在时返回空数据 + ``errors``。
"""

import json
import os
import pathlib
import sqlite3
import sys
import time
from datetime import datetime

try:
    from scripts import artifacts as _artifacts
    from scripts import brief as _brief
    from scripts import daily_budget as _daily_budget
    from scripts import run_state as _run_state
    from scripts import semantics as _semantics
except ImportError:  # pragma: no cover - 直接运行脚本时走此分支
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import artifacts as _artifacts
    import brief as _brief
    import daily_budget as _daily_budget
    import run_state as _run_state
    import semantics as _semantics

DEFAULT_STATE_DIR = os.path.expanduser("~/.boss-zhipin-scraper")
DEFAULT_CDP_PORT = 45222
COMMANDS = ("status", "today", "why", "diff", "templates", "batches")
DEFAULT_DIFF_LIMIT = 50
DEFAULT_BATCH_LIMIT = 10
MAX_LIMIT = 200
RISK_LINE = "risk"
# 状态/健康度文件（与 boss_cdp_raw / daily 的默认落盘位置一致，env 可覆盖隔离）
LOCK_NAME = "scrape.lock"
COOLDOWN_NAME = "cdp.cooldown"
BATCH_HEALTH_NAME = "batch_health.jsonl"


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def _now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _state_dir():
    return os.path.abspath(os.path.expanduser(
        os.environ.get("BOSS_STATE_DIR") or DEFAULT_STATE_DIR))


def _result_dir(result_dir=None):
    raw = result_dir or os.environ.get("BOSS_RESULT_DIR") or _artifacts.default_result_dir()
    return os.path.abspath(os.path.expanduser(str(raw)))


def _db_path(db_path=None):
    raw = db_path or os.environ.get("BOSS_DB_PATH") or os.path.join(_state_dir(), "boss.db")
    return os.path.abspath(os.path.expanduser(str(raw)))


def _read_json(path):
    """读 JSON；缺失/损坏/非 UTF-8 返回 ``None``（调用方决定降级方式）。"""
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def _load_index(result_dir):
    """扫产物目录生成清单（``build_index`` 只扫描，不写 ``artifacts.json``）。

    刻意不读缓存：agent 要回答的是"此刻"的状态，缓存可能落后于刚重建的交付物。
    实测 170 个文件约 1 秒，对问答频率可接受。
    """
    try:
        return _artifacts.build_index(result_dir), ""
    except (OSError, TypeError, ValueError, KeyError) as exc:
        return None, f"产物清单生成失败：{exc}"


def _safe_int(value, default=0, minimum=None, maximum=None):
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    if minimum is not None:
        number = max(number, minimum)
    if maximum is not None:
        number = min(number, maximum)
    return number


def _coerce_limit(value, default, maximum=MAX_LIMIT):
    return _safe_int(value, default, minimum=1, maximum=maximum)


def _fmt_time(value):
    text = str(value or "").strip()
    if not text:
        return "未知"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return text
    return moment.astimezone().strftime("%Y-%m-%d %H:%M")


def _fmt_duration(seconds):
    if seconds is None:
        return "未知"
    try:
        total = max(int(seconds), 0)
    except (TypeError, ValueError):
        return "未知"
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m"
    return f"{total / 3600:.1f}h"


def _answer(command, data, human, errors=None, ok=True):
    """统一信封：ok/command/data/human/errors（agent 只依赖这五个键）。"""
    return {
        "ok": bool(ok),
        "command": str(command),
        "data": data if isinstance(data, dict) else {"value": data},
        "human": str(human or ""),
        "errors": [str(item) for item in (errors or []) if str(item).strip()],
    }


def _note(errors, message):
    """记录一条降级项（不抛异常）。"""
    text = str(message or "").strip()
    if text and text not in errors:
        errors.append(text)
    return errors


# ---------------------------------------------------------------------------
# 运行态：运行状态 / 锁 / 冷却 / 预算 / CDP
# ---------------------------------------------------------------------------

def _run_view():
    state = _run_state.read()
    if not state:
        return {"state": "unknown", "running": False, "phase": "unknown", "stale": True,
                "progress_pct": None, "age_seconds": None, "message": "没有运行状态记录"}
    return _run_state.progress_view(state)


def _compact_run(view):
    keys = ("state", "running", "phase", "progress_pct", "stale", "age_seconds",
            "message", "started_at", "updated_at", "keyword", "city",
            "keywords_total", "keyword_index", "pages_done", "pages_total",
            "detail_done", "detail_total", "detail_ok", "detail_failed",
            "jobs_found", "jobs_kept", "budget_used", "budget_limit", "deliverable")
    return {key: view.get(key) for key in keys}


def _read_lock(errors):
    path = os.path.join(_state_dir(), LOCK_NAME)
    info = {"path": path, "held": False, "holders": [], "max_concurrent": 1,
            "risk": False, "detail": ""}
    try:
        with open(path, encoding="utf-8") as handle:
            lines = [line.strip() for line in handle.read().splitlines() if line.strip()]
    except FileNotFoundError:
        info["detail"] = "无锁文件（当前没有任务持锁）"
        return info
    except (OSError, UnicodeDecodeError) as exc:
        info["detail"] = f"锁文件读取失败：{exc}"
        _note(errors, info["detail"])
        return info
    if not lines:
        info["detail"] = "锁文件为空"
        return info
    info["max_concurrent"] = _safe_int(lines[0], 1, minimum=1)
    info["risk"] = RISK_LINE in lines
    info["holders"] = [line for line in lines[1:] if line != RISK_LINE]
    info["held"] = bool(info["holders"])
    if info["risk"]:
        info["detail"] = "熔断挂起（任一并发任务遇风控后全停，需人工确认）"
    elif info["held"]:
        info["detail"] = f"被 {len(info['holders'])} 个进程持有（存活未校验）"
    else:
        info["detail"] = "空闲"
    return info


def _read_cooldown(errors):
    path = os.path.join(_state_dir(), COOLDOWN_NAME)
    info = {"path": path, "active": False, "remaining_seconds": 0,
            "recovery_seconds": 0, "detail": ""}
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        deadline = float(lines[0])
        recovery_until = float(lines[1]) if len(lines) > 1 else deadline
    except FileNotFoundError:
        info["detail"] = "无冷却标记"
        return info
    except (OSError, UnicodeDecodeError, ValueError, IndexError) as exc:
        info["detail"] = f"冷却标记读取失败：{exc}"
        _note(errors, info["detail"])
        return info
    now = time.time()
    remaining = max(int(deadline - now), 0)
    info["remaining_seconds"] = remaining
    info["recovery_seconds"] = max(int(recovery_until - now), 0)
    info["active"] = remaining > 0
    if info["active"]:
        info["detail"] = f"冷却中，剩 {remaining}s"
    elif info["recovery_seconds"] > 0:
        info["detail"] = f"冷却结束，恢复期限速中（剩 {info['recovery_seconds']}s）"
    else:
        info["detail"] = "无冷却（残留标记，下次运行会清理）"
    return info


def _read_budget(errors):
    limit = _safe_int(getattr(_daily_budget, "DEFAULT_DAILY_DETAIL_BUDGET", 700),
                      700, minimum=0)
    try:
        state = _daily_budget.read_state()
        used = _safe_int(state.get("detail"), 0, minimum=0)
        date = str(state.get("date") or "")
    except (OSError, TypeError, ValueError) as exc:
        used, date = 0, ""
        _note(errors, f"详情日预算读取失败：{exc}")
    return {
        "used": used,
        "limit": limit,
        "remaining": max(limit - used, 0) if limit else 0,
        "date": date,
    }


def _probe_cdp(port=DEFAULT_CDP_PORT):
    """本机 CDP 端口探测（只连 127.0.0.1，不发 BOSS 请求；失败不抛）。"""
    info = {"probed": True, "reachable": False, "port": port, "browser": "", "detail": ""}
    import urllib.request  # 局部导入：ask 默认不依赖网络栈

    url = f"http://127.0.0.1:{port}/json/version"
    try:
        with urllib.request.urlopen(url, timeout=0.6) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except (OSError, ValueError) as exc:
        info["detail"] = f"{type(exc).__name__}: {exc}"
        return info
    info["reachable"] = True
    info["browser"] = str(payload.get("Browser") or "") if isinstance(payload, dict) else ""
    return info


# ---------------------------------------------------------------------------
# SQLite 只读（旧 schema 容错）
# ---------------------------------------------------------------------------

def _open_db_readonly(db_path):
    if not os.path.exists(db_path):
        raise FileNotFoundError(db_path)
    uri = pathlib.Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _tables(conn):
    return {str(row[0]) for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(conn, table):
    try:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _count(conn, table):
    try:
        row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    except sqlite3.Error:
        return 0
    return _safe_int(row[0]) if row is not None else 0


def _db_stats(db_path, errors):
    """原材料库只读统计（表/列缺失都降级为 0 或 None，不抛异常）。"""
    stats = {"path": db_path, "ok": False, "schema_version": None, "jobs": 0,
             "details": 0, "details_with_jd": 0, "runs": 0, "last_job_update": None,
             "error": ""}
    if not os.path.exists(db_path):
        stats["error"] = "数据库不存在"
        _note(errors, f"原材料库不存在：{db_path}（数字来自文件与台账）")
        return stats
    conn = None
    try:
        conn = _open_db_readonly(db_path)
        tables = _tables(conn)
        if "schema_meta" in tables:
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
            if row is not None:
                stats["schema_version"] = _safe_int(row[0], 0)
        if "jobs" in tables:
            stats["jobs"] = _count(conn, "jobs")
            jobs_cols = _columns(conn, "jobs")
            if "updated_at" in jobs_cols:
                row = conn.execute("SELECT MAX(updated_at) FROM jobs").fetchone()
                stats["last_job_update"] = row[0] if row is not None else None
        else:
            _note(errors, "原材料库是旧 schema：没有 jobs 表")
        if "details" in tables:
            stats["details"] = _count(conn, "details")
            detail_cols = _columns(conn, "details")
            if "jd" in detail_cols:
                row = conn.execute(
                    "SELECT COUNT(*) FROM details "
                    "WHERE jd IS NOT NULL AND TRIM(jd) != ''").fetchone()
                stats["details_with_jd"] = _safe_int(row[0]) if row is not None else 0
        if "runs" in tables:
            stats["runs"] = _count(conn, "runs")
        stats["ok"] = True
    except (OSError, sqlite3.Error) as exc:
        stats["error"] = f"{type(exc).__name__}: {exc}"
        _note(errors, f"原材料库只读打开失败：{exc}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
    return stats


def _db_lookup_job(db_path, job_id, errors):
    """按 job_id 查库：岗位记录 + 来源批次（runs lineage）+ 详情 JD。

    旧库可能没有 ``jobs.keyword/city`` 或 ``schema_meta``，只查存在的列。
    任何错误都降级成 ``error`` 字段，不影响其它证据。
    """
    info = {"path": db_path, "found": False, "job": None, "first_seen_at": "",
            "updated_at": "", "run_id": "", "run": None, "has_jd": False,
            "jd_len": 0, "detail_updated_at": "", "error": ""}
    if not os.path.exists(db_path):
        info["error"] = "数据库不存在"
        _note(errors, f"原材料库不存在，无法核对来源批次：{db_path}")
        return info
    conn = None
    try:
        conn = _open_db_readonly(db_path)
        tables = _tables(conn)
        if "jobs" not in tables:
            info["error"] = "库内没有 jobs 表（旧 schema）"
            return info
        job_cols = _columns(conn, "jobs")
        wanted = [name for name in ("payload", "first_seen_at", "updated_at", "run_id",
                                    "keyword", "city") if name in job_cols]
        if "job_id" not in job_cols:
            info["error"] = "jobs 表没有 job_id 列（旧 schema）"
            return info
        if not wanted:
            info["error"] = "jobs 表没有可读列（旧 schema）"
            return info
        row = conn.execute(
            f"SELECT {', '.join(wanted)} FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            return info
        info["found"] = True
        for name in wanted:
            if name == "payload":
                payload = None
                try:
                    payload = json.loads(row[name] or "")
                except (TypeError, ValueError):
                    payload = None
                if isinstance(payload, dict):
                    info["job"] = payload
            else:
                info[name] = row[name]
        run_id = str(info.get("run_id") or "")
        if "runs" in tables and run_id:
            run_cols = _columns(conn, "runs")
            run_wanted = [name for name in ("run_id", "started_at", "finished_at", "mode",
                                            "keyword", "city", "job_count", "detail_count")
                          if name in run_cols]
            if run_wanted:
                run_row = conn.execute(
                    f"SELECT {', '.join(run_wanted)} FROM runs WHERE run_id = ?",
                    (run_id,)).fetchone()
                if run_row is not None:
                    info["run"] = {name: run_row[name] for name in run_wanted}
        if "details" in tables:
            detail_cols = _columns(conn, "details")
            if "jd" in detail_cols:
                jd_expr = "jd"
                select = [f"{jd_expr} AS jd"]
                if "updated_at" in detail_cols:
                    select.append("updated_at")
                detail_row = conn.execute(
                    f"SELECT {', '.join(select)} FROM details WHERE job_id = ?",
                    (job_id,)).fetchone()
                if detail_row is not None:
                    jd = str(detail_row["jd"] or "")
                    info["has_jd"] = bool(jd.strip())
                    info["jd_len"] = len(jd)
                    if "updated_at" in detail_cols:
                        info["detail_updated_at"] = detail_row["updated_at"]
    except (OSError, sqlite3.Error) as exc:
        info["error"] = f"{type(exc).__name__}: {exc}"
        _note(errors, f"按 job_id 查库失败：{exc}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
    return info


# ---------------------------------------------------------------------------
# 产物 / 交付物工具
# ---------------------------------------------------------------------------

def _deliverable_entries(index):
    entries = [entry for entry in (index.get("artifacts") or [])
               if entry.get("kind") == "deliverable"]
    entries.sort(key=lambda entry: (str(entry.get("created_at") or ""),
                                    str(entry.get("path") or "")), reverse=True)
    return entries


def _entry_scope_key(entry):
    return str((entry.get("scope") or {}).get("scope_key") or "")


def _entry_counts(entry):
    counts = entry.get("counts") or {}
    return {
        "jobs": _safe_int(counts.get("jobs"), 0, minimum=0),
        "with_jd": _safe_int(counts.get("with_jd"), 0, minimum=0),
    }


def _ruleset_of(payload):
    """交付物盖章里的规则版本（旧交付物没有 → 返回空，不猜）。"""
    if not isinstance(payload, dict):
        return ""
    stamp = payload.get("artifact") if isinstance(payload.get("artifact"), dict) else {}
    for container in (payload, stamp):
        value = container.get("ruleset_version") if isinstance(container, dict) else None
        if str(value or "").strip():
            return str(value).strip()
    return ""


def _ruleset_info(deliverable_payloads):
    """规则版本：优先交付物盖章，其次当前代码的 ``semantics.RULESET_VERSION``。"""
    versions = []
    for payload in deliverable_payloads:
        artifact = payload.get("artifact") if isinstance(payload, dict) else None
        scope_key = str(artifact.get("scope_key") or "") \
            if isinstance(artifact, dict) else ""
        version = _ruleset_of(payload)
        if version:
            versions.append({"version": version, "source": "交付物盖章",
                             "scope_key": scope_key})
    if versions:
        return versions[0]["version"], "交付物盖章", versions
    current = str(getattr(_semantics, "RULESET_VERSION", "") or "")
    if current:
        return current, "semantics.RULESET_VERSION（交付物未盖章）", []
    return "", "未知（既无交付物盖章也无代码常量）", []


def _job_summary(job):
    if not isinstance(job, dict):
        return {}
    return {
        "job_id": str(job.get("job_id") or ""),
        "title": str(job.get("title") or ""),
        "company": str(job.get("company_name") or job.get("company")
                       or job.get("brand_name") or ""),
        "salary": str(job.get("salary") or ""),
        "location": str(job.get("location") or ""),
        "direction": str(job.get("direction") or ""),
        "recruit_type": str(job.get("recruit_type") or ""),
    }


def _job_line(job):
    """一行岗位描述（标题｜公司｜薪资｜地点）。"""
    summary = _job_summary(job)
    parts = [summary.get("title") or "（无标题）"]
    if summary.get("company"):
        parts.append(summary["company"])
    if summary.get("salary"):
        parts.append(summary["salary"])
    if summary.get("location"):
        parts.append(summary["location"])
    return "｜".join(parts)


def _evidence_of(job):
    """岗位的保留证据（方向分类/证据词/技能/标签/JD 长度；只列已有字段）。"""
    evidence = []
    if not isinstance(job, dict):
        return evidence
    direction = str(job.get("direction") or "").strip()
    if direction:
        evidence.append(f"方向分类={direction}")
    terms = job.get("direction_evidence")
    if isinstance(terms, list) and terms:
        evidence.append("证据词=" + "、".join(str(item) for item in terms[:8]))
    skills = job.get("skill_tags") or job.get("skills")
    if isinstance(skills, list) and skills:
        evidence.append("技能=" + "、".join(str(item) for item in skills[:8]))
    labels = job.get("job_labels")
    if isinstance(labels, str) and labels.strip():
        evidence.append(f"标签={labels.strip()[:60]}")
    post_type = str(job.get("post_type") or "").strip()
    if post_type:
        evidence.append(f"发布主体={post_type}")
    recruit_type = str(job.get("recruit_type") or "").strip()
    if recruit_type:
        evidence.append(f"招聘类型={recruit_type}")
    jd = str(job.get("jd") or "")
    if jd.strip():
        evidence.append(f"JD {len(jd)} 字")
    return evidence


def _index_dropped(value):
    """把 ``dropped_junk`` 归一成 ``{job_id: {reason,title,company}}``（容错旧形态）。"""
    result = {}
    for item in value or []:
        if isinstance(item, dict):
            job_id = str(item.get("job_id") or "").strip()
            if job_id:
                result[job_id] = {
                    "reason": str(item.get("reason") or ""),
                    "title": str(item.get("title") or ""),
                    "company": str(item.get("company") or ""),
                }
        elif isinstance(item, str) and item.strip():
            result[item.strip()] = {"reason": "", "title": "", "company": ""}
    return result


def _index_dropped_no_jd(value):
    result = set()
    for item in value or []:
        if isinstance(item, dict):
            job_id = str(item.get("job_id") or "").strip()
        else:
            job_id = str(item or "").strip()
        if job_id:
            result.add(job_id)
    return result


def _load_deliverable(result_dir, entry):
    """读交付物并整理成 diff/why 需要的最小结构；读失败返回 None。"""
    payload = _read_json(os.path.join(result_dir, str(entry.get("path") or "")))
    if not isinstance(payload, dict):
        return None
    jobs = {}
    for job in payload.get("jobs") or []:
        if isinstance(job, dict) and str(job.get("job_id") or "").strip():
            jobs[str(job["job_id"]).strip()] = job
    return {
        "payload": payload,
        "jobs": jobs,
        "dropped_junk": _index_dropped(payload.get("dropped_junk")),
        "dropped_no_jd": _index_dropped_no_jd(payload.get("dropped_no_jd")),
        "ruleset_version": _ruleset_of(payload),
    }


# ---------------------------------------------------------------------------
# 模板 / 人工剔除名单
# ---------------------------------------------------------------------------

def _profiles_dir():
    return os.path.abspath(os.path.expanduser(
        os.environ.get("BOSS_DAILY_PROFILES_DIR")
        or os.path.join(_state_dir(), "profiles")))


def _profile_paths():
    """模板文件：profiles/*.json；目录为空时兼容旧的单文件 daily.json。"""
    directory = _profiles_dir()
    paths = []
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        names = []
    for name in names:
        if name.endswith(".json"):
            path = os.path.join(directory, name)
            if os.path.isfile(path):
                paths.append(path)
    legacy = os.path.join(_state_dir(), "daily.json")
    if not paths and os.path.isfile(legacy):
        paths.append(legacy)
    return paths


def _load_profiles(errors):
    """读全部日常模板（坏模板带 ``error`` 字段，不影响其它模板）。"""
    profiles = []
    for path in _profile_paths():
        item = {"path": path, "file": os.path.basename(path), "error": ""}
        payload = _read_json(path)
        if not isinstance(payload, dict):
            item["error"] = "模板读取失败或不是 JSON 对象"
            item["name"] = os.path.splitext(item["file"])[0]
            _note(errors, f"日常模板不可读：{path}")
            profiles.append(item)
            continue
        keywords = payload.get("keywords")
        recruit = payload.get("recruit_types")
        if isinstance(recruit, str):
            recruit = [part.strip() for part in recruit.split(",") if part.strip()]
        item.update({
            "name": str(payload.get("name") or os.path.splitext(item["file"])[0]),
            "aliases": [str(alias) for alias in (payload.get("aliases") or [])
                        if str(alias).strip()],
            "city": str(payload.get("city") or ""),
            "keywords": len(keywords) if isinstance(keywords, list) else 0,
            "keyword_list": [str(word) for word in (keywords or [])][:20]
                            if isinstance(keywords, list) else [],
            "recruit_types": [str(value) for value in (recruit or [])]
                             if isinstance(recruit, list) else [],
            "source": str(payload.get("source") or "db"),
            "scope": str(payload.get("scope") or ""),
            "scope_key": str(payload.get("scope_key") or ""),
            "deliverable": str(payload.get("deliverable") or ""),
            "exclude_job_ids_file": str(payload.get("exclude_job_ids_file") or ""),
            "comment": str(payload.get("comment") or ""),
        })
        profiles.append(item)
    return profiles


def _load_exclude_ids(path, errors):
    """读人工剔除名单（``{"job_ids": [...]}`` 或裸 list）；返回 (ids, meta)。"""
    resolved = os.path.abspath(os.path.expanduser(str(path)))
    payload = _read_json(resolved)
    meta = {"file": resolved, "name": "", "comment": "", "count": 0}
    ids = set()
    if isinstance(payload, dict):
        ids = {str(item).strip() for item in (payload.get("job_ids") or [])
               if str(item).strip()}
        meta["name"] = str(payload.get("name") or "")
        meta["comment"] = str(payload.get("_comment") or payload.get("comment") or "")
        if not ids:
            for item in payload.get("items") or []:
                if isinstance(item, dict) and str(item.get("job_id") or "").strip():
                    ids.add(str(item["job_id"]).strip())
    elif isinstance(payload, list):
        ids = {str(item).strip() for item in payload if str(item).strip()}
    else:
        _note(errors, f"人工剔除名单不可读：{resolved}")
        return ids, meta
    meta["count"] = len(ids)
    return ids, meta


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def ask_status(result_dir=None, db_path=None, probe_cdp=True):
    """单屏状态：运行/预算/锁/冷却/CDP + 今日 + 最新交付物 + 异常 + 待拍板。"""
    errors = []
    result_dir = _result_dir(result_dir)
    db_path = _db_path(db_path)

    run = _compact_run(_run_view())
    if run.get("state") == "unknown":
        _note(errors, "没有运行状态记录（可能从未运行，或状态文件被清理）")
    budget = _read_budget(errors)
    lock = _read_lock(errors)
    cooldown = _read_cooldown(errors)
    cdp = _probe_cdp() if probe_cdp else {
        "probed": False, "reachable": False, "port": DEFAULT_CDP_PORT,
        "browser": "", "detail": "未探测（probe_cdp=False）"}
    db = _db_stats(db_path, errors)

    today = {"date": "", "runs": 0, "keywords": [], "new_jobs": 0, "by_title": [],
             "details_updated": 0, "details_with_jd": 0, "anomalies": {}}
    deliverables = []
    notes = []
    try:
        report = _brief.collect(result_dir=result_dir, db_path=db_path)
    except (OSError, TypeError, ValueError) as exc:
        report = None
        _note(errors, f"今日简报聚合失败：{exc}")
    if isinstance(report, dict):
        runs = report.get("runs") or {}
        new_jobs = report.get("new_jobs") or {}
        details = report.get("details") or {}
        anomalies = report.get("anomalies") or {}
        today = {
            "date": str(report.get("date") or ""),
            "runs": _safe_int(runs.get("count"), 0, minimum=0),
            "keywords": list(runs.get("keywords") or []),
            "new_jobs": _safe_int(new_jobs.get("count"), 0, minimum=0),
            "by_title": [
                {"title": str(item.get("title") or ""),
                 "count": _safe_int(item.get("count"), 0, minimum=0)}
                for item in (new_jobs.get("by_title") or [])[:8]
            ],
            "details_updated": _safe_int(details.get("updated"), 0, minimum=0),
            "details_with_jd": _safe_int(details.get("with_jd"), 0, minimum=0),
            "anomalies": {
                "risk_events": _safe_int(anomalies.get("risk_events"), 0, minimum=0),
                "known_bad": _safe_int(anomalies.get("known_bad"), 0, minimum=0),
                "failure_files": _safe_int(anomalies.get("failure_files"), 0, minimum=0),
                "failure_items": _safe_int(anomalies.get("failure_items"), 0, minimum=0),
            },
        }
        for item in report.get("deliverables") or []:
            changes = item.get("changes") or None
            deliverables.append({
                "scope_key": str(item.get("scope_key") or ""),
                "path": str(item.get("path") or ""),
                "jobs": _safe_int(item.get("jobs"), 0, minimum=0),
                "with_jd": _safe_int(item.get("with_jd"), 0, minimum=0),
                "created_at": str(item.get("created_at") or ""),
                "changes": {
                    "added": _safe_int(changes.get("added"), 0, minimum=0),
                    "removed": _safe_int(changes.get("removed"), 0, minimum=0),
                    "kept": _safe_int(changes.get("kept"), 0, minimum=0),
                } if isinstance(changes, dict) else None,
            })
        notes = [str(note) for note in (report.get("notes") or [])]

    if not os.path.isdir(result_dir):
        _note(errors, f"产物目录不存在：{result_dir}")
    data = {
        "generated_at": _now_iso(),
        "result_dir": result_dir,
        "state_dir": _state_dir(),
        "db": db,
        "run": run,
        "budget": budget,
        "lock": lock,
        "cooldown": cooldown,
        "cdp": cdp,
        "today": today,
        "deliverables": deliverables,
        "notes": notes,
    }
    data["open_questions"] = _open_questions(data)
    data["degraded"] = bool(errors)
    data["errors"] = list(errors)
    data["headline"] = _status_headline(data)
    ok = bool(os.path.isdir(result_dir) or db["ok"] or run.get("state") != "unknown")
    return _answer("status", data, _format_status(data), errors, ok=ok)


def _status_headline(data):
    run = data.get("run") or {}
    budget = data.get("budget") or {}
    today = data.get("today") or {}
    cdp = data.get("cdp") or {}
    if not cdp.get("probed"):
        cdp_text = "CDP 未探测"
    elif cdp.get("reachable"):
        cdp_text = "CDP 可连"
    else:
        cdp_text = "CDP 不可连"
    parts = [
        f"最近运行 {run.get('state') or 'unknown'}",
        f"今日新增 {today.get('new_jobs', 0)} 岗",
        f"详情预算 {budget.get('used', 0)}/{budget.get('limit', 0)}"
        f"（剩 {budget.get('remaining', 0)}）",
        cdp_text,
    ]
    lock = data.get("lock") or {}
    if lock.get("risk"):
        parts.append("锁⛔熔断")
    elif lock.get("held"):
        parts.append("锁🔒占用")
    else:
        parts.append("锁空闲")
    return "｜".join(parts)


def _open_questions(data):
    """待拍板：只列需要人确认的事（口径边界/风险），不猜。"""
    questions = []
    budget = data.get("budget") or {}
    if budget.get("limit") and budget.get("remaining", 0) <= 0:
        questions.append("今日详情额度已用尽：只能抓列表，明细等明天（或确认是否调整预算）")
    lock = data.get("lock") or {}
    if lock.get("risk"):
        questions.append("互斥锁处于熔断挂起：确认原因后需人工 --reset-lock 才能重开")
    elif lock.get("held"):
        questions.append("互斥锁被占用：若有任务在跑就等；确认进程已死可人工清理残留锁")
    cooldown = data.get("cooldown") or {}
    if cooldown.get("active"):
        questions.append(f"熔断冷却中（剩 {cooldown.get('remaining_seconds', 0)}s）："
                         "等冷却结束再跑，不要反复重试")
    run = data.get("run") or {}
    if run.get("running") and run.get("stale"):
        questions.append("运行状态长时间未更新（可能卡住/已结束）：确认是否重启任务")
    today = data.get("today") or {}
    if not today.get("runs"):
        questions.append("今天还没有 run：要不要现在跑一次日常作业（先确认用哪套模板）")
    elif not today.get("new_jobs"):
        questions.append("今天还没有新增岗位：可能还没跑列表，或平台召回没变化")
    return questions[:4]


def _format_status(data):
    run = data.get("run") or {}
    budget = data.get("budget") or {}
    today = data.get("today") or {}
    db = data.get("db") or {}
    lines = [f"## 状态 · {_fmt_time(data.get('generated_at'))}", "",
             f"**一句话**：{data.get('headline') or ''}"]
    phase = run.get("phase") or "unknown"
    state = run.get("state") or "unknown"
    age = _fmt_duration(run.get("age_seconds")) if run.get("age_seconds") is not None else "未知"
    run_line = f"**最近运行**：{state}/{phase}"
    if run.get("message"):
        run_line += f"｜{run['message']}"
    if run.get("state") != "unknown":
        run_line += f"｜{age}前更新"
    lines.append(run_line)
    if run.get("phase") == "detail" and run.get("detail_total"):
        lines.append(f"- 详情：{run.get('detail_done', 0)}/{run.get('detail_total', 0)}"
                     f"（成功 {run.get('detail_ok', 0)}，失败 {run.get('detail_failed', 0)}）")
    lines.append(f"**今日**：{today.get('runs', 0)} 次 run｜新增 {today.get('new_jobs', 0)} 岗"
                 f"｜详情更新 {today.get('details_updated', 0)}"
                 f"（含 JD {today.get('details_with_jd', 0)}）")
    lines.append(f"**预算**：详情 {budget.get('used', 0)}/{budget.get('limit', 0)}"
                 f"（剩 {budget.get('remaining', 0)}）｜锁：{(data.get('lock') or {}).get('detail') or '-'}"
                 f"｜冷却：{(data.get('cooldown') or {}).get('detail') or '-'}")
    cdp = data.get("cdp") or {}
    if cdp.get("probed"):
        cdp_text = (f"✅ 可连（{cdp.get('browser') or '浏览器版本未知'}）"
                    if cdp.get("reachable") else f"❌ 不可连（端口 {cdp.get('port')}）")
    else:
        cdp_text = "未探测（ask 默认不发本地探测；可传 probe_cdp=True）"
    lines.append(f"**CDP**：{cdp_text}｜登录态：未探测（ask 不发 BOSS 请求，"
                 "要确认可跑请让 agent 跑一次 --check）")
    if today.get("by_title"):
        parts = [f"{item.get('title')} {item.get('count')}"
                 for item in today["by_title"][:6]]
        lines.append("**今日岗位结构**（未过口径）：" + "｜".join(parts))
    deliverables = data.get("deliverables") or []
    if deliverables:
        lines.append("**交付物（每口径最新）**：")
        for item in deliverables[:5]:
            diff = ""
            changes = item.get("changes")
            if isinstance(changes, dict):
                diff = (f"｜较上版 +{changes.get('added', 0)}"
                        f"/-{changes.get('removed', 0)}/保持 {changes.get('kept', 0)}")
            scope = item.get("scope_key") or "（未标口径）"
            lines.append(f"- {scope} → {item.get('path')}｜{item.get('jobs', 0)} 条"
                         f"（JD {item.get('with_jd', 0)}）"
                         f"｜{_fmt_time(item.get('created_at'))}{diff}")
    else:
        lines.append("**交付物**：还没有交付物（先跑一次 --daily 或 build_deliverable）")
    anomalies = today.get("anomalies") or {}
    lines.append(f"**异常**：今日风控事件 {anomalies.get('risk_events', 0)}"
                 f"｜坏页台账 {anomalies.get('known_bad', 0)}"
                 f"｜今日失败文件 {anomalies.get('failure_files', 0)}"
                 f"（{anomalies.get('failure_items', 0)} 条）")
    schema = db.get("schema_version")
    schema_text = f"schema v{schema}" if schema is not None else "schema 版本未标（旧库）"
    lines.append(f"**原材料库**：{db.get('path')}"
                 f"｜{'可读' if db.get('ok') else '不可用（' + (db.get('error') or '未知') + '）'}"
                 f"｜jobs {db.get('jobs', 0)}｜details {db.get('details', 0)}"
                 f"（含 JD {db.get('details_with_jd', 0)}）｜runs {db.get('runs', 0)}"
                 f"｜{schema_text}")
    questions = data.get("open_questions") or []
    if questions:
        lines.append("**待拍板**：")
        lines.extend(f"- {question}" for question in questions)
    if data.get("notes"):
        lines.append("**注**：" + "；".join(data["notes"]))
    if data.get("degraded"):
        lines.append(f"**降级**（共 {len(data.get('errors') or [])} 项，不编造）："
                     + "；".join((data.get("errors") or [])[:4]))
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# today
# ---------------------------------------------------------------------------

def ask_today(result_dir=None, db_path=None, date=None):
    """今日简报：复用 ``brief.collect`` 的聚合，返回结构化 JSON + 一屏 markdown。"""
    errors = []
    result_dir = _result_dir(result_dir)
    db_path = _db_path(db_path)
    try:
        report = _brief.collect(date=date, result_dir=result_dir, db_path=db_path)
    except (OSError, TypeError, ValueError) as exc:
        return _answer("today", {"date": str(date or ""), "error": str(exc)},
                       f"## 今日简报\n\n不可用：{exc}\n", [f"今日简报聚合失败：{exc}"],
                       ok=False)
    if not os.path.isdir(result_dir):
        _note(errors, f"产物目录不存在：{result_dir}")
    if not os.path.exists(db_path):
        _note(errors, f"原材料库不存在：{db_path}（只有文件/台账数字）")
    report["sources"] = {"result_dir_exists": os.path.isdir(result_dir),
                         "db_exists": os.path.exists(db_path)}
    report["degraded"] = bool(errors)
    report["errors"] = list(errors)
    ok = bool(os.path.isdir(result_dir) or os.path.exists(db_path))
    return _answer("today", report, _format_today(report), errors, ok=ok)


def _format_today(report):
    runs = report.get("runs") or {}
    jobs = report.get("new_jobs") or {}
    details = report.get("details") or {}
    budget = report.get("budget") or {}
    lines = [f"## 今日简报 · {report.get('date') or '今天'}", ""]
    lines.append(f"- 抓取：{runs.get('count', 0)} 次 run"
                 + (f"（关键词 {'、'.join(runs.get('keywords') or [])[:120]}）"
                    if runs.get("keywords") else "")
                 + f"｜今日新增 {jobs.get('count', 0)} 岗")
    lines.append(f"- 详情：今日请求 {budget.get('used', 0)} 次"
                 f"（涉及 {details.get('with_jd', 0)} 岗有 JD）"
                 f"｜预算 {budget.get('used', 0)}/{budget.get('limit', 0)}"
                 f"（剩 {budget.get('remaining', 0)}）")
    by_title = jobs.get("by_title") or []
    if by_title:
        parts = [f"{item.get('title')} {item.get('count')}" for item in by_title[:8]]
        lines.append("- 今日岗位结构（未过口径）：" + "｜".join(parts))
    by_recruit = jobs.get("by_recruit") or {}
    if by_recruit:
        lines.append("- 今日招聘类型：" + "｜".join(
            f"{key} {value}" for key, value in by_recruit.items()))
    deliverables = report.get("deliverables") or []
    if deliverables:
        lines.append("- 交付物（每口径最新）：")
        for item in deliverables[:6]:
            change = item.get("changes") or {}
            diff = ""
            if change:
                diff = (f"｜较上版 +{change.get('added', 0)}"
                        f"/-{change.get('removed', 0)}/保持 {change.get('kept', 0)}")
            lines.append(f"  - {item.get('scope_key') or '（未标口径）'} → "
                         f"{item.get('path')}｜{item.get('jobs', 0)} 条"
                         f"（JD {item.get('with_jd', 0)}）"
                         f"｜今日新增 {item.get('new_today', 0)}{diff}")
    anomalies = report.get("anomalies") or {}
    lines.append(f"- 异常：风控事件 {anomalies.get('risk_events', 0)}"
                 f"｜坏页台账 {anomalies.get('known_bad', 0)}"
                 f"｜今日失败文件 {anomalies.get('failure_files', 0)}"
                 f"（{anomalies.get('failure_items', 0)} 条）")
    for note in report.get("notes") or []:
        lines.append(f"- 注：{note}")
    if report.get("degraded"):
        lines.append("- 降级：" + "；".join(report.get("errors") or [])[:2])
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# why
# ---------------------------------------------------------------------------

def ask_why(job_id=None, result_dir=None, db_path=None):
    """解释一个 job_id 为什么留/删：交付物、剔除原因、人工名单、规则版本、来源批次。"""
    errors = []
    job_id = str(job_id or "").strip()
    if not job_id:
        return _answer("why", {"job_id": "", "found": False},
                       "## 为什么这条岗位？\n\n缺少 job_id（用法：ask why <job_id>）\n",
                       ["缺少 job_id：无法解释"], ok=False)
    result_dir = _result_dir(result_dir)
    db_path = _db_path(db_path)
    index, index_error = _load_index(result_dir) if os.path.isdir(result_dir) else (None, "")
    if index is None:
        _note(errors, index_error or f"产物目录不存在/不可读：{result_dir}")

    kept_in, dropped_in, no_jd_in = [], [], []
    deliverable_payloads = []
    if index is not None:
        for entry in _deliverable_entries(index):
            loaded = _load_deliverable(result_dir, entry)
            if loaded is None:
                _note(errors, f"交付物读取失败：{entry.get('path')}")
                continue
            deliverable_payloads.append(loaded["payload"])
            scope_key = _entry_scope_key(entry)
            meta = {
                "path": str(entry.get("path") or ""),
                "scope_key": scope_key,
                "status": str(entry.get("status") or ""),
                "created_at": str(entry.get("created_at") or ""),
                "jobs": _entry_counts(entry)["jobs"],
                "with_jd": _entry_counts(entry)["with_jd"],
                "ruleset_version": loaded["ruleset_version"],
            }
            found_job = loaded["jobs"].get(job_id)
            if found_job is not None:
                kept_in.append(dict(meta, evidence=_evidence_of(found_job),
                                    job=_job_summary(found_job)))
            dropped = loaded["dropped_junk"].get(job_id)
            if dropped is not None:
                dropped_in.append(dict(meta, reason=dropped.get("reason") or "",
                                       title=dropped.get("title") or "",
                                       company=dropped.get("company") or ""))
            if job_id in loaded["dropped_no_jd"]:
                no_jd_in.append(dict(meta, reason="无 JD（口径一在详情阶段剔除）"))

    excluded_in = []
    for profile in _load_profiles(errors):
        exclude_path = profile.get("exclude_job_ids_file")
        if not exclude_path:
            continue
        ids, meta = _load_exclude_ids(exclude_path, errors)
        if job_id in ids:
            excluded_in.append({
                "file": meta["file"],
                "name": meta["name"] or profile.get("name") or "",
                "comment": meta["comment"],
                "profile": profile.get("name") or "",
            })

    db_info = _db_lookup_job(db_path, job_id, errors)

    ruleset_version, ruleset_source, ruleset_stamped = _ruleset_info(deliverable_payloads)
    if kept_in:
        decision = "kept"
    elif dropped_in:
        decision = "dropped"
    elif no_jd_in:
        decision = "no_jd"
    elif excluded_in:
        decision = "excluded"
    elif db_info.get("found"):
        decision = "db_only"
    else:
        decision = "unknown"
    labels = {"kept": "保留", "dropped": "删除（规则剔除）", "no_jd": "删除（无 JD）",
              "excluded": "人工剔除名单", "db_only": "找到了库记录，但不在归属交付物里",
              "unknown": "未找到"}
    job_summary = {}
    if kept_in:
        job_summary = kept_in[0].get("job") or {}
    elif db_info.get("job"):
        job_summary = _job_summary(db_info["job"])
    elif dropped_in:
        job_summary = {"job_id": job_id, "title": dropped_in[0].get("title") or "",
                       "company": dropped_in[0].get("company") or ""}
    evidence = kept_in[0].get("evidence") if kept_in else _evidence_of(db_info.get("job"))
    found = decision != "unknown"
    if not found:
        _note(errors, f"没有找到 job_id {job_id}：交付物/剔除清单/人工名单/原材料库"
                      "都没有这条记录（不编造原因）")
    data = {
        "job_id": job_id,
        "found": found,
        "decision": decision,
        "decision_label": labels[decision],
        "job": job_summary,
        "kept_in": kept_in,
        "dropped_in": dropped_in,
        "no_jd_in": no_jd_in,
        "excluded_in": excluded_in,
        "db": db_info,
        "ruleset_version": ruleset_version,
        "ruleset_source": ruleset_source,
        "ruleset_stamped": ruleset_stamped,
        "evidence": evidence,
        "searched": [
            f"交付物 {len(deliverable_payloads)} 份（含 archive）",
            f"人工剔除名单 {len(excluded_in)} 处命中/已查全部模板",
            f"原材料库 {db_path}",
        ],
        "degraded": bool(errors),
        "errors": list(errors),
    }
    return _answer("why", data, _format_why(data), errors, ok=found)


def _format_why(data):
    job_id = data.get("job_id") or ""
    job = data.get("job") or {}
    lines = [f"## 为什么这条岗位？（job_id `{job_id}`）", ""]
    lines.append(f"- 结论：**{data.get('decision_label')}**")
    if job:
        detail = [job.get("title") or "", job.get("company") or "",
                  job.get("salary") or "", job.get("location") or ""]
        lines.append("- 岗位：" + "｜".join(part for part in detail if part))
    if data.get("kept_in"):
        lines.append("- 保留在：")
        for item in data["kept_in"][:4]:
            lines.append(f"  - {item.get('path')}（口径 {item.get('scope_key') or '未标'}，"
                         f"{item.get('jobs', 0)} 条，{_fmt_time(item.get('created_at'))}）")
        if len(data["kept_in"]) > 4:
            lines.append(f"  - …以及另 {len(data['kept_in']) - 4} 份历史交付物"
                         "（序列见 data.kept_in）")
    if data.get("dropped_in"):
        lines.append("- 删除原因（交付物内留痕）：")
        seen = set()
        for item in data["dropped_in"]:
            key = (item.get("reason") or "", item.get("path") or "")
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"  - 「{item.get('reason') or '未记录原因'}」"
                         f"（{item.get('path')}，{_fmt_time(item.get('created_at'))}）")
    if data.get("no_jd_in"):
        lines.append("- 无 JD 剔除：" + "；".join(
            f"{item.get('path')}（{_fmt_time(item.get('created_at'))}）"
            for item in data["no_jd_in"][:3]))
    if data.get("excluded_in"):
        lines.append("- 人工剔除名单命中：")
        for item in data["excluded_in"]:
            lines.append(f"  - {item.get('file')}"
                         f"（{item.get('name') or '未命名'}；模板 {item.get('profile') or '-'}）")
            if item.get("comment"):
                lines.append(f"    {item['comment']}")
    db = data.get("db") or {}
    if db.get("found"):
        run = db.get("run") or {}
        lines.append(f"- 来源批次：run_id `{db.get('run_id') or '未知'}`"
                     + (f"｜{_fmt_time(run.get('started_at'))}"
                        f"｜mode={run.get('mode') or '-'}"
                        f"｜keyword={run.get('keyword') or '-'}"
                        f"｜city={run.get('city') or '-'}"
                        f"｜列表 {run.get('job_count', '-')} 条"
                        if run else ""))
        lines.append(f"- 首次入库：{_fmt_time(db.get('first_seen_at'))}"
                     f"｜最近更新：{_fmt_time(db.get('updated_at'))}"
                     f"｜详情 JD：{'有' if db.get('has_jd') else '无'}"
                     f"{('（' + str(db.get('jd_len')) + ' 字）') if db.get('jd_len') else ''}")
        if db.get("job") and not data.get("kept_in"):
            lines.append(f"- 库内记录：{_job_line(db['job'])}")
    elif db.get("error"):
        lines.append(f"- 原材料库核对：不可用（{db.get('error')}）")
    if data.get("evidence"):
        lines.append("- 保留证据：" + "；".join(str(item) for item in data["evidence"][:6]))
    lines.append(f"- 规则版本：{data.get('ruleset_version') or '未知'}"
                 f"（{data.get('ruleset_source')}）")
    if data.get("decision") == "db_only":
        lines.append("- 说明：库里有这条记录，但当前交付物/剔除清单里都没有它——"
                     "可能没参与最近一次构建，或不在该口径的输入范围；不臆测原因。")
    if data.get("decision") == "unknown":
        lines.append("- 说明：本地所有来源都没有这个 job_id。已查："
                     + "；".join(data.get("searched") or []))
        lines.append("- 没有编造原因：需要更早的产物/库备份才能追溯这条岗位。")
    if data.get("degraded"):
        lines.append("- 降级：" + "；".join((data.get("errors") or [])[:3]))
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------

def _version_meta(entry, loaded):
    return {
        "path": str(entry.get("path") or ""),
        "scope_key": _entry_scope_key(entry),
        "status": str(entry.get("status") or ""),
        "created_at": str(entry.get("created_at") or ""),
        "jobs": _entry_counts(entry)["jobs"],
        "with_jd": _entry_counts(entry)["with_jd"],
        "ruleset_version": loaded.get("ruleset_version") or "",
    }


def _diff_reason_added(job_id, previous):
    dropped = previous.get("dropped_junk", {}).get(job_id)
    if dropped is not None:
        return (f"上一版按规则剔除（{dropped.get('reason') or '未记录原因'}），本版保留")
    return "上一版没有、本版有（新增或重新入池）"


def _diff_reason_removed(job_id, current):
    dropped = current.get("dropped_junk", {}).get(job_id)
    if dropped is not None:
        return f"本版按规则剔除：{dropped.get('reason') or '未记录原因'}"
    if job_id in current.get("dropped_no_jd", set()):
        return "本版无 JD（口径一在详情阶段剔除）"
    return "上一版有、本版没有（平台下架/不在本轮输入）"


def _diff_items(job_ids, current, previous, limit, removed=False):
    items = []
    for job_id in job_ids[:limit]:
        job = (previous if removed else current)["jobs"].get(job_id) \
            or current["jobs"].get(job_id) or previous["jobs"].get(job_id) or {}
        reason = (_diff_reason_removed(job_id, current) if removed
                  else _diff_reason_added(job_id, previous))
        item = _job_summary(job)
        item["reason"] = reason
        items.append(item)
    return items


def _scope_groups(index):
    """按 scope_key 归组交付物，组内按创建时间升序（最新在最后）。"""
    groups = {}
    for entry in _deliverable_entries(index):
        key = _entry_scope_key(entry)
        groups.setdefault(key, []).append(entry)
    for items in groups.values():
        items.sort(key=lambda entry: (str(entry.get("created_at") or ""),
                                      str(entry.get("path") or "")))
    return groups


def _scope_summaries(groups, limit):
    summaries = []
    for key, items in groups.items():
        current = items[-1]
        previous = items[-2] if len(items) > 1 else None
        summary = {
            "scope_key": key,
            "versions": len(items),
            "has_previous": previous is not None,
            "current": _entry_counts(current),
            "current_path": str(current.get("path") or ""),
            "created_at": str(current.get("created_at") or ""),
        }
        if previous is not None:
            summary["previous"] = _entry_counts(previous)
            summary["previous_path"] = str(previous.get("path") or "")
        summaries.append(summary)
    summaries.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return summaries[:limit]


def ask_diff(scope_key=None, result_dir=None, limit=DEFAULT_DIFF_LIMIT):
    """最新交付物 vs 同口径上一版：+新增/−消失/保持 + 每类原因。"""
    errors = []
    result_dir = _result_dir(result_dir)
    limit = _coerce_limit(limit, DEFAULT_DIFF_LIMIT)
    if not os.path.isdir(result_dir):
        return _answer("diff", {"scope_key": scope_key, "available": False,
                                "has_previous": False, "scopes": []},
                       f"## 交付物对比\n\n产物目录不存在：{result_dir}\n",
                       [f"产物目录不存在：{result_dir}"], ok=False)
    index, index_error = _load_index(result_dir)
    if index is None:
        return _answer("diff", {"available": False, "scopes": []},
                       "## 交付物对比\n\n产物清单生成失败\n",
                       [index_error or "产物清单生成失败"], ok=False)
    groups = _scope_groups(index)
    if not groups:
        return _answer("diff", {"available": False, "has_previous": False, "scopes": []},
                       "## 交付物对比\n\n还没有交付物，无法对比。\n",
                       [f"{result_dir} 下没有交付物"], ok=False)
    scopes = _scope_summaries(groups, MAX_LIMIT)
    requested = str(scope_key or "").strip()
    key = ""
    if requested:
        if requested in groups:
            key = requested
        else:
            matches = [candidate for candidate in groups
                       if requested.lower() in candidate.lower()]
            if matches:
                # 模糊匹配多个口径时取最新构建的那个（scopes 已按 created_at 倒序）。
                key = matches[0]
            else:
                options = "；".join(
                    f"{item['scope_key'] or '（未标口径）'}" for item in scopes[:8])
                return _answer(
                    "diff", {"scope_key": requested, "available": False,
                             "has_previous": False, "scopes": scopes},
                    f"## 交付物对比\n\n没有找到口径「{requested}」。可选：{options}\n",
                    [f"没有找到口径 {requested!r}（可选：{options}）"], ok=False)
    else:
        # 默认对比最新构建的交付物（用户"跟昨天比"指当前在用的口径）。
        key = scopes[0]["scope_key"]
    items = groups[key]
    current_entry, previous_entry = items[-1], (items[-2] if len(items) > 1 else None)
    current = _load_deliverable(result_dir, current_entry)
    previous = _load_deliverable(result_dir, previous_entry) if previous_entry else None
    if current is None:
        return _answer("diff", {"scope_key": key, "available": False, "has_previous": False},
                       "## 交付物对比\n\n当前交付物读取失败\n",
                       [f"交付物读取失败：{current_entry.get('path')}"], ok=False)
    if previous_entry is not None and previous is None:
        _note(errors, f"上一版交付物读取失败：{previous_entry.get('path')}（按缺上一版处理）")
        previous_entry = None
    data = {
        "scope_key": key,
        "available": True,
        "has_previous": previous is not None,
        "current": _version_meta(current_entry, current),
        "previous": _version_meta(previous_entry, previous) if previous is not None else None,
        "scopes": scopes,
        "limit": limit,
        "errors": list(errors),
    }
    if previous is None:
        data["counts"] = None
        data["added"] = []
        data["removed"] = []
        data["truncated"] = {"added": False, "removed": False}
        data["reasons"] = {}
        data["notes"] = ["该口径只有一版交付物，缺上一版，无法给 +/−"]
        _note(errors, f"口径 {key or '未标口径'} 只有一版交付物（上一版缺失）")
        data["errors"] = list(errors)
        return _answer("diff", data, _format_diff(data), errors, ok=True)
    current_ids, previous_ids = set(current["jobs"]), set(previous["jobs"])
    added_ids = sorted(current_ids - previous_ids)
    removed_ids = sorted(previous_ids - current_ids)
    kept_ids = current_ids & previous_ids
    data["counts"] = {"added": len(added_ids), "removed": len(removed_ids),
                      "kept": len(kept_ids),
                      "current_jobs": len(current_ids), "previous_jobs": len(previous_ids)}
    data["added"] = _diff_items(added_ids, current, previous, limit)
    data["removed"] = _diff_items(removed_ids, current, previous, limit, removed=True)
    data["truncated"] = {"added": len(added_ids) > limit,
                         "removed": len(removed_ids) > limit}
    data["reasons"] = {
        "added": "本版有、上一版没有：新增/重新入池；若上一版是规则剔除，会标注原剔除原因",
        "removed": "上一版有、本版没有：可能是规则剔除/无 JD/平台下架/不在本轮输入",
        "kept": "两版都有（内容是否变化需逐条比 JD，此处只比 job_id）",
    }
    data["notes"] = [
        "消失不等于下架：平台模糊召回/本轮输入范围变化也会导致；逐条原因按可用证据标注，不猜。",
        "对比只按 job_id；JD/薪资变化不计入 +/−。",
    ]
    data["errors"] = list(errors)
    return _answer("diff", data, _format_diff(data), errors, ok=True)


def _format_diff(data):
    lines = [f"## 交付物对比 · {data.get('scope_key') or '（未标口径）'}", ""]
    current = data.get("current") or {}
    previous = data.get("previous")
    lines.append(f"- 当前：{current.get('path')}｜{current.get('jobs', 0)} 条"
                 f"（JD {current.get('with_jd', 0)}）"
                 f"｜{_fmt_time(current.get('created_at'))}"
                 f"｜规则 {current.get('ruleset_version') or '未盖章'}")
    if previous:
        lines.append(f"- 上一版：{previous.get('path')}｜{previous.get('jobs', 0)} 条"
                     f"｜{_fmt_time(previous.get('created_at'))}")
    else:
        lines.append("- 上一版：缺失（该口径只有一版，无法给 +/−）")
    counts = data.get("counts")
    if counts:
        lines.append(f"- 差异：**+{counts.get('added', 0)} 新增**｜"
                     f"**-{counts.get('removed', 0)} 消失**｜"
                     f"保持 {counts.get('kept', 0)}")
        if data.get("added"):
            lines.append(f"- 新增（最多 {data.get('limit')} 条）：")
            for item in data["added"]:
                lines.append(f"  - {item.get('title') or '（无标题）'}｜"
                             f"{item.get('company') or ''}｜{item.get('salary') or ''}"
                             f"｜{item.get('reason') or ''}")
        if data.get("removed"):
            lines.append(f"- 消失（最多 {data.get('limit')} 条）：")
            for item in data["removed"]:
                lines.append(f"  - {item.get('title') or '（无标题）'}｜"
                             f"{item.get('company') or ''}｜{item.get('salary') or ''}"
                             f"｜{item.get('reason') or ''}")
        truncated = data.get("truncated") or {}
        if truncated.get("added") or truncated.get("removed"):
            lines.append("- 注：列表已截断，完整差异见 data.added/data.removed 计数。")
    for note in data.get("notes") or []:
        lines.append(f"- 注：{note}")
    scopes = data.get("scopes") or []
    others = [item for item in scopes if item.get("scope_key") != data.get("scope_key")]
    if others:
        lines.append("- 其它口径（可指定 scope_key 再比）：" + "；".join(
            f"{item.get('scope_key') or '未标'}（{item.get('versions')} 版"
            f"{'，可比' if item.get('has_previous') else '，单版'}）"
            for item in others[:6]))
    if data.get("errors"):
        lines.append("- 降级：" + "；".join(data["errors"][:3]))
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# templates
# ---------------------------------------------------------------------------

def ask_templates(result_dir=None, db_path=None):
    """日常模板：名称/别名/口径/交付物/上次运行（只读；不执行任何抓取）。"""
    del db_path  # 模板列表不需要库；保留参数与统一入口一致
    errors = []
    profiles = _load_profiles(errors)
    result_dir = _result_dir(result_dir)
    index = None
    if os.path.isdir(result_dir):
        index, index_error = _load_index(result_dir)
        if index is None:
            _note(errors, index_error or "产物清单生成失败")
    deliverables = _deliverable_entries(index) if index is not None else []
    run = _run_view()

    items = []
    for profile in profiles:
        item = dict(profile)
        item["last_deliverable"] = None
        target = item.get("deliverable") or ""
        scope_key = item.get("scope_key") or ""
        for entry in deliverables:
            name = str(entry.get("path") or "").rsplit("/", 1)[-1]
            matched = bool(scope_key) and _entry_scope_key(entry) == scope_key
            if target and name == target:
                matched = True
            if not matched:
                continue
            counts = _entry_counts(entry)
            item["last_deliverable"] = {
                "path": str(entry.get("path") or ""),
                "created_at": str(entry.get("created_at") or ""),
                "jobs": counts["jobs"],
                "with_jd": counts["with_jd"],
                "status": str(entry.get("status") or ""),
            }
            break
        item["last_run"] = None
        if run.get("state") != "unknown" and (
                (target and str(run.get("deliverable") or "") == target)
                or (scope_key and str(run.get("scope_key") or "") == scope_key)):
            item["last_run"] = {
                "state": run.get("state"),
                "phase": run.get("phase"),
                "message": run.get("message"),
                "updated_at": run.get("updated_at"),
                "age_seconds": run.get("age_seconds"),
            }
        exclude_path = item.get("exclude_job_ids_file")
        if exclude_path:
            ids, meta = _load_exclude_ids(exclude_path, errors)
            item["exclude_meta"] = {
                "file": meta["file"], "name": meta["name"],
                "count": meta["count"], "comment": meta["comment"],
            }
        items.append(item)

    data = {
        "profiles_dir": _profiles_dir(),
        "count": len(items),
        "profiles": items,
        "degraded": bool(errors),
        "errors": list(errors),
    }
    return _answer("templates", data, _format_templates(data), errors, ok=True)


def _format_templates(data):
    profiles = data.get("profiles") or []
    lines = [f"## 日常模板（{data.get('count', 0)} 个）· {data.get('profiles_dir')}", ""]
    if not profiles:
        lines.append("还没有日常模板。把模板 JSON 放到该目录下，"
                     "示例见 data/daily_profile.example.json。")
        lines.append("")
        return "\n".join(lines)
    for item in profiles:
        if item.get("error"):
            lines.append(f"- ✗ {item.get('file')}：{item['error']}")
            continue
        title = item.get("name") or item.get("file")
        aliases = item.get("aliases") or []
        lines.append(f"- **{title}**" + (f"（别名：{'、'.join(aliases)}）" if aliases else ""))
        lines.append(f"  - 口径：{item.get('scope_key') or item.get('scope') or '未标'}"
                     f"｜{item.get('city') or '-'}｜{item.get('keywords', 0)} 词"
                     f"｜{','.join(item.get('recruit_types') or []) or '全部类型'}"
                     f"｜source={item.get('source') or 'db'}")
        deliverable = item.get("last_deliverable")
        if deliverable:
            lines.append(f"  - 交付物：{deliverable.get('path')}"
                         f"｜{deliverable.get('jobs', 0)} 条"
                         f"（JD {deliverable.get('with_jd', 0)}）"
                         f"｜上次构建 {_fmt_time(deliverable.get('created_at'))}")
        elif item.get("deliverable"):
            lines.append(f"  - 交付物：{item['deliverable']}（还没构建过）")
        last_run = item.get("last_run")
        if last_run:
            lines.append(f"  - 上次运行：{last_run.get('state')}/{last_run.get('phase')}"
                         f"｜{last_run.get('message') or ''}"
                         f"｜{_fmt_duration(last_run.get('age_seconds'))}前")
        exclude = item.get("exclude_meta")
        if exclude and exclude.get("count"):
            lines.append(f"  - 人工剔除：{exclude.get('file')}"
                         f"（{exclude.get('count')} 个 job_id）")
        if item.get("comment"):
            lines.append(f"  - 说明：{item['comment']}")
    if data.get("degraded"):
        lines.append("- 降级：" + "；".join(data.get("errors") or [])[:2])
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# batches
# ---------------------------------------------------------------------------

def _batch_health_path(path=None):
    raw = path or os.environ.get("BOSS_BATCH_HEALTH_PATH") \
        or os.path.join(_state_dir(), BATCH_HEALTH_NAME)
    return os.path.abspath(os.path.expanduser(str(raw)))


def ask_batches(limit=DEFAULT_BATCH_LIMIT, path=None):
    """批次健康度：读 ``batch_health.jsonl`` 最近 N 条（文件可能还没生成）。"""
    errors = []
    limit = _coerce_limit(limit, DEFAULT_BATCH_LIMIT)
    health_path = _batch_health_path(path)
    rows = []
    skipped = 0
    try:
        with open(health_path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    skipped += 1
                    continue
                if isinstance(row, dict):
                    rows.append(row)
                else:
                    skipped += 1
    except FileNotFoundError:
        pass
    except (OSError, UnicodeDecodeError) as exc:
        _note(errors, f"批次健康度读取失败：{exc}")
    available = os.path.exists(health_path)
    recent = list(reversed(rows[-limit:])) if rows else []
    summary = {"runs": len(rows), "skipped": skipped}
    for key in ("detail_ok", "detail_failed", "budget_used", "risk_events"):
        summary[key] = sum(_safe_int(row.get(key), 0, minimum=0) for row in rows)
    summary["ok_runs"] = sum(1 for row in rows
                             if str(row.get("status") or "ok") in ("ok", "done", "success"))
    summary["failed_runs"] = len(rows) - summary["ok_runs"]
    if not available:
        _note(errors, f"批次健康度文件还没生成：{health_path}"
                      "（每次 run 结束才会追加一行）")
    data = {"path": health_path, "available": available, "count": len(rows),
            "skipped": skipped, "summary": summary, "items": recent,
            "degraded": bool(errors), "errors": list(errors)}
    return _answer("batches", data, _format_batches(data), errors, ok=True)


def _format_batches(data):
    lines = [f"## 批次健康度 · {data.get('path')}", ""]
    if not data.get("available"):
        lines.append("还没有批次健康度记录（文件未生成：每次 run 结束才会追加一行）。")
        lines.append("")
        return "\n".join(lines)
    summary = data.get("summary") or {}
    lines.append(f"- 汇总：{summary.get('runs', 0)} 次 run"
                 f"（成功 {summary.get('ok_runs', 0)} / 失败 {summary.get('failed_runs', 0)}）"
                 f"｜详情成功 {summary.get('detail_ok', 0)}"
                 f"/失败 {summary.get('detail_failed', 0)}"
                 f"｜预算 {summary.get('budget_used', 0)}"
                 f"｜风控事件 {summary.get('risk_events', 0)}"
                 + (f"｜跳过坏行 {summary.get('skipped', 0)}"
                    if summary.get("skipped") else ""))
    if not data.get("items"):
        lines.append("- 文件存在但没有可读行。")
    for row in data.get("items") or []:
        lines.append(
            f"- {_fmt_time(row.get('finished_at') or row.get('started_at'))}"
            f"｜{row.get('status') or '未知'}"
            f"｜{row.get('mode') or '-'}"
            f"｜{row.get('city') or '-'}"
            f"｜{row.get('keyword') or '-'}"
            f"｜列表 {row.get('list_pages', '-')} 页"
            f"｜详情成功 {row.get('detail_ok', 0)}"
            f"/失败 {row.get('detail_failed', 0)}"
            f"｜预算 {row.get('budget_used', 0)}"
            f"｜风控 {row.get('risk_events', 0)}")
    if data.get("degraded"):
        lines.append("- 降级：" + "；".join(data.get("errors") or [])[:2])
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 统一入口（C 的 --ask 只调这一个函数）
# ---------------------------------------------------------------------------

def run_ask(command, args=None, result_dir=None):
    """统一入口：``command`` ∈ status/today/why/diff/templates/batches。

    ``args`` 可含 ``job_id``（why）、``scope_key``（diff）、``limit``（diff/batches）、
    ``db``/``result_dir``、``date``（today）、``probe_cdp``（status）、
    ``batch_health_path``（batches）。**任何异常都不外抛**，统一返回信封。
    """
    command = str(command or "").strip().lower()
    options = dict(args) if isinstance(args, dict) else {}
    try:
        if options.get("result_dir"):
            result_dir = options["result_dir"]
        if command == "status":
            return ask_status(result_dir=result_dir, db_path=options.get("db"),
                              probe_cdp=bool(options.get("probe_cdp", True)))
        if command == "today":
            return ask_today(result_dir=result_dir, db_path=options.get("db"),
                             date=options.get("date"))
        if command == "why":
            job_id = (options.get("job_id") or options.get("job")
                      or options.get("id") or options.get("target"))
            return ask_why(job_id=job_id, result_dir=result_dir,
                           db_path=options.get("db"))
        if command == "diff":
            scope_key = options.get("scope_key") or options.get("scope")
            return ask_diff(scope_key=scope_key, result_dir=result_dir,
                            limit=options.get("limit"))
        if command == "templates":
            return ask_templates(result_dir=result_dir, db_path=options.get("db"))
        if command == "batches":
            path = options.get("batch_health_path") or options.get("path")
            return ask_batches(limit=options.get("limit"), path=path)
        return _answer(
            command, {"commands": list(COMMANDS)},
            f"## 未知命令\n\n支持：{'、'.join(COMMANDS)}；"
            "用法由 agent 在对话里决定，不是给用户敲的 CLI。\n",
            [f"未知命令 {command!r}（支持 {', '.join(COMMANDS)}）"], ok=False)
    except Exception as exc:  # agent API 边界兜底：绝不把异常抛给会话
        message = f"{type(exc).__name__}: {exc}"
        return _answer(
            command, {"error": message, "command": command},
            f"## ask 执行失败\n\n{message}\n（只读问答不应影响抓取；"
            "可换 command 或检查 result_dir/db 路径）\n",
            [message], ok=False)


def format_answer(payload, as_json=False):
    """把信封格式化为 agent 可读文本：``as_json=True`` 输出纯 JSON（无横幅）。"""
    if not isinstance(payload, dict):
        payload = _answer("unknown", {"value": payload},
                          "ask 返回了非 dict 结果（已兜底）",
                          ["ask 返回了非 dict 结果"], ok=False)
    if as_json:
        try:
            return json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        except (TypeError, ValueError):
            safe = {key: str(value) for key, value in payload.items()}
            return json.dumps(safe, ensure_ascii=False, indent=2)
    return str(payload.get("human") or "")
