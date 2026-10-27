"""结果文件管理命令（从 boss_cdp_raw.py 增量拆分）。"""

import logging

log = logging.getLogger("boss_cdp")

def _core():
    import sys as _sys
    candidates = [m for m in _sys.modules.values()
                 if getattr(m, "__file__", None)
                 and str(m.__file__).endswith("boss_cdp_raw.py")]
    if candidates:
        return candidates[-1]
    try:
        from . import boss_cdp_raw as _m
        return _m
    except Exception:
        if "boss_cdp_raw" in _sys.modules:
            return _sys.modules["boss_cdp_raw"]
        import boss_cdp_raw as _m
        return _m


def _is_detail_sidecar(name):
    """详情文件的 sidecar（pending/failures）不是详情本体。"""
    return name.endswith((".pending.json", ".failures.json"))


def _classify_result_file(name):
    """按文件名分类结果目录条目：jobs / details / pending / other。"""
    if name.endswith(".pending.json"):
        return "pending"
    if name.endswith(".failures.json"):
        return "other"
    if name.startswith("boss_jobs_"):
        return "jobs"
    if name.startswith("boss_details_"):
        return "details"
    return "other"


def list_results(result_dir=_core().DEFAULT_RESULT_DIR):
    """列出结果目录中的结果文件（按修改时间倒序）。

    Returns:
        list of dict: {"path", "kind", "size", "modified"}
    """
    entries = []
    try:
        names = _core().os.listdir(result_dir)
    except OSError:
        return entries
    for name in names:
        if not name.endswith(".json"):
            continue
        path = _core().os.path.join(result_dir, name)
        kind = _classify_result_file(name)
        try:
            st = _core().os.stat(path)
        except OSError:
            continue
        entries.append({
            "path": path, "kind": kind,
            "size": st.st_size, "modified": st.st_mtime,
        })
    entries.sort(key=lambda e: e["modified"], reverse=True)
    return entries


# _merge_jd_into_export 已抽出到 scripts/export_contract.py
# （见顶部导入兼容层；P1 架构重构 2026-09-22）。


def archive_results(result_dir=_core().DEFAULT_RESULT_DIR, keep_latest=1, archive_dir=None):
    """归档结果目录中的历史结果文件。

    jobs 与 details 各自保留最新的 keep_latest 个，其余移到 archive 子目录；
    pending 是断点续抓的活动文件，不归档；CSV 不移动。

    Args:
        result_dir: 结果目录
        keep_latest: 每个类型保留的最新文件数
        archive_dir: 归档目录（默认 result_dir/archive）

    Returns:
        int: 归档的文件数
    """
    archive_dir = archive_dir or _core().os.path.join(result_dir, "archive")
    moved = 0
    for kind in ("jobs", "details"):
        candidates = [e for e in list_results(result_dir) if e["kind"] == kind]
        for entry in candidates[keep_latest:]:
            _core().os.makedirs(archive_dir, exist_ok=True)
            dst = _core().os.path.join(archive_dir, _core().os.path.basename(entry["path"]))
            _core().shutil.move(entry["path"], dst)
            # failures sidecar 跟着详情本体走，避免归档后根目录留下孤儿。
            sidecar = entry["path"] + ".failures.json"
            if _core().os.path.exists(sidecar):
                _core().shutil.move(
                    sidecar, _core().os.path.join(archive_dir, _core().os.path.basename(sidecar)))
            moved += 1
            print(f"  📦 归档 {_core().os.path.basename(entry['path'])}")
    return moved


def run_list_results(result_dir=_core().DEFAULT_RESULT_DIR):
    """打印 --list-results 报告并返回退出码。"""
    entries = list_results(result_dir)
    if not entries:
        print(f"结果目录为空: {result_dir}")
        return 0
    print(f"\n=== 抓取结果文件（{len(entries)} 个）===")
    for e in entries:
        modified = _core().datetime.fromtimestamp(e["modified"]).strftime("%Y-%m-%d %H:%M")
        print(f"  [{e['kind']:>7}] {modified}  {e['size']:>9} B  {_core().os.path.basename(e['path'])}")
    print()
    print("💡 分类/口径/取代关系/时效用 --artifacts（加 --json 输出机器可读清单）")
    return 0


def run_archive(result_dir=_core().DEFAULT_RESULT_DIR, keep_latest=1):
    """执行 --archive 并返回退出码。"""
    print(f"\n=== 归档历史结果（保留每个类型最新 {keep_latest} 个）===")
    moved = archive_results(result_dir, keep_latest=keep_latest)
    if moved == 0:
        print("  ℹ️  无需归档")
    else:
        print(f"  ✅ 已归档 {moved} 个文件到 {_core().os.path.join(result_dir, 'archive')}")
    print()
    return 0



def latest_results_file(kind):
    """默认结果目录下最新文件（kind: "jobs" / "details"）；目录缺失返回 None。"""
    prefix = "boss_jobs_" if kind == "jobs" else "boss_details_"
    candidates = []
    try:
        for name in _core().os.listdir(_core().DEFAULT_RESULT_DIR):
            if (name.startswith(prefix) and name.endswith(".json")
                    and not (kind == "details" and _is_detail_sidecar(name))):
                path = _core().os.path.join(_core().DEFAULT_RESULT_DIR, name)
                candidates.append((_core().os.path.getmtime(path), path))
    except OSError:
        return None
    return max(candidates)[1] if candidates else None


def _latest_details_path(list_path):
    """自动查找与列表同目录的详情文件：同时间戳优先，其次最新。"""
    base = _core().os.path.dirname(list_path) or "."
    stem = _core().os.path.basename(list_path)
    if stem.startswith("boss_jobs_"):
        stamp = stem[len("boss_jobs_"):]
        same_stamp = _core().os.path.join(base, f"boss_details_{stamp}")
        if _core().os.path.exists(same_stamp):
            return same_stamp
    candidates = []
    try:
        for name in _core().os.listdir(base):
            if (name.startswith("boss_details_") and name.endswith(".json")
                    and not _is_detail_sidecar(name)):
                path = _core().os.path.join(base, name)
                candidates.append((_core().os.path.getmtime(path), path))

    except OSError:
        return None
    return max(candidates)[1] if candidates else None

def default_output_path(kind):
    # 秒级时间戳 + pid 后缀：并发（--max-concurrent/多 batch 进程）同秒写盘不撞名
    # （分钟级在单进程下够用，并发后同分钟互相覆盖导致数据丢失——灰度实测暴露）
    filename = (f"boss_{kind}_{_core().datetime.now().strftime('%Y%m%d_%H%M%S')}"
                f"_{_core().os.getpid()}.json")
    return _core().os.path.join(_core().DEFAULT_RESULT_DIR, filename)

