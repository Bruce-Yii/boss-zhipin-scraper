# -*- coding: utf-8 -*-
"""Security sidecar (restricted securityId storage) extracted from boss_cdp_raw.py."""

import os
import json
import time
import logging

log = logging.getLogger("boss_cdp")

SECURITY_SIDECAR_DIR = os.path.expanduser("~/.boss-zhipin-scraper/.session")
SECURITY_SIDECAR_TTL_SECONDS = 3600

try:
    from . import export_contract as _ec
except Exception:
    import export_contract as _ec

_atomic_write_json = _ec._atomic_write_json


def _sidecar_dir():
    try:
        from . import boss_cdp_raw as _core
        return _core.SECURITY_SIDECAR_DIR
    except Exception:
        try:
            import boss_cdp_raw as _core
            return _core.SECURITY_SIDECAR_DIR
        except Exception:
            return SECURITY_SIDECAR_DIR
def security_sidecar_path(list_path):
    """sidecar 路径：<SECURITY_SIDECAR_DIR>/<列表文件名>.security.json。"""
    base = os.path.basename(list_path or "list.json")
    return os.path.join(_sidecar_dir(), base + ".security.json")


def write_security_sidecar(list_path, security_map):
    path = security_sidecar_path(list_path)
    if not security_map:
        return None
    os.makedirs(_sidecar_dir(), exist_ok=True)
    payload = {"security_map": security_map, "created_at": time.time()}
    _atomic_write_json(path, payload)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def load_security_sidecar(list_path):
    path = security_sidecar_path(list_path)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = data.get("created_at", 0)
        if time.time() - mtime > SECURITY_SIDECAR_TTL_SECONDS:
            delete_security_sidecar(list_path)
            return {}
        smap = data.get("security_map", {})
        if not isinstance(smap, dict):
            return {}
        return smap
    except (OSError, ValueError):
        return {}


def delete_security_sidecar(list_path):
    path = security_sidecar_path(list_path)
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


