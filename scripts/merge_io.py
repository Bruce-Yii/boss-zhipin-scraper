"""CSV/合并/raw 落盘与保真核对入口（从 boss_cdp_raw.py 增量拆分）。

含：CSV 列定义、列表/详情 CSV 写出、平台原始响应与详情原文落盘、
保真核对（列表侧/详情侧）、合并外部详情文件查找。

注意：保真失败标志（_FIDELITY_FAILED/set_fidelity_failed/fidelity_failed）
与 LAST_DETAIL_RAW_PATH 留在主文件——它们是易变模块态（每次 load 一份
fresh）与 orchestration 的 global 赋值，搬进共享模块会跨 run 泄漏。

约束：boss 侧全局经 _core() 取用（调用栈优先，见下）；datetime/time 走
_core()（tests 有 patch boss.datetime 的先例）；其余 stdlib 直引。
"""

import csv
import glob
import json
import logging
import os

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


# ============================================================
# CSV 导出
# ============================================================
CSV_COLUMNS = [
    "job_id", "title", "salary", "salary_source", "location", "tags", "boss_name",
    "boss_active_status",
    "company_scale", "company_stage", "company_industry", "skills",
    "job_link", "welfare",
]

# CSV 的列**由岗位模型派生**：新增模型字段自动进 CSV，不再手工维护两份列清单
# （2026-10-22 踩过：post_type/active_state 等语义字段只进了 JSON，CSV 上等于没有）。
# 前 11 列是历史列位（保持向后兼容：company 仍是公司名那一列），其余按模型声明顺序追加。
_DETAIL_CSV_LEADING_COLUMNS = [
    "job_id", "title", "company", "salary", "salary_source", "location",
    "boss_active_status", "tags_list", "job_link", "skill_tags", "jd",
]
# 历史列位与模型字段名的对应（写 CSV 时取值用模型字段名）
_DETAIL_CSV_VALUE_SOURCE = {
    "company": "company_name",
    "tags_list": "tags",
}
try:
    DETAIL_CSV_COLUMNS = _DETAIL_CSV_LEADING_COLUMNS + [
        name for name in _core()._job_model.EXPORT_PUBLIC_FIELDS
        if name not in _DETAIL_CSV_LEADING_COLUMNS
    ]
except Exception:
    DETAIL_CSV_COLUMNS = list(_DETAIL_CSV_LEADING_COLUMNS)


RAW_DIR_NAME = "raw"


def save_raw_responses(run_id, keyword, city, pages, detail_payloads=None):
    """把平台**原始响应**落盘（可溯源 + 保真核对的依据）。

    - 只写仓库外的 ~/.boss-zhipin-scraper/job-result/raw/（产物目录）✓
    - 落盘前过 _sanitize_payload（不落 cookie/token/securityId 等凭据）✓
    - 原子写 ✓
    返回落盘路径（失败返回 None，不影响抓取主流程）。
    """
    core = _core()
    try:
        raw_dir = os.path.join(os.path.dirname(core.default_output_path("jobs")), RAW_DIR_NAME)
        os.makedirs(raw_dir, exist_ok=True)
        stamp = core.datetime.now().strftime("%Y%m%d_%H%M%S")
        payload = {
            "run_id": run_id,
            "keyword": keyword,
            "city": city,
            "captured_at": core.datetime.now().astimezone().isoformat(timespec="seconds"),
            "list_pages": pages or [],
            "detail_pages": detail_payloads or [],
        }
        cleaned = core._sanitize_raw_payload(payload)
        path = os.path.join(raw_dir, f"list_{run_id[:8]}_{stamp}.json")
        core._atomic_write_json(path, cleaned)
        return path
    except (OSError, TypeError, ValueError) as exc:
        print(f"⚠️ 原始响应落盘失败（不影响抓取）：{exc}")
        return None


