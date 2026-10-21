#!/usr/bin/env python3
"""SQLite 增量存储层（WAL 断点续抓 / 跨 run 持久化）。

从 ``scripts/boss_cdp_raw.py`` 抽出的稳定纯逻辑（2026-09-22 P4c-2）：
仅用标准库 ``sqlite3``，无 CDP / 网络依赖，可独立测试；主文件对同名符号做
re-export，保持 ``scripts.boss_cdp_raw.X`` 导入面不变（同 ratelimit/export_contract）。

设计要点
--------
- **WAL 模式**：``journal_mode=WAL`` + ``synchronous=NORMAL`` + ``busy_timeout``，
  多进程读写不互相锁表（与 ``--max-concurrent`` / ``--batch`` 共存）。
- **增量 upsert**：``job_id`` 唯一键；``first_seen_at`` 首次写入后不再变，
  ``updated_at`` 每次覆盖 → 支撑跨 run 增量与后续趋势分析（P3）。
- **payload 存 JSON**：抓取字段随版本演进，无需跟随建列；只把关键查询列
  （``job_id`` / ``updated_at``）单独抽出并建索引。
- **合规**：入库前统一走 ``_sanitize_job``（cookie / token / securityId / BOSS
  内部标识一律不落库）；DB 默认放 ``~/.boss-zhipin-scraper/``（仓库外，不进 git）。
"""

import json
import os
import pathlib
import sqlite3
from datetime import UTC, datetime

try:
    from scripts import export_contract as _export_contract
except ImportError:  # pragma: no cover - 直接运行脚本时走此分支
    import export_contract as _export_contract

_sanitize_job = _export_contract._sanitize_job

# 默认库路径：仓库外（用户状态目录），与 job-result/、.session/ 同源约定
DEFAULT_DB_PATH = os.path.expanduser("~/.boss-zhipin-scraper/boss.db")
SCHEMA_VERSION = 1


class DbSchemaError(RuntimeError):
    """数据库 schema 高于当前代码可安全处理的版本。"""


