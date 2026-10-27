#!/usr/bin/env python3
"""资产层迁移 / 回填（SQLite schema v2 → v3 + 历史数据补齐）。

为什么需要这一层（2026-10-28 资产层 P0，设计稿 §3）
---------------------------------------------------
库里的 v2 是「payload JSON 堆」：``jobs`` 只有 job_id/payload/时间戳，查一个城市、
一个薪资区间、一个招聘类型都要把 JSON 反序列化再跑 Python；旧行只有 payload，
没有可查询的规范化列。v3 在**不推翻旧表**的前提下加列/加表（``scripts/db_store.py``）：

- ``jobs`` 规范化列（title/company/city/district/salary_min/max/...）；
- ``job_versions``（JD 版本历史）、``job_flags``（派生标记）；
- ``runs`` 批次健康度/口径血缘列。

本脚本负责把**存量数据**搬进新结构：
1. ``db`` 阶段：打开库（``open_store`` 幂等迁移 schema）→ 从 payload 回填
   ``jobs`` 规范化列 → 从 ``details`` 回填 ``job_versions`` → 回填 ``job_flags``；
2. ``artifacts`` 阶段：扫产物目录（列表/详情/交付物快照），对库里已有的岗位
   **只补 NULL 列**、补历史 JD 版本与标记——文件是原始账，回填只补资产，不扩资产
   （不插入库里没有的岗位；需要扩库请走正式抓取/``--from-artifacts`` 重建）。

幂等：所有步骤只写差异，连跑第二次 ``backfilled`` 应为 0；``--dry-run`` 只统计
"会改多少"，不迁移 schema、不写任何行。

用法
----
    python3 scripts/assets_migrate.py                 # 迁移默认库 + 回填产物目录
    python3 scripts/assets_migrate.py --dry-run       # 只看要改什么（不写库）
    python3 scripts/assets_migrate.py --json          # agent 用：纯 JSON
    python3 scripts/assets_migrate.py --db /path/boss.db --result-dir /path/job-result

环境变量：``BOSS_DB_PATH`` / ``BOSS_RESULT_DIR`` 可覆盖默认路径（与 brief.py 一致）。

退出码：0 成功；1 失败（库不存在/损坏/schema 高于代码版本）；2 参数错误。
"""

import argparse
import json
import os
import pathlib
import sqlite3
import sys
import time
from datetime import UTC, datetime

try:
    from scripts import artifacts, db_store
except ImportError:  # pragma: no cover - 直接 python scripts/assets_migrate.py 时
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import artifacts
    import db_store


def default_db_path():
    """默认库路径：``BOSS_DB_PATH`` 环境变量优先（测试/多库场景可覆盖）。"""
    return os.environ.get("BOSS_DB_PATH") or db_store.DEFAULT_DB_PATH


def default_result_dir():
    """默认产物目录：``BOSS_RESULT_DIR`` 环境变量优先。"""
    return os.environ.get("BOSS_RESULT_DIR") or artifacts.default_result_dir()


def _now_iso():
    return datetime.now(UTC).isoformat(timespec="seconds")


def _read_json(path):
    """读一个 JSON 产物；坏文件返回 ``(False, None)``（不阻断迁移）。"""
    try:
        with open(path, encoding="utf-8") as handle:
            return True, json.load(handle)
    except (OSError, ValueError, UnicodeDecodeError):
        return False, None


def _empty_stats(result_dir=""):
    return {
        "result_dir": result_dir,
        "files_scanned": 0,
        "files_failed": 0,
        "records_scanned": 0,
        "jobs_matched": 0,
        "jobs_backfilled": 0,
        "versions_backfilled": 0,
        "flags_backfilled": 0,
        "not_in_db": 0,
        "skipped": 0,
        "failed": 0,
        "error": "",
    }