def save_raw_details(raw_details):
    """把详情页**清洗前原文**落盘（详情侧保真核对的依据）。

    - 只写仓库外的 job-result/raw/ ✓
    - 落盘前过 _sanitize_raw_payload（不落凭据/HR 身份）✓
    - 原子写 ✓
    失败返回 None（不阻断抓取）。
    """
    core = _core()
    try:
        raw_dir = os.path.join(os.path.dirname(core.default_output_path("jobs")), RAW_DIR_NAME)
        os.makedirs(raw_dir, exist_ok=True)
        stamp = core.datetime.now().strftime("%Y%m%d_%H%M%S")
        payload = {
            "kind": "detail",
            "captured_at": core.datetime.now().astimezone().isoformat(timespec="seconds"),
            "details": list(raw_details or []),
        }
        cleaned = core._sanitize_raw_payload(payload)
        path = os.path.join(raw_dir, f"detail_{stamp}.json")
        core._atomic_write_json(path, cleaned)
        return path
    except (OSError, TypeError, ValueError) as exc:
        print(f"⚠️ 详情原文落盘失败（不影响抓取）：{exc}")
        return None


def run_fidelity_check(raw_path, jobs):
    """跑完自动做保真核对：平台原文 ↔ 我们的记录。

    返回 ``{"ok": bool, "fidelity_rate": float, "mismatch_count": int,
    "mismatches": [...], "checked_jobs": int}``；对不上时**打印明细并把退出码置 1**
    （数据不可信就不能当成功交出去）。
    """
    core = _core()
    try:
        from scripts import fidelity as _fidelity  # 延迟导入，避免循环依赖
    except ImportError:
        try:
            import fidelity as _fidelity  # type: ignore
        except ImportError:
            return {"ok": True, "skipped": "fidelity 模块不可用"}
    try:
        with open(raw_path, encoding="utf-8") as handle:
            raw_payload = json.load(handle)
    except (OSError, ValueError) as exc:
        return {"ok": True, "skipped": f"原文读取失败: {exc}"}
    ok, report = _fidelity.check_fidelity(raw_payload, {"jobs": jobs})
    summary = {
        "ok": bool(ok),
        "fidelity_rate": round(float(report.get("fidelity_rate") or 0.0), 4),
        "mismatch_count": len(report.get("mismatches") or []),
        "checked_jobs": report.get("checked_jobs", 0),
        "mismatches": (report.get("mismatches") or [])[:20],
    }
    if ok:
        print(f"✅ 保真核对通过：{summary['checked_jobs']} 条记录，"
              f"保真率 {summary['fidelity_rate']:.1%}（平台显示什么我们就是什么）")
    else:
        print(f"❌ 保真核对未通过：{summary['mismatch_count']} 处与平台原文不一致")
        for item in summary["mismatches"][:10]:
            print(f"   · {item['field']}｜{item.get('title', '')}｜"
                  f"平台={item.get('expected', '')!r}｜我们={item.get('actual', '')!r}")
        print("   → 数据与平台不一致，先查清楚再交付（不要直接拿去投简历）")
        core.set_fidelity_failed()
    return summary


