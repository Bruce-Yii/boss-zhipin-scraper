"""stoken 热刷新：真实打开一次 BOSS 搜索页，等安全 JS 重跑换发 __zp_stoken__。

背景（boss-agent-cli PR #453 的机理，本仓 2026-10-27 实测复现）：

- 平台安全令牌 ``__zp_stoken__`` 被标记后，**所有自动化请求**（含登录探测、列表、
  详情）都返回 ``code 37 您的环境存在异常``；
- 页面里由平台自己的安全 JS 重跑才会换发新令牌——而注入式 XHR/fetch 不会触发它；
- 做法：真实导航到搜索页并停留一段时间（实测 75 秒足够），再发请求即可恢复。

本模块提供 ``warm_stoken()`` 与 ``--warm-stoken [SECONDS]``；登录探测命中
code 37 时也会自动调用一次（见 ``login_probe``），仍失败才停手（不循环重试）。
"""

import os
import sys
import time
from urllib.parse import quote

# CDP 层**延迟导入**：cdp_session 在 import 时就会回查 boss 主模块，
# 顶层直接 import 会造成 warm_stoken ↔ boss 循环导入（测试单独加载本模块时炸）。
_CDP = None


def _cdp():
    global _CDP
    if _CDP is None:
        try:
            from scripts import cdp_session as module
        except ImportError:  # pragma: no cover - 直接运行脚本时走此分支
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import cdp_session as module
        _CDP = module
    return _CDP

DEFAULT_WAIT_SECONDS = 75
WAIT_ENV = "BOSS_WARM_STOKEN_SECONDS"
DEFAULT_QUERY = "AI产品实习生"
DEFAULT_CITY_CODE = "101210100"  # 杭州
SEARCH_URL = "https://www.zhipin.com/web/geek/job?query={query}&city={city}"


def wait_seconds(value=None):
    """热刷新停留秒数：显式参数 > 环境变量 > 默认 75。"""
    if value is not None:
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return DEFAULT_WAIT_SECONDS
    raw = os.environ.get(WAIT_ENV)
    try:
        return max(0, int(raw)) if raw else DEFAULT_WAIT_SECONDS
    except (TypeError, ValueError):
        return DEFAULT_WAIT_SECONDS


def build_search_url(query=None, city_code=None):
    return SEARCH_URL.format(query=quote(str(query or DEFAULT_QUERY)),
                             city=str(city_code or DEFAULT_CITY_CODE))


def warm_stoken(cdp_port=None, seconds=None, query=None, city_code=None):
    """真实打开搜索页等待安全 JS 重跑。返回 ``{ok, title, ready_state, waited}``。

    只做一次导航+等待，不循环、不重试；失败返回 ok=False（调用方决定是否停手）。
    """
    port = int(cdp_port or 45222)
    wait = wait_seconds(seconds)
    url = build_search_url(query=query, city_code=city_code)
    cdp_mod = _cdp()
    cdp = None
    tid = None
    summary = {"ok": False, "title": "", "ready_state": "", "waited": wait,
               "url": url}
    try:
        cdp = cdp_mod.CDPSession(port)
        tid, sid = cdp_mod.create_page_session(
            cdp, background=cdp_mod.page_background_default())
        cdp.send("Page.enable", {}, sid)
        cdp.send("Page.navigate", {"url": url}, sid)
        print(f"♻️  stoken 热刷新：真实打开搜索页，等待安全 JS {wait}s ...")
        time.sleep(wait)
        try:
            summary["title"] = str(cdp.eval_js("document.title", sid) or "")
            summary["ready_state"] = str(cdp.eval_js("document.readyState", sid) or "")
        except (KeyError, RuntimeError, TimeoutError):
            pass
        summary["ok"] = True
        print(f"   页面：{summary['title'] or '（未取到标题）'}"
              f"｜readyState={summary['ready_state'] or '?'}")
    except Exception as exc:  # 旁路能力：任何 CDP/页面异常都不应让主流程崩
        print(f"⚠️  stoken 热刷新失败：{exc}")
        summary["error"] = str(exc)
    finally:
        if cdp is not None:
            if tid is not None:
                try:
                    cdp.send("Target.closeTarget", {"targetId": tid})
                except Exception:
                    pass
            try:
                cdp.close()
            except Exception:
                pass
    return summary


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(
        description="stoken 热刷新：真实打开搜索页等待安全 JS（code 37 自愈）")
    parser.add_argument("seconds", nargs="?", type=int, default=None,
                        help=f"停留秒数（默认 {DEFAULT_WAIT_SECONDS}，{WAIT_ENV} 可覆盖）")
    parser.add_argument("--cdp-port", type=int, default=45222)
    parser.add_argument("--query", default=None)
    parser.add_argument("--city-code", default=None)
    args = parser.parse_args(argv)
    summary = warm_stoken(args.cdp_port, args.seconds, args.query, args.city_code)
    return 0 if summary.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