def backfill_from_artifacts(conn, result_dir=None, *, dry_run=False,
                            version_memo=None, column_memo=None,
                            flag_memo=None):
    """扫产物目录回填资产库（只补 NULL 列 / 补历史 JD 版本与标记）。幂等。

    返回统计（``files_scanned`` / ``records_scanned`` / ``jobs_matched`` /
    ``jobs_backfilled`` / ``versions_backfilled`` / ``flags_backfilled`` /
    ``not_in_db`` / ``skipped`` / ``failed``）。

    三个 ``*_memo`` 是 dry-run 的影子表（版本/列/标记），与 DB 阶段模拟共用后
    dry-run 报的"将回填量"才准（否则每条重复快照都被算成一次追加/回填）。
    """
    directory = os.path.abspath(os.path.expanduser(
        result_dir or default_result_dir()))
    stats = _empty_stats(directory)
    if not os.path.isdir(directory):
        stats["error"] = f"产物目录不存在: {directory}"
        return stats
    try:
        index = artifacts.build_index(directory, include_archive=True, stale_days=-1)
    except (OSError, TypeError, ValueError) as exc:
        stats["error"] = f"产物清单生成失败: {exc}"
        return stats
    index_dir = index.get("result_dir") or directory
    # 按采集时间升序处理：JD 版本号是追加顺序，从旧到新追才符合"变化历史"直觉
    entries = sorted(
        index.get("artifacts") or [],
        key=lambda item: (str(item.get("created_at") or ""),
                          str(item.get("path") or "")))
    for entry in entries:
        kind = entry.get("kind")
        if kind not in ("list", "deliverable", "details"):
            continue
        path = os.path.join(index_dir, str(entry.get("path") or ""))
        ok, payload = _read_json(path)
        stats["files_scanned"] += 1
        if not ok:
            stats["files_failed"] += 1
            stats["failed"] += 1
            continue
        if kind == "details":
            _backfill_detail_records(conn, payload, entry, stats, dry_run,
                                     version_memo)
        else:
            _backfill_job_records(conn, payload, entry, stats, dry_run,
                                  version_memo, column_memo, flag_memo)
    return stats


def _backfill_detail_records(conn, payload, entry, stats, dry_run, memo=None):
    """详情快照（list payload）→ 补 job_versions（同 JD 不重复追加）。"""
    records = payload if isinstance(payload, list) else []
    captured_at = str(entry.get("created_at") or "").strip() or None
    for record in records:
        if not isinstance(record, dict):
            stats["skipped"] += 1
            continue
        stats["records_scanned"] += 1
        job_id = str(record.get("job_id") or "").strip()
        if not job_id or not str(record.get("jd") or "").strip():
            stats["skipped"] += 1
            continue
        version = db_store.append_version_from_payload(
            conn, record, captured_at=captured_at, run_id=None,
            source="artifact:details", dry_run=dry_run, memo=memo)
        if version is None:
            stats["skipped"] += 1
        else:
            stats["versions_backfilled"] += 1


def _backfill_job_records(conn, payload, entry, stats, dry_run, version_memo=None,
                          column_memo=None, flag_memo=None):
    """列表/交付物快照（dict payload）→ 补 jobs 缺失列 + 历史 JD 版本 + 标记。"""
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(jobs, list):
        stats["failed"] += 1
        return
    captured_at = str(entry.get("created_at") or "").strip() or None
    for job in jobs:
        if not isinstance(job, dict):
            stats["skipped"] += 1
            continue
        job_id = str(job.get("job_id") or "").strip()
        if not job_id:
            stats["skipped"] += 1
            continue
        stats["records_scanned"] += 1
        merged = db_store.merge_job_columns(conn, job, dry_run=dry_run,
                                            memo=column_memo)
        if not merged["matched"]:
            # 文件里比库多出来的历史岗位：不扩资产（原始账仍在文件里，可随时重建）
            stats["not_in_db"] += 1
            stats["skipped"] += 1
            continue
        stats["jobs_matched"] += 1
        if merged["updated"]:
            stats["jobs_backfilled"] += 1
        if str(job.get("jd") or "").strip():
            version = db_store.append_version_from_payload(
                conn, job, captured_at=captured_at, run_id=None,
                source="artifact:list", dry_run=dry_run, memo=version_memo)
            if version is not None:
                stats["versions_backfilled"] += 1
        stats["flags_backfilled"] += db_store.write_job_flags(
            conn, job_id, db_store.extract_job_flags(job), run_id=None,
            dry_run=dry_run, memo=flag_memo)


def _empty_report(dry_run):
    return {
        "ok": False,
        "dry_run": bool(dry_run),
        "generated_at": _now_iso(),
        "schema": {
            "before": None,
            "after": None,
            "target": db_store.SCHEMA_VERSION,
            "migrated": False,
            "needs_migration": False,
        },
        "db_path": "",
        "result_dir": "",
        "db": None,
        "artifacts": None,
        "errors": [],
        "warnings": [],
    }


