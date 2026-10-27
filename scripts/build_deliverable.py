"""交付物构建入口：把「原材料」合成「端上桌的菜」，走生产合并路径落盘。

两种输入来源，输出完全同一条生产路径（``export_contract._merge_jd_into_export``：
口径一 / 删库 / 派生字段 / 验收 / CSV 一致）：

1. **文件模式**（原有）：``--lists/--details/--old`` 手工指定哪几个列表/详情/旧交付；
2. **原材料库模式**（issue #184）：``--from-db [PATH]`` 从 SQLite 库按口径取候选与详情——
   多轮抓取的合并/去重由库承担，不再需要翻文件清单、写一次性 temp 拼装脚本。

用法（重建 143 交付物那次就是这么拼的）：

    python3 scripts/build_deliverable.py \\
        --result-dir ~/.boss-zhipin-scraper/job-result \\
        --lists boss_list_20261023_杭州AI实习_AI产品实习生.json \\
                boss_list_20261023_杭州产品实习生.json \\
        --details boss_details_20261023_杭州AI实习.json \\
                  boss_details_20261023_杭州AI实习_rest.json \\
        --old boss_jobs_20261023_杭州实习_AI非技术岗.json \\
        --target boss_jobs_20261023_杭州实习_AI非技术岗.json \\
        --keyword "产品实习生,AI产品实习生" --city 杭州 \\
        --scope "杭州实习 AI 非技术岗：销售/技术/噪声已删库"

用法（issue #184，从库一键重建；实测与手工四库合并逐 job_id 一致）：

    python3 scripts/build_deliverable.py \\
        --from-db --city 杭州 --recruit-type intern,campus \\
        --target boss_jobs_20261027_实习校招AI非技术岗.json \\
        --scope "杭州实习+校招 AI 非技术岗（库口径）"
"""

import argparse
import csv
import json
import os
import pathlib
import sqlite3
import sys
from datetime import UTC, datetime

try:
    from scripts import artifacts, db_store, semantics
    from scripts import export_contract as ec
except ImportError:  # 直接 python scripts/build_deliverable.py 运行时
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import artifacts
    import db_store
    import export_contract as ec
    import semantics

RECRUIT_TYPES = ("intern", "social", "campus")


def _load_jobs(path):
    """读列表/详情/旧交付文件，返回 jobs 列表（两种形态都认）。"""
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        jobs = data.get("jobs")
        return jobs if isinstance(jobs, list) else []
    return []


