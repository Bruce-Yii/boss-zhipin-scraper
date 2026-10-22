#!/usr/bin/env python3
"""风险事件审计日志（append-only JSONL）。

抽出稳定纯逻辑（2026-09-22 P4e）：仅用标准库，无 CDP / 网络依赖；主文件对
同名符号做 re-export，保持 ``scripts.boss_cdp_raw.X`` 导入面不变。

用途：把"风控/验证码/登录失效/熔断冷却"等关键事件落到本地审计日志，
便于事后复盘与对外可解释（合规工程化）。

约定
----
- 路径：``~/.boss-zhipin-scraper/risk_events.jsonl``（**仓库外，不进 git**）。
- 追加写：单行 JSON + UTC 秒级时间戳；超过 ``MAX_BYTES`` 滚动为 ``.1``。
- **best-effort**：任何 IO 失败都吞掉（审计绝不阻塞主流程）。
- **不落凭据**：字符串字段经 ``_scrub_secrets`` 脱敏后再写入。
"""

import json
import os
import re
import time
from contextlib import contextmanager
from datetime import UTC, datetime

try:
    from scripts import export_contract as _export_contract
except ImportError:  # pragma: no cover - 直接运行脚本时走此分支
    import export_contract as _export_contract

_scrub_secrets = _export_contract._scrub_secrets
_sanitize_payload = _export_contract._sanitize_payload
_AUTH_HEADER_RE = re.compile(
    r"(?i)(authorization\s*:\s*bearer\s+)([^\s,;]+)")


def _scrub_audit_text(value):
    """在通用 key=value 脱敏之外，清理 Authorization: Bearer 头。"""
    return _AUTH_HEADER_RE.sub(r"\1***", _scrub_secrets(value))


def _json_safe_audit_value(value):
    """递归转换审计字段；不可序列化值变成类型标记而非抛异常。"""
    if isinstance(value, dict):
        safe = {}
        for key, nested in value.items():
            safe_key = key if isinstance(key, (str, int, float, bool)) or key is None \
                else f"<{type(key).__name__}>"
            safe[safe_key] = _json_safe_audit_value(nested)
        return safe
    if isinstance(value, list):
        return [_json_safe_audit_value(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe_audit_value(item) for item in value]
    if isinstance(value, str):
        return _scrub_audit_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return f"<{type(value).__name__}>"


@contextmanager
def _append_lock(path):
    """短暂互斥保护滚动+追加；锁竞争超时则 best-effort 继续。"""
    lock_path = f"{path}.lock"
    deadline = time.monotonic() + 1.0
    acquired = False
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            acquired = True
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                break
            time.sleep(0.01)
        except OSError:
            break
    try:
        yield
    finally:
        if acquired:
            try:
                os.remove(lock_path)
            except OSError:
                pass


DEFAULT_AUDIT_PATH = os.path.expanduser("~/.boss-zhipin-scraper/risk_events.jsonl")
ALT_AUDIT_PATH_ENV = "BOSS_AUDIT_PATH"  # 测试/多环境覆盖
MAX_BYTES = 5 * 1024 * 1024             # 5MB 滚动阈值


def audit_path():
    """当前审计日志路径（``BOSS_AUDIT_PATH`` 环境变量可覆盖，供测试/多环境）。"""
    return os.environ.get(ALT_AUDIT_PATH_ENV) or DEFAULT_AUDIT_PATH


def _replace_with_retry(src, dst, attempts=6, base_delay=0.02):
    """os.replace 带 PermissionError 退避重试（Windows 上目标文件可能被并发占用）。

    独立审计 2026-10-22：同型问题在冷却标记上会让**熔断静默失效**。轮转失败只丢
    一次滚动（不丢事件），但要退避重试而不是第一次就放弃。
    """
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return True
        except PermissionError:
            time.sleep(base_delay * (2 ** attempt))
    return False


def _rotate(path):
    """超过阈值则把当前日志滚动为 ``.1``（覆盖旧 ``.1``）。"""
    try:
        if os.path.exists(path) and os.path.getsize(path) > MAX_BYTES:
            rotated = path + ".1"
            if os.path.exists(rotated):
                os.remove(rotated)
            _replace_with_retry(path, rotated)
    except (OSError, ValueError):
        pass


def record_event(event, *, path=None, **fields):
    """追加一条审计事件。

    Args:
        event: 事件名（如 ``alert`` / ``cooldown``）。
        path: 可选覆盖路径（默认 ``audit_path()``；测试注入临时文件）。
        **fields: 附加字段（字符串值自动脱敏）。

    Returns:
        bool: 写入成功 True；IO 失败静默 False（best-effort，不抛异常）。
    """
    target = str(path or audit_path())
    try:
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        safe_fields = _json_safe_audit_value(_sanitize_payload(fields))
        entry = dict(safe_fields)
        # 保留字段由审计器最后生成，调用方不能伪造事件身份或时间。
        entry["ts"] = datetime.now(UTC).isoformat(timespec="seconds")
        entry["event"] = _scrub_audit_text(str(event))
        with _append_lock(target):
            _rotate(target)
            with open(target, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return True
    except (OSError, TypeError, ValueError):
        return False


def read_events(path=None, limit=None):
    """读回审计事件（解析失败的行跳过）；``limit`` 只取最后 N 条。"""
    target = path or audit_path()
    if not os.path.exists(target):
        return []
    events = []
    try:
        with open(target, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except (json.JSONDecodeError, ValueError):
                    continue
    except OSError:
        return []
    return events[-limit:] if limit else events