def run_detail_fidelity_check(detail_raw_path, records):
    """详情侧保真核对：交付的 JD 必须是页面原文的子序列。

    详情原文落盘在 run 的 raw 目录里（run_detail_raw_path 给出）；
    对不上 → 与列表侧一样：大声报 + 退出码 1。
    """
    core = _core()
    if not detail_raw_path or not records:
        return {}
    try:
        from scripts import fidelity as _fidelity
    except ImportError:
        try:
            import fidelity as _fidelity  # type: ignore
        except ImportError:
            return {}
    try:
        with open(detail_raw_path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as exc:
        return {"error": f"详情原文读取失败: {exc}"}
    ok, report = _fidelity.check_detail_fidelity(payload, records)
    summary = {
        "ok": bool(ok),
        "checked_details": report.get("checked_details", 0),
        "error_count": report.get("detail_error_count", 0),
        "findings": (report.get("detail_findings") or [])[:20],
    }
    if ok and not report.get("detail_findings"):
        print(f"✅ 详情保真核对通过：{summary['checked_details']} 条 JD 均为页面原文的子序列")
    elif ok:
        print(f"⚠️ 详情保真核对：有 {len(report['detail_findings'])} 条可疑（未达失真）")
        for item in summary["findings"][:5]:
            print(f"   · {item['field']}｜{item.get('title', '')}｜{item.get('actual', '')}")
    else:
        print(f"❌ 详情保真核对未通过：{summary['error_count']} 条 JD 与页面原文不符")
        for item in summary["findings"][:10]:
            print(f"   · {item.get('title', '')}｜{item.get('actual', '')}")
        print("   → JD 与页面不一致，先查清楚再交付")
        core.set_fidelity_failed()
    return summary


def write_csv(csv_path, jobs):
    """将 jobs 列表写入 CSV 文件（写出前递归脱敏）。"""
    core = _core()
    os.makedirs(os.path.dirname(csv_path) or ".", exist_ok=True)
    cleaned_jobs = core._sanitize_payload(jobs or [])
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for job in cleaned_jobs:
            if not isinstance(job, dict):
                continue
            # 确保每列都有值
            row = {col: job.get(col, "") for col in CSV_COLUMNS}
            # 列表型字段统一转成 " | " 分隔的文本（CSV 里写成 Python 列表的
            # repr 会变成 "['a', 'b']"，用户在 Excel 里没法筛）
            for col, value in row.items():
                if isinstance(value, list):
                    row[col] = " | ".join(str(v) for v in value)
            writer.writerow(row)
    print(f"CSV 已保存: {csv_path}")


def write_detail_csv(csv_path, details):
    """将岗位详情列表写入 CSV 文件（写出前递归脱敏）。"""
    core = _core()
    os.makedirs(os.path.dirname(csv_path) or ".", exist_ok=True)
    cleaned_details = core._sanitize_payload(details or [])
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=DETAIL_CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for detail in cleaned_details:
            if not isinstance(detail, dict):
                continue
            row = {}
            for col in DETAIL_CSV_COLUMNS:
                source = _DETAIL_CSV_VALUE_SOURCE.get(col, col)
                row[col] = detail.get(source, "")
            if isinstance(row.get("skill_tags"), list):
                row["skill_tags"] = " | ".join(row["skill_tags"])
            # 列表型字段统一转成 " | " 分隔的文本（CSV 里写成 Python 列表
            # 的 repr 会变成 "['a', 'b']"，用户在 Excel 里没法筛）
            for col, value in row.items():
                if isinstance(value, list):
                    row[col] = " | ".join(str(v) for v in value)
            writer.writerow(row)
    print(f"详情 CSV 已保存: {csv_path}")


def find_latest_detail_file(result_dir=_core().DEFAULT_RESULT_DIR):
    pattern = os.path.join(result_dir, "boss_details_*.json")
    files = [path for path in glob.glob(pattern)
             if os.path.isfile(path) and not path.endswith(".pending.json")]
    if not files:
        return None
    return max(files, key=lambda path: (os.path.getmtime(path), path))


def detail_candidate_paths(input_path=None, detail_output=None,
                           result_dir=_core().DEFAULT_RESULT_DIR):
    core = _core()
    candidates = []
    if detail_output:
        candidates.append(detail_output)
    if input_path:
        directory = os.path.dirname(input_path) or "."
        basename = os.path.basename(input_path)
        if basename.startswith("boss_jobs_"):
            candidates.append(os.path.join(directory, basename.replace("boss_jobs_", "boss_details_", 1)))
    latest = core.find_latest_detail_file(result_dir)
    if latest:
        candidates.append(latest)

    deduped = []
    seen = set()
    for path in candidates:
        normalized = os.path.abspath(os.path.expanduser(path))
        if normalized not in seen:
            deduped.append(path)
            seen.add(normalized)
    return deduped


def load_existing_details(input_path=None, detail_output=None,
                          result_dir=_core().DEFAULT_RESULT_DIR):
    core = _core()
    for path in core.detail_candidate_paths(input_path, detail_output, result_dir):
        if not path or not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                details = json.load(f)
            if isinstance(details, list):
                print(f"加载详情文件: {path}")
                return details
        except (json.JSONDecodeError, OSError, ValueError) as e:
            core.log.warning(f"无法加载详情文件 {path}: {e}")
    return None
