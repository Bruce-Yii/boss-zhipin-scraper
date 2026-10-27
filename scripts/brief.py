"""今日简报：把「今天干了什么」聚合成一份 agent 可直接汇报/消费的报告。

只用本地落盘数据，零网络请求：

- SQLite 原材料库：今日 runs、今日**新增岗位**（``first_seen_at``）、今日更新详情；
- 日预算台账：今日已用/剩余额度；
- 产物清单：每个口径当前最新交付物（含取代关系）；
- 交付物版本 diff：当前版 vs 同口径上一版（added/removed/kept，按 job_id）；
- 异常：风控事件（risk_events.jsonl）、坏页台账、详情失败文件。

输出两种形态：人看（终端，``--brief``）与机器读（``--brief --json``，稳定字段）。
"""

import json
import os
import sys
from collections import Counter
from datetime import UTC, datetime

try:
    from scripts import artifacts as _artifacts
    from scripts import daily_budget as _daily_budget
    from scripts import db_store as _db_store
    from scripts import semantics as _semantics
except ImportError:  # pragma: no cover - 直接运行脚本时走此分支
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import artifacts as _artifacts
    import daily_budget as _daily_budget
    import db_store as _db_store
    import semantics as _semantics

DEFAULT_RESULT_DIR = os.path.expanduser("~/.boss-zhipin-scraper/job-result")
DEFAULT_DB_PATH = _db_store.DEFAULT_DB_PATH
TOP_TITLES = 20


def _now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _local_day_start_utc_iso(now=None):
    """本地当天 0 点 → UTC ISO（DB 里存的是 UTC 秒级时间戳）。"""
    now = now or datetime.now().astimezone()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(UTC).isoformat(timespec="seconds")


def _load_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def _today_risk_events(date_str):
    """今日风控/审计事件数（risk_events.jsonl，UTC 时间戳 → 本地日期比较）。"""
    path = os.environ.get("BOSS_AUDIT_PATH") or os.path.expanduser(
        "~/.boss-zhipin-scraper/risk_events.jsonl")
    count = 0
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                stamp = str(event.get("ts") or event.get("time") or "")
                try:
                    moment = datetime.fromisoformat(stamp)
                except ValueError:
                    continue
                if moment.tzinfo is None:
                    moment = moment.replace(tzinfo=UTC)
                if moment.astimezone().date().isoformat() == date_str:
                    count += 1
    except OSError:
        return 0
    return count


def _known_bad_count():
    path = os.environ.get("BOSS_KNOWN_BAD_PATH") or os.path.expanduser(
        "~/.boss-zhipin-scraper/known_bad_jobs.json")
    data = _load_json(path)
    if isinstance(data, dict):
        return len(data)
    if isinstance(data, list):
        return len(data)
    return 0


def _failure_files_today(result_dir, date_str):
    """今日写出的详情失败文件（数量 + 条目数）。"""
    files, items = 0, 0
    try:
        names = os.listdir(result_dir)
    except OSError:
        return 0, 0
    for name in names:
        if not name.endswith(".failures.json"):
            continue
        path = os.path.join(result_dir, name)
        try:
            if datetime.fromtimestamp(os.path.getmtime(path)).date().isoformat() != date_str:
                continue
        except OSError:
            continue
        payload = _load_json(path)
        if isinstance(payload, list):
            files += 1
            items += len(payload)
    return files, items


def _title_mix(jobs):
    """按归一化标题分组（AI产品经理/ai产品经理/AI 产品经理 归并）。

    展示名取该组里出现最多的**原始标题**（归一化键全小写，不适合直接给人看）。
    """
    keys = {}
    for job in jobs:
        key = job.get("title_key") or _semantics.normalize_title(job.get("title"))
        if not key:
            continue
        bucket = keys.setdefault(str(key), {"count": 0, "titles": Counter()})
        bucket["count"] += 1
        raw = str(job.get("title") or "").strip()
        if raw:
            bucket["titles"][raw] += 1
    ordered = sorted(keys.items(), key=lambda item: (-item[1]["count"], item[0]))
    return [{
        "title": (bucket["titles"].most_common(1)[0][0] if bucket["titles"]
                  else key),
        "key": key,
        "count": bucket["count"],
    } for key, bucket in ordered]


