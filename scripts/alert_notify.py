"""告警推送（从 boss_cdp_raw.py 增量拆分）。

规格侧 Worker /webhook/alert，Bearer token；配置放项目根 .env
（ALERT_WEBHOOK_URL / ALERT_WEBHOOK_TOKEN，不落仓库）；
推送是旁路：配置缺失/发送失败不抛异常，不影响抓取主流程。

约束：boss 侧全局经 _core() 取用（调用栈优先）；requests/log 走 _core()
（tests 会 patch boss.requests 为 Mock）；其余 stdlib 直引。
"""

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


def load_alert_config(env_path=None):
    """读取 .env 中的告警配置：ALERT_WEBHOOK_URL / ALERT_WEBHOOK_TOKEN。

    默认查找项目根目录 .env；文件缺失或键缺失返回空配置（推送禁用）。
    """
    if env_path is None:
        env_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", ".env")
    cfg = {"url": "", "token": ""}
    try:
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key == "ALERT_WEBHOOK_URL":
                    cfg["url"] = value
                elif key == "ALERT_WEBHOOK_TOKEN":
                    cfg["token"] = value
    except OSError:
        pass
    return cfg


def send_alert(title, text, timeout=10):
    """发送告警到规格侧 Worker 端点（requests POST，Bearer 鉴权）。

    幂等由端点保证（同内容去重）；失败仅记日志返回 False。

    P4e：无论端点是否配置，都先落一条**本地审计**（风险/登录事件的统一入口）。
    """
    core = _core()
    core.record_audit_event("alert", title=title, text=text)
    cfg = core.load_alert_config()
    if not cfg["url"] or not cfg["token"]:
        return False
    if core.requests is None:  # 延迟导入：测试/未初始化环境静默禁用
        return False
    try:
        resp = core.requests.post(
            cfg["url"],
            json={"source": "boss-zhipin-scraper", "title": title, "text": text},
            headers={"Authorization": f"Bearer {cfg['token']}"},
            timeout=timeout,
        )
        return resp.status_code == 200
    except OSError as exc:
        # requests.RequestException 继承 IOError(=OSError)；网络层失败不阻塞主流程。
        # 端点挂掉时只留一行提示——刷一屏 traceback 会把真正的错误淹掉。
        core.log.warning("告警发送失败（已忽略，不影响主流程）: %s", exc)
        return False
