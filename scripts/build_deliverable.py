"""交付物构建入口：列表 + 详情 + 旧交付 → 走生产合并路径落盘。

替代夜班一次性脚本（temp/assemble_100b.py，已清理）：同样的三段拼装
（列表池 recruit-type 过滤 + 旧交付回填 + 详情收集），但直接调
``export_contract._merge_jd_into_export(base=...)``，不写中间 temp 文件。

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
"""

import argparse
import csv
import json
import os
import pathlib
import sys

try:
    from scripts import export_contract as ec
    from scripts import semantics
except ImportError:  # 直接 python scripts/build_deliverable.py 运行时
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import export_contract as ec
    import semantics


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


def build(args):
    """拼装并落盘，返回 (kept, exit_code)。"""
    missing = [p for p in list(args.lists) + list(args.details)
               + ([args.old] if args.old else []) if not os.path.exists(p)]
    if missing:
        print(f"输入文件不存在：{', '.join(missing)}")
        return 0, 2

    # ---- 1) 候选池：列表 + recruit-type 过滤 + 旧交付回填 ----
    pool = {}
    for path in args.lists:
        for job in _load_jobs(path):
            if isinstance(job, dict) and job.get("job_id"):
                pool[str(job["job_id"])] = job
    if args.recruit_type != "all":
        pool = {jid: job for jid, job in pool.items()
                if semantics.classify_recruit_type(job) == args.recruit_type}
    print(f"候选: {len(pool)}")
    old_jobs = []
    if args.old:
        old_jobs = [j for j in _load_jobs(args.old) if isinstance(j, dict)]
        for job in old_jobs:
            if job.get("job_id"):
                pool.setdefault(str(job["job_id"]), job)

    # ---- 2) 详情：JD 来源 + 旧交付 JD 回填 ----
    details = _collect_details(args.details)
    for job in old_jobs:
        jid = str(job.get("job_id") or "")
        if jid and jid not in details and str(job.get("jd") or "").strip():
            details[jid] = job
    print(f"详情(JD): {len(details)}")

    # ---- 3) 走生产合并路径 ----
    base = {"keyword": args.keyword, "city": args.city,
            "jobs": list(pool.values()), "job_count": len(pool),
            "total": len(pool)}
    if args.target_roots:
        base["target_roots"] = [r.strip() for r in args.target_roots.split(",")
                                if r.strip()]
    target = args.target
    if not os.path.isabs(target):
        target = os.path.join(args.result_dir, target)
    extra = {"scope": args.scope} if args.scope else None
    res = ec._merge_jd_into_export(target, list(details.values()), base=base,
                                   keep_junk=args.keep_junk, extra_meta=extra)
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
        description="交付物构建：列表+详情+旧交付走生产合并路径落盘")
    parser.add_argument("--result-dir", default=_default_result_dir(),
                        help="job-result 目录（相对路径的 target 落在这里）")
    parser.add_argument("--lists", nargs="+", required=True,
                        help="列表文件（多个）")
    parser.add_argument("--details", nargs="+", required=True,
                        help="详情文件（多个，list 或 {jobs:[...]} 都认）")
    parser.add_argument("--old", default="",
                        help="旧交付文件（候选+JD 回填，可选）")
    parser.add_argument("--target", required=True,
                        help="输出交付物文件名（写进 result-dir）或绝对路径")
    parser.add_argument("--keyword", default="", help="写入产物 meta 的关键词")
    parser.add_argument("--city", default="", help="写入产物 meta 的城市")
    parser.add_argument("--target-roots", default="",
                        help="写入 meta.target_roots 的词根列表（逗号分隔；"
                             "P11 优先用它核对标题命中率，不给就退回 keyword 切分）")
    parser.add_argument("--scope", default="",
                        help="写入 extra_meta.scope 的口径说明")
    parser.add_argument("--recruit-type", default="intern",
                        choices=("all", "intern", "social", "campus"),
                        help="列表阶段招聘类型过滤（默认 intern，用户既定链路）")
    parser.add_argument("--keep-junk", action="store_true",
                        help="保留销售/技术/噪声岗（排查用，默认删库）")
    parser.add_argument("--no-csv", action="store_true",
                        help="只写 JSON，不写 CSV")
    args = parser.parse_args(argv)
    # 相对路径都按 result-dir 解析
    args.lists = [p if os.path.isabs(p) else os.path.join(args.result_dir, p)
                  for p in args.lists]
    args.details = [p if os.path.isabs(p) else os.path.join(args.result_dir, p)
                    for p in args.details]
    if args.old and not os.path.isabs(args.old):
        args.old = os.path.join(args.result_dir, args.old)
    try:
        _, code = build(args)
    except (OSError, ValueError) as exc:
        print(f"构建失败：{exc}")
        return 1
    return code


if __name__ == "__main__":
    sys.exit(main())