def _recruit_mix(jobs):
    counter = Counter()
    for job in jobs:
        try:
            counter[_semantics.classify_recruit_type(job)] += 1
        except (OSError, TypeError, ValueError):
            counter["unknown"] += 1
    return dict(counter)


def _deliverable_changes(index):
    """每个口径当前版 vs 上一版（同 scope_key 的 superseded 产物）的岗位差。"""
    entries = [item for item in (index.get("artifacts") or [])
               if item.get("kind") == "deliverable"]
    by_scope = {}
    for entry in entries:
        key = (entry.get("scope") or {}).get("scope_key") or ""
        by_scope.setdefault(key, []).append(entry)
    changes = {}
    result_dir = index.get("result_dir") or ""
    for key, items in by_scope.items():
        items.sort(key=lambda item: (item.get("created_at", ""), item.get("path", "")))
        if len(items) < 2:
            continue
        current, previous = items[-1], items[-2]
        current_ids = _job_ids(os.path.join(result_dir, current["path"]))
        previous_ids = _job_ids(os.path.join(result_dir, previous["path"]))
        if current_ids is None or previous_ids is None:
            continue
        changes[key] = {
            "current": current["path"],
            "previous": previous["path"],
            "added": len(current_ids - previous_ids),
            "removed": len(previous_ids - current_ids),
            "kept": len(current_ids & previous_ids),
        }
    return changes


def _load_jobs(path):
    payload = _load_json(path)
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
        return None
    return [job for job in payload["jobs"] if isinstance(job, dict)]


def _job_ids(path):
    jobs = _load_jobs(path)
    if jobs is None:
        return None
    return {str(job["job_id"]) for job in jobs if job.get("job_id")}


def _job_ids_of(jobs):
    return {str(job["job_id"]) for job in jobs if job.get("job_id")}


