#!/usr/bin/env python3
"""已知坏页台账（跨 run、跨日持久化）。

为什么需要（2026-10-22）：实测每天固定有 3 个岗位的详情页抽不出 JD
（`invalid_detail`），它们的页面结构与选择器不匹配 → 工具**每天都重试**，
每天白花 3 次详情页额度，而结果每次都一样。

根因不是"岗位数据缺失"，而是**"这类页面的解析路径不适用"**。既然结果可复现，
就应该沉淀下来：命中台账的岗位**直接跳过、不发请求、不消耗额度**；
等平台改版或我们补了兜底解析再重试（`--retry-job` 可强制重试）。

台账文件在仓库外：`~/.boss-zhipin-scraper/known_bad_jobs.json`
（`BOSS_KNOWN_BAD_PATH` 可覆盖）。
"""

import json
import os
import tempfile
import threading
import time

KNOWN_BAD_PATH_ENV = "BOSS_KNOWN_BAD_PATH"
STATE_DIR_ENV = "BOSS_STATE_DIR"

_LOCK = threading.Lock()


def _state_dir():
    return os.environ.get(STATE_DIR_ENV) or os.path.expanduser(
        "~/.boss-zhipin-scraper")


def known_bad_path():
    """坏页台账文件路径。"""
    return os.environ.get(KNOWN_BAD_PATH_ENV) or os.path.join(
        _state_dir(), "known_bad_jobs.json")


def _read(path=None):
    target = path or known_bad_path()
    try:
        with open(target, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(data, path=None):
    target = path or known_bad_path()
    directory = os.path.dirname(target)
    if directory:
        os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".known_bad-", dir=directory or None)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def record(job_id, fingerprint="", reason="", path=None):
    """记一条坏页（job_id → {fingerprint, reason, first_seen, hits}）。"""
    key = str(job_id or "").strip()
    if not key:
        return
    with _LOCK:
        data = _read(path)
        entry = data.get(key) or {}
        entry["fingerprint"] = str(fingerprint or entry.get("fingerprint") or "")
        entry["reason"] = str(reason or entry.get("reason") or "")[:300]
        entry["first_seen"] = entry.get("first_seen") or time.strftime("%Y-%m-%d")
        entry["hits"] = int(entry.get("hits") or 0) + 1
        entry["last_seen"] = time.strftime("%Y-%m-%d")
        data[key] = entry
        _write(data, path)


def is_known(job_id, path=None):
    """该岗位是否已在坏页台账里（→ 不必再花额度重试）。"""
    key = str(job_id or "").strip()
    if not key:
        return False
    return key in _read(path)


def get(job_id, path=None):
    """取坏页记录（不存在返回 None）。"""
    return _read(path).get(str(job_id or "").strip())


def fingerprint_counts(path=None):
    """按失败指纹统计（用于判断"哪一类页面结构需要补兜底解析"）。"""
    counts = {}
    for entry in _read(path).values():
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("fingerprint") or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return counts


def summary(path=None):
    """台账概览：总数 + 按指纹分布。"""
    data = _read(path)
    return {"total": len(data), "by_fingerprint": fingerprint_counts(path)}


def clear(path=None):
    """清空台账（解析兜底补上后用）。"""
    with _LOCK:
        _write({}, path)
