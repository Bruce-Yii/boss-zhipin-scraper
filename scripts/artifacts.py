"""产物层统一：把 job-result 目录扫成一份机器可读清单（``artifacts.json``）。

为什么有这个模块
----------------
项目在 agent 会话里运行：agent 需要一句话回答「现在有哪些产物、哪个最新、
哪个口径对应哪份交付物、哪些可以归档」，而不是按 mtime 去猜 80+ 个文件。
文件仍是唯一真相；``artifacts.json`` 只是**可随时重建的缓存**（损坏/缺失
自动重建，不参与任何写盘决策）。

分类
----
- ``list``        列表快照（``boss_jobs_*``，详情抓完后可能已并入 JD）
- ``details``     详情快照（``boss_details_*``）
- ``deliverable`` 交付物（多库合并 / 口径筛选的成品；``build_deliverable`` 会盖章）
- ``ledger``      台账 / 孤儿 sidecar（pending / failures / quarantine）

口径（scope）与取代关系
----------------------
同一 ``scope_key`` 的多份交付物只把最新一份标 ``active``，其余标
``superseded``——只打标**不移动文件**；归档仍由 ``--archive`` 显式执行。
``scope_key`` 优先取交付物里 ``artifact.scope_key``（构建时盖的章），
旧交付物按 ``城市|关键词或口径说明`` 推断并标 ``inferred``。

只读保证：本模块只读产物文件 + 原子写 ``artifacts.json``，不改任何产物本身。
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta

try:
    from scripts import export_contract as _ec
except ImportError:  # pragma: no cover - 直接运行脚本时走此分支
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import export_contract as _ec

FORMAT_VERSION = 1
INDEX_NAME = "artifacts.json"
RAW_DIR_NAME = "raw"
QUARANTINE_DIR_NAME = "quarantine"
ARCHIVE_DIR_NAME = "archive"

KINDS = ("list", "details", "deliverable", "ledger")
STATUSES = ("active", "archived", "superseded")


def default_result_dir():
    return os.path.expanduser("~/.boss-zhipin-scraper/job-result")


def _now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _iso_from_ts(timestamp):
    try:
        return datetime.fromtimestamp(timestamp).astimezone().isoformat(
            timespec="seconds")
    except (OSError, OverflowError, ValueError):
        return ""


def _read_json(path):
    """读 JSON；不可解析返回 ``(False, None)``（坏文件不阻断清单生成）。"""
    try:
        with open(path, encoding="utf-8") as handle:
            return True, json.load(handle)
    except (OSError, ValueError, UnicodeDecodeError):
        return False, None


def _is_sidecar(name):
    return name.endswith((".pending.json", ".failures.json"))


def _stem(name):
    return name[:-5] if name.endswith(".json") else name


def _jobs(payload):
    if isinstance(payload, dict) and isinstance(payload.get("jobs"), list):
        return payload["jobs"]
    return []


def _counts(kind, payload):
    """清单里的数量摘要（只算便宜的字段，不重新推导业务规则）。"""
    counts = {}
    if kind in ("list", "deliverable"):
        jobs = [job for job in _jobs(payload) if isinstance(job, dict)]
        counts["jobs"] = len(jobs)
        counts["with_jd"] = sum(
            1 for job in jobs if str(job.get("jd") or "").strip())
    elif kind == "details":
        records = [row for row in payload or [] if isinstance(row, dict)]
        counts["records"] = len(records)
        counts["with_jd"] = sum(
            1 for row in records if str(row.get("jd") or "").strip())
    return counts


def _artifact_stamp(payload):
    if isinstance(payload, dict) and isinstance(payload.get("artifact"), dict):
        return payload["artifact"]
    return {}


def _infer_scope(payload):
    """从产物 meta 里推断口径；推不出来返回 None。"""
    if not isinstance(payload, dict):
        return None
    stamp = _artifact_stamp(payload)
    scope = stamp.get("scope") if isinstance(stamp.get("scope"), dict) else {}
    source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
    city = str(scope.get("city") or source.get("city")
               or payload.get("city") or "").strip()
    keyword = str(scope.get("keyword") or source.get("keyword")
                  or payload.get("keyword") or "").strip()
    label = str(scope.get("label") or payload.get("scope") or "").strip()
    recruit = scope.get("recruit_types") or source.get("recruit_types") or []
    if not isinstance(recruit, list):
        recruit = []
    scope_key = str(stamp.get("scope_key") or "").strip()
    inferred = False
    if not scope_key:
        anchor = keyword or label[:40]
        if city and anchor:
            scope_key = f"{city}|{anchor}"
            inferred = True
    if not (city or keyword or label or scope_key):
        return None
    return {
        "city": city,
        "keyword": keyword,
        "label": label,
        "recruit_types": [str(item) for item in recruit],
        "scope_key": scope_key,
        "inferred": inferred,
    }


def _classify(name, payload):
    """按文件名 + 内容形态判定产物类型（交付物靠构建时盖的 artifact 章）。"""
    if _is_sidecar(name):
        return "ledger"
    if name.startswith("boss_details_"):
        return "details"
    if isinstance(payload, list):
        return "details"
    if isinstance(payload, dict) and isinstance(payload.get("jobs"), list):
        stamp = _artifact_stamp(payload)
        if stamp.get("kind") in KINDS:
            return stamp["kind"]
        source = payload.get("source")
        if (isinstance(source, dict) or payload.get("clean_of")
                or payload.get("dropped_junk_manual")
                or payload.get("sources")):
            return "deliverable"
        # 旧交付物（手工脚本产出）：带 scope 说明、没有列表快照标记
        if payload.get("scope") and not payload.get("format_version"):
            return "deliverable"
        return "list"
    return "ledger"


def _created_at(payload, mtime):
    stamp = _artifact_stamp(payload)
    for candidate in (stamp.get("created_at"),
                      payload.get("scraped_at") if isinstance(payload, dict) else None):
        text = str(candidate or "").strip()
        if text:
            try:
                moment = datetime.fromisoformat(text)
            except ValueError:
                return text
            if moment.tzinfo is None:
                moment = moment.astimezone()
            return moment.astimezone().isoformat(timespec="seconds")
    return _iso_from_ts(mtime)


def _entry(kind, rel_path, abs_path, payload, status, scope=None):
    try:
        stat = os.stat(abs_path)
        size = stat.st_size
        mtime = stat.st_mtime
    except OSError:
        size, mtime = 0, 0
    entry = {
        "artifact_id": _stem(os.path.basename(rel_path)),
        "kind": kind,
        "path": rel_path.replace(os.sep, "/"),
        "status": status,
        "created_at": _created_at(payload, mtime),
        "bytes": size,
        "counts": _counts(kind, payload),
        "scope": scope,
        "sidecars": [],
    }
    if isinstance(payload, dict):
        for key in ("jd_coverage", "cross_run", "empty_export_blocked"):
            if key in payload:
                entry.setdefault("meta", {})[key] = payload[key]
        acceptance = payload.get("acceptance")
        if isinstance(acceptance, dict):
            entry.setdefault("meta", {})["acceptance"] = {
                "ok": acceptance.get("ok"),
                "error_count": acceptance.get("error_count"),
                "warn_count": acceptance.get("warn_count"),
            }
    return entry


def _iter_json_files(directory):
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return
    for name in names:
        if name == INDEX_NAME or not name.endswith(".json") or ".tmp" in name:
            continue
        path = os.path.join(directory, name)
        if os.path.isfile(path):
            yield name, path


def _pair_scope(kind, entries_by_path):
    """详情快照按文件名时间戳后缀找同批列表快照，继承口径。"""
    for entry in entries_by_path.values():
        if entry["kind"] != "details" or entry["scope"] is not None:
            continue
        name = entry["path"].rsplit("/", 1)[-1]
        if not name.startswith("boss_details_"):
            continue
        suffix = name[len("boss_details_"):]
        for other in entries_by_path.values():
            other_name = other["path"].rsplit("/", 1)[-1]
            if (other["kind"] == "list"
                    and other_name == f"boss_jobs_{suffix}"
                    and other["scope"] is not None):
                entry["scope"] = other["scope"]
                break


def _apply_status(entries, stale_days):
    """取代关系 + 时效标记（只改清单里的 status/stale，不动文件）。"""
    groups = {}
    for entry in entries:
        scope = entry.get("scope") or {}
        key = scope.get("scope_key")
        if entry["kind"] == "deliverable" and entry["status"] == "active" and key:
            groups.setdefault(key, []).append(entry)
    for items in groups.values():
        items.sort(key=lambda item: (item["created_at"], item["path"]))
        for older in items[:-1]:
            older["status"] = "superseded"
    stale_before = _now_iso()[:10]
    if stale_days is not None and stale_days >= 0:
        try:
            cutoff = (datetime.now().astimezone()
                      - timedelta(days=stale_days)).isoformat(timespec="seconds")
        except (OverflowError, ValueError):
            cutoff = ""
        for entry in entries:
            if cutoff and entry.get("created_at"):
                entry["stale"] = entry["created_at"] < cutoff
                if entry["stale"]:
                    stale_before = min(stale_before, entry["created_at"][:10])
    return stale_before


def _summary(entries, result_dir):
    summary = {
        "total": len(entries),
        "bytes": sum(int(entry.get("bytes") or 0) for entry in entries),
    }
    for kind in KINDS:
        summary[kind] = sum(1 for entry in entries if entry["kind"] == kind)
    for status in STATUSES:
        summary[status] = sum(1 for entry in entries if entry["status"] == status)
    summary["stale"] = sum(1 for entry in entries if entry.get("stale"))
    for extra_dir in (RAW_DIR_NAME, QUARANTINE_DIR_NAME):
        directory = os.path.join(result_dir, extra_dir)
        try:
            summary[f"{extra_dir}_files"] = sum(
                1 for name in os.listdir(directory) if name.endswith(".json"))
        except OSError:
            summary[f"{extra_dir}_files"] = 0
    return summary


def _latest(entries):
    """给 agent 的快捷入口：最新列表/最新详情/每个口径最新交付物。"""
    def newest(kind, status="active"):
        candidates = [item for item in entries
                      if item["kind"] == kind and item["status"] == status]
        candidates.sort(key=lambda item: (item["created_at"], item["path"]))
        return candidates[-1] if candidates else None

    latest_list = newest("list")
    latest_details = newest("details")
    deliverables = [item for item in entries
                    if item["kind"] == "deliverable" and item["status"] == "active"]
    deliverables.sort(key=lambda item: (item["created_at"], item["path"]),
                      reverse=True)
    return {
        "list": latest_list["path"] if latest_list else None,
        "details": latest_details["path"] if latest_details else None,
        "deliverables": [
            {
                "scope_key": (item.get("scope") or {}).get("scope_key"),
                "path": item["path"],
                "counts": item.get("counts") or {},
                "created_at": item["created_at"],
            }
            for item in deliverables
        ],
    }


def build_index(result_dir=None, include_archive=True, stale_days=7):
    """扫描目录生成清单（不写盘）。返回 dict，可直接 JSON 化给 agent 消费。"""
    result_dir = os.path.abspath(os.path.expanduser(result_dir or default_result_dir()))
    directories = [(result_dir, "active")]
    if include_archive:
        directories.append((os.path.join(result_dir, ARCHIVE_DIR_NAME), "archived"))
    entries = []
    entries_by_path = {}
    consumed_sidecars = set()
    for directory, status in directories:
        for name, path in _iter_json_files(directory):
            ok, payload = _read_json(path)
            if not ok:
                rel = os.path.relpath(path, result_dir)
                entries.append(_entry("ledger", rel, path, None, status))
                continue
            kind = _classify(name, payload)
            scope = _infer_scope(payload) if kind in ("list", "deliverable") else None
            rel = os.path.relpath(path, result_dir)
            entry = _entry(kind, rel, path, payload, status, scope)
            entry["_abs"] = path
            entries.append(entry)
            entries_by_path[entry["path"]] = entry
    # sidecar 挂到本体：failures/pending 跟着详情或列表走，不当独立产物
    for entry in list(entries):
        if entry["kind"] != "ledger":
            continue
        base = entry.get("_abs")
        if not base:
            continue
        parent = None
        if base.endswith(".failures.json"):
            parent = entries_by_path.get(os.path.relpath(
                base[: -len(".failures.json")], result_dir).replace(os.sep, "/"))
        elif base.endswith(".pending.json"):
            parent = entries_by_path.get(os.path.relpath(
                base[: -len(".pending.json")], result_dir).replace(os.sep, "/"))
        if parent is not None:
            parent["sidecars"].append(entry["path"])
            consumed_sidecars.add(entry["path"])
    entries = [entry for entry in entries if entry["path"] not in consumed_sidecars]
    for entry in entries:
        entry.pop("_abs", None)
    _pair_scope("details", {entry["path"]: entry for entry in entries})
    _apply_status(entries, stale_days)
    entries.sort(key=lambda item: (item["kind"], item["created_at"], item["path"]),
                 reverse=True)
    return {
        "format_version": FORMAT_VERSION,
        "generated_at": _now_iso(),
        "result_dir": result_dir,
        "summary": _summary(entries, result_dir),
        "latest": _latest(entries),
        "artifacts": entries,
    }


def write_index(result_dir, index=None):
    """原子写 artifacts.json（缓存；写失败不阻断调用方）。"""
    result_dir = os.path.abspath(os.path.expanduser(result_dir or default_result_dir()))
    payload = index if index is not None else build_index(result_dir)
    try:
        _ec._atomic_write_json(os.path.join(result_dir, INDEX_NAME), payload)
        return True
    except (OSError, TypeError, ValueError):
        return False


def load_index(result_dir=None):
    """读缓存清单；缺失/损坏返回 None（调用方应 rebuild）。"""
    result_dir = os.path.abspath(os.path.expanduser(result_dir or default_result_dir()))
    ok, payload = _read_json(os.path.join(result_dir, INDEX_NAME))
    if not ok or not isinstance(payload, dict):
        return None
    if payload.get("format_version") != FORMAT_VERSION:
        return None
    return payload


def refresh_index(result_dir=None, include_archive=True, stale_days=7):
    """重建并落盘缓存清单，返回 index。"""
    index = build_index(result_dir, include_archive=include_archive,
                        stale_days=stale_days)
    write_index(index["result_dir"], index)
    return index


def _filtered(index, kind=None, scope=None):
    items = list(index.get("artifacts") or [])
    if kind and kind != "all":
        items = [item for item in items if item["kind"] == kind]
    if scope:
        needle = str(scope).strip().lower()
        items = [item for item in items
                 if needle in json.dumps(item.get("scope") or {},
                                         ensure_ascii=False).lower()]
    return items


def format_human(index, kind=None, scope=None):
    """人类可读总览（agent 用 --json，这个给终端肉眼）。"""
    summary = index.get("summary") or {}
    lines = []
    lines.append(f"\n=== 产物总览：{index.get('result_dir', '')} ===")
    lines.append(
        f"共 {summary.get('total', 0)} 个｜"
        f"列表 {summary.get('list', 0)}｜详情 {summary.get('details', 0)}｜"
        f"交付物 {summary.get('deliverable', 0)}｜台账 {summary.get('ledger', 0)}"
        f"｜{summary.get('bytes', 0) / 1048576:.1f} MB")
    lines.append(
        f"状态：active {summary.get('active', 0)}｜"
        f"archived {summary.get('archived', 0)}｜"
        f"superseded {summary.get('superseded', 0)}｜stale {summary.get('stale', 0)}")
    items = _filtered(index, kind=kind, scope=scope)
    deliverables = [item for item in items if item["kind"] == "deliverable"]
    if deliverables:
        lines.append("\n交付物（同口径只留最新为 active）：")
        for item in deliverables:
            sc = item.get("scope") or {}
            label = sc.get("scope_key") or "（未标口径）"
            counts = item.get("counts") or {}
            lines.append(
                f"  [{item['status']}] {label}")
            lines.append(
                f"      {item['path']}｜{counts.get('jobs', 0)} 条"
                f"（含 JD {counts.get('with_jd', 0)}）｜{item['created_at']}")
    latest_list = next((item for item in items if item["kind"] == "list"), None)
    if latest_list:
        counts = latest_list.get("counts") or {}
        lines.append(f"\n最新列表：{latest_list['path']}｜{counts.get('jobs', 0)} 条"
                     f"｜{latest_list['created_at']}")
    latest_details = next((item for item in items if item["kind"] == "details"), None)
    if latest_details:
        counts = latest_details.get("counts") or {}
        lines.append(f"最新详情：{latest_details['path']}｜{counts.get('with_jd', 0)} 条含 JD"
                     f"｜{latest_details['created_at']}")
    raw = summary.get("raw_files", 0)
    if raw:
        lines.append(f"\n原始响应：{raw} 个（raw/，保真核对依据）")
    if summary.get("stale"):
        lines.append(f"\n⚠️  {summary['stale']} 个产物超过时效阈值（可用 --archive 归档历史）")
    lines.append("")
    return "\n".join(lines)


def run_artifacts(result_dir=None, as_json=False, kind=None, scope=None,
                  stale_days=7, write=True):
    """``--artifacts`` 实现：刷新清单 → 输出（JSON 给 agent / 文本给人）。"""
    try:
        index = build_index(result_dir, stale_days=stale_days)
    except (OSError, TypeError, ValueError) as exc:
        print(f"产物清单生成失败：{exc}")
        return 1
    if write:
        write_index(index["result_dir"], index)
    if as_json:
        payload = dict(index)
        payload["artifacts"] = _filtered(index, kind=kind, scope=scope)
        payload["filter"] = {"kind": kind or "all", "scope": scope or ""}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print(format_human(index, kind=kind, scope=scope))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="产物层统一清单（artifacts.json）：分类 / 口径 / 取代关系 / 时效")
    parser.add_argument("--result-dir", default=None,
                        help="产物目录（默认 ~/.boss-zhipin-scraper/job-result）")
    parser.add_argument("--json", action="store_true",
                        help="输出机器可读 JSON（agent 用；含 summary + artifacts）")
    parser.add_argument("--kind", default=None,
                        choices=("all",) + KINDS, help="只输出某类产物")
    parser.add_argument("--scope", default=None,
                        help="按口径关键词过滤（城市/关键词/scope_key 子串）")
    parser.add_argument("--stale-days", type=int, default=7,
                        help="超过 N 天未更新标记 stale（默认 7；-1 关闭）")
    parser.add_argument("--no-write", action="store_true",
                        help="只扫描不写 artifacts.json 缓存")
    args = parser.parse_args(argv)
    return run_artifacts(result_dir=args.result_dir, as_json=args.json,
                         kind=args.kind, scope=args.scope,
                         stale_days=args.stale_days,
                         write=not args.no_write)


if __name__ == "__main__":
    sys.exit(main())