def collect(date=None, result_dir=None, db_path=None, now=None):
    """聚合今日简报（纯本地；DB 只读；返回稳定 dict）。"""
    now = now or datetime.now().astimezone()
    date_str = date or now.date().isoformat()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_start_utc = day_start.astimezone(UTC).isoformat(timespec="seconds")
    result_dir = os.path.abspath(os.path.expanduser(result_dir or DEFAULT_RESULT_DIR))
    db_path = os.path.abspath(os.path.expanduser(
        db_path or os.environ.get("BOSS_DB_PATH") or DEFAULT_DB_PATH))

    report = {
        "generated_at": _now_iso(),
        "date": date_str,
        "city": "",
        "runs": {"count": 0, "keywords": []},
        "new_jobs": {"count": 0, "by_title": [], "by_recruit": {}},
        "details": {"updated": 0, "with_jd": 0},
        "budget": {"used": 0, "limit": 0, "remaining": 0},
        "deliverables": [],
        "changes": {},
        "anomalies": {"risk_events": 0, "known_bad": 0,
                      "failure_files": 0, "failure_items": 0},
        "notes": [],
    }

    conn = None
    new_job_ids = set()
    if os.path.exists(db_path):
        try:
            conn = _db_store.open_store_readonly(db_path)
        except (OSError, ValueError):
            conn = None
    if conn is None:
        report["notes"].append(f"原材料库不存在/不可读：{db_path}（数字仅来自文件与台账）")
    else:
        try:
            runs = list(conn.execute(
                "SELECT keyword, city, job_count, detail_count, started_at, mode "
                "FROM runs WHERE started_at >= ? ORDER BY started_at", (day_start_utc,)))
            report["runs"]["count"] = len(runs)
            keywords = []
            cities = Counter()
            for row in runs:
                for part in str(row["keyword"] or "").split(","):
                    part = part.strip()
                    if part and part not in keywords:
                        keywords.append(part)
                if row["city"]:
                    cities[str(row["city"])] += 1
            report["runs"]["keywords"] = keywords
            if cities:
                report["city"] = cities.most_common(1)[0][0]
            new_jobs = []
            for row in conn.execute(
                    "SELECT payload FROM jobs WHERE first_seen_at >= ?", (day_start_utc,)):
                try:
                    job = json.loads(row["payload"])
                except (TypeError, ValueError):
                    continue
                if isinstance(job, dict):
                    new_jobs.append(job)
            report["new_jobs"]["count"] = len(new_jobs)
            report["new_jobs"]["by_title"] = _title_mix(new_jobs)
            report["new_jobs"]["by_recruit"] = _recruit_mix(new_jobs)
            new_job_ids = _job_ids_of(new_jobs)
            detail_rows = conn.execute(
                "SELECT jd FROM details WHERE updated_at >= ?", (day_start_utc,)).fetchall()
            report["details"]["updated"] = len(detail_rows)
            report["details"]["with_jd"] = sum(
                1 for row in detail_rows if str(row["jd"] or "").strip())
        finally:
            conn.close()

    try:
        budget_state = _daily_budget.read_state()
        used = int(budget_state.get("detail") or 0)
    except (OSError, TypeError, ValueError):
        used = 0
    limit = int(getattr(_daily_budget, "DEFAULT_DAILY_DETAIL_BUDGET", 700) or 0)
    report["budget"] = {
        "used": used,
        "limit": limit,
        "remaining": max(limit - used, 0) if limit else 0,
    }

    index = _artifacts.build_index(result_dir)
    changes = _deliverable_changes(index)
    report["changes"] = changes
    for item in index.get("latest", {}).get("deliverables", []):
        scope_key = item.get("scope_key") or ""
        jobs = _load_jobs(os.path.join(result_dir, item["path"])) or []
        title_mix = _title_mix(jobs)
        report["deliverables"].append({
            "scope_key": scope_key,
            "path": item["path"],
            "jobs": (item.get("counts") or {}).get("jobs", 0),
            "with_jd": (item.get("counts") or {}).get("with_jd", 0),
            "created_at": item.get("created_at", ""),
            "changes": changes.get(scope_key),
            "by_title": title_mix[:TOP_TITLES],
            "other_titles": max(0, len(title_mix) - TOP_TITLES),
            "other_jobs": max(0, sum(item["count"] for item in title_mix[TOP_TITLES:])),
            "by_recruit": _recruit_mix(jobs),
            "new_today": len(_job_ids_of(jobs) & new_job_ids),
        })

    failure_files, failure_items = _failure_files_today(result_dir, date_str)
    report["anomalies"] = {
        "risk_events": _today_risk_events(date_str),
        "known_bad": _known_bad_count(),
        "failure_files": failure_files,
        "failure_items": failure_items,
    }
    if not report["new_jobs"]["count"]:
        report["notes"].append("今天还没有新增岗位（或今天还没跑列表）")
    return report


def _top_titles(titles, limit=TOP_TITLES):
    return titles[:limit]


def _synth_next(report):
    """一句下一步建议（只基于数字，不猜）。"""
    budget = report.get("budget") or {}
    if budget.get("limit") and budget.get("remaining", 0) <= 0:
        return "今日详情额度已用完；只能抓列表，明细等明天。"
    if report.get("new_jobs", {}).get("count"):
        return (f"预算剩 {budget.get('remaining', 0)} 次详情，可继续补详情；"
                "当前交付物已按口径更新（见上）。")
    return "还没新增岗位：先跑一次列表（或 --daily）再回来看看。"