def run_migration(db_path=None, result_dir=None, *, dry_run=False,
                  stale_days=None, include_artifacts=True):
    """执行（或 dry-run）一次完整迁移/回填，返回报告 dict（不抛业务异常）。

    报告结构固定（agent 直接消费）：``ok / dry_run / schema / db_path /
    result_dir / db / artifacts / errors / warnings``。
    """
    report = _empty_report(dry_run)
    path = os.path.abspath(os.path.expanduser(db_path or default_db_path()))
    directory = os.path.abspath(os.path.expanduser(
        result_dir or default_result_dir()))
    report["db_path"] = path
    report["result_dir"] = directory
    if not os.path.exists(path):
        report["errors"].append(f"数据库不存在: {path}")
        return report
    try:
        before = db_store.schema_version_at(path)
    except (OSError, sqlite3.Error) as exc:
        report["errors"].append(f"读取 schema 版本失败: {exc}")
        return report
    report["schema"]["before"] = before
    report["schema"]["needs_migration"] = before != db_store.SCHEMA_VERSION
    try:
        if dry_run:
            # 只读打开：不迁移、不写行；逐项统计"会改多少"
            conn = db_store.open_store_readonly(path)
        else:
            conn = db_store.open_store(path)
    except FileNotFoundError as exc:
        report["errors"].append(f"数据库不存在: {exc}")
        return report
    except (OSError, sqlite3.Error, db_store.DbSchemaError) as exc:
        report["errors"].append(f"打开数据库失败: {exc}")
        return report
    try:
        report["schema"]["after"] = db_store.stored_schema_version(conn)
        report["schema"]["migrated"] = (
            not dry_run and report["schema"]["after"] == db_store.SCHEMA_VERSION
            and before != db_store.SCHEMA_VERSION)
        # dry-run 没有落库：用影子表（版本/列/标记）模拟本轮写入，避免重复计数
        version_memo = {} if dry_run else None
        column_memo = {} if dry_run else None
        flag_memo = {} if dry_run else None
        counts = db_store.stats(conn)
        report["db"] = {
            "counts": {
                "jobs": counts["jobs"],
                "details": counts["details"],
                "details_with_jd": counts["details_with_jd"],
                "runs": counts["runs"],
            },
            "jobs_backfill": db_store.backfill_jobs(
                conn, dry_run=dry_run, stale_days=stale_days, memo=column_memo),
            "versions_backfill": db_store.backfill_job_versions(
                conn, dry_run=dry_run, memo=version_memo),
            "flags_backfill": db_store.backfill_job_flags(
                conn, dry_run=dry_run, memo=flag_memo),
            "runs_backfill": db_store.backfill_runs(conn, dry_run=dry_run),
        }
        if include_artifacts:
            report["artifacts"] = backfill_from_artifacts(
                conn, directory, dry_run=dry_run,
                version_memo=version_memo, column_memo=column_memo,
                flag_memo=flag_memo)
            if report["artifacts"].get("error"):
                report["warnings"].append(report["artifacts"]["error"])
        else:
            report["artifacts"] = {"skipped": True, "result_dir": directory}
        # 资产账统计放在所有回填之后：真实运行要报"迁移后的最终账"，
        # dry-run 时报的是当前库现状（没有写入，本来就不变）
        report["db"]["asset_stats"] = db_store.asset_stats(conn)
    except (OSError, sqlite3.Error, db_store.DbSchemaError) as exc:
        report["errors"].append(f"迁移/回填失败: {exc}")
    finally:
        conn.close()
    report["ok"] = not report["errors"]
    return report


