"""运行进度状态文件：长任务在 agent 会话里的外部可观测性。

用户侧只能看到 agent 的工具调用，看不到命令 stdout 的实时滚动；抓取一轮动辄
15-20 分钟，期间必须能回答「到哪了、还剩多少、是不是卡住了」。做法：

- 抓取/日常流水线在关键节点把状态**原子写**到 ``~/.boss-zhipin-scraper/run_state.json``；
- ``--progress [--json]`` 只读查询（agent 轮询用）；
- ``updated_at`` 超过阈值未更新 → 标记 ``stale``，提示「已结束或卡住」；
- 状态文件只是运行期缓存，缺失/损坏不影响抓取（读失败返回 None）。

字段（稳定契约，供 agent 消费）::

    running        bool    是否仍在跑（正常结束/异常中断都会置 False）
    phase          str     preflight/list/detail/export/deliverable/done/failed/aborted
    started_at     str     开始时间（本地时区 ISO）
    updated_at     str     最近心跳（本地时区 ISO）
    pid            int
    run_id         str
    keyword        str     当前关键词（列表阶段）
    keyword_index  int / keywords_total int
    pages_done / pages_total
    detail_total / detail_done / detail_ok / detail_failed / detail_skipped
    jobs_found / jobs_kept
    budget_used / budget_limit
    deliverable    str     流水线当前/最近交付物
    message        str     一句话人话进度
"""

import json
import os
import sys
import threading
from datetime import datetime

STATE_PATH_ENV = "BOSS_RUN_STATE_PATH"
DEFAULT_STALE_SECONDS = 300
_lock = threading.Lock()


def state_path():
    """状态文件路径：**运行期取 HOME**（测试/多环境改 HOME 自动隔离）。"""
    if os.environ.get(STATE_PATH_ENV):
        return os.environ[STATE_PATH_ENV]
    return os.path.expanduser("~/.boss-zhipin-scraper/run_state.json")


def _now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def read():
    """读状态；文件缺失/损坏返回 None（绝不因状态文件影响主流程）。"""
    path = state_path()
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _write(payload):
    path = state_path()
    parent = os.path.dirname(path)
    try:
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = f"{path}.tmp{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError):
        # 状态只是旁路可观测性，写失败不影响抓取
        return


def start(phase="preflight", **fields):
    """开始一次运行：running=True + started_at 固定，其余字段由调用方给。"""
    with _lock:
        payload = {
            "running": True,
            "phase": phase,
            "started_at": _now_iso(),
            "pid": os.getpid(),
            "run_id": "",
            "message": "",
        }
        payload.update(fields)
        payload["updated_at"] = _now_iso()
        _write(payload)
        return payload


def update(**fields):
    """合并更新状态；**没有进行中的运行就不写**（避免只读/单测路径凭空造状态）。"""
    with _lock:
        payload = read()
        if not payload:
            return {}
        payload.update(fields)
        payload["updated_at"] = _now_iso()
        _write(payload)
        return payload


def finish(phase="done", **fields):
    """结束运行：running=False，写终态（没有进行中的运行则空操作）。"""
    fields.setdefault("message", "")
    payload = update(running=False, phase=phase, **fields)
    return payload


def is_stale(payload, stale_seconds=DEFAULT_STALE_SECONDS):
    """updated_at 超过 stale_seconds 没更新 → 视为「已结束/卡住」。"""
    if not isinstance(payload, dict) or not payload.get("running"):
        return True
    try:
        updated = datetime.fromisoformat(str(payload.get("updated_at") or ""))
    except ValueError:
        return True
    age = (datetime.now().astimezone() - updated).total_seconds()
    return age > stale_seconds


def _pct(payload, key_done, key_total):
    try:
        total = int(payload.get(key_total) or 0)
        done = int(payload.get(key_done) or 0)
    except (TypeError, ValueError):
        return None
    if total <= 0:
        return None
    return max(0, min(100, round(done * 100.0 / total)))


def progress_view(payload, stale_seconds=DEFAULT_STALE_SECONDS):
    """把状态整理成 agent 可直接汇报的稳定结构（含 progress_pct/age/stale）。"""
    if not isinstance(payload, dict):
        return {"running": False, "phase": "unknown", "stale": True,
                "state": "unknown",
                "message": "没有运行状态记录（可能从未运行，或状态文件被清理）"}
    view = dict(payload)
    stale = is_stale(payload, stale_seconds)
    view["stale"] = stale
    phase = str(payload.get("phase") or "")
    if phase == "detail":
        view["progress_pct"] = _pct(payload, "detail_done", "detail_total")
    elif phase == "list":
        view["progress_pct"] = _pct(payload, "pages_done", "pages_total")
    else:
        view["progress_pct"] = None
    age = None
    try:
        updated = datetime.fromisoformat(str(payload.get("updated_at") or ""))
        age = max(0, int((datetime.now().astimezone() - updated).total_seconds()))
    except ValueError:
        pass
    view["age_seconds"] = age
    if not payload.get("running"):
        view["state"] = "finished"
    elif stale:
        view["state"] = "stale"
    else:
        view["state"] = "running"
    return view


def format_progress(view):
    """人看的一行/几行进度（agent 用 --json）。"""
    if view.get("state") == "unknown":
        return f"❔ {view.get('message', '')}"
    state = view.get("state")
    phase = view.get("phase") or "?"
    if not view.get("running") and phase in ("failed", "aborted"):
        icon = "❌"
    else:
        icon = {"running": "⏳", "finished": "✅", "stale": "⚠️"}.get(state, "❔")
    parts = [f"{icon} 阶段 {phase}"]
    if view.get("progress_pct") is not None:
        parts.append(f"{view['progress_pct']}%")
    if phase == "detail":
        parts.append(f"详情 {view.get('detail_done', 0)}/{view.get('detail_total', 0)}"
                     f"（成功 {view.get('detail_ok', 0)}）")
    if phase == "list":
        keyword = view.get("keyword") or ""
        if keyword:
            parts.append(f"当前 {keyword}")
        if view.get("keywords_total"):
            parts.append(f"第 {view.get('keyword_index', 0)}/{view.get('keywords_total')} 词")
        if view.get("pages_total"):
            parts.append(f"页 {view.get('pages_done', 0)}/{view.get('pages_total')}")
    if view.get("budget_limit"):
        parts.append(f"预算剩 {max(int(view['budget_limit']) - int(view.get('budget_used') or 0), 0)}")
    if view.get("message"):
        parts.append(str(view["message"]))
    if view.get("age_seconds") is not None:
        parts.append(f"（{view['age_seconds']}s 前更新）")
    if state == "stale":
        parts.append("→ 超过阈值未更新，可能已结束或卡住")
    return "｜".join(parts)


def run_progress(as_json=False, stale_seconds=DEFAULT_STALE_SECONDS):
    """``--progress`` 实现：只读输出运行进度。"""
    view = progress_view(read(), stale_seconds)
    if as_json:
        print(json.dumps(view, ensure_ascii=False, indent=2))
    else:
        print(format_progress(view))
    return 0


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(
        description="运行进度（长任务在 agent 会话里的可观测性；只读）")
    parser.add_argument("--json", action="store_true", help="机器可读 JSON")
    parser.add_argument("--stale-seconds", type=int, default=DEFAULT_STALE_SECONDS,
                        help=f"超过多少秒未更新视为 stale（默认 {DEFAULT_STALE_SECONDS}）")
    args = parser.parse_args(argv)
    return run_progress(as_json=args.json, stale_seconds=args.stale_seconds)


if __name__ == "__main__":
    sys.exit(main())