def format_brief(report):
    """人看的今日简报（数字 + 岗位结构 + 交付物 + 异常 + 下一步）。"""
    lines = [f"\n=== 今日简报 {report.get('date', '')}"
             f"{('（' + report['city'] + '）') if report.get('city') else ''} ==="]
    runs = report.get("runs") or {}
    jobs = report.get("new_jobs") or {}
    details = report.get("details") or {}
    budget = report.get("budget") or {}
    lines.append(
        f"抓取：{runs.get('count', 0)} 次 run｜今日新增岗位 {jobs.get('count', 0)}"
        f"｜今日详情请求 {budget.get('used', 0)} 次（涉及 {details.get('with_jd', 0)} 岗）"
        f"｜预算 {budget.get('used', 0)}/{budget.get('limit', 0)}"
        f"（剩 {budget.get('remaining', 0)}）")
    # 岗位结构以「最新交付物」为准（过了口径、带 JD，才是用户真正要看的岗）
    structured = next((item for item in report.get("deliverables") or []
                       if item.get("with_jd")), None)
    if structured and structured.get("by_title"):
        new_note = (f"，其中今日新增 {structured['new_today']} 条"
                    if structured.get("new_today") else "")
        lines.append(f"岗位结构（{structured.get('scope_key') or structured.get('path')}，"
                     f"{structured.get('jobs', 0)} 条{new_note}）：")
        titles = structured["by_title"]
        parts = [f"{item['title']} {item['count']}" for item in titles]
        if structured.get("other_jobs"):
            parts.append(f"其他 {structured['other_jobs']} 条"
                         f"（{structured.get('other_titles', 0)} 类）")
        lines.append("  " + "｜".join(parts))
        recruit = structured.get("by_recruit") or {}
        if recruit:
            lines.append("  按类型：" + "｜".join(f"{key} {value}" for key, value in recruit.items()))
    elif jobs.get("by_title"):
        lines.append(f"岗位结构（今日新增 {jobs.get('count', 0)} 条，未过口径）：")
        parts = [f"{item['title']} {item['count']}" for item in jobs["by_title"]]
        lines.append("  " + "｜".join(parts))
    if report.get("deliverables"):
        lines.append("交付物（每口径最新，最多 6 个）：")
        shown = [item for item in report["deliverables"] if item.get("with_jd")] or \
                report["deliverables"]
        for item in shown[:6]:
            change = item.get("changes") or {}
            diff = ""
            if change:
                diff = (f"｜较上版 +{change.get('added', 0)}"
                        f"/-{change.get('removed', 0)}/保持 {change.get('kept', 0)}")
            lines.append(f"  · {item.get('scope_key') or '未标口径'} → "
                         f"{item['path']}｜{item['jobs']} 条（JD {item['with_jd']}）"
                         f"｜{str(item.get('created_at', ''))[:16]}{diff}")
    anomalies = report.get("anomalies") or {}
    lines.append(
        f"异常：风控事件 {anomalies.get('risk_events', 0)}"
        f"｜坏页台账 {anomalies.get('known_bad', 0)}"
        f"｜今日失败文件 {anomalies.get('failure_files', 0)}"
        f"（{anomalies.get('failure_items', 0)} 条）")
    for note in report.get("notes") or []:
        lines.append(f"注：{note}")
    lines.append(f"下一步：{_synth_next(report)}")
    lines.append("")
    return "\n".join(lines)


def run_brief(result_dir=None, db_path=None, as_json=False, date=None):
    """``--brief`` 实现：只读输出今日简报。"""
    try:
        report = collect(date=date, result_dir=result_dir, db_path=db_path)
    except (OSError, TypeError, ValueError) as exc:
        print(f"今日简报生成失败：{exc}")
        return 1
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_brief(report))
    return 0


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(
        description="今日简报（新增岗位/岗位结构/详情/预算/交付物/异常；只读）")
    parser.add_argument("--result-dir", default=None)
    parser.add_argument("--db", default=None, help="SQLite 库路径（默认库）")
    parser.add_argument("--date", default=None, help="YYYY-MM-DD（默认今天）")
    parser.add_argument("--json", action="store_true", help="机器可读 JSON")
    args = parser.parse_args(argv)
    return run_brief(result_dir=args.result_dir, db_path=args.db,
                     as_json=args.json, date=args.date)


if __name__ == "__main__":
    sys.exit(main())