def format_human(report):
    """人看的一屏结果（数字全部来自真实统计，不估算）。"""
    mode = "dry-run（只统计，不写库）" if report.get("dry_run") else "执行"
    schema = report.get("schema") or {}
    lines = [
        f"资产层迁移/回填（schema → v{schema.get('target')}）｜{mode}",
        f"库: {report.get('db_path') or '(未指定)'}",
    ]
    if schema.get("before") is None and not report.get("errors"):
        lines.append("  schema: 未知")
    elif report.get("dry_run"):
        lines.append(
            f"  schema: 当前 v{schema.get('before')}"
            f"（{'需迁移' if schema.get('needs_migration') else '已是最新'}，dry-run 未迁移）")
    else:
        status = "已迁移" if schema.get("migrated") else (
            "已是最新" if not schema.get("needs_migration") else "未迁移")
        lines.append(f"  schema: v{schema.get('before')} → "
                     f"v{schema.get('after')}（{status}）")
    db = report.get("db")
    if db:
        counts = db.get("counts") or {}
        lines.append(
            f"  存量: 列表 {counts.get('jobs', 0)}｜详情 {counts.get('details', 0)}"
            f"（含 JD {counts.get('details_with_jd', 0)}）｜runs {counts.get('runs', 0)}")
        for label, key in (
            ("jobs 规范化列", "jobs_backfill"),
            ("JD 版本", "versions_backfill"),
            ("岗位标记", "flags_backfill"),
            ("runs 元数据", "runs_backfill"),
        ):
            stats = db.get(key) or {}
            lines.append(
                f"  {label}: 扫描 {stats.get('scanned', 0)}"
                f"｜回填 {stats.get('backfilled', 0)}"
                f"｜跳过 {stats.get('skipped', 0)}"
                f"｜失败 {stats.get('failed', 0)}")
        columns = (db.get("jobs_backfill") or {}).get("columns_filled") or {}
        filled = {name: count for name, count in columns.items() if count}
        if filled:
            lines.append("  回填列: " + "、".join(
                f"{name}×{count}" for name, count in sorted(filled.items())))
        asset = db.get("asset_stats") or {}
        lines.append(
            f"  资产: 身份组 {asset.get('canonical_groups', 0)}"
            f"｜JD 版本 {asset.get('job_versions', 0)}"
            f"｜标记 {asset.get('job_flags', 0)}"
            f"｜状态 {asset.get('states') or {}}")
    art = report.get("artifacts")
    if art is not None:
        if art.get("skipped"):
            lines.append("产物目录: 已跳过（--no-artifacts）")
        elif art.get("error"):
            lines.append(f"产物目录: 跳过（{art['error']}）")
        else:
            lines.append(
                f"产物目录: {art.get('result_dir')}"
                f"｜文件 {art.get('files_scanned', 0)}"
                f"（失败 {art.get('files_failed', 0)}）"
                f"｜记录 {art.get('records_scanned', 0)}")
            lines.append(
                f"  产物回填: 命中岗位 {art.get('jobs_matched', 0)}"
                f"｜补列岗位 {art.get('jobs_backfilled', 0)}"
                f"｜补 JD 版本 {art.get('versions_backfilled', 0)}"
                f"｜补标记 {art.get('flags_backfilled', 0)}"
                f"｜库外岗位 {art.get('not_in_db', 0)}"
                f"｜跳过 {art.get('skipped', 0)}"
                f"｜失败 {art.get('failed', 0)}")
    for warning in report.get("warnings") or []:
        lines.append(f"⚠️  {warning}")
    for error in report.get("errors") or []:
        lines.append(f"❌ {error}")
    lines.append("✅ 完成（幂等：再跑一次 backfilled 应为 0）"
                 if report.get("ok") else "❌ 失败")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="assets_migrate",
        description="资产层迁移/回填：SQLite schema v2→v3 + 历史 payload/产物补列",
    )
    parser.add_argument(
        "--db", default=None,
        help=f"SQLite 库路径（默认 BOSS_DB_PATH 或 {db_store.DEFAULT_DB_PATH}）")
    parser.add_argument(
        "--result-dir", default=None,
        help="产物目录（默认 BOSS_RESULT_DIR 或 ~/.boss-zhipin-scraper/job-result）")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="只统计会回填多少，不迁移 schema、不写任何行")
    parser.add_argument(
        "--json", action="store_true",
        help="输出机器可读 JSON（agent 用，stdout 无横幅）")
    parser.add_argument(
        "--no-artifacts", action="store_true",
        help="只迁移/回填 DB，不扫产物目录")
    parser.add_argument(
        "--stale-days", type=int, default=db_store.STATE_STALE_DAYS,
        help=f"超过 N 天未再出现标 stale（默认 {db_store.STATE_STALE_DAYS}）")
    args = parser.parse_args(argv)
    started = time.monotonic()
    report = run_migration(
        args.db, result_dir=args.result_dir, dry_run=args.dry_run,
        stale_days=args.stale_days, include_artifacts=not args.no_artifacts)
    report["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_human(report))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