_FINISHED_AT_UNSET = object()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    mode         TEXT,
    keyword      TEXT,
    city         TEXT,
    job_count    INTEGER NOT NULL DEFAULT 0,
    detail_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS jobs (
    job_id        TEXT PRIMARY KEY,
    payload       TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    run_id        TEXT
);
CREATE TABLE IF NOT EXISTS details (
    job_id        TEXT PRIMARY KEY,
    jd            TEXT,
    payload       TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    run_id        TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_updated_at ON jobs(updated_at);
CREATE INDEX IF NOT EXISTS idx_details_updated_at ON details(updated_at);
"""

_SQLITE_MAX_VARS = 900  # 单条 SQL 变量上限（默认 999）内留余量，分批 IN 查询


def _utcnow():
    """UTC 秒级 ISO 时间戳（跨时区可比较；本地展示由消费端决定）。"""
    return datetime.now(UTC).isoformat(timespec="seconds")


def _chunked(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _payload_json(record):
    """脱敏后序列化：凭据 / securityId / BOSS 内部标识不落库（红线）。"""
    return json.dumps(_sanitize_job(record), ensure_ascii=False, sort_keys=True)


def _stored_schema_version(conn):
    """读取已持久化 schema 版本；无表/无值返回 None，非法值 fail-closed。"""
    table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_meta'"
    ).fetchone()
    if table is None:
        return None
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    if row is None:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError) as exc:
        raise DbSchemaError(f"schema_version 非法: {row[0]!r}") from exc


def open_store(path=None):
    """打开（必要时创建）SQLite 库并确保 schema，返回连接。

    已存在且版本高于当前代码时立即拒绝，不执行 WAL/DDL/版本覆盖等写操作。
    """
    db_path = os.path.expanduser(path or DEFAULT_DB_PATH)
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        stored = _stored_schema_version(conn)
        if stored is not None and stored > SCHEMA_VERSION:
            raise DbSchemaError(
                f"数据库 schema v{stored} 高于当前支持版本 v{SCHEMA_VERSION}")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(_SCHEMA)
        if stored is None:
            conn.execute(
                "INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
        conn.commit()
        return conn
    except (OSError, sqlite3.Error, DbSchemaError):
        conn.close()
        raise


def open_store_readonly(path):
    """以 SQLite read-only URI 打开已有库；绝不创建或修改文件。"""
    db_path = os.path.expanduser(path)
    if not os.path.exists(db_path):
        raise FileNotFoundError(db_path)
    uri = pathlib.Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _upsert_rows(conn, table, rows, *, with_jd=False):
    """按 ``job_id`` 唯一键 upsert；返回 (inserted, updated)。

    ON CONFLICT 只更新 payload/updated_at/run_id（+jd），**不碰 first_seen_at**，
    从而保留"首次入库时间"语义。
    """
    if not rows:
        return 0, 0
    ids = [row[0] for row in rows]
    existing = set()
    for chunk in _chunked(ids, _SQLITE_MAX_VARS):
        placeholders = ",".join("?" for _ in chunk)
        existing.update(
            str(row[0]) for row in conn.execute(
                f"SELECT job_id FROM {table} WHERE job_id IN ({placeholders})", chunk)
        )
    inserted = sum(1 for jid in ids if jid not in existing)
    if with_jd:
        sql = (
            f"INSERT INTO {table} (job_id, jd, payload, first_seen_at, updated_at, run_id) "
            f"VALUES (?, ?, ?, ?, ?, ?) "
            f"ON CONFLICT(job_id) DO UPDATE SET jd=excluded.jd, payload=excluded.payload, "
            f"updated_at=excluded.updated_at, run_id=excluded.run_id"
        )
    else:
        sql = (
            f"INSERT INTO {table} (job_id, payload, first_seen_at, updated_at, run_id) "
            f"VALUES (?, ?, ?, ?, ?) "
            f"ON CONFLICT(job_id) DO UPDATE SET payload=excluded.payload, "
            f"updated_at=excluded.updated_at, run_id=excluded.run_id"
        )
    conn.executemany(sql, rows)
    conn.commit()
    return inserted, len(ids) - inserted


def upsert_jobs(conn, jobs, *, run_id=None, now=None):
    """列表记录增量入库（唯一键 upsert，与 JSON/CSV 并存）。"""
    ts = now or _utcnow()
    rows = []
    for job in jobs or []:
        if not isinstance(job, dict):
            continue
        job_id = str(job.get("job_id") or "").strip()
        if not job_id:
            continue
        rows.append((job_id, _payload_json(job), ts, ts, run_id))
    return _upsert_rows(conn, "jobs", rows)


def upsert_details(conn, details, *, run_id=None, now=None):
    """详情记录增量入库（抽出 jd 列便于 SQL 过滤/统计）。"""
    ts = now or _utcnow()
    rows = []
    for detail in details or []:
        if not isinstance(detail, dict):
            continue
        job_id = str(detail.get("job_id") or "").strip()
        if not job_id:
            continue
        jd = str(detail.get("jd") or "").strip()
        rows.append((job_id, jd, _payload_json(detail), ts, ts, run_id))
    return _upsert_rows(conn, "details", rows, with_jd=True)


def load_job_ids(conn):
    """库内全部列表 job_id 集合。"""
    return {str(row[0]) for row in conn.execute("SELECT job_id FROM jobs")}


def load_detail_ids(conn):
    """库内**含 JD** 的详情 job_id 集合（断点续抓种子）。"""
    return {str(row[0]) for row in conn.execute(
        "SELECT job_id FROM details WHERE jd IS NOT NULL AND TRIM(jd) != ''")}


def load_details(conn, job_ids=None):
    """读回详情记录（payload 反序列化为 dict），可按 job_id 子集过滤。

    仅返回含 JD 的行；损坏 payload 静默跳过（不阻塞续抓）。
    """
    results = []

    def collect(sql, params):
        for row in conn.execute(sql, params):
            try:
                payload = json.loads(row[0])
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            if isinstance(payload, dict) and payload.get("job_id"):
                results.append(payload)

    if job_ids is None:
        collect("SELECT payload FROM details WHERE jd IS NOT NULL AND TRIM(jd) != ''", [])
        return results
    ids = [str(j).strip() for j in job_ids if str(j).strip()]
    for chunk in _chunked(ids, _SQLITE_MAX_VARS):
        placeholders = ",".join("?" for _ in chunk)
        collect(
            f"SELECT payload FROM details WHERE jd IS NOT NULL AND TRIM(jd) != '' "
            f"AND job_id IN ({placeholders})", chunk)
    return results


def record_run(conn, run_id, *, mode=None, keyword=None, city=None,
               started_at=None, finished_at=_FINISHED_AT_UNSET, job_count=0,
               detail_count=0, now=None):
    """记录/更新一次 run；显式 ``finished_at=None`` 保持未完成状态。"""
    ts = now or _utcnow()
    finish = ts if finished_at is _FINISHED_AT_UNSET else finished_at
    conn.execute(
        "INSERT INTO runs (run_id, started_at, finished_at, mode, keyword, city, "
        "job_count, detail_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(run_id) DO UPDATE SET mode=excluded.mode, "
        "keyword=excluded.keyword, city=excluded.city, "
        "finished_at=excluded.finished_at, job_count=excluded.job_count, "
        "detail_count=excluded.detail_count",
        (run_id, started_at or ts, finish, mode, keyword, city,
         int(job_count or 0), int(detail_count or 0)),
    )
    conn.commit()
    return run_id


def stats(conn):
    """只读统计：列表/详情/含 JD/runs 计数与最近更新时间。"""
    db_path = None
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "main":
            db_path = row[2]
    stored_schema = None
    try:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        if row is not None:
            stored_schema = int(row[0])
    except (sqlite3.Error, TypeError, ValueError):
        stored_schema = None
    return {
        "db_path": db_path,
        "schema_version": stored_schema,
        "jobs": conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
        "details": conn.execute("SELECT COUNT(*) FROM details").fetchone()[0],
        "details_with_jd": conn.execute(
            "SELECT COUNT(*) FROM details WHERE jd IS NOT NULL AND TRIM(jd) != ''"
        ).fetchone()[0],
        "runs": conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
        "last_job_update": conn.execute("SELECT MAX(updated_at) FROM jobs").fetchone()[0],
    }


def _main(argv=None):
    """独立只读入口：``python -m scripts.db_store [DB_PATH]`` 打印统计。"""
    import argparse  # 局部导入：主流程无 CLI 依赖

    parser = argparse.ArgumentParser(
        prog="db_store",
        description=f"SQLite 增量层只读统计（默认库 {DEFAULT_DB_PATH}）")
    parser.add_argument("db", nargs="?", default=DEFAULT_DB_PATH,
                        help="SQLite 库路径（默认 ~/.boss-zhipin-scraper/boss.db）")
    args = parser.parse_args(argv)
    if not os.path.exists(args.db):
        print(f"数据库不存在: {args.db}")
        return 1
    try:
        conn = open_store_readonly(args.db)
    except (OSError, sqlite3.Error) as exc:
        print(f"数据库只读打开失败: {exc}")
        return 1
    try:
        info = stats(conn)
    except sqlite3.Error as exc:
        print(f"数据库统计失败: {exc}")
        return 1
    finally:
        conn.close()
    print(f"库: {info['db_path']}（schema v{info['schema_version']}）")
    print(f"列表: {info['jobs']} 条｜详情: {info['details']} 条"
          f"（含 JD {info['details_with_jd']}）｜runs: {info['runs']}")
    if info["last_job_update"]:
        print(f"最近更新: {info['last_job_update']}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