def _load_payload(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def load_exclude_ids(path):
    """读取排除名单（job_id）：JSON 数组 / {"job_ids": [...]} / 逗号或换行文本。"""
    text = str(path or "").strip()
    if not text:
        return set()
    full = os.path.abspath(os.path.expanduser(text))
    with open(full, encoding="utf-8") as handle:
        raw = handle.read()
    ids = set()
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if isinstance(data, dict):
        values = data.get("job_ids") or data.get("exclude_job_ids") or []
    elif isinstance(data, list):
        values = data
    else:
        values = [part for part in raw.replace("\n", ",").split(",")]
    for item in values or []:
        jid = str(item or "").strip()
        if jid:
            ids.add(jid)
    return ids


def load_artifacts_scope(result_dir, city="", include_archived=True):
    """从产物目录（列表/详情快照，含 archive）并集取候选与详情。

    为什么要这个口径：DB 只从启用 `--db` 的那天开始积累，更早的 JD 只存在产物
    文件里（实测文件并集比库多 31 条详情）。文件是唯一真相，库只是缓存；
    本函数只读产物，不移动/不改文件。

    返回 ``(jobs, details, stats)``；details 是含 jd 的 payload。
    """
    result_dir = os.path.abspath(os.path.expanduser(result_dir))
    index = artifacts.build_index(result_dir, include_archive=include_archived,
                                  stale_days=-1)
    city_text = str(city or "").strip()
    jobs = {}
    files = {"lists": 0, "details": 0}
    for entry in index.get("artifacts") or []:
        if entry.get("kind") not in ("list", "deliverable"):
            continue
        payload = _load_payload(os.path.join(result_dir, entry["path"]))
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            continue
        entry_city = str((entry.get("scope") or {}).get("city")
                         or payload.get("city") or "").strip()
        if city_text and entry_city and entry_city != city_text:
            continue
        files["lists"] += 1
        for job in payload["jobs"]:
            if isinstance(job, dict) and job.get("job_id"):
                jobs.setdefault(str(job["job_id"]), job)
    ids = set(jobs)
    detail_map = {}
    for entry in index.get("artifacts") or []:
        if entry.get("kind") != "details":
            continue
        payload = _load_payload(os.path.join(result_dir, entry["path"]))
        if not isinstance(payload, list):
            continue
        files["details"] += 1
        for detail in payload:
            if not isinstance(detail, dict):
                continue
            jid = str(detail.get("job_id") or "")
            if jid in ids and str(detail.get("jd") or "").strip():
                detail_map.setdefault(jid, detail)
    stats = {
        "result_dir": result_dir,
        "jobs_in_files": len(jobs),
        "details_with_jd": len(detail_map),
        "files": files,
        "include_archived": bool(include_archived),
        "city": city_text,
    }
    return list(jobs.values()), list(detail_map.values()), stats


def _collect_details(paths):
    """详情按 job_id 归拢：有 JD 的才要；同岗多条取 JD 非空的第一条。"""
    details = {}
    for path in paths:
        for job in _load_jobs(path):
            if not isinstance(job, dict):
                continue
            jid = str(job.get("job_id") or "")
            if not jid or jid in details:
                continue
            if str(job.get("jd") or "").strip():
                details[jid] = job
    return details


def parse_recruit_types(value):
    """``--recruit-type`` 值 → ``None``（全要）或类型集合。

    支持逗号组合（如 ``intern,campus``）：跨类型交付物（实习+校招）此前只能
    抓两种再手工并，现在一次过滤出来（issue #184）。
    """
    text = str(value or "all").strip()
    if text in ("", "all"):
        return None
    parts = [item.strip() for item in text.split(",") if item.strip()]
    unknown = [item for item in parts if item not in RECRUIT_TYPES]
    if not parts or unknown:
        raise ValueError(
            f"--recruit-type 非法：{value!r}"
            f"（可选 all / {' / '.join(RECRUIT_TYPES)}，可用逗号组合）")
    return set(parts)


def parse_since(value):
    """``--since`` 值 → 带时区 datetime（空 → None）。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(
            f"--since 需要 YYYY-MM-DD（或 ISO 时间）：{value!r}") from exc
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _parse_updated_at(value):
    try:
        moment = datetime.fromisoformat(str(value or "").strip())
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def load_db_scope(db_path, city="", keyword="", since=""):
    """从 SQLite 原材料库取候选（列表）与详情，返回 ``(jobs, details, stats)``。

    - 只读打开（``open_store_readonly``），**绝不改库**；
    - ``city`` 精确匹配库内口径（upsert 时记录的搜索城市）；
    - ``keyword`` 逗号分隔、任一命中即选中：匹配 ``jobs.keyword``（新写入）或
      关联 run 的关键词标签（旧行 keyword 为 NULL 时兜底）；
    - ``since`` 按 ``updated_at``（= 最后一次在列表里出现的时间）过滤，
      用于「只要最近还在架上」的口径；空 = 全量。

    返回的 details 是详情 payload（含 jd），供生产合并路径使用。
    """
    path = os.path.abspath(os.path.expanduser(str(db_path)))
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    since_moment = parse_since(since)
    city_text = str(city or "").strip()
    tokens = [item.strip().lower()
              for item in str(keyword or "").split(",") if item.strip()]
    conn = db_store.open_store_readonly(path)
    try:
        rows = list(conn.execute(
            "SELECT j.payload AS payload, j.updated_at AS updated_at, "
            "       j.city AS city, j.keyword AS keyword, "
            "       r.keyword AS run_keyword "
            "FROM jobs j LEFT JOIN runs r ON j.run_id = r.run_id"))
        stats = {"db": path, "jobs_in_db": len(rows), "unknown_city": 0}
        jobs = []
        for row in rows:
            row_city = str(row["city"] or "").strip()
            if city_text and row_city != city_text:
                if not row_city:
                    stats["unknown_city"] += 1
                continue
            if since_moment is not None:
                updated = _parse_updated_at(row["updated_at"])
                if updated is None or updated < since_moment:
                    continue
            if tokens:
                haystack = " ".join((
                    str(row["keyword"] or ""),
                    str(row["run_keyword"] or ""))).lower()
                if not any(token in haystack for token in tokens):
                    continue
            try:
                payload = json.loads(row["payload"])
            except (TypeError, ValueError):
                continue
            if isinstance(payload, dict) and payload.get("job_id"):
                jobs.append(payload)
        job_ids = [str(job.get("job_id")) for job in jobs]
        details = db_store.load_details(conn, job_ids) if job_ids else []
    finally:
        conn.close()
    stats.update({
        "jobs_selected": len(jobs),
        "details_selected": len(details),
        "city": city_text,
        "keyword": str(keyword or ""),
        "since": str(since or ""),
    })
    return jobs, details, stats


def _db_stats_meta(stats, recruit_types):
    """把库口径写成产物 meta.source（交付物可溯源：哪份库、什么口径）。"""
    return {
        "kind": "db",
        "db": stats.get("db", ""),
        "jobs_in_db": stats.get("jobs_in_db", 0),
        "jobs_selected": stats.get("jobs_selected", 0),
        "details_selected": stats.get("details_selected", 0),
        "city": stats.get("city", ""),
        "keyword": stats.get("keyword", ""),
        "since": stats.get("since", ""),
        "recruit_types": sorted(recruit_types) if recruit_types else ["all"],
    }


def _scope_key(args, recruit_types):
    """交付物系列标识：同 key 的多次构建互为版本（清单里旧的标 superseded）。

    显式 ``--scope-key`` 优先；否则按 城市|招聘类型|口径说明(或关键词) 推导，
    让"同一个口径反复重建"能自动认出是同一份菜的版本。
    """
    explicit = str(getattr(args, "scope_key", "") or "").strip()
    if explicit:
        return explicit
    city = str(args.city or "").strip() or "*"
    label = str(args.scope or args.keyword or "").strip() or "*"
    types = ",".join(sorted(recruit_types)) if recruit_types else "all"
    return f"{city}|{types}|{label}"


def _artifact_meta(args, recruit_types, db_stats, artifacts_stats=None,
                   excluded_count=0):
    """构建时盖章（产物层统一清单据此识别交付物系列，不再靠文件名猜）。"""
    if db_stats is not None:
        inputs = {
            "kind": "db",
            "db": os.path.basename(str(db_stats.get("db") or "")),
            "jobs_in_db": db_stats.get("jobs_in_db", 0),
            "jobs_selected": db_stats.get("jobs_selected", 0),
        }
    elif artifacts_stats is not None:
        inputs = {
            "kind": "artifacts",
            "result_dir": os.path.basename(str(artifacts_stats.get("result_dir") or "")),
            "jobs_in_files": artifacts_stats.get("jobs_in_files", 0),
            "details_with_jd": artifacts_stats.get("details_with_jd", 0),
            "files": artifacts_stats.get("files") or {},
        }
    else:
        inputs = {
            "kind": "files",
            "lists": [os.path.basename(str(p)) for p in (args.lists or [])],
            "details": [os.path.basename(str(p)) for p in (args.details or [])],
            "old": os.path.basename(str(args.old)) if args.old else "",
        }
    if excluded_count:
        inputs["excluded_job_ids"] = int(excluded_count)
    return {
        "kind": "deliverable",
        "format_version": 1,
        "builder": "build_deliverable",
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "scope_key": _scope_key(args, recruit_types),
        "scope": {
            "city": str(args.city or "").strip(),
            "keyword": str(args.keyword or "").strip(),
            "label": str(args.scope or "").strip(),
            "recruit_types": sorted(recruit_types) if recruit_types else ["all"],
        },
        "inputs": inputs,
    }


def _artifacts_stats_meta(stats, recruit_types, excluded_count=0):
    return {
        "kind": "artifacts",
        "result_dir": stats.get("result_dir", ""),
        "jobs_in_files": stats.get("jobs_in_files", 0),
        "details_with_jd": stats.get("details_with_jd", 0),
        "files": stats.get("files") or {},
        "include_archived": stats.get("include_archived", True),
        "city": stats.get("city", ""),
        "recruit_types": sorted(recruit_types) if recruit_types else ["all"],
        "excluded_job_ids": int(excluded_count),
    }


def build(args):
    """拼装并落盘，返回 (kept, exit_code)。"""
    try:
        recruit_types = parse_recruit_types(args.recruit_type)
        parse_since(args.since)  # 只做格式校验，真正的过滤在 load_db_scope
    except ValueError as exc:
        print(exc)
        return 0, 2
    if args.old and not os.path.exists(args.old):
        print(f"输入文件不存在：{args.old}")
        return 0, 2
    try:
        excluded = load_exclude_ids(args.exclude_job_ids_file)
    except OSError as exc:
        print(f"排除名单读取失败：{exc}")
        return 0, 2

    db_stats = None
    artifacts_stats = None
    if args.from_db:
        if args.lists or args.details or args.from_artifacts:
            print("--from-db 与 --lists/--details/--from-artifacts 不能同时使用")
            return 0, 2
        try:
            db_jobs, db_details, db_stats = load_db_scope(
                args.from_db, city=args.city, keyword=args.keyword,
                since=args.since)
        except FileNotFoundError:
            print(f"数据库不存在：{args.from_db}")
            return 0, 2
        except (OSError, ValueError, sqlite3.Error) as exc:
            print(f"读取原材料库失败：{exc}")
            return 0, 2
        if not db_jobs:
            print("原材料库没有匹配岗位（检查 --city/--keyword/--since）")
            return 0, 2
        pool = {}
        for job in db_jobs:
            if isinstance(job, dict) and job.get("job_id"):
                pool[str(job["job_id"])] = job
        print(f"原材料库: 库内 {db_stats['jobs_in_db']} 条 → 口径选中 {len(pool)} 条"
              f"（详情 {db_stats['details_selected']}）")
        if not str(args.city or "").strip():
            print("  ⚠️  未给 --city：按全城市口径重建（多城市会混在一起，建议给）")
        if db_stats.get("unknown_city"):
            print(f"  ⚠️  {db_stats['unknown_city']} 条历史记录无城市口径"
                  "（旧库行），按 --city 过滤时已跳过")
        details = {}
        for detail in db_details:
            if not isinstance(detail, dict):
                continue
            jid = str(detail.get("job_id") or "")
            if jid and str(detail.get("jd") or "").strip():
                details[jid] = detail
    elif args.from_artifacts:
        if args.lists or args.details:
            print("--from-artifacts 与 --lists/--details 不能同时使用")
            return 0, 2
        try:
            art_jobs, art_details, artifacts_stats = load_artifacts_scope(
                args.result_dir, city=args.city)
        except (OSError, ValueError) as exc:
            print(f"读取产物目录失败：{exc}")
            return 0, 2
        if not art_jobs:
            print("产物目录没有匹配岗位（检查 --city）")
            return 0, 2
        pool = {}
        for job in art_jobs:
            if isinstance(job, dict) and job.get("job_id"):
                pool[str(job["job_id"])] = job
        details = {}
        for detail in art_details:
            if isinstance(detail, dict):
                jid = str(detail.get("job_id") or "")
                if jid and str(detail.get("jd") or "").strip():
                    details[jid] = detail
        print(f"产物并集: {artifacts_stats['jobs_in_files']} 个岗位"
              f"（列表文件 {artifacts_stats['files']['lists']} / 详情文件 "
              f"{artifacts_stats['files']['details']}）"
              f"｜有 JD {artifacts_stats['details_with_jd']}｜含 archive")
    else:
        if not args.lists or not args.details:
            print("文件模式需要 --lists 和 --details（或改用 --from-db 从库重建）")
            return 0, 2
        missing = [p for p in list(args.lists) + list(args.details)
                   if not os.path.exists(p)]
        if missing:
            print(f"输入文件不存在：{', '.join(missing)}")
            return 0, 2
        # ---- 1) 候选池：列表 ----
        pool = {}
        for path in args.lists:
            for job in _load_jobs(path):
                if isinstance(job, dict) and job.get("job_id"):
                    pool[str(job["job_id"])] = job
        details = _collect_details(args.details)

    # ---- recruit-type 过滤（文件/库两种模式同口径）----
    if recruit_types is not None:
        pool = {jid: job for jid, job in pool.items()
                if semantics.classify_recruit_type(job) in recruit_types}
    print(f"候选: {len(pool)}")

    # ---- 旧交付回填：候选补齐 + JD 回填（两种模式都可用）----
    old_jobs = []
    if args.old:
        old_jobs = [j for j in _load_jobs(args.old) if isinstance(j, dict)]
        for job in old_jobs:
            if job.get("job_id"):
                pool.setdefault(str(job["job_id"]), job)
    for job in old_jobs:
        jid = str(job.get("job_id") or "")
        if jid and jid not in details and str(job.get("jd") or "").strip():
            details[jid] = job
    if excluded:
        before = len(pool)
        pool = {jid: job for jid, job in pool.items() if jid not in excluded}
        details = {jid: detail for jid, detail in details.items()
                   if jid not in excluded}
        print(f"排除名单: {len(excluded)} 个 job_id → 候选 {before} → {len(pool)}")
    print(f"详情(JD): {len(details)}")

    # ---- 走生产合并路径 ----
    base = {"keyword": args.keyword, "city": args.city,
            "jobs": list(pool.values()), "job_count": len(pool),
            "total": len(pool)}
    if args.target_roots:
        base["target_roots"] = [r.strip() for r in args.target_roots.split(",")
                                if r.strip()]
    target = args.target
    if not os.path.isabs(target):
        target = os.path.join(args.result_dir, target)
    # 交付物重建必须是「以本次候选为全量」的语义：否则 target 已存在时
    # _merge_jd_into_export 会优先读旧交付物，只更新旧岗位、**新岗位永远进不来**
    # （2026-10-28 实测：每日重建计数一直停在 242，实际规则保留了 245）。
    prefer_base = bool(args.from_db or args.from_artifacts)
    if prefer_base and os.path.exists(target):
        old = _load_payload(target)
        if isinstance(old, dict):
            for key in ("dropped_junk", "dropped_no_jd"):
                if key in old and key not in base:
                    base[key] = old[key]
            if "target_roots" not in base and old.get("target_roots"):
                base["target_roots"] = old["target_roots"]
    extra = {}
    if args.scope:
        extra["scope"] = args.scope
    if db_stats is not None:
        extra["source"] = _db_stats_meta(db_stats, recruit_types)
    elif artifacts_stats is not None:
        extra["source"] = _artifacts_stats_meta(artifacts_stats, recruit_types,
                                                len(excluded))
    # 产物层统一：交付物自带身份/口径/输入血缘，清单据此排序与取代
    extra["artifact"] = _artifact_meta(args, recruit_types, db_stats,
                                       artifacts_stats, len(excluded))
    # 口径可回溯：每条交付物记录规则版本，--why/审计据此解释"为什么留/删"。
    extra["ruleset_version"] = semantics.RULESET_VERSION
    res = ec._merge_jd_into_export(target, list(details.values()), base=base,
                                   keep_junk=args.keep_junk,
                                   extra_meta=extra or None,
                                   prefer_base=prefer_base)
    if res is None:
        print("合并失败：输入不可处理")
        return 0, 1
    kept, dropped = res
    print(f"合并结果 (kept, dropped_no_jd): {res}")
    if kept == 0:
        print(" kept=0，未产出交付物")
        return 0, 1

    with open(target, encoding="utf-8") as handle:
        final = json.load(handle)
    print(f"最终条数: {final.get('job_count')}")
    print(f"删库: {len(final.get('dropped_junk') or [])} 条")
    print(f"疑似销售(预警): {len(final.get('suspect_sales_jd') or [])} 条")
    acceptance = final.get("acceptance") or {}
    print(f"验收: ok={acceptance.get('ok')} "
          f"error={acceptance.get('error_count')} "
          f"warn={acceptance.get('warn_count')}")
    for finding in acceptance.get("findings") or []:
        if finding.get("level") != "info":
            print(f"    {finding.get('level')} {finding.get('id')} "
                  f"{str(finding.get('detail'))[:80]}")

    # ---- 4) CSV（表头沿用已存在的交付物 CSV，否则用首条 keys）----
    if not args.no_csv:
        csv_path = os.path.splitext(target)[0] + ".csv"
        try:
            with open(csv_path, encoding="utf-8-sig") as handle:
                header = next(csv.reader(handle))
        except (FileNotFoundError, StopIteration, OSError):
            header = list(final["jobs"][0].keys())
        try:
            with open(csv_path, "w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=header,
                                        extrasaction="ignore")
                writer.writeheader()
                for record in final["jobs"]:
                    row = {c: record.get(c, "") for c in header}
                    for column, value in row.items():
                        if isinstance(value, list):
                            row[column] = " | ".join(str(v) for v in value)
                    writer.writerow(row)
        except OSError as exc:
            print(f"CSV 写出失败：{exc}")
            return kept, 1
        print(f"CSV 写出: {os.path.basename(csv_path)}")
    return kept, 0


def _default_result_dir():
    return os.path.join(os.path.expanduser("~"),
                        ".boss-zhipin-scraper", "job-result")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="交付物构建：列表+详情+旧交付（或 SQLite 原材料库）"
                    "走生产合并路径落盘")
    parser.add_argument("--result-dir", default=_default_result_dir(),
                        help="job-result 目录（相对路径的 target 落在这里）")
    parser.add_argument("--lists", nargs="+", default=None,
                        help="列表文件（多个；文件模式必填，与 --from-db 互斥）")
    parser.add_argument("--details", nargs="+", default=None,
                        help="详情文件（多个，list 或 {jobs:[...]} 都认；文件模式必填）")
    parser.add_argument("--from-db", nargs="?", const=db_store.DEFAULT_DB_PATH,
                        default=None, metavar="PATH",
                        help="从 SQLite 原材料库重建（不带值用默认库；与 "
                             "--lists/--details 互斥。issue #184）")
    parser.add_argument("--from-artifacts", action="store_true",
                        help="从产物目录（列表/详情快照并集，含 archive）重建："
                             "取全历史口径，库只从启用 --db 那天起算；只读产物")
    parser.add_argument("--exclude-job-ids-file", default="",
                        help="排除名单文件（JSON 数组 / {job_ids:[]} / 逗号文本）："
                             "构建前从候选池剔除（如人工杂项清单）")
    parser.add_argument("--since", default="",
                        help="只保留该日期（YYYY-MM-DD）之后仍在列表里出现过的岗位"
                             "（按库内 updated_at，UTC；仅 --from-db 生效）")
    parser.add_argument("--old", default="",
                        help="旧交付文件（候选+JD 回填，可选）")
    parser.add_argument("--target", required=True,
                        help="输出交付物文件名（写进 result-dir）或绝对路径")
    parser.add_argument("--keyword", default="",
                        help="写入产物 meta 的关键词；--from-db 时也按它过滤"
                             "（逗号分隔、任一命中，匹配库内关键词或 run 标签）")
    parser.add_argument("--city", default="",
                        help="写入产物 meta 的城市；--from-db 时按它精确过滤")
    parser.add_argument("--target-roots", default="",
                        help="写入 meta.target_roots 的词根列表（逗号分隔；"
                             "P11 优先用它核对标题命中率，不给就退回 keyword 切分）")
    parser.add_argument("--scope", default="",
                        help="写入 extra_meta.scope 的口径说明")
    parser.add_argument("--scope-key", default="",
                        help="交付物系列标识（产物清单用它判定同口径新旧版本；"
                             "不传则按 城市|招聘类型|口径或关键词 推导）")
    parser.add_argument("--recruit-type", default="all",
                        help="招聘类型过滤：all（默认=全要）/ intern / social / "
                             "campus，可逗号组合（如 intern,campus）")
    parser.add_argument("--keep-junk", action="store_true",
                        help="保留销售/技术/噪声岗（排查用，默认删库）")
    parser.add_argument("--no-csv", action="store_true",
                        help="只写 JSON，不写 CSV")
    args = parser.parse_args(argv)
    # 相对路径都按 result-dir 解析
    if args.lists:
        args.lists = [p if os.path.isabs(p) else os.path.join(args.result_dir, p)
                      for p in args.lists]
    if args.details:
        args.details = [p if os.path.isabs(p) else os.path.join(args.result_dir, p)
                        for p in args.details]
    if args.old and not os.path.isabs(args.old):
        args.old = os.path.join(args.result_dir, args.old)
    if args.exclude_job_ids_file and not os.path.isabs(args.exclude_job_ids_file):
        args.exclude_job_ids_file = os.path.join(args.result_dir,
                                                 args.exclude_job_ids_file)
    try:
        _, code = build(args)
    except (OSError, ValueError) as exc:
        print(f"构建失败：{exc}")
        return 1
    return code


if __name__ == "__main__":
    sys.exit(main())
